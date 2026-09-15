"""验证 C 的优化器追踪：真实调用、禁用占位与零语义影响。

优化器不感知追踪，所有记录都由 Runner 的装饰器或显式禁用分支产出。
本文件只断言上报口径，规则本身的正确性由等价性测试负责。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from compiler import parse, parse_script
from contracts.errors import E_COLUMN_NOT_FOUND, SqlError
from runner import Runner
from runner.logical_plan.optimizer import OptimizationLog
from runner.trace_hooks import emit_disabled_operation
from storage import DatabaseServer


def _runner(tmp_path: Path, events: list[dict[str, object]]) -> Runner:
    """构造带字典回调的 Runner，直接观察 C 提交的原始记录。"""

    return Runner(
        DatabaseServer(tmp_path),
        parse,
        parse_script=parse_script,
        trace_sink=events.append,
    )


def _optimizer_records(
    events: list[dict[str, object]],
) -> list[dict[str, object]]:
    """筛出 optimizer 组件的记录，其余组件不参与本文件的断言。"""

    return [event for event in events if event.get("component") == "optimizer"]


def _prepare_users(runner: Runner) -> None:
    """准备两行数据，后续语句才有可被规则改写的计划。"""

    runner.execute("CREATE TABLE users (id INT, name TEXT, active BOOLEAN);")
    runner.execute("INSERT INTO users VALUES (1, 'a', TRUE);")
    runner.execute("INSERT INTO users VALUES (2, 'b', FALSE);")


def test_select_reports_one_record_with_the_optimization_log(tmp_path) -> None:
    """SELECT 应上报一条成功记录，携带 OptimizationLog 与规则命中摘要。"""

    events: list[dict[str, object]] = []
    runner = _runner(tmp_path, events)
    _prepare_users(runner)
    events.clear()

    result = runner.execute("SELECT name FROM users WHERE id = 2;")

    records = _optimizer_records(events)
    assert len(records) == 1
    record = records[0]
    assert record["operation"] == "optimize"
    assert record["status"] == "success"
    assert isinstance(record["result"], OptimizationLog)
    metrics = record["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["rounds"] >= 1
    assert metrics["hit_limit"] is False
    assert metrics["application_count"] == len(metrics["applications"])
    assert sum(metrics["rule_hits"].values()) == metrics["application_count"]
    assert metrics["application_count"] >= 1
    for application in metrics["applications"]:
        # 前后形态来自规则自己的单行表示，追踪层不重新渲染计划
        assert application["plan_before"].startswith(
            ("Projection", "Filter", "Join", "Scan", "Empty")
        )
        assert application["plan_after"].strip()
    assert record["elapsed_ms"] >= 0
    assert runner.last_optimization_log is record["result"]
    assert result.rows == (("b",),)


def test_disabled_switch_reports_a_disabled_record(tmp_path) -> None:
    """optimize=False 应上报禁用记录，而不是让阶段看起来像没跑过。"""

    events: list[dict[str, object]] = []
    runner = _runner(tmp_path, events)
    _prepare_users(runner)
    sql = "SELECT name FROM users WHERE id = 2;"
    optimized = runner.execute(sql, optimize=True)
    events.clear()

    plain = runner.execute(sql, optimize=False)

    records = _optimizer_records(events)
    assert len(records) == 1
    assert records[0]["status"] == "disabled"
    assert records[0]["metrics"] == {"reason": "optimize=False"}
    assert records[0]["result"] is None
    assert records[0]["elapsed_ms"] == 0.0
    assert runner.last_optimization_log is None
    assert plain == optimized


def test_ddl_and_dml_report_zero_applications(tmp_path) -> None:
    """非 SELECT 计划不经规则改写，但优化器确实运行过。"""

    events: list[dict[str, object]] = []
    runner = _runner(tmp_path, events)
    runner.execute("CREATE TABLE items (id INT);")
    events.clear()

    runner.execute("INSERT INTO items VALUES (1);")
    runner.execute("UPDATE items SET id = 2 WHERE id = 1;")
    runner.execute("CREATE INDEX idx_items_id ON items (id);")

    records = _optimizer_records(events)
    assert len(records) == 3
    for record in records:
        assert record["status"] == "success"
        assert record["metrics"]["rounds"] == 0
        assert record["metrics"]["application_count"] == 0


def test_binding_failure_reports_no_optimizer_record(tmp_path) -> None:
    """绑定失败的语句没有计划可优化，因此不产生任何优化记录。"""

    events: list[dict[str, object]] = []
    runner = _runner(tmp_path, events)
    runner.execute("CREATE TABLE users (id INT);")
    events.clear()

    with pytest.raises(SqlError) as caught:
        runner.execute("SELECT missing FROM users;")

    assert caught.value.code == E_COLUMN_NOT_FOUND
    assert _optimizer_records(events) == []


def test_each_statement_of_a_script_reports_its_own_record(tmp_path) -> None:
    """多语句脚本按语句逐条上报，开关对所有语句一致生效。"""

    events: list[dict[str, object]] = []
    runner = _runner(tmp_path, events)
    runner.execute_script(
        "CREATE TABLE notes (id INT);\n"
        "INSERT INTO notes VALUES (7);\n"
        "SELECT * FROM notes WHERE id = 7;"
    )

    records = _optimizer_records(events)
    assert [record["status"] for record in records] == ["success"] * 3
    assert records[-1]["metrics"]["rounds"] >= 1


def test_broken_optimizer_sink_cannot_change_the_result(tmp_path) -> None:
    """观察者异常仍被隔离：优化器记录失败不影响查询结果。"""

    def broken_sink(_payload: dict[str, object]) -> None:
        raise RuntimeError("trace viewer failed")

    runner = Runner(DatabaseServer(tmp_path), parse, trace_sink=broken_sink)
    assert runner.execute("CREATE TABLE safe (id INT);").affected_rows == 0
    assert runner.execute("INSERT INTO safe VALUES (7);").affected_rows == 1
    assert runner.execute(
        "SELECT id FROM safe WHERE id = 7;", optimize=False
    ).rows == ((7,),)


def test_disabled_emitter_rejects_unknown_components() -> None:
    """禁用记录与装饰器共用组件白名单，避免出现无法归类的阶段。"""

    events: list[dict[str, object]] = []
    with pytest.raises(ValueError):
        emit_disabled_operation(events.append, "planner", "optimize", reason="x")
    with pytest.raises(ValueError):
        emit_disabled_operation(events.append, "optimizer", "optimize", reason=" ")
    assert events == []
