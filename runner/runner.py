"""SQL 语句运行入口与交互式命令行。

Runner 依次组装 Parser、Binder/LogicalPlan、Executor 和 Runtime，
并保存 USE 产生的会话状态。可选 trace_sink 只观察真实调用，
不介入 SQL 语义。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from typing import Protocol

from contracts.ast import ParsedStatement, Script, SourceSpan, Statement
from contracts.errors import E_INPUT_FILE, SqlError
from contracts.result import QueryResult, ScriptResult, StatementResult
from contracts.storage import BaseDatabaseServer, IndexInfo, TableInfo, TableStats
from runner.executor.builder import ExecutorTreeBuilder
from runner.executor.context import ExecutionContext
from runner.logical_plan.base import LogicalPlan
from runner.logical_plan.builder import LogicalPlanBuilder
from runner.logical_plan.optimizer import LogicalOptimizer, OptimizationLog
from runner.physical.planner import (
    AccessPath,
    PhysicalMode,
    validate_physical_mode,
)
from runner.trace_hooks import (
    RunnerTraceSink,
    emit_disabled_operation,
    trace_runner_operation,
)


DEFAULT_DATABASE = "main"
ParseSql = Callable[[str], Statement]
ParseScript = Callable[[str], Script]


class RunnerInspector(Protocol):
    """定义 Runner 与可选可视化编排器之间的最小接口。

    Runner 只知道编排器能执行单语句和脚本，不导入 UI 的具体类。
    这个依赖倒置保持运行层可独立测试，也使未启用追踪的调用方完全
    沿用原有路径。物理模式与优化开关由 Runner 校验后透传，编排器
    不得改写，否则追踪记录的阶段状态会与实际执行不一致。
    """

    def execute(
        self,
        runner: Runner,
        sql: str,
        *,
        optimize: bool = True,
        physical: PhysicalMode = "auto",
    ) -> QueryResult:
        """执行并追踪一条 SQL，返回原 QueryResult。"""

        ...

    def execute_script(
        self,
        runner: Runner,
        sql: str,
        *,
        stop_on_error: bool = True,
        optimize: bool = True,
        physical: PhysicalMode = "auto",
    ) -> ScriptResult:
        """执行并追踪完整 SQL 脚本，返回原 ScriptResult。"""

        ...

    def latest(self, module: str | None = None) -> object | None:
        """返回最近查询的可选模块快照，没有记录时返回 None。"""

        ...

    def open_view(self, module: str | None = None) -> tuple[str, bool]:
        """打开本地查看器，返回 URL 与浏览器打开结果。"""

        ...


class Runner:
    """串联 SQL 各执行阶段，并维护单个会话状态。"""

    def __init__(
        self,
        server: BaseDatabaseServer,
        parse: ParseSql,
        current_database: str = DEFAULT_DATABASE,
        parse_script: ParseScript | None = None,
        trace_sink: RunnerTraceSink | None = None,
        inspector: RunnerInspector | None = None,
    ) -> None:
        """连接初始数据库并组装 Parser、Planner、Executor 和可选追踪。

        Args:
            server: 实现公开契约的数据库服务器。
            parse: 保持 V1 兼容的单语句解析入口。
            current_database: 会话初始连接的数据库名。
            parse_script: 可选多语句解析入口；缺省时使用单语句适配。
            trace_sink: 可选 C 字典事件回调，同时注入绑定、
                计划、优化、Executor 构建和运行上下文。
            inspector: 可选查询追踪编排器。启用后由它在不重复
                执行 SQL 的前提下组装 A/B/C 完整记录。

        Raises:
            SqlError: 初始数据库无法连接时原样向上抛出。
        """

        # 先完成连接；连接失败时不创建半初始化的会话上下文。
        storage = server.connect(current_database)
        self._parse = parse
        self._parse_script = parse_script or self._parse_as_single_statement_script
        self._inspector = inspector
        # Runner 自己保留回调：优化开关的「关闭」分支没有可装饰的真实调用，
        # 只能由编排层显式提交一条禁用记录。
        self._trace_sink = trace_sink
        self._context = ExecutionContext(
            server=server,
            storage=storage,
            current_database=current_database,
            trace_sink=trace_sink,
        )
        self._logical_plan_builder = LogicalPlanBuilder(
            self._describe_current_table,
            trace_sink=trace_sink,
        )
        self._logical_optimizer = LogicalOptimizer()
        # 构建期需要「当前库名 + 表结构 + 统计 + 索引清单」四个动态回调：同名表可能
        # 存在于多个库，库名与元数据都必须每次现取，不能在建 Runner 时固化成常量。
        self._executor_tree_builder = ExecutorTreeBuilder(
            self._describe_current_table,
            self._current_database_name,
            statistics=self._statistics_of_current_table,
            list_indexes=self._list_indexes_of_current_table,
            trace_sink=trace_sink,
        )
        self._last_optimization_log: OptimizationLog | None = None
        self._last_access_paths: tuple[AccessPath, ...] = ()

    @property
    def current_database(self) -> str:
        """返回当前会话所连接的数据库名。"""
        return self._context.current_database

    @property
    def last_optimization_log(self) -> OptimizationLog | None:
        """最近一条语句的优化日志；关闭优化或尚未执行语句时为 None。"""
        return self._last_optimization_log

    @property
    def last_access_paths(self) -> tuple[AccessPath, ...]:
        """最近一条语句的选路记录，每条语句执行前重置。

        与 last_optimization_log 同构：JOIN 一条语句可能有多条（每个 Scan 一条），
        不含扫描的语句为空元组。
        """
        return self._last_access_paths

    @property
    def inspector(self) -> RunnerInspector | None:
        """返回当前会话的可选追踪编排器。

        终端只通过这个只读属性实现 ``/inspect``，不接触 Runner 的
        Parser、Executor 或 Storage 内部状态。
        """

        return self._inspector

    def _describe_current_table(self, table: str) -> TableInfo:
        """动态读取当前 Storage 的表结构，保证 USE 后访问新数据库。"""
        return self._context.storage.describe(table)

    def _statistics_of_current_table(self, table: str) -> TableStats:
        """动态读取当前 Storage 的表统计，供构建期估算选择性与代价。"""
        return self._context.storage.statistics(table)

    def _list_indexes_of_current_table(self, table: str) -> tuple[IndexInfo, ...]:
        """动态读取当前 Storage 的索引清单，供构建期判断哪些列可用索引。"""
        return tuple(self._context.storage.list_indexes(table))

    def _current_database_name(self) -> str:
        """动态读取当前库名，作为执行器构建期表结构缓存的键。"""
        return self._context.current_database

    def execute(
        self,
        sql: str,
        *,
        optimize: bool = True,
        physical: PhysicalMode = "auto",
    ) -> QueryResult:
        """执行一条 SQL，并原样返回执行器产生的结果。

        optimize 逐语句生效，只影响是否经过逻辑优化器，不影响绑定阶段，
        因此开关前后名称绑定与类型错误的行为完全一致。

        physical 决定每个 Scan 的访问路径，取值非法时在解析之前就抛 E_BAD_ARG。
        开启 inspector 时由编排器负责真实解析与执行，optimize 与 physical
        都原样透传，使追踪记录与实际执行落在同一个开关状态上。
        """

        validate_physical_mode(physical)
        if self._inspector is not None:
            return self._inspector.execute(
                self, sql, optimize=optimize, physical=physical
            )
        statement = self._parse(sql)
        return self._execute_statement(
            statement, optimize=optimize, physical=physical
        )

    def _execute_statement(
        self,
        statement: Statement,
        *,
        optimize: bool = True,
        physical: PhysicalMode = "auto",
    ) -> QueryResult:
        """执行已解析的单条语句，避免脚本路径重复解析原 SQL。"""
        self._last_access_paths = ()
        plan = self._logical_plan_builder.build(statement)
        if optimize:
            log = self._optimize_plan(plan)
            self._last_optimization_log = log
            plan = log.optimized
        else:
            self._last_optimization_log = None
            emit_disabled_operation(
                self._trace_sink,
                "optimizer",
                "optimize",
                reason="optimize=False",
            )
        executor = self._executor_tree_builder.build(plan, physical=physical)
        self._last_access_paths = self._executor_tree_builder.last_access_paths
        return executor.execute(self._context)

    @trace_runner_operation("optimizer", "optimize")
    def _optimize_plan(self, plan: LogicalPlan) -> OptimizationLog:
        """对已绑定的计划跑一遍逻辑优化器，并提交一条优化记录。

        优化器本身不感知追踪：它只返回 OptimizationLog，由装饰器把该日志
        与真实耗时一起上报，因此「计划」「产生计划的记录」与追踪事件同源。
        """
        return self._logical_optimizer.optimize(plan)

    def _parse_as_single_statement_script(self, sql: str) -> Script:
        """在未注入 parse_script 时为旧调用方提供单语句兼容。

        该适配只能解析一条语句；需要多语句能力时应向 Runner
        显式注入 compiler.parse_script。
        """
        if not sql.strip():
            return ()
        statement = self._parse(sql)
        start_offset = next(
            index for index, character in enumerate(sql) if not character.isspace()
        )
        end_offset = len(sql.rstrip())
        start_line, start_col = self._source_position(sql, start_offset)
        end_line, end_col = self._source_position(sql, end_offset - 1)
        return (
            ParsedStatement(
                statement=statement,
                sql=sql[start_offset:end_offset],
                span=SourceSpan(start_line, start_col, end_line, end_col),
            ),
        )

    @staticmethod
    def _source_position(source: str, offset: int) -> tuple[int, int]:
        """把零基字符偏移转换为一基行列位置。"""
        prefix = source[:offset]
        line = prefix.count("\n") + 1
        last_newline = prefix.rfind("\n")
        column = offset + 1 if last_newline < 0 else offset - last_newline
        return line, column

    def execute_script(
        self,
        sql: str,
        *,
        stop_on_error: bool = True,
        optimize: bool = True,
        physical: PhysicalMode = "auto",
    ) -> ScriptResult:
        """按源码顺序执行脚本中的语句并汇总逐条结果。

        整段脚本先由 parse_script 解析；解析错误直接向调用方抛出。
        stop_on_error 只控制名称绑定和执行阶段的 SqlError；optimize 透传给
        每条语句，使基准工具能对整段脚本统一关闭优化器；physical 同样透传给
        每条语句，非法取值在跑第一条语句之前就报错。启用 inspector 时交给
        编排器执行，optimize 与 physical 一并透传。
        """
        validate_physical_mode(physical)
        if self._inspector is not None:
            return self._inspector.execute_script(
                self,
                sql,
                stop_on_error=stop_on_error,
                optimize=optimize,
                physical=physical,
            )
        parsed_statements = self._parse_script(sql)
        results: list[StatementResult] = []
        stopped_early = False

        for parsed in parsed_statements:
            started = perf_counter()
            try:
                result = self._execute_statement(
                    parsed.statement, optimize=optimize, physical=physical
                )
            except SqlError as error:
                results.append(
                    StatementResult(
                        sql=parsed.sql,
                        span=parsed.span,
                        error=error,
                        elapsed_ms=(perf_counter() - started) * 1000,
                    )
                )
                if stop_on_error:
                    stopped_early = True
                    break
            else:
                results.append(
                    StatementResult(
                        sql=parsed.sql,
                        span=parsed.span,
                        result=result,
                        elapsed_ms=(perf_counter() - started) * 1000,
                    )
                )

        return ScriptResult(tuple(results), stopped_early=stopped_early)

    def execute_file(
        self,
        path: str | Path,
        *,
        stop_on_error: bool = True,
        optimize: bool = True,
        physical: PhysicalMode = "auto",
    ) -> ScriptResult:
        """按 UTF-8 读取 SQL 文件并交给 execute_script 执行。"""
        validate_physical_mode(physical)
        try:
            input_path = Path(path).expanduser()
            sql = input_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, TypeError, ValueError) as error:
            raise SqlError(E_INPUT_FILE, f"cannot read SQL file {path!s}: {error}") from None
        return self.execute_script(
            sql,
            stop_on_error=stop_on_error,
            optimize=optimize,
            physical=physical,
        )

    def list_databases(self) -> list[str]:
        """向终端提供库名，终端不接触存储内部结构。"""
        return self._context.server.list_databases()

    def list_tables(self) -> list[str]:
        """向终端返回当前数据库的用户表名，不暴露 Catalog。"""

        return self._context.storage.list_tables()

    def describe_table(self, name: str) -> TableInfo:
        """向终端返回当前库的表结构，复用动态 Schema 查询路径。"""

        return self._describe_current_table(name)

    def repl(
        self, *, data_dir: Path | None = None, plain: bool = False,
        history: bool = True, stop_on_error: bool = True,
        physical: PhysicalMode = "auto",
    ) -> int:
        """进入终端会话；非 TTY 自动使用纯文本，返回会话退出码。

        physical 是会话初值，交互中可用 ``/physical`` 覆盖；非法取值在进入
        终端之前就以 E_BAD_ARG 拒绝，避免整场会话静默跑在别的模式上。
        """
        from runner.terminal.session import TerminalSession

        return TerminalSession(
            self,
            data_dir=data_dir,
            plain=plain,
            history=history,
            stop_on_error=stop_on_error,
            physical=physical,
        ).run()

    @staticmethod
    def _print_result(result: QueryResult) -> None:
        """以简单的制表符格式展示 QueryResult，不改变结果对象。"""
        from runner.terminal.render import safe_text

        if result.columns is not None and result.rows is not None:
            print("\t".join(safe_text(column) for column in result.columns))
            for row in result.rows:
                print("\t".join(safe_text(value) for value in row))
            return

        print(f"{result.affected_rows} row(s) affected")
