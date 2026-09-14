"""C 模块的查询级追踪：名称绑定、计划、执行树与运行时。

Runner 核心通过 ``runner.trace_hooks`` 发送普通字典。本模块在回调
活动期间对 AST、表结构、计划、执行树、数据行与结果对象做快照，
再转换为共享 ``StageTrace`` 契约。因此查看器只回放已捕获数据，
不会再次绑定或执行 SQL。

``ExecutionTraceRouter`` 的生命周期与 Runner 一致，每条语句单独打开
捕获上下文，使并发和嵌套语句不会混用事件。优化器关闭时收集器发布
“未启用”阶段，而不伪造优化结果。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
import math
from pathlib import Path
from threading import RLock

from UI.trace_models import StageTrace, TraceEvent, TraceOwner, TraceStatus


_COMPONENTS = ("binding", "logical_plan", "optimizer", "executor", "runtime")
_STAGE_CONFIG: dict[str, tuple[str, int, str, str, str, str]] = {
    "binding": (
        "c.binding",
        10,
        "Binder",
        "Resolve tables, columns, qualifiers, types, predicates, and projection order.",
        "AST statement + Catalog schema",
        "bound schema and expressions",
    ),
    "logical_plan": (
        "c.logical_plan",
        11,
        "Logical Plan",
        "Build an immutable plan tree from the bound statement.",
        "bound statement",
        "LogicalPlan tree",
    ),
    "optimizer": (
        "c.optimizer",
        12,
        "Optimizer",
        "Apply the enabled rewrite rules and report the rule log.",
        "LogicalPlan tree",
        "optimized LogicalPlan tree",
    ),
    "executor": (
        "c.executor",
        13,
        "Executor Tree",
        "Project the logical tree into a DQL, DML, or DDL executor tree.",
        "LogicalPlan tree",
        "StatementExecutor tree",
    ),
    "runtime": (
        "c.runtime",
        14,
        "Runtime",
        "Record pull-operator rows, samples, elapsed time, and final results.",
        "Executor tree + ExecutionContext",
        "QueryResult and operator statistics",
    ),
}


@dataclass(frozen=True, slots=True)
class _ExecutionCallRecord:
    """一次已完成、失败或提前停止的 C 调用脱离式快照。"""

    ordinal: int
    component: str
    operation: str
    status: str
    started_at: float
    elapsed_ms: float
    arguments: object
    keyword_arguments: object
    result: object
    metrics: object
    error_code: str | None
    error_message: str | None


class ExecutionTraceCollector:
    """收集一条语句的 C 模块事件，并构建五个稳定阶段。

    记录保留回调到达顺序，该顺序在嵌套调用中也是完成顺序。最终计划
    返回前必须先完成绑定，叶子扫描也会早于上层过滤与投影完成，
    因此这一顺序适合展示数据如何在拉取式流水线中向上流动。
    """

    def __init__(self) -> None:
        """创建线程安全的空记录列表和从一开始的序号。"""

        self._lock = RLock()
        self._records: list[_ExecutionCallRecord] = []
        self._next_ordinal = 1

    def __call__(self, payload: dict[str, object]) -> None:
        """允许收集器实例直接作为 ``Runner.trace_sink`` 传入。"""

        self.record(payload)

    def record(self, payload: Mapping[str, object]) -> None:
        """验证并立即快照一条 C 回调数据。

        Args:
            payload: 包含组件、操作、状态、耗时、参数和结果的映射。

        Raises:
            TypeError: 数据或错误字段类型不正确。
            ValueError: 组件、状态、耗时或失败详情不合法。
        """

        if not isinstance(payload, Mapping):
            raise TypeError("execution trace payload must be a mapping")
        component = payload.get("component")
        operation = payload.get("operation")
        status = payload.get("status")
        started_at = payload.get("started_at")
        elapsed_ms = payload.get("elapsed_ms")
        if component not in _COMPONENTS:
            raise ValueError(f"unknown execution component: {component!r}")
        if not isinstance(operation, str) or not operation.strip():
            raise ValueError("execution operation must be non-empty")
        if status not in {"success", "failed", "stopped", "disabled"}:
            raise ValueError(f"invalid execution status: {status!r}")
        if not _finite_number(started_at):
            raise ValueError("started_at must be finite")
        if not _finite_number(elapsed_ms) or float(elapsed_ms) < 0:
            raise ValueError("elapsed_ms must be finite and non-negative")
        error_code = payload.get("error_code")
        error_message = payload.get("error_message")
        if status == "failed" and error_code is None and error_message is None:
            raise ValueError("failed call requires error details")
        if status == "disabled" and (
            error_code is not None or error_message is not None
        ):
            raise ValueError("disabled call must not carry error details")
        if error_code is not None and not isinstance(error_code, str):
            raise TypeError("error_code must be str or None")
        if error_message is not None and not isinstance(error_message, str):
            raise TypeError("error_message must be str or None")

        with self._lock:
            self._records.append(
                _ExecutionCallRecord(
                    ordinal=self._next_ordinal,
                    component=component,
                    operation=operation,
                    status=status,
                    started_at=float(started_at),
                    elapsed_ms=float(elapsed_ms),
                    arguments=_snapshot(payload.get("arguments", ())),
                    keyword_arguments=_snapshot(
                        payload.get("keyword_arguments", {})
                    ),
                    result=_snapshot(payload.get("result")),
                    metrics=_snapshot(payload.get("metrics", {})),
                    error_code=error_code,
                    error_message=error_message,
                )
            )
            self._next_ordinal += 1

    @property
    def operation_count(self) -> int:
        """返回已终止的绑定、计划与运行时调用总数。"""

        with self._lock:
            return len(self._records)

    def clear(self) -> int:
        """清空记录、重置事件序号，并返回被移除的记录数。"""

        with self._lock:
            removed = len(self._records)
            self._records.clear()
            self._next_ordinal = 1
            return removed

    def build_stages(self) -> tuple[StageTrace, ...]:
        """构建绑定、计划、未启用优化器、执行树和运行时阶段。

        已实现但本次没有调用的组件标记为“已跳过”，存在任意失败调用的
        组件标记为“失败”。当前版本没有优化规则，因此优化器始终标记为“未启用”。
        """

        with self._lock:
            records = tuple(self._records)
        sequences = {
            item.ordinal: index
            for index, item in enumerate(records, start=1)
        }
        binding = self._build_component_stage("binding", records, sequences)
        logical_plan = self._build_component_stage(
            "logical_plan", records, sequences
        )
        optimizer = self._build_component_stage("optimizer", records, sequences)
        executor = self._build_component_stage("executor", records, sequences)
        runtime = self._build_component_stage("runtime", records, sequences)
        return (binding, logical_plan, optimizer, executor, runtime)

    @staticmethod
    def _build_component_stage(
        component: str,
        all_records: tuple[_ExecutionCallRecord, ...],
        sequences: Mapping[int, int],
    ) -> StageTrace:
        """将一个已实现的 C 组件转换为只读阶段。

        Args:
            component: binding、logical_plan、optimizer、executor 或 runtime。
            all_records: 按完成顺序排列的全部 C 记录。
            sequences: 回调序号到界面播放序号的映射。
        """

        records = tuple(
            item for item in all_records if item.component == component
        )
        config = _STAGE_CONFIG[component]
        stage_id, stage_sequence, name, description = config[:4]
        input_contract, output_contract = config[4:]
        if not records:
            return StageTrace(
                stage_id=stage_id,
                sequence=stage_sequence,
                owner=TraceOwner.C,
                name=name,
                description=f"{description} This statement did not run the stage.",
                status=TraceStatus.SKIPPED,
                input_contract=input_contract,
                output_contract=output_contract,
            )
        events = tuple(
            _record_to_event(item, sequences[item.ordinal])
            for item in records
        )
        failed = next(
            (item for item in records if item.status == "failed"),
            None,
        )
        disabled = next(
            (item for item in records if item.status == "disabled"),
            None,
        )
        if disabled is not None:
            description = f"{description} This stage was explicitly switched off."
        if failed is not None:
            status = TraceStatus.FAILED
        elif disabled is not None:
            status = TraceStatus.DISABLED
        else:
            status = TraceStatus.SUCCESS
        first_started = min(item.started_at for item in records)
        last_finished = max(
            item.started_at + item.elapsed_ms / 1000
            for item in records
        )
        return StageTrace(
            stage_id=stage_id,
            sequence=stage_sequence,
            owner=TraceOwner.C,
            name=name,
            description=description,
            status=status,
            input_contract=input_contract,
            output_contract=output_contract,
            input_snapshot={
                "first_arguments": records[0].arguments,
                "operation_count": len(records),
            },
            events=events,
            output_snapshot=_stage_output(component, records, events),
            metrics={
                "operation_count": len(records),
                "failed_count": sum(
                    item.status == "failed" for item in records
                ),
                "stopped_count": sum(
                    item.status == "stopped" for item in records
                ),
                "disabled_count": sum(
                    item.status == "disabled" for item in records
                ),
            },
            elapsed_ms=(last_finished - first_started) * 1000,
            error_code=failed.error_code if failed else None,
            error_message=failed.error_message if failed else None,
        )


class ExecutionTraceRouter:
    """将共享 Runner 产生的事件路由到当前查询收集器。

    ``ContextVar`` 隔离线程、任务与嵌套捕获。捕获范围之外的事件直接丢弃，
    因此长期存活的路由器不会无限积累历史。
    """

    def __init__(self) -> None:
        """创建默认不含活动收集器的上下文变量。"""

        self._current: ContextVar[ExecutionTraceCollector | None] = ContextVar(
            f"hello_sql_execution_trace_{id(self)}",
            default=None,
        )

    def __call__(self, payload: dict[str, object]) -> None:
        """若当前存在活动收集器，则把一条 C 事件转发给它。"""

        collector = self._current.get()
        if collector is not None:
            collector.record(payload)

    @contextmanager
    def capture(self) -> Iterator[ExecutionTraceCollector]:
        """创建当前查询收集器，退出时恢复外层路由。

        Yields:
            当前上下文专用的 ``ExecutionTraceCollector``。

        Notes:
            内层事件不会复制到外层捕获中；即使绑定或执行抛出异常，
            ``finally`` 块也会恢复正确路由。
        """

        collector = ExecutionTraceCollector()
        token = self._current.set(collector)
        try:
            yield collector
        finally:
            self._current.reset(token)


def _record_to_event(
    record: _ExecutionCallRecord,
    sequence: int,
) -> TraceEvent:
    """将一条 C 调用转换为包含输入、输出、错误与指标的事件。"""

    if record.status == "failed":
        description = "The call failed and preserved its original error."
    elif record.status == "stopped":
        description = "The row stream closed before full consumption."
    elif record.status == "disabled":
        description = "The feature was explicitly switched off; the call was not made."
    else:
        description = "The call completed successfully."
    return TraceEvent(
        event_id=f"{record.component}.call.{sequence:04d}",
        sequence=sequence,
        action=f"{record.component}.{record.operation}",
        description=description,
        input_snapshot={
            "arguments": record.arguments,
            "keyword_arguments": record.keyword_arguments,
        },
        output_snapshot={
            "status": record.status,
            "result": record.result,
            "error_code": record.error_code,
            "error_message": record.error_message,
        },
        metrics=(
            record.metrics if isinstance(record.metrics, Mapping) else {}
        ),
        elapsed_ms=record.elapsed_ms,
    )


def _stage_output(
    component: str,
    records: tuple[_ExecutionCallRecord, ...],
    events: tuple[TraceEvent, ...],
) -> dict[str, object]:
    """概括操作分布、最终输出与运行时数据行数量。"""

    output: dict[str, object] = {
        "operation_counts": dict(
            Counter(item.operation for item in records)
        ),
        "last_result": records[-1].result,
    }
    if component == "optimizer":
        # 规则命中摘要由 C 侧在优化器返回值上提取，查看器无需解析计划快照
        output["optimization"] = records[-1].metrics
    if component == "runtime":
        output["operator_statistics"] = [
            {"action": event.action, **dict(event.metrics)}
            for event in events
        ]
        execute_events = [
            event for event in events if event.action.endswith(".execute")
        ]
        if execute_events:
            output["query_result"] = dict(execute_events[-1].metrics)
    return output


def _finite_number(value: object) -> bool:
    """判断值是否为排除布尔值的有限整数或浮点数。"""

    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _snapshot(
    value: object,
    active: set[int] | None = None,
    depth: int = 0,
) -> object:
    """将 C 模块内部值转换为大小受控、与业务对象脱离的 JSON 快照。

    数据类保留类型与字段，枚举保留公开值，序列最多保留五十项，递归最深二十层。
    活动对象标识集用于检测循环引用，不会把共享子树误判为循环。
    """

    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, Path):
        return str(value)
    if depth >= 20:
        return {"value_type": type(value).__name__, "truncated": "max_depth"}
    active = active if active is not None else set()
    identity = id(value)
    if identity in active:
        return {"value_type": type(value).__name__, "circular_reference": True}
    active.add(identity)
    try:
        if is_dataclass(value) and not isinstance(value, type):
            return {
                "value_type": type(value).__name__,
                "fields": {
                    item.name: _snapshot(
                        getattr(value, item.name), active, depth + 1
                    )
                    for item in fields(value)
                },
            }
        if isinstance(value, Mapping):
            items = list(value.items())
            return {
                str(key): _snapshot(item, active, depth + 1)
                for key, item in items[:50]
            }
        if isinstance(value, (tuple, list)):
            return [
                _snapshot(item, active, depth + 1)
                for item in value[:50]
            ]
        return {
            "value_type": type(value).__name__,
            "representation": repr(value),
        }
    finally:
        active.remove(identity)


__all__ = ["ExecutionTraceCollector", "ExecutionTraceRouter"]
