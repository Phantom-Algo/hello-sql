"""Query-level tracing for C: Binder, plans, executors, and runtime.

The runner core emits plain dictionaries through runner.trace_hooks.  This
module snapshots AST, schema, plan, executor, row, and result objects while
the callback is active, then converts those snapshots into the shared
StageTrace contract.  A viewer therefore replays captured data and never
binds or executes SQL a second time.

ExecutionTraceRouter is intended to live as long as Runner.  One capture
context is opened per statement, so concurrent and nested statements do not
share events.  The current project has no optimizer; the collector exposes a
real DISABLED optimizer stage instead of inventing an optimization result.
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


_COMPONENTS = ("binding", "logical_plan", "executor", "runtime")
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
    """Detached snapshot of one completed, failed, or stopped C call."""

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
    """Collect one statement's C events and build five stable stages.

    Records retain callback arrival order, which is completion order for
    nested calls.  Binding completes before the final plan is returned, and
    a leaf scan completes before its filter and projection parents.  This is
    useful for visualizing data as it flows upward through a pull pipeline.
    """

    def __init__(self) -> None:
        """Create a thread-safe empty record list and one-based ordinal."""

        self._lock = RLock()
        self._records: list[_ExecutionCallRecord] = []
        self._next_ordinal = 1

    def __call__(self, payload: dict[str, object]) -> None:
        """Allow a collector to be passed directly as Runner.trace_sink."""

        self.record(payload)

    def record(self, payload: Mapping[str, object]) -> None:
        """Validate and immediately snapshot one C callback payload.

        Args:
            payload: Component, operation, state, timing, arguments, and result.

        Raises:
            TypeError: A payload or error field has the wrong type.
            ValueError: Component, state, timing, or failure details are invalid.
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
        if status not in {"success", "failed", "stopped"}:
            raise ValueError(f"invalid execution status: {status!r}")
        if not _finite_number(started_at):
            raise ValueError("started_at must be finite")
        if not _finite_number(elapsed_ms) or float(elapsed_ms) < 0:
            raise ValueError("elapsed_ms must be finite and non-negative")
        error_code = payload.get("error_code")
        error_message = payload.get("error_message")
        if status == "failed" and error_code is None and error_message is None:
            raise ValueError("failed call requires error details")
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
        """Return the number of terminal binding, plan, and runtime calls."""

        with self._lock:
            return len(self._records)

    def clear(self) -> int:
        """Clear records, reset event numbering, and return the removed count."""

        with self._lock:
            removed = len(self._records)
            self._records.clear()
            self._next_ordinal = 1
            return removed

    def build_stages(self) -> tuple[StageTrace, ...]:
        """Build Binder, Plan, disabled Optimizer, Executor, and Runtime stages.

        Implemented components with no call are SKIPPED, and components with
        any failed call are FAILED.  Optimizer is always DISABLED because no
        optimization rules exist in this version.
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
        executor = self._build_component_stage("executor", records, sequences)
        runtime = self._build_component_stage("runtime", records, sequences)
        optimizer = StageTrace(
            stage_id="c.optimizer",
            sequence=12,
            owner=TraceOwner.C,
            name="Optimizer",
            description=(
                "No logical optimizer exists yet; this stage is reserved for rules."
            ),
            status=TraceStatus.DISABLED,
            input_contract="LogicalPlan tree",
            output_contract="optimized LogicalPlan tree",
        )
        return (binding, logical_plan, optimizer, executor, runtime)

    @staticmethod
    def _build_component_stage(
        component: str,
        all_records: tuple[_ExecutionCallRecord, ...],
        sequences: Mapping[int, int],
    ) -> StageTrace:
        """Convert one implemented C component into a read-only stage.

        Args:
            component: binding, logical_plan, executor, or runtime.
            all_records: Every C record in completion order.
            sequences: Mapping from callback ordinal to playback sequence.
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
            status=TraceStatus.FAILED if failed else TraceStatus.SUCCESS,
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
            },
            elapsed_ms=(last_finished - first_started) * 1000,
            error_code=failed.error_code if failed else None,
            error_message=failed.error_message if failed else None,
        )


class ExecutionTraceRouter:
    """Route events from one shared Runner to the active query collector.

    ContextVar isolates threads, tasks, and nested captures.  Events outside
    capture are discarded, so a long-lived router does not accumulate history.
    """

    def __init__(self) -> None:
        """Create a context variable whose default has no active collector."""

        self._current: ContextVar[ExecutionTraceCollector | None] = ContextVar(
            f"hello_sql_execution_trace_{id(self)}",
            default=None,
        )

    def __call__(self, payload: dict[str, object]) -> None:
        """Forward one C event to the active collector, if one exists."""

        collector = self._current.get()
        if collector is not None:
            collector.record(payload)

    @contextmanager
    def capture(self) -> Iterator[ExecutionTraceCollector]:
        """Create a query collector and restore the outer route on exit.

        Yields:
            An ExecutionTraceCollector dedicated to the current context.

        Notes:
            Inner events are not copied into an outer capture.  The finally
            block restores routing even when binding or execution raises.
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
    """Convert one C call into an event with inputs, outputs, errors, and metrics."""

    if record.status == "failed":
        description = "The call failed and preserved its original error."
    elif record.status == "stopped":
        description = "The row stream closed before full consumption."
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
    """Summarize operation distribution, final output, and runtime row counts."""

    output: dict[str, object] = {
        "operation_counts": dict(
            Counter(item.operation for item in records)
        ),
        "last_result": records[-1].result,
    }
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
    """Return whether value is a finite int or float, excluding bool."""

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
    """Convert an internal C value into a bounded, detached JSON snapshot.

    Dataclasses retain type and fields, enums retain public values, sequences
    retain at most fifty items, and recursion stops at twenty levels.  The
    active identity set detects cycles without misclassifying shared subtrees.
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
