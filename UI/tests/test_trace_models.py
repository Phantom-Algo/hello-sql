"""验证 A、B、C 共用追踪模型的不变式、冻结行为和 JSON 输出。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import json

import pytest

from contracts.ast import SourceSpan
from UI import QueryTrace, StageTrace, TraceEvent, TraceOwner, TraceStatus


def _event(sequence: int = 0, event_id: str = "token.select") -> TraceEvent:
    """构造一个最小但包含嵌套数据的 Lexer 事件供多项测试复用。"""

    return TraceEvent(
        event_id=event_id,
        sequence=sequence,
        action="识别 SELECT 关键字",
        description="Lexer 从源码开头生成第一个 Token",
        input_snapshot={"lexeme": "SELECT", "chars": ["S", "E"]},
        output_snapshot={"token": {"type": "KW_SELECT", "value": "SELECT"}},
        metrics={"characters": 6},
        source_span=SourceSpan(1, 1, 1, 6),
        elapsed_ms=0.01,
    )


def _stage(
    sequence: int = 0,
    stage_id: str = "a.lexer",
    owner: TraceOwner = TraceOwner.A,
) -> StageTrace:
    """构造一个成功阶段，集中提供 QueryTrace 测试所需的公共样例。"""

    return StageTrace(
        stage_id=stage_id,
        sequence=sequence,
        owner=owner,
        name="Lexer",
        description="逐字符扫描 SQL 并生成带位置的 Token",
        status=TraceStatus.SUCCESS,
        input_contract="str",
        output_contract="list[Token]",
        input_snapshot={"sql_length": 15},
        events=(_event(),),
        output_snapshot={"token_count": 4},
        metrics={"elapsed_ms": 0.01},
        source_span=SourceSpan(1, 1, 1, 15),
        elapsed_ms=0.01,
    )


def test_query_trace_serializes_complete_abc_shape() -> None:
    """完整追踪应保留中文、模块归属、嵌套事件和源码范围。"""

    stages = (
        _stage(),
        StageTrace(
            stage_id="b.catalog",
            sequence=1,
            owner=TraceOwner.B,
            name="Catalog 查询",
            description="读取 students 表结构",
            status=TraceStatus.SUCCESS,
            output_snapshot={"columns": ["id", "name"]},
        ),
        StageTrace(
            stage_id="c.logical_plan",
            sequence=2,
            owner=TraceOwner.C,
            name="Logical Plan",
            description="将已绑定 AST 转换为逻辑计划",
            status=TraceStatus.SUCCESS,
            output_snapshot={"root": "LogicalProjection"},
        ),
    )
    trace = QueryTrace(
        trace_id="trace-0001",
        query_number=1,
        sql="SELECT * FROM students;",
        database="main",
        status=TraceStatus.SUCCESS,
        stages=stages,
        source_span=SourceSpan(1, 1, 1, 23),
        elapsed_ms=1.25,
        result_summary={"columns": ["id", "name"], "row_count": 2},
    )

    decoded = json.loads(trace.to_json(indent=2))
    assert decoded["trace_id"] == "trace-0001"
    assert [stage["owner"] for stage in decoded["stages"]] == ["A", "B", "C"]
    assert decoded["stages"][0]["events"][0]["action"] == "识别 SELECT 关键字"
    assert decoded["source_span"] == {
        "start_line": 1,
        "start_col": 1,
        "end_line": 1,
        "end_col": 23,
    }
    assert decoded["result_summary"]["row_count"] == 2


def test_payloads_are_copied_and_recursively_frozen() -> None:
    """调用方后续修改原字典时，不得改变已经生成的历史追踪。"""

    original = {"node": {"children": ["Scan", "Filter"]}}
    event = TraceEvent(
        event_id="plan.created",
        sequence=0,
        action="创建计划",
        output_snapshot=original,
    )
    original["node"]["children"].append("Projection")

    assert event.to_dict()["output_snapshot"] == {
        "node": {"children": ["Scan", "Filter"]}
    }
    with pytest.raises(TypeError):
        event.output_snapshot["changed"] = True


def test_payload_enum_is_normalized_to_its_public_value() -> None:
    """字符串枚举进入任意快照后应输出普通字符串而不是 Python 枚举对象。"""

    event = TraceEvent(
        event_id="owner.normalized",
        sequence=0,
        action="记录模块归属",
        output_snapshot={"owner": TraceOwner.B, "status": TraceStatus.SUCCESS},
    )

    assert event.to_dict()["output_snapshot"] == {
        "owner": "B",
        "status": "SUCCESS",
    }


def test_frozen_dataclasses_reject_attribute_reassignment() -> None:
    """追踪对象创建完成后不得替换 ID、状态或其他顶层字段。"""

    event = _event()
    with pytest.raises(FrozenInstanceError):
        event.action = "被外部修改"


@pytest.mark.parametrize("elapsed", [-0.1, float("inf"), float("nan")])
def test_event_rejects_invalid_elapsed_time(elapsed: float) -> None:
    """负数、无穷大和 NaN 耗时不能进入浏览器可视化数据。"""

    with pytest.raises(ValueError):
        TraceEvent(
            event_id="bad.time",
            sequence=0,
            action="非法耗时",
            elapsed_ms=elapsed,
        )


def test_event_rejects_invalid_source_span() -> None:
    """源码结束位置早于开始位置时应在契约边界立即报错。"""

    with pytest.raises(ValueError, match="end must not precede"):
        TraceEvent(
            event_id="bad.span",
            sequence=0,
            action="非法范围",
            source_span=SourceSpan(2, 3, 1, 5),
        )


def test_stage_requires_strict_event_order_and_unique_ids() -> None:
    """阶段事件必须真实有序，并且每个事件都可以被界面唯一定位。"""

    with pytest.raises(ValueError, match="strictly increasing"):
        StageTrace(
            stage_id="a.parser",
            sequence=0,
            owner=TraceOwner.A,
            name="Parser",
            description="解析 Token",
            status=TraceStatus.SUCCESS,
            events=(_event(1, "later"), _event(0, "earlier")),
        )
    with pytest.raises(ValueError, match="unique event_id"):
        StageTrace(
            stage_id="a.parser",
            sequence=0,
            owner=TraceOwner.A,
            name="Parser",
            description="解析 Token",
            status=TraceStatus.SUCCESS,
            events=(_event(0, "same"), _event(1, "same")),
        )


def test_failed_state_requires_error_details() -> None:
    """失败状态必须有可展示原因，成功状态则不能同时携带错误。"""

    with pytest.raises(ValueError, match="requires error details"):
        StageTrace(
            stage_id="c.binding",
            sequence=0,
            owner=TraceOwner.C,
            name="语义绑定",
            description="绑定列引用",
            status=TraceStatus.FAILED,
        )
    with pytest.raises(ValueError, match="only when FAILED"):
        QueryTrace(
            trace_id="trace-bad",
            query_number=1,
            sql="SELECT 1;",
            database="main",
            status=TraceStatus.SUCCESS,
            stages=(),
            error_code="E_SYNTAX",
        )


def test_query_requires_ordered_unique_stages_and_valid_script_index() -> None:
    """查询阶段和脚本下标必须能支持稳定的前后导航。"""

    with pytest.raises(ValueError, match="strictly increasing"):
        QueryTrace(
            trace_id="trace-order",
            query_number=1,
            sql="SELECT 1;",
            database="main",
            status=TraceStatus.SUCCESS,
            stages=(
                _stage(1, "a.parser"),
                _stage(0, "a.lexer"),
            ),
        )
    with pytest.raises(ValueError, match="between 1 and statement_count"):
        QueryTrace(
            trace_id="trace-index",
            query_number=1,
            sql="SELECT 1;",
            database="main",
            status=TraceStatus.SUCCESS,
            stages=(),
            statement_index=2,
            statement_count=1,
        )


def test_successful_query_cannot_contain_failed_stage() -> None:
    """查询级成功状态不能掩盖任何已经失败的内部阶段。"""

    failed = StageTrace(
        stage_id="b.storage_io",
        sequence=0,
        owner=TraceOwner.B,
        name="Storage I/O",
        description="读取数据页",
        status=TraceStatus.FAILED,
        error_code="E_IO",
        error_message="读取页失败",
    )
    with pytest.raises(ValueError, match="cannot contain a failed stage"):
        QueryTrace(
            trace_id="trace-status",
            query_number=1,
            sql="SELECT * FROM students;",
            database="main",
            status=TraceStatus.SUCCESS,
            stages=(failed,),
        )


def test_query_number_must_be_positive_and_is_serialized() -> None:
    """查询编号必须从一开始，并作为独立字段提供给历史列表和窗口标题。"""

    with pytest.raises(ValueError, match="positive integer"):
        QueryTrace(
            trace_id="trace-zero",
            query_number=0,
            sql="SELECT 1;",
            database="main",
            status=TraceStatus.SUCCESS,
            stages=(),
        )

    trace = QueryTrace(
        trace_id="trace-000042",
        query_number=42,
        sql="SELECT 1;",
        database="main",
        status=TraceStatus.SUCCESS,
        stages=(),
    )
    assert trace.to_dict()["query_number"] == 42



def test_payload_rejects_live_business_objects() -> None:
    """文件句柄、执行器等非 JSON 对象必须先由所属模块转换成简单快照。"""

    with pytest.raises(TypeError, match="unsupported value type"):
        TraceEvent(
            event_id="bad.object",
            sequence=0,
            action="提交活动对象",
            output_snapshot={"executor": object()},
        )
