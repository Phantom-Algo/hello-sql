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
from contracts.storage import BaseDatabaseServer, TableInfo
from runner.executor.builder import ExecutorTreeBuilder
from runner.executor.context import ExecutionContext
from runner.logical_plan.builder import LogicalPlanBuilder
from runner.trace_hooks import RunnerTraceSink


DEFAULT_DATABASE = "main"
ParseSql = Callable[[str], Statement]
ParseScript = Callable[[str], Script]


class RunnerInspector(Protocol):
    """定义 Runner 与可选可视化编排器之间的最小接口。

    Runner 只知道编排器能执行单语句和脚本，不导入 UI 的具体类。
    这个依赖倒置保持运行层可独立测试，也使未启用追踪的调用方完全
    沿用原有路径。
    """

    def execute(self, runner: Runner, sql: str) -> QueryResult:
        """执行并追踪一条 SQL，返回原 QueryResult。"""

        ...

    def execute_script(
        self,
        runner: Runner,
        sql: str,
        *,
        stop_on_error: bool = True,
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
                计划、Executor 构建和运行上下文。
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
        self._executor_tree_builder = ExecutorTreeBuilder(trace_sink=trace_sink)

    @property
    def current_database(self) -> str:
        """返回当前会话所连接的数据库名。"""
        return self._context.current_database

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

    def execute(self, sql: str) -> QueryResult:
        """执行一条 SQL，并原样返回执行器产生的结果。

        开启 inspector 时，编排器负责真实解析和调用
        ``_execute_statement``；未开启时保持原有快速路径。
        """

        if self._inspector is not None:
            return self._inspector.execute(self, sql)
        statement = self._parse(sql)
        return self._execute_statement(statement)

    def _execute_statement(self, statement: Statement) -> QueryResult:
        """执行已解析的单条语句，避免脚本路径重复解析原 SQL。"""
        plan = self._logical_plan_builder.build(statement)
        executor = self._executor_tree_builder.build(plan)
        return executor.execute(self._context)

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
    ) -> ScriptResult:
        """按源码顺序执行脚本中的语句并汇总逐条结果。

        整段脚本先由 parse_script 解析；解析错误直接向调用方抛出。
        stop_on_error 只控制名称绑定和执行阶段的 SqlError。
        """
        if self._inspector is not None:
            return self._inspector.execute_script(
                self,
                sql,
                stop_on_error=stop_on_error,
            )
        parsed_statements = self._parse_script(sql)
        results: list[StatementResult] = []
        stopped_early = False

        for parsed in parsed_statements:
            started = perf_counter()
            try:
                result = self._execute_statement(parsed.statement)
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
    ) -> ScriptResult:
        """按 UTF-8 读取 SQL 文件并交给 execute_script 执行。"""
        try:
            input_path = Path(path).expanduser()
            sql = input_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, TypeError, ValueError) as error:
            raise SqlError(E_INPUT_FILE, f"cannot read SQL file {path!s}: {error}") from None
        return self.execute_script(sql, stop_on_error=stop_on_error)

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
    ) -> int:
        """进入终端会话；非 TTY 自动使用纯文本，返回会话退出码。"""
        from runner.terminal.session import TerminalSession

        return TerminalSession(
            self,
            data_dir=data_dir,
            plain=plain,
            history=history,
            stop_on_error=stop_on_error,
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
