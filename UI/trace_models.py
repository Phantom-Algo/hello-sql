"""A、B、C 三模块共同使用的查询追踪数据契约。

设计目标
========

``QueryTrace`` 表示一次 SQL 语句从终端输入到结果渲染的完整只读记录；
``StageTrace`` 表示其中一个可独立查看的处理阶段；``TraceEvent`` 表示阶段
内部按时间顺序发生的一步操作。三个层次共同支持界面展示“阶段输入、内部
过程、阶段输出、源码位置和运行指标”。

模块边界
========

* A 只提交 Lexer、Parser、AST 和 SourceSpan 相关追踪；
* B 只提交 Catalog、BufferPool、Pager、记录编解码和 CRUD 相关追踪；
* C 提交 REPL、语义绑定、LogicalPlan、Optimizer、Executor 和结果渲染追踪，
  并在后续阶段负责汇总；
* 本模块不导入 compiler、storage 或 runner，从而避免循环依赖；
* 追踪对象只描述已经发生的行为，任何查看器都不得利用它重新执行 SQL。

不可变与序列化
==============

外部模块可以用普通 ``dict``、``list`` 和 ``tuple`` 构造快照。模型创建时会
递归复制并冻结这些值，防止后续业务代码修改追踪历史；``to_dict`` 和
``to_json`` 再把只读快照转换为浏览器可以直接消费的 JSON 结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import math
from types import MappingProxyType
from typing import Mapping, TypeAlias

from contracts.ast import SourceSpan


TracePayload: TypeAlias = Mapping[str, object]
"""阶段快照与指标的输入类型；值必须能递归转换为 JSON。"""


class TraceOwner(str, Enum):
    """追踪阶段的责任模块。

    ``A``、``B``、``C`` 与项目既有三人分工完全一致；``SHARED`` 只用于真正
    跨模块的交接或公共装配阶段，不能用来掩盖本应明确的模块负责人。
    """

    A = "A"
    B = "B"
    C = "C"
    SHARED = "SHARED"


class TraceStatus(str, Enum):
    """查询、阶段或事件所在的生命周期状态。

    ``SKIPPED`` 表示上游失败导致该阶段没有运行；``DISABLED`` 表示该功能
    明确关闭，例如当前尚未启用逻辑优化器。两者都不同于执行失败。
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    DISABLED = "DISABLED"


def _require_text(value: str, field_name: str) -> None:
    """验证必填文本字段包含至少一个非空白字符。

    追踪 ID、阶段名称和动作名称会成为界面的稳定标签。若允许空字符串进入
    契约，后续界面只能显示无法定位的空节点，因此在模型边界统一拒绝。
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_non_negative_number(value: float, field_name: str) -> None:
    """验证耗时等数值有限且不小于零。

    ``NaN`` 和无穷大虽然属于 Python 浮点数，却不是可靠的 JSON 指标；负耗时
    也没有业务意义，所以在生成追踪对象时立即报告数据生产方的错误。
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    if value < 0 or not math.isfinite(float(value)):
        raise ValueError(f"{field_name} must be finite and non-negative")


def _validate_span(span: SourceSpan | None, field_name: str) -> None:
    """检查可选源码范围使用合法的一基行列坐标。

    ``SourceSpan`` 来源于 A 的完整脚本坐标。这里不尝试根据 SQL 文本重新计算
    位置，只保证起止坐标为正且结束位置不早于开始位置。
    """

    if span is None:
        return
    values = (span.start_line, span.start_col, span.end_line, span.end_col)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in values):
        raise ValueError(f"{field_name} coordinates must be positive integers")
    if (span.end_line, span.end_col) < (span.start_line, span.start_col):
        raise ValueError(f"{field_name} end must not precede its start")


def _freeze_value(value: object, path: str) -> object:
    """递归复制并冻结一个追踪值。

    标量保持原值，枚举转换为公开字符串值，映射转换为只读
    ``MappingProxyType``，列表和元组统一转换为元组。其他 Python 对象可能
    泄露模块内部实现或无法序列化，因此明确拒绝，并在异常中返回字段路径。
    """

    # TraceOwner/TraceStatus 继承 str，因此 Enum 判断必须早于字符串判断，
    # 才能保证内部快照保存公开 value，而不是保存枚举实例本身。
    if isinstance(value, Enum):
        return _freeze_value(value.value, path)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite float")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string mapping key")
            frozen[key] = _freeze_value(nested, f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_value(nested, f"{path}[{index}]")
            for index, nested in enumerate(value)
        )
    raise TypeError(
        f"{path} contains unsupported value type {type(value).__name__}; "
        "convert it to a JSON-compatible snapshot first"
    )


def _freeze_payload(payload: TracePayload, field_name: str) -> Mapping[str, object]:
    """把模块提交的顶层字典复制为递归只读快照。

    顶层必须是字符串键映射，便于界面用字段名稳定渲染 Input、Output 和
    Metrics；返回值与调用方原字典不共享可变容器。
    """

    if not isinstance(payload, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    frozen = _freeze_value(payload, field_name)
    assert isinstance(frozen, Mapping)
    return frozen


def _thaw_value(value: object) -> object:
    """把内部只读值转换为标准 JSON 容器。

    该转换只创建新的 ``dict`` 和 ``list``，不会暴露模型内部使用的只读映射，
    因此调用方可以安全修改 ``to_dict`` 的返回结果而不影响原追踪对象。
    """

    if isinstance(value, Mapping):
        return {key: _thaw_value(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_thaw_value(nested) for nested in value]
    return value


def _span_to_dict(span: SourceSpan | None) -> dict[str, int] | None:
    """把可选 ``SourceSpan`` 转换为前端需要的普通字典。

    字段沿用 A 模块的一基闭区间命名，使浏览器不需要猜测位置语义，也避免
    可视化层直接依赖 dataclass 的内部表示。
    """

    if span is None:
        return None
    return {
        "start_line": span.start_line,
        "start_col": span.start_col,
        "end_line": span.end_line,
        "end_col": span.end_col,
    }


def _validate_ordered_sequences(items: tuple[object, ...], field_name: str) -> None:
    """验证事件或阶段的 ``sequence`` 严格递增且没有重复。

    可视化界面依赖这一顺序提供“上一步/下一步”。严格递增让生产方在契约
    边界暴露排序错误，而不是让前端悄悄重新排序并掩盖真实执行顺序。
    """

    sequences = tuple(getattr(item, "sequence") for item in items)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in sequences):
        raise ValueError(f"{field_name} sequences must be non-negative integers")
    if any(left >= right for left, right in zip(sequences, sequences[1:])):
        raise ValueError(f"{field_name} sequences must be strictly increasing")


def _validate_error_state(
    status: TraceStatus,
    error_code: str | None,
    error_message: str | None,
    field_name: str,
) -> None:
    """保证失败状态与错误详情彼此一致。

    ``FAILED`` 至少需要错误码或消息，便于查看器解释失败原因；其他状态不能
    携带错误详情，防止界面同时显示“成功”和错误信息。
    """

    if error_code is not None and not error_code.strip():
        raise ValueError(f"{field_name}.error_code must not be blank")
    if error_message is not None and not error_message.strip():
        raise ValueError(f"{field_name}.error_message must not be blank")
    has_error = error_code is not None or error_message is not None
    if status is TraceStatus.FAILED and not has_error:
        raise ValueError(f"{field_name} with FAILED status requires error details")
    if status is not TraceStatus.FAILED and has_error:
        raise ValueError(f"{field_name} may contain error details only when FAILED")


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """一个处理阶段内部已经发生的最小可视化步骤。

    ``event_id`` 在所属阶段内保持唯一；``sequence`` 决定单步播放顺序；输入、
    输出和指标都是与业务对象分离的只读 JSON 快照。``source_span`` 可把事件
    与 SQL 原文联动，``elapsed_ms`` 表示该事件自身耗时而非整个阶段耗时。
    """

    event_id: str
    sequence: int
    action: str
    description: str = ""
    input_snapshot: TracePayload = field(default_factory=dict)
    output_snapshot: TracePayload = field(default_factory=dict)
    metrics: TracePayload = field(default_factory=dict)
    source_span: SourceSpan | None = None
    elapsed_ms: float = 0.0

    def __post_init__(self) -> None:
        """验证事件标识、顺序、耗时和源码位置，并冻结全部快照。

        dataclass 虽然声明为 frozen，但调用方仍可能传入可变字典；这里使用
        ``object.__setattr__`` 完成构造期规范化，构造完成后对象及嵌套容器均
        不能再被外部修改。
        """

        _require_text(self.event_id, "TraceEvent.event_id")
        _require_text(self.action, "TraceEvent.action")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("TraceEvent.sequence must be a non-negative integer")
        _require_non_negative_number(self.elapsed_ms, "TraceEvent.elapsed_ms")
        _validate_span(self.source_span, "TraceEvent.source_span")
        object.__setattr__(
            self,
            "input_snapshot",
            _freeze_payload(self.input_snapshot, "TraceEvent.input_snapshot"),
        )
        object.__setattr__(
            self,
            "output_snapshot",
            _freeze_payload(self.output_snapshot, "TraceEvent.output_snapshot"),
        )
        object.__setattr__(
            self,
            "metrics",
            _freeze_payload(self.metrics, "TraceEvent.metrics"),
        )

    def to_dict(self) -> dict[str, object]:
        """返回可直接交给 JSON 编码器的事件副本。

        返回的容器与内部快照完全分离，适用于本地查看器响应、测试快照以及
        后续导出功能；枚举和 ``SourceSpan`` 均已转换为普通 JSON 值。
        """

        return {
            "event_id": self.event_id,
            "sequence": self.sequence,
            "action": self.action,
            "description": self.description,
            "input_snapshot": _thaw_value(self.input_snapshot),
            "output_snapshot": _thaw_value(self.output_snapshot),
            "metrics": _thaw_value(self.metrics),
            "source_span": _span_to_dict(self.source_span),
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass(frozen=True, slots=True)
class StageTrace:
    """一次查询中可独立查看的完整处理阶段。

    阶段明确记录负责人、输入/输出契约、最终状态和内部事件。``stage_id`` 是
    稳定机器标识，例如 ``a.lexer`` 或 ``b.cache``；``name`` 和
    ``description`` 用于面向用户展示。事件必须按真实发生顺序提交。
    """

    stage_id: str
    sequence: int
    owner: TraceOwner
    name: str
    description: str
    status: TraceStatus
    input_contract: str = ""
    output_contract: str = ""
    input_snapshot: TracePayload = field(default_factory=dict)
    events: tuple[TraceEvent, ...] = ()
    output_snapshot: TracePayload = field(default_factory=dict)
    metrics: TracePayload = field(default_factory=dict)
    source_span: SourceSpan | None = None
    elapsed_ms: float = 0.0
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        """验证阶段不变式并把事件序列和数据快照规范化为只读值。

        除基础字段外，本方法检查事件 ID 唯一、事件顺序严格递增以及失败状态
        与错误详情一致，确保前端无需再次修复或猜测后端数据。
        """

        _require_text(self.stage_id, "StageTrace.stage_id")
        _require_text(self.name, "StageTrace.name")
        _require_text(self.description, "StageTrace.description")
        if not isinstance(self.owner, TraceOwner):
            raise TypeError("StageTrace.owner must be a TraceOwner")
        if not isinstance(self.status, TraceStatus):
            raise TypeError("StageTrace.status must be a TraceStatus")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("StageTrace.sequence must be a non-negative integer")
        _require_non_negative_number(self.elapsed_ms, "StageTrace.elapsed_ms")
        _validate_span(self.source_span, "StageTrace.source_span")
        _validate_error_state(
            self.status,
            self.error_code,
            self.error_message,
            "StageTrace",
        )
        events = tuple(self.events)
        if any(not isinstance(event, TraceEvent) for event in events):
            raise TypeError("StageTrace.events must contain only TraceEvent values")
        _validate_ordered_sequences(events, "StageTrace.events")
        event_ids = tuple(event.event_id for event in events)
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("StageTrace.events must use unique event_id values")
        object.__setattr__(self, "events", events)
        object.__setattr__(
            self,
            "input_snapshot",
            _freeze_payload(self.input_snapshot, "StageTrace.input_snapshot"),
        )
        object.__setattr__(
            self,
            "output_snapshot",
            _freeze_payload(self.output_snapshot, "StageTrace.output_snapshot"),
        )
        object.__setattr__(
            self,
            "metrics",
            _freeze_payload(self.metrics, "StageTrace.metrics"),
        )

    def to_dict(self) -> dict[str, object]:
        """返回包含全部事件和阶段元数据的 JSON 兼容副本。

        字段名称直接对应后续界面的阶段详情面板，使前端可以统一渲染 A、B、C
        三方数据，而不需要判断具体 Python 类。
        """

        return {
            "stage_id": self.stage_id,
            "sequence": self.sequence,
            "owner": self.owner.value,
            "name": self.name,
            "description": self.description,
            "status": self.status.value,
            "input_contract": self.input_contract,
            "output_contract": self.output_contract,
            "input_snapshot": _thaw_value(self.input_snapshot),
            "events": [event.to_dict() for event in self.events],
            "output_snapshot": _thaw_value(self.output_snapshot),
            "metrics": _thaw_value(self.metrics),
            "source_span": _span_to_dict(self.source_span),
            "elapsed_ms": self.elapsed_ms,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


@dataclass(frozen=True, slots=True)
class QueryTrace:
    """一条 SQL 在 A、B、C 三模块中的完整最终追踪快照。

    ``query_number`` 是 C 在查询开始时分配的进程内递增编号，``trace_id`` 是
    由该编号派生的稳定字符串；``statement_index`` 和 ``statement_count`` 支持
    多语句脚本；``stages`` 严格按照真实执行顺序保存。查询失败时，已经完成的
    阶段仍被保留，未运行阶段可以明确标记为 ``SKIPPED``，因此查看器能够展示
    完整错误链路。
    """

    trace_id: str
    query_number: int
    sql: str
    database: str
    status: TraceStatus
    stages: tuple[StageTrace, ...]
    statement_index: int = 1
    statement_count: int = 1
    source_span: SourceSpan | None = None
    elapsed_ms: float = 0.0
    result_summary: TracePayload = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        """验证查询级标识、脚本下标、阶段顺序和最终状态。

        本方法同时确保阶段 ID 唯一，避免查看器点击左侧阶段时定位到多个对象；
        查询数据构造完成后，阶段元组和结果摘要均保持只读。
        """

        _require_text(self.trace_id, "QueryTrace.trace_id")
        if (
            isinstance(self.query_number, bool)
            or not isinstance(self.query_number, int)
            or self.query_number < 1
        ):
            raise ValueError("QueryTrace.query_number must be a positive integer")
        _require_text(self.sql, "QueryTrace.sql")
        _require_text(self.database, "QueryTrace.database")
        if not isinstance(self.status, TraceStatus):
            raise TypeError("QueryTrace.status must be a TraceStatus")
        if (
            isinstance(self.statement_count, bool)
            or not isinstance(self.statement_count, int)
            or self.statement_count < 1
        ):
            raise ValueError("QueryTrace.statement_count must be a positive integer")
        if (
            isinstance(self.statement_index, bool)
            or not isinstance(self.statement_index, int)
            or not 1 <= self.statement_index <= self.statement_count
        ):
            raise ValueError(
                "QueryTrace.statement_index must be between 1 and statement_count"
            )
        _require_non_negative_number(self.elapsed_ms, "QueryTrace.elapsed_ms")
        _validate_span(self.source_span, "QueryTrace.source_span")
        _validate_error_state(
            self.status,
            self.error_code,
            self.error_message,
            "QueryTrace",
        )
        stages = tuple(self.stages)
        if any(not isinstance(stage, StageTrace) for stage in stages):
            raise TypeError("QueryTrace.stages must contain only StageTrace values")
        _validate_ordered_sequences(stages, "QueryTrace.stages")
        stage_ids = tuple(stage.stage_id for stage in stages)
        if len(stage_ids) != len(set(stage_ids)):
            raise ValueError("QueryTrace.stages must use unique stage_id values")
        if self.status is TraceStatus.SUCCESS and any(
            stage.status is TraceStatus.FAILED for stage in stages
        ):
            raise ValueError("a successful QueryTrace cannot contain a failed stage")
        object.__setattr__(self, "stages", stages)
        object.__setattr__(
            self,
            "result_summary",
            _freeze_payload(self.result_summary, "QueryTrace.result_summary"),
        )

    def to_dict(self) -> dict[str, object]:
        """返回整个查询追踪的 JSON 兼容深拷贝。

        该结果是未来本地查看器的唯一数据输入，包含查询元数据、所有阶段、事件、
        结果摘要和错误信息，但不包含任何可调用的业务对象或执行函数。
        """

        return {
            "trace_id": self.trace_id,
            "query_number": self.query_number,
            "sql": self.sql,
            "database": self.database,
            "status": self.status.value,
            "statement_index": self.statement_index,
            "statement_count": self.statement_count,
            "source_span": _span_to_dict(self.source_span),
            "elapsed_ms": self.elapsed_ms,
            "stages": [stage.to_dict() for stage in self.stages],
            "result_summary": _thaw_value(self.result_summary),
            "error_code": self.error_code,
            "error_message": self.error_message,
        }

    def to_json(self, *, indent: int | None = None) -> str:
        """把查询追踪编码为保留中文的 JSON 文本。

        ``indent`` 为 ``None`` 时生成适合本地 HTTP 响应的紧凑格式；传入正整数
        时生成便于答辩、调试和快照测试阅读的缩进格式。编码过程不会改变对象。
        """

        if indent is not None and (
            isinstance(indent, bool) or not isinstance(indent, int) or indent < 0
        ):
            raise ValueError("indent must be a non-negative integer or None")
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
