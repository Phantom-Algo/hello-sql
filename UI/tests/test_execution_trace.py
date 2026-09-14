"""验证 C 的绑定、逻辑计划、Executor 树和运行时追踪。"""

from __future__ import annotations

import json

import pytest

from compiler import parse, parse_script
from contracts.errors import E_COLUMN_NOT_FOUND, E_TABLE_EXISTS, SqlError
from runner import Runner
from storage import DatabaseServer
from UI import ExecutionTraceRouter, QueryTrace, TraceStatus


def _stage(stages: tuple[object, ...], stage_id: str):
    """按稳定 stage_id 取得唯一 C 阶段，避免测试重复遍历样板。"""

    return next(stage for stage in stages if stage.stage_id == stage_id)


def _runner(tmp_path, router: ExecutionTraceRouter) -> Runner:
    """构造使用真实 Parser、Storage 与 C 事件 Router 的 Runner。"""

    return Runner(
        DatabaseServer(tmp_path),
        parse,
        parse_script=parse_script,
        trace_sink=router,
    )


def _prepare_users(runner: Runner) -> None:
    """在捕获之外准备两行 BOOLEAN 测试数据，避免污染目标追踪。"""

    runner.execute("CREATE TABLE users (id INT, enabled BOOLEAN);")
    runner.execute("INSERT INTO users VALUES (1, TRUE);")
    runner.execute("INSERT INTO users VALUES (2, FALSE);")


def test_select_builds_all_c_stages_and_runtime_statistics(tmp_path) -> None:
    """SELECT 应展示四个真实 C 阶段及一个明确禁用的优化阶段。"""

    router = ExecutionTraceRouter()
    runner = _runner(tmp_path, router)
    _prepare_users(runner)

    with router.capture() as collector:
        result = runner.execute(
            "SELECT id FROM users WHERE enabled = TRUE;"
        )

    assert result.rows == ((1,),)
    stages = collector.build_stages()
    assert [stage.stage_id for stage in stages] == [
        "c.binding",
        "c.logical_plan",
        "c.optimizer",
        "c.executor",
        "c.runtime",
    ]
    assert [stage.sequence for stage in stages] == [10, 11, 12, 13, 14]
    assert [stage.status for stage in stages] == [
        TraceStatus.SUCCESS,
        TraceStatus.SUCCESS,
        TraceStatus.DISABLED,
        TraceStatus.SUCCESS,
        TraceStatus.SUCCESS,
    ]

    plan = _stage(stages, "c.logical_plan")
    assert plan.output_snapshot["last_result"]["value_type"] == (
        "LogicalProjection"
    )
    executor = _stage(stages, "c.executor")
    assert executor.output_snapshot["last_result"]["value_type"] == (
        "SelectExecutor"
    )
    runtime = _stage(stages, "c.runtime")
    assert [event.action for event in runtime.events] == [
        "runtime.seq_scan.rows",
        "runtime.filter.rows",
        "runtime.projection.rows",
        "runtime.select.execute",
    ]
    assert runtime.output_snapshot["query_result"] == {
        "column_count": 1,
        "returned_rows": 1,
        "affected_rows": None,
    }


def test_join_runtime_records_each_scan_and_operator_row_count(tmp_path) -> None:
    """JOIN 应分别展示两次 Scan、嵌套循环、投影和最终行数。"""

    router = ExecutionTraceRouter()
    runner = _runner(tmp_path, router)
    runner.execute("CREATE TABLE users (id INT);")
    runner.execute("CREATE TABLE orders (id INT, user_id INT);")
    runner.execute("INSERT INTO users VALUES (1);")
    runner.execute("INSERT INTO users VALUES (2);")
    runner.execute("INSERT INTO orders VALUES (10, 1);")
    runner.execute("INSERT INTO orders VALUES (11, 1);")

    with router.capture() as collector:
        result = runner.execute(
            "SELECT u.id FROM users u INNER JOIN orders o "
            "ON u.id = o.user_id;"
        )

    assert result.rows == ((1,), (1,))
    runtime = _stage(collector.build_stages(), "c.runtime")
    scans = [
        event
        for event in runtime.events
        if event.action == "runtime.seq_scan.rows"
    ]
    join = next(
        event
        for event in runtime.events
        if event.action == "runtime.nested_loop_join.rows"
    )
    assert sorted(event.metrics["yielded_rows"] for event in scans) == [2, 2]
    assert join.metrics["yielded_rows"] == 2
    assert join.metrics["sampled_rows"] == 2


def test_binding_error_fails_plan_and_skips_executor_runtime(tmp_path) -> None:
    """未知列应保留 Binder 与 Planner 失败，后续阶段不得伪运行。"""

    router = ExecutionTraceRouter()
    runner = _runner(tmp_path, router)
    runner.execute("CREATE TABLE users (id INT);")

    with router.capture() as collector:
        with pytest.raises(SqlError) as caught:
            runner.execute("SELECT missing FROM users;")

    assert caught.value.code == E_COLUMN_NOT_FOUND
    stages = collector.build_stages()
    assert _stage(stages, "c.binding").status is TraceStatus.FAILED
    assert _stage(stages, "c.logical_plan").status is TraceStatus.FAILED
    assert _stage(stages, "c.optimizer").status is TraceStatus.DISABLED
    assert _stage(stages, "c.executor").status is TraceStatus.SKIPPED
    assert _stage(stages, "c.runtime").status is TraceStatus.SKIPPED
    assert _stage(stages, "c.binding").error_code == E_COLUMN_NOT_FOUND


def test_runtime_error_keeps_completed_plan_and_executor(tmp_path) -> None:
    """重复建表在 Runtime 失败时，已构建的计划和 Executor 必须保留。"""

    router = ExecutionTraceRouter()
    runner = _runner(tmp_path, router)
    runner.execute("CREATE TABLE users (id INT);")

    with router.capture() as collector:
        with pytest.raises(SqlError) as caught:
            runner.execute("CREATE TABLE users (id INT);")

    assert caught.value.code == E_TABLE_EXISTS
    stages = collector.build_stages()
    assert _stage(stages, "c.binding").status is TraceStatus.SKIPPED
    assert _stage(stages, "c.logical_plan").status is TraceStatus.SUCCESS
    assert _stage(stages, "c.executor").status is TraceStatus.SUCCESS
    runtime = _stage(stages, "c.runtime")
    assert runtime.status is TraceStatus.FAILED
    assert runtime.error_code == E_TABLE_EXISTS


def test_dml_runtime_reports_affected_rows(tmp_path) -> None:
    """UPDATE 的 Runtime 结果应记录影响行数而不将其误当作结果行。"""

    router = ExecutionTraceRouter()
    runner = _runner(tmp_path, router)
    _prepare_users(runner)

    with router.capture() as collector:
        result = runner.execute(
            "UPDATE users SET enabled = TRUE WHERE id = 2;"
        )

    assert result.affected_rows == 1
    runtime = _stage(collector.build_stages(), "c.runtime")
    assert runtime.output_snapshot["query_result"] == {
        "column_count": 0,
        "returned_rows": 0,
        "affected_rows": 1,
    }


def test_runtime_samples_are_bounded_but_row_count_is_complete(tmp_path) -> None:
    """Runtime keeps five samples while retaining the complete operator count."""

    router = ExecutionTraceRouter()
    runner = _runner(tmp_path, router)
    runner.execute("CREATE TABLE items (id INT);")
    for value in range(8):
        runner.execute(f"INSERT INTO items VALUES ({value});")

    with router.capture() as collector:
        result = runner.execute("SELECT id FROM items;")

    assert len(result.rows or ()) == 8
    runtime = _stage(collector.build_stages(), "c.runtime")
    scan = next(
        event for event in runtime.events if event.action == "runtime.seq_scan.rows"
    )
    snapshot = scan.output_snapshot["result"]
    assert scan.metrics["yielded_rows"] == 8
    assert scan.metrics["sampled_rows"] == 5
    assert len(snapshot["sampled_items"]) == 5


def test_broken_c_trace_sink_cannot_change_query_result(tmp_path) -> None:
    """A broken observer is isolated from every original SQL result."""

    def broken_sink(_payload: dict[str, object]) -> None:
        """Simulate an implementation error inside the future viewer."""

        raise RuntimeError("trace viewer failed")

    runner = Runner(
        DatabaseServer(tmp_path),
        parse,
        parse_script=parse_script,
        trace_sink=broken_sink,
    )
    assert runner.execute("CREATE TABLE safe (id INT);").affected_rows == 0
    assert runner.execute("INSERT INTO safe VALUES (7);").affected_rows == 1
    assert runner.execute("SELECT id FROM safe;").rows == ((7,),)


def test_router_isolates_nested_captures_and_ignores_outside_calls(tmp_path) -> None:
    """C Router 应隔离嵌套查询，且不收集 capture 外的语句。"""

    router = ExecutionTraceRouter()
    runner = _runner(tmp_path, router)
    _prepare_users(runner)
    runner.execute("SELECT id FROM users;")

    with router.capture() as outer:
        runner.execute("SELECT id FROM users;")
        with router.capture() as inner:
            runner.execute("SELECT enabled FROM users;")
        runner.execute("SELECT id FROM users;")
    runner.execute("SELECT id FROM users;")

    assert outer.operation_count == inner.operation_count * 2
    assert inner.operation_count > 0


def test_c_stages_are_query_trace_json_compatible(tmp_path) -> None:
    """C 五阶段应可直接并入 QueryTrace 并完整序列化。"""

    router = ExecutionTraceRouter()
    runner = _runner(tmp_path, router)
    _prepare_users(runner)

    with router.capture() as collector:
        result = runner.execute("SELECT id FROM users;")

    trace = QueryTrace(
        trace_id="trace-000001",
        query_number=1,
        sql="SELECT id FROM users;",
        database="main",
        status=TraceStatus.SUCCESS,
        stages=collector.build_stages(),
        result_summary={"row_count": len(result.rows or ())},
    )
    decoded = json.loads(trace.to_json())
    assert decoded["stages"][2]["status"] == "DISABLED"
    assert decoded["stages"][4]["output_snapshot"]["query_result"][
        "returned_rows"
    ] == 2
