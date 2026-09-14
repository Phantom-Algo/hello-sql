"""B 模块内部使用的可选、与 UI 类型无关的追踪钩子。

Catalog、Cache、Pager 和 Engine 都位于存储核心，不能反向导入 ``UI``。
本模块因此只定义一个普通字典回调和一个装饰器：当 BufferPool 没有安装
回调时，装饰器直接调用原函数；安装回调后，才记录参数、返回值、异常、
真实耗时及缓存统计前后值。具体如何冻结、分组和显示完全由 UI 决定。

生成器需要特别处理：TableEngine.scan 的函数体在迭代时才真正执行，
所以生成器装饰器把事件结束时间放在迭代完成或报错时，而不是创建迭代器时。
追踪回调自身的异常会由 BufferPool 隔离，观察功能绝不能改变存储结果。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from functools import wraps
from inspect import isgeneratorfunction
from time import perf_counter
from typing import Protocol, TypeVar, cast


StorageTracePayload = dict[str, object]
"""B 发给可选观察者的一条瞬时调用记录；观察者必须立即复制所需数据。"""

StorageTraceSink = Callable[[StorageTracePayload], None]
"""接收瞬时存储调用记录的同步回调类型。"""

_T = TypeVar("_T")


class _TraceEmitter(Protocol):
    """装饰器从 BufferPool 或持有 BufferPool 的对象获取的最小能力。"""

    _trace_sink: StorageTraceSink | None

    def _trace_stats_snapshot(self) -> dict[str, int | float]:
        """返回不触发额外追踪的缓存统计副本。"""

    def _emit_trace(self, payload: StorageTracePayload) -> None:
        """同步提交一条记录，并隔离观察者异常。"""


def _find_emitter(
    arguments: tuple[object, ...],
    keyword_arguments: dict[str, object],
) -> _TraceEmitter | None:
    """从位置或关键字参数中定位 BufferPool 形式的追踪发送器。

    Pager 自由函数的第一个参数就是 pool；Catalog 和 TableEngine 方法的
    第一个参数是 self，其 ``_pool`` 指向同一发送器；BufferPool 方法则由
    self 自己发送。Pager 也允许以 ``pool=`` 关键字调用；找不到
    发送器表示当前函数不在可追踪调用环境中。
    """

    candidates = arguments[:1] + (keyword_arguments.get("pool"),)
    for candidate in candidates:
        if hasattr(candidate, "_emit_trace") and hasattr(candidate, "_trace_sink"):
            return cast(_TraceEmitter, candidate)
        pool = getattr(candidate, "_pool", None)
        if hasattr(pool, "_emit_trace") and hasattr(pool, "_trace_sink"):
            return cast(_TraceEmitter, pool)
    return None


def _base_payload(
    component: str,
    operation: str,
    arguments: tuple[object, ...],
    keyword_arguments: dict[str, object],
    started_at: float,
    elapsed_ms: float,
    stats_before: dict[str, int | float],
    stats_after: dict[str, int | float],
) -> StorageTracePayload:
    """构造成功、失败和提前停止记录共用的稳定字段。

    首位置参数是 self 或 pool，而关键字 ``pool`` 也只代表实现
    对象，因此二者都从公开业务参数快照中移除。``started_at`` 使用单调
    时钟，只供 UI 恢复嵌套调用进入顺序，不会作为用户时间戳展示。
    """

    return {
        "component": component,
        "operation": operation,
        "arguments": arguments[1:],
        "keyword_arguments": {
            key: value
            for key, value in keyword_arguments.items()
            if key != "pool"
        },
        "started_at": started_at,
        "elapsed_ms": elapsed_ms,
        "cache_stats_before": stats_before,
        "cache_stats_after": stats_after,
    }


def trace_storage_operation(
    component: str,
    operation: str | None = None,
) -> Callable[[Callable[..., _T]], Callable[..., _T]]:
    """创建一个不改变业务签名和异常语义的存储操作装饰器。

    Args:
        component: ``catalog``、``cache``、``pager`` 或 ``engine``。
        operation: 可选界面操作名；省略时使用原函数名。

    Returns:
        保留原函数元数据的装饰器。无追踪回调时走快速直通路径；存在回调时
        记录真实调用结果。生成器会延迟到实际迭代期间记录。

    Raises:
        ValueError: component 或显式 operation 是空白字符串时抛出。
    """

    if not isinstance(component, str) or not component.strip():
        raise ValueError("trace component must be non-empty")
    if operation is not None and (not isinstance(operation, str) or not operation.strip()):
        raise ValueError("trace operation must be non-empty")

    def decorate(function: Callable[..., _T]) -> Callable[..., _T]:
        """根据普通函数或生成器函数形态选择对应包装方式。"""

        action = operation or function.__name__
        if isgeneratorfunction(function):

            @wraps(function)
            def generator_wrapper(*args: object, **kwargs: object) -> Iterator[object]:
                """在生成器真正迭代完成、失败或提前关闭时提交一次调用记录。"""

                emitter = _find_emitter(args, kwargs)
                if emitter is None or emitter._trace_sink is None:
                    yield from cast(Iterator[object], function(*args, **kwargs))
                    return
                started = perf_counter()
                before = emitter._trace_stats_snapshot()
                yielded = 0
                try:
                    for item in cast(Iterator[object], function(*args, **kwargs)):
                        yielded += 1
                        yield item
                except GeneratorExit:
                    payload = _base_payload(
                        component, action, args, kwargs, started,
                        (perf_counter() - started) * 1000,
                        before, emitter._trace_stats_snapshot(),
                    )
                    payload.update({"status": "stopped", "result": {"yielded": yielded}})
                    emitter._emit_trace(payload)
                    raise
                except Exception as error:
                    payload = _base_payload(
                        component, action, args, kwargs, started,
                        (perf_counter() - started) * 1000,
                        before, emitter._trace_stats_snapshot(),
                    )
                    payload.update(
                        {
                            "status": "failed",
                            "result": {"yielded": yielded},
                            "error_code": getattr(error, "code", type(error).__name__),
                            "error_message": getattr(error, "message", str(error)),
                        }
                    )
                    emitter._emit_trace(payload)
                    raise
                else:
                    payload = _base_payload(
                        component, action, args, kwargs, started,
                        (perf_counter() - started) * 1000,
                        before, emitter._trace_stats_snapshot(),
                    )
                    payload.update({"status": "success", "result": {"yielded": yielded}})
                    emitter._emit_trace(payload)

            return cast(Callable[..., _T], generator_wrapper)

        @wraps(function)
        def call_wrapper(*args: object, **kwargs: object) -> object:
            """围绕普通存储调用记录返回值或异常，并原样返回或重新抛出。"""

            emitter = _find_emitter(args, kwargs)
            if emitter is None or emitter._trace_sink is None:
                return function(*args, **kwargs)
            started = perf_counter()
            before = emitter._trace_stats_snapshot()
            try:
                result = function(*args, **kwargs)
            except Exception as error:
                payload = _base_payload(
                    component, action, args, kwargs, started,
                    (perf_counter() - started) * 1000,
                    before, emitter._trace_stats_snapshot(),
                )
                payload.update(
                    {
                        "status": "failed",
                        "result": None,
                        "error_code": getattr(error, "code", type(error).__name__),
                        "error_message": getattr(error, "message", str(error)),
                    }
                )
                emitter._emit_trace(payload)
                raise
            payload = _base_payload(
                component, action, args, kwargs, started,
                (perf_counter() - started) * 1000,
                before, emitter._trace_stats_snapshot(),
            )
            payload.update({"status": "success", "result": result})
            emitter._emit_trace(payload)
            return result

        return cast(Callable[..., _T], call_wrapper)

    return decorate


__all__ = ["StorageTracePayload", "StorageTraceSink", "trace_storage_operation"]
