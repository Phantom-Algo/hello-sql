"""HELLO-SQL 查询追踪编排器与 A/B/C 模块筛选模型。

本模块是可视化功能的运行时入口。``QueryInspector`` 使用 A 提供的
真实编译追踪解析 SQL，在同一次语句执行期间打开 B 和 C 的上下文
捕获，最后将三方阶段合并为不可变的 ``QueryTrace`` 并发布到
``TraceHub``。整个过程只执行一次 SQL，``/inspect`` 只读取已保存快照。

``InspectionModule`` 和 ``InspectionSnapshot`` 是界面边界。它们将用户输入的
``A``、``B``、``C`` 或 ``ALL`` 转换为有序阶段子集，但始终保留完整
``QueryTrace`` 作为来源。因此切换筛选不会重新解析、访问存储或执行语句。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from time import perf_counter
from typing import TYPE_CHECKING

from contracts.ast import ParsedStatement, SourceSpan
from contracts.errors import SqlError
from contracts.result import QueryResult, ScriptResult, StatementResult
from UI.compiler_trace import CompilerTraceResult, trace_parse, trace_parse_script
from UI.execution_trace import ExecutionTraceCollector, ExecutionTraceRouter
from UI.storage_trace import StorageTraceCollector, StorageTraceRouter
from UI.trace_hub import TraceHub
from UI.trace_models import (
    QueryTrace,
    StageTrace,
    TraceEvent,
    TraceOwner,
    TraceStatus,
)

if TYPE_CHECKING:
    from runner.runner import Runner
    from UI.viewer import InspectionViewer


class InspectionModule(str, Enum):
    """定义 ``/inspect`` 支持的模块视图。

    ``ALL`` 按全链路顺序显示所有阶段；``A``、``B`` 和 ``C`` 只显示
    对应负责人的阶段。枚举值同时是稳定的终端参数和后续网页查询参数。
    """

    ALL = "ALL"
    A = "A"
    B = "B"
    C = "C"

    @classmethod
    def parse(cls, value: str | InspectionModule | None) -> InspectionModule:
        """将可选用户参数规范化为模块枚举。

        Args:
            value: ``None`` 表示默认全部；字符串不区分大小写。

        Returns:
            规范化后的 ``InspectionModule``。

        Raises:
            ValueError: 参数不是 A、B、C 或 ALL 时抛出。
        """

        if value is None:
            return cls.ALL
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise ValueError("inspect module must be A, B, C, or ALL")
        try:
            return cls(value.strip().upper())
        except ValueError:
            raise ValueError("inspect module must be A, B, C, or ALL") from None


@dataclass(frozen=True, slots=True)
class InspectionSnapshot:
    """一次 ``/inspect`` 读取得到的不可变界面快照。

    ``trace`` 始终是 TraceHub 中的完整记录，``stages`` 是按当前
    ``module`` 计算的只读子集。这种结构使终端和未来窗口可共用同一
    筛选结果，而不会修改或丢失原始追踪。
    """

    trace: QueryTrace
    module: InspectionModule
    stages: tuple[StageTrace, ...]

    def __post_init__(self) -> None:
        """验证快照类型、阶段归属和原有顺序。

        筛选后的阶段必须来自完整追踪；非 ALL 视图中的每个阶段还必须
        与选定负责人一致。构造期检查可防止界面展示错位数据。
        """

        if not isinstance(self.trace, QueryTrace):
            raise TypeError("InspectionSnapshot.trace must be QueryTrace")
        if not isinstance(self.module, InspectionModule):
            raise TypeError("InspectionSnapshot.module must be InspectionModule")
        stages = tuple(self.stages)
        if any(not isinstance(stage, StageTrace) for stage in stages):
            raise TypeError("InspectionSnapshot.stages must contain StageTrace")
        trace_ids = {id(stage) for stage in self.trace.stages}
        if any(id(stage) not in trace_ids for stage in stages):
            raise ValueError("filtered stages must come from the source QueryTrace")
        if self.module is not InspectionModule.ALL and any(
            stage.owner.value != self.module.value for stage in stages
        ):
            raise ValueError("filtered stage owner does not match the selected module")
        object.__setattr__(self, "stages", stages)

    def to_dict(self) -> dict[str, object]:
        """返回前端可直接消费的脱离快照字典。

        字典保留完整查询元数据，但 ``stages`` 只包含当前筛选结果。
        ``linkage`` 是从同一份追踪生成的节点/Token/页/SQL 只读索引。
        返回值中不含 dataclass、枚举或可调用对象。
        """

        # 延迟导入使联动层只依赖已经构建完成的快照契约，
        # 避免 inspection 模型在定义阶段与 linkage 的类型提示形成循环导入。
        from UI.linkage import build_linkage

        data = self.trace.to_dict()
        data["module"] = self.module.value
        data["stages"] = [stage.to_dict() for stage in self.stages]
        data["linkage"] = build_linkage(self)
        return data


class QueryInspector:
    """编排真实 SQL 执行并为 ``/inspect`` 保存最近记录。

    对象生命周期应与一个 Runner 会话一致。``storage_router`` 在构造
    ``DatabaseServer`` 时注入，``execution_router`` 在构造 Runner 时注入。
    Runner 再把自身的 execute 调用委托给本对象，从而保证解析、绑定、
    执行和存储全部来自同一次业务调用。
    """

    def __init__(self, *, capacity: int = 20) -> None:
        """创建一个会话级追踪编排器。

        Args:
            capacity: TraceHub 最多保留的语句数，默认为 20。

        构造函数只建立内存对象，不连接数据库、不解析 SQL，也不创建
        后台线程。
        """

        self._hub = TraceHub(capacity=capacity)
        self._storage_router = StorageTraceRouter()
        self._execution_router = ExecutionTraceRouter()
        self._viewer: InspectionViewer | None = None

    @property
    def hub(self) -> TraceHub:
        """返回会话使用的线程安全 TraceHub。"""

        return self._hub

    @property
    def storage_router(self) -> StorageTraceRouter:
        """返回应注入 ``DatabaseServer`` 的 B 追踪路由器。"""

        return self._storage_router

    @property
    def execution_router(self) -> ExecutionTraceRouter:
        """返回应注入 ``Runner`` 的 C 追踪路由器。"""

        return self._execution_router

    def execute(self, runner: Runner, sql: str) -> QueryResult:
        """追踪并执行一条与 ``Runner.execute`` 兼容的 SQL。

        A 在同一次解析中产生 AST 和四个编译阶段；解析成功后才打开
        B/C 捕获并执行该 AST。语法或运行错误会在发布 FAILED 追踪后
        按原类型重新抛出，不改变 Runner 公开契约。
        """

        compiled = trace_parse(sql)
        if compiled.error is not None:
            self._publish_compile_failure(runner, sql, compiled)
            compiled.raise_for_error()
        parsed = compiled.statements[0]
        result, error, _ = self._run_statement(
            runner,
            parsed,
            compiled,
            statement_index=1,
            statement_count=1,
            input_sql=sql,
        )
        if error is not None:
            raise error
        assert result is not None
        return result

    def execute_script(
        self,
        runner: Runner,
        sql: str,
        *,
        stop_on_error: bool = True,
    ) -> ScriptResult:
        """只解析一次脚本，逐条执行并发布查询追踪。

        Args:
            runner: 实际执行已解析 AST 的会话 Runner。
            sql: 可包含多条语句的完整 SQL 原文。
            stop_on_error: 绑定或执行错误后是否停止后续语句。

        Returns:
            与 ``Runner.execute_script`` 完全一致的逐语句结果。

        Raises:
            ParseError: 整段脚本语法无效时，在保存失败追踪后抛出。
        """

        compiled = trace_parse_script(sql)
        if compiled.error is not None:
            self._publish_compile_failure(runner, sql, compiled)
            compiled.raise_for_error()

        statements = compiled.statements
        results: list[StatementResult] = []
        stopped_early = False
        for index, parsed in enumerate(statements, start=1):
            result, error, elapsed_ms = self._run_statement(
                runner,
                parsed,
                compiled,
                statement_index=index,
                statement_count=len(statements),
                input_sql=sql,
            )
            results.append(
                StatementResult(
                    sql=parsed.sql,
                    span=parsed.span,
                    result=result,
                    error=error,
                    elapsed_ms=elapsed_ms,
                )
            )
            if error is not None and stop_on_error:
                stopped_early = True
                break
        return ScriptResult(tuple(results), stopped_early=stopped_early)

    def latest(
        self,
        module: str | InspectionModule | None = None,
    ) -> InspectionSnapshot | None:
        """读取最近一条追踪并按 A/B/C/ALL 筛选。

        本方法不会消费缓存记录，也不会触发任何 SQL 阶段。当 Hub 为空时
        返回 ``None``，便于终端给出友好的“暂无记录”提示。
        """

        selected = InspectionModule.parse(module)
        trace = self._hub.latest()
        if trace is None:
            return None
        stages = trace.stages
        if selected is not InspectionModule.ALL:
            stages = tuple(
                stage for stage in stages if stage.owner.value == selected.value
            )
        return InspectionSnapshot(trace, selected, stages)

    def open_view(
        self,
        module: str | InspectionModule | None = None,
    ) -> tuple[str, bool]:
        """懒启动本地查看器并打开指定模块页面。

        Args:
            module: A、B、C 或 ALL；``None`` 默认为 ALL。

        Returns:
            ``(url, opened)``：本地 URL 以及系统浏览器是否报告打开成功。

        服务器只在首次调用时创建，后续 A/B/C 切换复用同一本机
        端口。打开前先通过 ``InspectionModule`` 验证参数，防止命令层与
        网页层对可用筛选产生不一致理解。
        """

        selected = InspectionModule.parse(module)
        if self._viewer is None:
            # 延迟导入可避免只使用 TraceHub/API 的场景初始化 HTTP 层，
            # 同时保持 viewer 对 InspectionSnapshot 的清晰类型依赖。
            from UI.viewer import InspectionViewer

            self._viewer = InspectionViewer(self.latest)
        return self._viewer.open(selected.value)

    def close_view(self) -> None:
        """关闭已启动的本地查看器，未启动时无副作用。

        本方法不清空 TraceHub，所以关闭窗口服务后仍可以用终端
        摘要读取历史，也可在下次 ``open_view`` 时重建服务。
        """

        if self._viewer is not None:
            self._viewer.close()
            self._viewer = None

    def _publish_compile_failure(
        self,
        runner: Runner,
        sql: str,
        compiled: CompilerTraceResult,
    ) -> QueryTrace:
        """将 Lexer 或 Parser 失败保存为可继续检查的追踪。

        编译失败时没有可执行的 ParsedStatement，因此使用整个输入作为
        追踪 SQL，并用 ParseError 的一基行列建立单点 SourceSpan。
        发布后原 ParseError 仍由调用方抛出和渲染。
        """

        error = compiled.error
        assert error is not None
        reservation = self._hub.reserve()
        span = SourceSpan(error.line, error.col, error.line, error.col)
        trace = QueryTrace(
            trace_id=reservation.trace_id,
            query_number=reservation.query_number,
            sql=sql,
            database=runner.current_database,
            status=TraceStatus.FAILED,
            stages=(self._repl_stage(sql, 1, 1, span), *compiled.stages),
            source_span=span,
            result_summary={"kind": "error", "phase": "compile"},
            error_code=error.code,
            error_message=error.message,
        )
        return self._hub.publish(trace)

    def _run_statement(
        self,
        runner: Runner,
        parsed: ParsedStatement,
        compiled: CompilerTraceResult,
        *,
        statement_index: int,
        statement_count: int,
        input_sql: str,
    ) -> tuple[QueryResult | None, SqlError | None, float]:
        """执行已解析语句，合并 A/B/C 阶段并发布最终记录。

        B 和 C 的 capture 在调用 ``Runner._execute_statement`` 之前同时开启，
        所以收集的是这条语句的真实存储、绑定、计划和执行事件。
        可预期 SqlError 转为 FAILED 追踪后返回；非 SQL 异常会取消未发布
        预约并原样上抛，防止 TraceHub 留下假的运行中记录。
        """

        reservation = self._hub.reserve()
        database = runner.current_database
        started = perf_counter()
        result: QueryResult | None = None
        error: SqlError | None = None
        storage_collector: StorageTraceCollector
        execution_collector: ExecutionTraceCollector
        try:
            with self._storage_router.capture() as storage_collector:
                with self._execution_router.capture() as execution_collector:
                    try:
                        result = runner._execute_statement(parsed.statement)
                    except SqlError as caught:
                        error = caught
        except BaseException:
            self._hub.abandon(reservation)
            raise

        elapsed_ms = (perf_counter() - started) * 1000
        stages = (
            self._repl_stage(
                input_sql,
                statement_index,
                statement_count,
                parsed.span,
            ),
            *compiled.stages,
            *storage_collector.build_stages(),
            *execution_collector.build_stages(),
        )
        trace = QueryTrace(
            trace_id=reservation.trace_id,
            query_number=reservation.query_number,
            sql=parsed.sql,
            database=database,
            status=TraceStatus.FAILED if error else TraceStatus.SUCCESS,
            stages=stages,
            statement_index=statement_index,
            statement_count=statement_count,
            source_span=parsed.span,
            elapsed_ms=elapsed_ms,
            result_summary=(
                {"kind": "error", "phase": "execute"}
                if error
                else self._result_summary(result)
            ),
            error_code=error.code if error else None,
            error_message=error.message if error else None,
        )
        self._hub.publish(trace)
        return result, error, elapsed_ms

    @staticmethod
    def _repl_stage(
        sql: str,
        statement_index: int,
        statement_count: int,
        span: SourceSpan,
    ) -> StageTrace:
        """构建第 1 阶段，记录 REPL 接收的原始输入与语句位置。

        该阶段只陈述输入已被会话接收，不声称后续解析或执行成功。
        对多语句脚本，原始缓冲区保留在输入快照中，SourceSpan 标记当前
        语句在整个缓冲区中的位置。
        """

        event = TraceEvent(
            event_id="repl.receive.0001",
            sequence=1,
            action="接收 SQL 输入",
            description=f"脚本中的第 {statement_index}/{statement_count} 条语句。",
            input_snapshot={"buffer": sql},
            output_snapshot={
                "statement_index": statement_index,
                "statement_count": statement_count,
            },
            metrics={"buffer_character_count": len(sql)},
            source_span=span,
        )
        return StageTrace(
            stage_id="c.repl",
            sequence=1,
            owner=TraceOwner.C,
            name="REPL",
            description="接收用户 SQL 缓冲区并建立查询追踪身份。",
            status=TraceStatus.SUCCESS,
            input_contract="terminal SQL buffer",
            output_contract="query identity + source location",
            input_snapshot={"sql": sql},
            events=(event,),
            output_snapshot={
                "statement_index": statement_index,
                "statement_count": statement_count,
            },
            metrics={"character_count": len(sql)},
            source_span=span,
        )

    @staticmethod
    def _result_summary(result: QueryResult | None) -> dict[str, object]:
        """把成功 QueryResult 转换为小型、可 JSON 序列化的摘要。

        SELECT 保存列名、总行数和最多 5 行预览；DDL/DML 保存影响
        行数。预览而不是完整结果可避免 TraceHub 在大查询时成为第二份
        数据缓存。
        """

        assert result is not None
        if result.columns is not None and result.rows is not None:
            return {
                "kind": "rows",
                "columns": list(result.columns),
                "row_count": len(result.rows),
                "rows_preview": [list(row) for row in result.rows[:5]],
                "preview_limit": 5,
            }
        return {
            "kind": "affected_rows",
            "affected_rows": result.affected_rows or 0,
        }


def format_inspection_text(snapshot: InspectionSnapshot) -> str:
    """将筛选快照渲染为无 ANSI 的稳定文本。

    纯文本 REPL、管道输入和测试环境使用该格式。每个阶段占一行，
    保留负责模块、顺序、状态、事件数和耗时，既可人工阅读也方便
    后续脚本采集。
    """

    trace = snapshot.trace
    lines = [
        (
            f"Query #{trace.query_number} {trace.trace_id} "
            f"[{trace.status.value}] module={snapshot.module.value}"
        ),
        f"Database: {trace.database}",
        f"SQL: {trace.sql}",
    ]
    for stage in snapshot.stages:
        lines.append(
            f"{stage.sequence:02d} {stage.owner.value} {stage.name} "
            f"[{stage.status.value}] events={len(stage.events)} "
            f"elapsed={stage.elapsed_ms:.3f}ms"
        )
    if not snapshot.stages:
        lines.append("(no stages for selected module)")
    return "\n".join(lines)
