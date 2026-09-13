"""``QueryInspector`` 统一编排、失败保留和 A/B/C 筛选测试。

这组测试使用真实 Compiler、Runner 和临时存储，验证追踪来自同一次
SQL 执行。同时覆盖语法失败、运行失败、多语句编号和只读筛选，
防止 ``/inspect`` 在查看时意外重新解析或执行。
"""

from __future__ import annotations

import pytest

from compiler import parse, parse_script
from contracts.errors import E_SYNTAX, E_TABLE_NOT_FOUND, ParseError
from runner import Runner
from storage import DatabaseServer
from UI import InspectionModule, QueryInspector, TraceStatus, format_inspection_text


def _traced_runner(tmp_path) -> tuple[Runner, QueryInspector]:
    """构建注入同一 B/C 路由器的真实 Runner 和 QueryInspector。

    Args:
        tmp_path: pytest 为当前测试创建的独立数据目录。

    Returns:
        可执行 SQL 的 Runner 与可读取追踪的 Inspector。
    """

    inspector = QueryInspector(capacity=20)
    server = DatabaseServer(tmp_path, trace_sink=inspector.storage_router)
    runner = Runner(
        server,
        parse,
        parse_script=parse_script,
        trace_sink=inspector.execution_router,
        inspector=inspector,
    )
    return runner, inspector


def test_single_execution_publishes_ordered_full_pipeline_and_filters(tmp_path):
    """一条真实 SELECT 应发布 1–14 阶段并可按负责人筛选。"""

    runner, inspector = _traced_runner(tmp_path)
    runner.execute("CREATE TABLE users (id INT, enabled BOOLEAN);")
    runner.execute("INSERT INTO users VALUES (1, TRUE);")
    result = runner.execute("SELECT id FROM users WHERE enabled = TRUE;")

    assert result.rows == ((1,),)
    snapshot = inspector.latest()
    assert snapshot is not None
    assert [stage.sequence for stage in snapshot.stages] == list(range(1, 15))
    assert [stage.owner.value for stage in inspector.latest("a").stages] == ["A"] * 4
    assert [stage.owner.value for stage in inspector.latest("b").stages] == ["B"] * 4
    assert [stage.owner.value for stage in inspector.latest("c").stages] == ["C"] * 6
    assert inspector.latest("all").trace is snapshot.trace
    assert snapshot.trace.result_summary["row_count"] == 1


def test_script_assigns_one_trace_number_per_executed_statement(tmp_path):
    """多语句脚本应逐条编号并保留全局 SourceSpan 与脚本下标。"""

    runner, inspector = _traced_runner(tmp_path)
    result = runner.execute_script(
        "CREATE TABLE notes (id INT);\n"
        "INSERT INTO notes VALUES (7);\n"
        "SELECT * FROM notes;"
    )

    assert len(result.statements) == 3
    traces = tuple(reversed(inspector.hub.recent()))
    assert [trace.query_number for trace in traces] == [1, 2, 3]
    assert [trace.statement_index for trace in traces] == [1, 2, 3]
    assert [trace.statement_count for trace in traces] == [3, 3, 3]
    assert traces[2].source_span.start_line == 3


def test_compile_failure_is_inspectable_before_original_error_is_raised(tmp_path):
    """语法错误应保留 A 失败阶段和精确位置，同时继续抛 ParseError。"""

    runner, inspector = _traced_runner(tmp_path)
    with pytest.raises(ParseError) as caught:
        runner.execute_script("SELECT FROM users;")

    assert caught.value.code == E_SYNTAX
    snapshot = inspector.latest(InspectionModule.A)
    assert snapshot is not None
    assert snapshot.trace.status is TraceStatus.FAILED
    assert snapshot.trace.error_code == E_SYNTAX
    assert any(stage.status is TraceStatus.FAILED for stage in snapshot.stages)
    assert all(stage.owner.value == "A" for stage in snapshot.stages)


def test_runtime_failure_and_continue_mode_keep_individual_traces(tmp_path):
    """执行错误应写入 FAILED，continue 模式仍为后续语句发布追踪。"""

    runner, inspector = _traced_runner(tmp_path)
    result = runner.execute_script(
        "SELECT * FROM missing; CREATE TABLE recovered (id INT);",
        stop_on_error=False,
    )

    assert result.statements[0].error.code == E_TABLE_NOT_FOUND
    assert result.statements[1].result is not None
    traces = tuple(reversed(inspector.hub.recent()))
    assert traces[0].status is TraceStatus.FAILED
    assert traces[0].error_code == E_TABLE_NOT_FOUND
    assert traces[1].status is TraceStatus.SUCCESS


def test_filter_is_read_only_and_plain_text_contains_selected_stages(tmp_path):
    """反复切换筛选不应新增查询，纯文本输出只含选中模块。"""

    runner, inspector = _traced_runner(tmp_path)
    runner.execute("CREATE TABLE flags (enabled BOOLEAN);")
    count_before = len(inspector.hub)

    snapshot = inspector.latest("B")
    assert snapshot is not None
    text = format_inspection_text(snapshot)
    assert "module=B" in text
    assert "B Catalog" in text
    assert "A Lexer" not in text
    assert "C Runtime" not in text
    assert len(inspector.hub) == count_before


@pytest.mark.parametrize("value", ["D", "AB", "", 1])
def test_invalid_module_filter_is_rejected(value):
    """未知模块不应被静默解释为 ALL，避免用户误以为筛选成功。"""

    with pytest.raises(ValueError, match="A, B, C, or ALL"):
        InspectionModule.parse(value)
