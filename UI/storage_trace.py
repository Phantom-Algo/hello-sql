"""B 模块 Catalog、Cache、Pager 与 Engine 的查询级追踪收集器。

存储核心只通过 storage.trace_hooks 发送与 UI 类型无关的瞬时字典；本模块
立即复制其中的路径、页数据、Schema、行和错误，再分组成四个 StageTrace。
推荐把 StorageTraceRouter 长期注入 DatabaseServer，并为每次查询打开一次
capture，使共享 BufferPool 的事件按上下文隔离且不会污染业务对象。
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


_COMPONENTS = ("catalog", "cache", "pager", "engine")
_STAGE_CONFIG: dict[str, tuple[str, int, str, str, str]] = {
    "catalog": (
        "b.catalog", 6, "Catalog",
        "维护用户表 Schema，并通过两张页式系统表持久化目录。",
        "Catalog arguments -> schema/system-table result",
    ),
    "cache": (
        "b.cache", 7, "Buffer Cache",
        "展示页帧命中、缺页读盘、pin、dirty、LRU 淘汰与写回。",
        "file/page key -> cached frame",
    ),
    "pager": (
        "b.pager", 8, "Pager",
        "管理页号、页 0、空闲页链表以及固定大小页的读写。",
        "page operation -> page number or page bytes",
    ),
    "engine": (
        "b.engine", 9, "Storage Engine",
        "执行行插入、扫描、定位、更新、删除与溢出页链操作。",
        "row values/row id -> row result or mutation",
    ),
}


@dataclass(frozen=True, slots=True)
class _StorageCallRecord:
    """一条已经脱离 B 内部可变页帧和实现对象的安全调用快照。

    started_at 用于恢复嵌套调用的真实进入顺序；ordinal 是回调到达顺序，
    在单调时钟值相同的极端情况下提供稳定的次级排序。
    """

    ordinal: int
    component: str
    operation: str
    status: str
    started_at: float
    elapsed_ms: float
    arguments: object
    keyword_arguments: object
    result: object
    cache_stats_before: object
    cache_stats_after: object
    error_code: str | None
    error_message: str | None


class StorageTraceCollector:
    """收集一次查询的 B 调用并构建四个只读阶段。

    Collector 是同步可调用对象，可直接作为 trace_sink 使用。record 在回调
    期间立即完成安全快照；build_stages 可重复调用且不会清空已有记录。
    """

    def __init__(self) -> None:
        """初始化线程安全的空记录列表和从一开始的到达序号。"""

        self._lock = RLock()
        self._records: list[_StorageCallRecord] = []
        self._next_ordinal = 1

    def __call__(self, payload: dict[str, object]) -> None:
        """允许 Collector 实例直接充当 DatabaseServer 的 trace_sink。"""

        self.record(payload)

    def record(self, payload: Mapping[str, object]) -> None:
        """验证并复制一条 B 调用，避免可变参数事后改变历史。

        Args:
            payload: 钩子提交的组件、操作、状态、耗时、参数和结果映射。

        Raises:
            TypeError: payload 或关键字段的类型不正确。
            ValueError: 组件、状态、耗时或失败详情不符合钩子契约。
        """

        if not isinstance(payload, Mapping):
            raise TypeError("storage trace payload must be a mapping")
        component = payload.get("component")
        operation = payload.get("operation")
        status = payload.get("status")
        started_at = payload.get("started_at")
        elapsed_ms = payload.get("elapsed_ms")
        if component not in _COMPONENTS:
            raise ValueError(f"unknown storage component: {component!r}")
        if not isinstance(operation, str) or not operation.strip():
            raise ValueError("storage operation must be non-empty")
        if status not in {"success", "failed", "stopped"}:
            raise ValueError(f"invalid storage status: {status!r}")
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
                _StorageCallRecord(
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
                    cache_stats_before=_snapshot(
                        payload.get("cache_stats_before", {})
                    ),
                    cache_stats_after=_snapshot(
                        payload.get("cache_stats_after", {})
                    ),
                    error_code=error_code,
                    error_message=error_message,
                )
            )
            self._next_ordinal += 1

    @property
    def operation_count(self) -> int:
        """返回已经完成、失败或提前停止的 B 操作数量。"""

        with self._lock:
            return len(self._records)

    def clear(self) -> int:
        """清空当前 Collector，重置序号并返回删除的记录数量。"""

        with self._lock:
            removed = len(self._records)
            self._records.clear()
            self._next_ordinal = 1
            return removed

    def build_stages(self) -> tuple[StageTrace, ...]:
        """按真实进入顺序生成 Catalog、Cache、Pager、Engine 四阶段。

        无调用的组件标记 SKIPPED；含失败调用的组件标记 FAILED。阶段耗时
        使用首个调用开始到最后调用结束的窗口，避免累加嵌套调用重复计时。

        Returns:
            sequence 为 6、7、8、9 且 stage_id 稳定的四个 StageTrace。
        """

        with self._lock:
            records = tuple(self._records)
        ordered = sorted(records, key=lambda item: (item.started_at, item.ordinal))
        sequences = {
            item.ordinal: index
            for index, item in enumerate(ordered, start=1)
        }
        return tuple(
            self._build_component_stage(
                component,
                tuple(item for item in ordered if item.component == component),
                sequences,
            )
            for component in _COMPONENTS
        )

    @staticmethod
    def _build_component_stage(
        component: str,
        records: tuple[_StorageCallRecord, ...],
        sequences: Mapping[int, int],
    ) -> StageTrace:
        """把一个 B 组件的有序调用转换为状态明确的 StageTrace。

        Args:
            component: catalog、cache、pager 或 engine。
            records: 已按进入时间排列的该组件记录。
            sequences: 每条记录到全局播放序号的映射。
        """

        stage_id, stage_sequence, name, description, contract = _STAGE_CONFIG[
            component
        ]
        input_contract, output_contract = contract.split(" -> ", maxsplit=1)
        if not records:
            return StageTrace(
                stage_id=stage_id,
                sequence=stage_sequence,
                owner=TraceOwner.B,
                name=name,
                description=f"{description} 本次查询未调用该组件。",
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
        counts = Counter(item.operation for item in records)
        first_started = min(item.started_at for item in records)
        last_finished = max(
            item.started_at + item.elapsed_ms / 1000
            for item in records
        )
        return StageTrace(
            stage_id=stage_id,
            sequence=stage_sequence,
            owner=TraceOwner.B,
            name=name,
            description=description,
            status=TraceStatus.FAILED if failed else TraceStatus.SUCCESS,
            input_contract=input_contract,
            output_contract=output_contract,
            input_snapshot={"operation_count": len(records)},
            events=events,
            output_snapshot={
                "operation_counts": dict(counts),
                "last_cache_stats": records[-1].cache_stats_after,
            },
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


class StorageTraceRouter:
    """把共享 BufferPool 的事件路由到当前查询 Collector。

    Router 长期作为 sink；ContextVar 让并发查询互不混入。没有活动 capture
    时事件会被静默忽略，因此正常运行不会增长追踪历史。
    """

    def __init__(self) -> None:
        """创建默认值为空的当前查询上下文变量。"""

        self._current: ContextVar[StorageTraceCollector | None] = ContextVar(
            f"hello_sql_storage_trace_{id(self)}",
            default=None,
        )

    def __call__(self, payload: dict[str, object]) -> None:
        """把同步 B 事件转发给当前 Collector；没有 capture 时直接返回。"""

        collector = self._current.get()
        if collector is not None:
            collector.record(payload)

    @contextmanager
    def capture(self) -> Iterator[StorageTraceCollector]:
        """建立当前查询专属捕获，并在退出时恢复外层上下文。

        嵌套 capture 时，内层事件只进入内层 Collector；业务抛错时 finally
        仍会恢复 ContextVar，避免后续查询收到错误路由。

        Yields:
            当前查询专属的 StorageTraceCollector。
        """

        collector = StorageTraceCollector()
        token = self._current.set(collector)
        try:
            yield collector
        finally:
            self._current.reset(token)


def _record_to_event(record: _StorageCallRecord, sequence: int) -> TraceEvent:
    """把一条调用记录转换为可跨组件按时间播放的 TraceEvent。

    缓存统计差值来自调用前后真实计数器；get_page 额外标记 hit 或 miss，
    使界面不必根据累计数字自行猜测本次访问结果。
    """

    deltas = _cache_stat_deltas(
        record.cache_stats_before,
        record.cache_stats_after,
    )
    cache_outcome: str | None = None
    if record.component == "cache" and record.operation == "get_page":
        if deltas.get("hits") == 1:
            cache_outcome = "hit"
        elif deltas.get("misses") == 1:
            cache_outcome = "miss"
    if record.status == "stopped":
        description = "调用提前停止"
    elif record.status == "failed":
        description = "调用失败并向上游传播异常"
    else:
        description = "调用成功返回"
    return TraceEvent(
        event_id=f"{record.component}.call.{sequence:04d}",
        sequence=sequence,
        action=f"{record.component}.{record.operation}",
        description=description,
        input_snapshot={
            "arguments": record.arguments,
            "keyword_arguments": record.keyword_arguments,
            "cache_stats_before": record.cache_stats_before,
        },
        output_snapshot={
            "status": record.status,
            "result": record.result,
            "cache_outcome": cache_outcome,
            "cache_stats_after": record.cache_stats_after,
            "error_code": record.error_code,
            "error_message": record.error_message,
        },
        metrics={"cache_deltas": deltas},
        elapsed_ms=record.elapsed_ms,
    )


def _cache_stat_deltas(
    before: object,
    after: object,
) -> dict[str, int | float]:
    """计算一次调用导致的可加缓存计数变化。

    capacity 与 hit_rate 不是可加计数，不进入差值；resident_frames 可以
    展示载入或淘汰结果，因此与命中、缺页、淘汰和脏页写回一起保留。
    """

    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return {}
    deltas: dict[str, int | float] = {}
    keys = (
        "hits",
        "misses",
        "evictions",
        "dirty_writes",
        "resident_frames",
    )
    for key in keys:
        old = before.get(key)
        new = after.get(key)
        if _finite_number(old) and _finite_number(new):
            deltas[key] = new - old
    return deltas


def _finite_number(value: object) -> bool:
    """判断值是否为非布尔、有限的整数或浮点数。"""

    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _snapshot(value: object) -> object:
    """把 B 内部值转换为大小受控、JSON 兼容的独立快照。

    页和记录字节只保存长度及前 16 字节十六进制预览，避免最近查询缓存因
    复制整页膨胀；Path 转为字符串；dataclass 和枚举保留结构；未知对象
    只记录类型与 repr，不向界面泄漏 BufferPool 或 Engine 实例。
    """

    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return {
            "value_type": type(value).__name__,
            "byte_length": len(raw),
            "hex_preview": raw[:16].hex(),
            "preview_truncated": len(raw) > 16,
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "value_type": type(value).__name__,
            "fields": {
                item.name: _snapshot(getattr(value, item.name))
                for item in fields(value)
            },
        }
    if isinstance(value, Mapping):
        return {str(key): _snapshot(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_snapshot(item) for item in value]
    return {
        "value_type": type(value).__name__,
        "representation": repr(value),
    }


__all__ = ["StorageTraceCollector", "StorageTraceRouter"]
