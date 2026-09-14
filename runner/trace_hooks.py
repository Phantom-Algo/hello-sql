"""C 模块内部使用的可选运行追踪钩子。

Runner、LogicalPlanBuilder 和 Executor 属于查询编排核心，不应为了
可视化而构造 UI 模型。本模块只定义一个普通字典回调和通用
装饰器：无回调时直接执行原函数；有回调时记录真实参数、
返回值、异常与耗时。

行执行器的 ``rows`` 是惰性生成器，所以必须把追踪生命周期延长到
实际迭代结束。生成器只保留前五条样例，但始终计算完整产出行数，
既能展示数据流又不会让查询历史随结果集无限增长。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from functools import wraps
from inspect import isgeneratorfunction
from time import perf_counter
from typing import TypeVar, cast


RunnerTracePayload = dict[str, object]
"""C 提交给观察者的瞬时调用记录。"""

RunnerTraceSink = Callable[[RunnerTracePayload], None]
"""接收 C 调用记录的同步回调类型。"""

_T = TypeVar("_T")
_SAMPLE_LIMIT = 5
_COMPONENTS = frozenset({"binding", "logical_plan", "executor", "runtime"})


def _find_sink(
    arguments: tuple[object, ...],
    keyword_arguments: Mapping[str, object],
) -> RunnerTraceSink | None:
    """从 Builder 或 ExecutionContext 参数中定位追踪回调。

    Builder 把回调保存为 ``_trace_sink``；Executor 本身是不可变计划
    投影，通过第二个参数 ExecutionContext 的 ``trace_sink`` 取得回调。
    同时检查关键字参数，保证两种 Python 调用方式行为一致。
    """

    for candidate in (*arguments, *keyword_arguments.values()):
        sink = getattr(candidate, "_trace_sink", None)
        if callable(sink):
            return cast(RunnerTraceSink, sink)
        sink = getattr(candidate, "trace_sink", None)
        if callable(sink):
            return cast(RunnerTraceSink, sink)
    return None


def _is_implementation_argument(value: object) -> bool:
    """识别 self 和 ExecutionContext，防止它们进入公开数据快照。

    核心对象包含 Storage、Server 或回调等不可序列化状态。UI 只需
    业务输入，所以装饰器用回调属性作为最小、不导入具体类的识别标志。
    """

    return hasattr(value, "_trace_sink") or hasattr(value, "trace_sink")


def _business_arguments(
    arguments: tuple[object, ...],
    keyword_arguments: Mapping[str, object],
) -> tuple[tuple[object, ...], dict[str, object]]:
    """移除绑定方法的 self 和执行上下文，仅保留业务参数。"""

    positional = arguments[1:] if arguments else ()
    positional = tuple(
        value for value in positional if not _is_implementation_argument(value)
    )
    keywords = {
        key: value
        for key, value in keyword_arguments.items()
        if not _is_implementation_argument(value)
    }
    return positional, keywords


def _base_payload(
    component: str,
    operation: str,
    arguments: tuple[object, ...],
    keyword_arguments: Mapping[str, object],
    started_at: float,
) -> RunnerTracePayload:
    """构造所有 C 事件共用的组件、操作、参数和单调时间字段。"""

    positional, keywords = _business_arguments(arguments, keyword_arguments)
    return {
        "component": component,
        "operation": operation,
        "arguments": positional,
        "keyword_arguments": keywords,
        "started_at": started_at,
    }


def _emit(sink: RunnerTraceSink, payload: RunnerTracePayload) -> None:
    """提交一条追踪记录，并隔离观察者自身的所有异常。

    追踪是诊断能力，不属于 SQL 语义。即使查看器存在编程错误，
    原本的绑定、计划构建与运行结果也必须保持不变。
    """

    try:
        sink(payload)
    except Exception:
        return


def _result_metrics(result: object) -> dict[str, object]:
    """Extract exact row counts from a QueryResult before UI snapshot limits.

    Duck typing keeps this low-level hook independent from contracts.result.
    Only the public QueryResult shape is recognized; other return values do
    not receive fabricated metrics.
    """

    if type(result).__name__ != "QueryResult":
        return {}
    columns = getattr(result, "columns", None)
    rows = getattr(result, "rows", None)
    return {
        "column_count": len(columns) if columns is not None else 0,
        "returned_rows": len(rows) if rows is not None else 0,
        "affected_rows": getattr(result, "affected_rows", None),
    }


def trace_runner_operation(
    component: str,
    operation: str | None = None,
) -> Callable[[Callable[..., _T]], Callable[..., _T]]:
    """创建不改变函数签名、返回值和异常语义的 C 追踪装饰器。

    Args:
        component: binding、logical_plan、executor 或 runtime。
        operation: 界面显示的稳定操作名；省略时使用函数名。

    Returns:
        保留原函数元数据的装饰器。普通函数记录返回值，生成器
        记录完整产出数和最多五条样例。

    Raises:
        ValueError: component 不在四个 C 阶段中，或 operation 为空。
    """

    if component not in _COMPONENTS:
        raise ValueError(f"unknown runner trace component: {component!r}")
    if operation is not None and (
        not isinstance(operation, str) or not operation.strip()
    ):
        raise ValueError("runner trace operation must be non-empty")

    def decorate(function: Callable[..., _T]) -> Callable[..., _T]:
        """根据原函数是否为生成器，选择同步或惰性包装器。"""

        action = operation or function.__name__
        if isgeneratorfunction(function):

            @wraps(function)
            def generator_wrapper(*args: object, **kwargs: object) -> Iterator[object]:
                """迭代原生成器，统计行数、样例、失败和提前关闭。"""

                sink = _find_sink(args, kwargs)
                if sink is None:
                    yield from cast(Iterator[object], function(*args, **kwargs))
                    return
                started = perf_counter()
                payload = _base_payload(
                    component, action, args, kwargs, started
                )
                yielded = 0
                samples: list[object] = []
                try:
                    for item in cast(Iterator[object], function(*args, **kwargs)):
                        yielded += 1
                        if len(samples) < _SAMPLE_LIMIT:
                            samples.append(item)
                        yield item
                except GeneratorExit:
                    payload.update(
                        {
                            "status": "stopped",
                            "result": {
                                "yielded": yielded,
                                "sampled_items": tuple(samples),
                            },
                            "metrics": {
                                "yielded_rows": yielded,
                                "sampled_rows": len(samples),
                            },
                            "elapsed_ms": (perf_counter() - started) * 1000,
                        }
                    )
                    _emit(sink, payload)
                    raise
                except Exception as error:
                    payload.update(
                        {
                            "status": "failed",
                            "result": {
                                "yielded": yielded,
                                "sampled_items": tuple(samples),
                            },
                            "metrics": {
                                "yielded_rows": yielded,
                                "sampled_rows": len(samples),
                            },
                            "error_code": getattr(
                                error, "code", type(error).__name__
                            ),
                            "error_message": getattr(error, "message", str(error)),
                            "elapsed_ms": (perf_counter() - started) * 1000,
                        }
                    )
                    _emit(sink, payload)
                    raise
                else:
                    payload.update(
                        {
                            "status": "success",
                            "result": {
                                "yielded": yielded,
                                "sampled_items": tuple(samples),
                            },
                            "metrics": {
                                "yielded_rows": yielded,
                                "sampled_rows": len(samples),
                            },
                            "elapsed_ms": (perf_counter() - started) * 1000,
                        }
                    )
                    _emit(sink, payload)

            return cast(Callable[..., _T], generator_wrapper)

        @wraps(function)
        def call_wrapper(*args: object, **kwargs: object) -> object:
            """执行普通函数，记录成功返回值或原样向上抛出的异常。"""

            sink = _find_sink(args, kwargs)
            if sink is None:
                return function(*args, **kwargs)
            started = perf_counter()
            payload = _base_payload(component, action, args, kwargs, started)
            try:
                result = function(*args, **kwargs)
            except Exception as error:
                payload.update(
                    {
                        "status": "failed",
                        "result": None,
                        "metrics": {},
                        "error_code": getattr(
                            error, "code", type(error).__name__
                        ),
                        "error_message": getattr(error, "message", str(error)),
                        "elapsed_ms": (perf_counter() - started) * 1000,
                    }
                )
                _emit(sink, payload)
                raise
            payload.update(
                {
                    "status": "success",
                    "result": result,
                    "metrics": _result_metrics(result),
                    "elapsed_ms": (perf_counter() - started) * 1000,
                }
            )
            _emit(sink, payload)
            return result

        return cast(Callable[..., _T], call_wrapper)

    return decorate


__all__ = ["RunnerTracePayload", "RunnerTraceSink", "trace_runner_operation"]
