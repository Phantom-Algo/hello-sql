"""C 模块内部使用的可选运行追踪钩子。

Runner、LogicalPlanBuilder 和 Executor 属于查询编排核心，不应为了
可视化而构造 UI 模型。本模块只定义一个普通字典回调和通用
装饰器：无回调时直接执行原函数；有回调时记录真实参数、
返回值、异常与耗时。

行执行器的 ``rows`` 是惰性生成器，所以必须把追踪生命周期延长到
实际迭代结束。生成器只保留前五条样例，但始终计算完整产出行数，
既能展示数据流又不会让查询历史随结果集无限增长。

开关关闭的功能没有可观察的调用，因此无法由装饰器上报。
``emit_disabled_operation`` 专门为这种情况提交一条占位记录，
让查看器把「被显式关闭」与「上游失败没跑」区分开。
"""

from __future__ import annotations

from collections import Counter
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
_COMPONENTS = frozenset(
    {"binding", "logical_plan", "optimizer", "executor", "runtime"}
)


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
    """提取返回值中不随追踪快照截断而丢失的精确指标。

    用类型名做鸭子判定，使这个底层钩子不依赖 contracts.result 与优化器模块。
    只识别 QueryResult 与 OptimizationLog 两种公开形状，其余返回值不会被
    编造出指标。
    """

    name = type(result).__name__
    if name == "QueryResult":
        columns = getattr(result, "columns", None)
        rows = getattr(result, "rows", None)
        return {
            "column_count": len(columns) if columns is not None else 0,
            "returned_rows": len(rows) if rows is not None else 0,
            "affected_rows": getattr(result, "affected_rows", None),
        }
    if name == "OptimizationLog":
        return _optimization_metrics(result)
    return {}


def _optimization_metrics(log: object) -> dict[str, object]:
    """把 OptimizationLog 摊平成规则命中摘要。

    规则明细由规则自己产出（``plan_before`` / ``plan_after`` 是紧凑单行表示），
    驱动层与追踪层都不反推规则语义；这里只做汇总，使查看器无需解析计划快照。
    """

    applications = tuple(getattr(log, "applications", ()) or ())
    return {
        "rounds": getattr(log, "rounds", 0),
        "hit_limit": bool(getattr(log, "hit_limit", False)),
        "application_count": len(applications),
        "rule_hits": dict(Counter(item.rule for item in applications)),
        "applications": [
            {
                "rule": item.rule,
                "summary": item.summary,
                "plan_before": item.plan_before,
                "plan_after": item.plan_after,
            }
            for item in applications
        ],
    }


def emit_disabled_operation(
    sink: RunnerTraceSink | None,
    component: str,
    operation: str,
    *,
    reason: str,
) -> None:
    """为「功能被显式关闭」的阶段提交一条占位记录。

    开关关闭时原函数根本不会被调用，装饰器因此没有可上报的调用。
    这条记录只陈述阶段没有运行的真实原因，让查看器把该阶段标为
    DISABLED，而不是与「上游失败导致没跑」混为一谈。

    Args:
        sink: 可选 C 字典事件回调；为 None 时本函数不产生任何开销。
        component: binding、logical_plan、optimizer、executor 或 runtime。
        operation: 界面显示的稳定操作名。
        reason: 关闭原因，例如 ``optimize=False``。

    Raises:
        ValueError: component 不在 C 阶段中，或 operation / reason 为空。
    """

    if component not in _COMPONENTS:
        raise ValueError(f"unknown runner trace component: {component!r}")
    for field_name, value in (("operation", operation), ("reason", reason)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"disabled trace {field_name} must be non-empty")
    if sink is None:
        return
    _emit(
        sink,
        {
            "component": component,
            "operation": operation,
            "arguments": (),
            "keyword_arguments": {},
            "started_at": perf_counter(),
            "status": "disabled",
            "result": None,
            "metrics": {"reason": reason},
            "elapsed_ms": 0.0,
        },
    )


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


__all__ = [
    "RunnerTracePayload",
    "RunnerTraceSink",
    "emit_disabled_operation",
    "trace_runner_operation",
]
