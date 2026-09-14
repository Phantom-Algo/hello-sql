"""SQL 语句运行入口与交互式命令行。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from time import perf_counter

from contracts.ast import ParsedStatement, Script, SourceSpan, Statement
from contracts.errors import E_INPUT_FILE, SqlError
from contracts.result import QueryResult, ScriptResult, StatementResult
from contracts.storage import BaseDatabaseServer, TableInfo
from runner.executor.builder import ExecutorTreeBuilder
from runner.executor.context import ExecutionContext
from runner.logical_plan.builder import LogicalPlanBuilder
from runner.logical_plan.optimizer import LogicalOptimizer, OptimizationLog


DEFAULT_DATABASE = "main"
ParseSql = Callable[[str], Statement]
ParseScript = Callable[[str], Script]


class Runner:
    """串联 SQL 各执行阶段，并维护单个会话状态。"""

    def __init__(
        self,
        server: BaseDatabaseServer,
        parse: ParseSql,
        current_database: str = DEFAULT_DATABASE,
        parse_script: ParseScript | None = None,
    ) -> None:
        # 先完成连接；连接失败时不创建半初始化的会话上下文。
        storage = server.connect(current_database)
        self._parse = parse
        self._parse_script = parse_script or self._parse_as_single_statement_script
        self._context = ExecutionContext(
            server=server,
            storage=storage,
            current_database=current_database,
        )
        self._logical_plan_builder = LogicalPlanBuilder(
            self._describe_current_table
        )
        self._logical_optimizer = LogicalOptimizer()
        self._executor_tree_builder = ExecutorTreeBuilder(
            self._describe_current_table,
            self._current_database_name,
        )
        self._last_optimization_log: OptimizationLog | None = None

    @property
    def current_database(self) -> str:
        """返回当前会话所连接的数据库名。"""
        return self._context.current_database

    @property
    def last_optimization_log(self) -> OptimizationLog | None:
        """最近一条语句的优化日志；关闭优化或尚未执行语句时为 None。"""
        return self._last_optimization_log

    def _describe_current_table(self, table: str) -> TableInfo:
        """动态读取当前 Storage 的表结构，保证 USE 后访问新数据库。"""
        return self._context.storage.describe(table)

    def _current_database_name(self) -> str:
        """动态读取当前库名，作为执行器构建期表结构缓存的键。"""
        return self._context.current_database

    def execute(self, sql: str, *, optimize: bool = True) -> QueryResult:
        """执行一条 SQL，并原样返回执行器产生的结果。

        optimize 逐语句生效，只影响是否经过逻辑优化器，不影响绑定阶段，
        因此开关前后名称绑定与类型错误的行为完全一致。
        """
        statement = self._parse(sql)
        return self._execute_statement(statement, optimize=optimize)

    def _execute_statement(
        self,
        statement: Statement,
        *,
        optimize: bool = True,
    ) -> QueryResult:
        """执行已解析的单条语句，避免脚本路径重复解析原 SQL。"""
        plan = self._logical_plan_builder.build(statement)
        if optimize:
            log = self._logical_optimizer.optimize(plan)
            self._last_optimization_log = log
            plan = log.optimized
        else:
            self._last_optimization_log = None
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
        optimize: bool = True,
    ) -> ScriptResult:
        """按源码顺序执行脚本中的语句并汇总逐条结果。

        整段脚本先由 parse_script 解析；解析错误直接向调用方抛出。
        stop_on_error 只控制名称绑定和执行阶段的 SqlError；optimize 透传给
        每条语句，使基准工具能对整段脚本统一关闭优化器。
        """
        parsed_statements = self._parse_script(sql)
        results: list[StatementResult] = []
        stopped_early = False

        for parsed in parsed_statements:
            started = perf_counter()
            try:
                result = self._execute_statement(
                    parsed.statement, optimize=optimize
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
    ) -> ScriptResult:
        """按 UTF-8 读取 SQL 文件并交给 execute_script 执行。"""
        try:
            input_path = Path(path).expanduser()
            sql = input_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, TypeError, ValueError) as error:
            raise SqlError(E_INPUT_FILE, f"cannot read SQL file {path!s}: {error}") from None
        return self.execute_script(
            sql, stop_on_error=stop_on_error, optimize=optimize
        )

    def list_databases(self) -> list[str]:
        """向终端提供库名，终端不接触存储内部结构。"""
        return self._context.server.list_databases()

    def list_tables(self) -> list[str]:
        return self._context.storage.list_tables()

    def describe_table(self, name: str) -> TableInfo:
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
