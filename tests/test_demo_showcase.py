"""V3 演示手册的回归护栏（`docs/v3-dev/demo/`）。

演示脚本会随时间腐化：改了代价模型、改了错误归属、改了文法，手册里的"预期"
就变成谎话。本文件把手册里的**教学结论**逐条断言下来：

- 装载脚本能跑通且数据形状符合手册（2000 行 / 25 页 / 两个索引）；
- 八条选路场景的 reason 与手册一致（含"分水岭"两条）；
- 优化器对 JOIN 的单侧下推 + 开/关等价；
- 错误矩阵的 5 个错误码；
- DML 与索引同步；
- 统计里 min/max 精确、distinct 是近似（契约 3.1）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from compiler import parse, parse_script
from contracts.errors import SqlError
from runner import Runner
from storage import DatabaseServer


DEMO_DIR = Path(__file__).resolve().parents[1] / "docs" / "v3-dev" / "demo"
LOAD_SQL = DEMO_DIR / "v3_showcase_load.sql"
ERRORS_SQL = DEMO_DIR / "v3_showcase_errors.sql"


ROUTING_EXPECTATIONS: tuple[tuple[str, str], ...] = (
    ("SELECT id FROM events WHERE id = 42;", "INDEX_EQUALITY"),
    ("SELECT id FROM events WHERE id > 1990;", "INDEX_RANGE"),
    ("SELECT id FROM events WHERE id > 1900;", "SEQ_CHEAPER"),
    ("SELECT id FROM events WHERE grp = 1;", "SEQ_CHEAPER"),
    ("SELECT id FROM events WHERE id = 999999;", "INDEX_EQUALITY"),
    ("SELECT id FROM events WHERE note = 'n00042';", "NO_MATCHING_INDEX"),
    ("SELECT id FROM events;", "NO_PREDICATE"),
    ("SELECT id, amount FROM events WHERE id > 1990 AND amount = 51;", "INDEX_RANGE"),
)

JOIN_SQL = (
    "SELECT e.id, m.label FROM events e INNER JOIN labels m ON e.grp = m.id "
    "WHERE 1 = 1 AND NOT (e.id < 0) AND e.id > 1990 AND m.id = 1;"
)

ERROR_EXPECTATIONS: tuple[tuple[str, str], ...] = (
    ("CREATE INDEX __sys_bad ON events (id);", "E_BAD_ARG"),
    ("CREATE INDEX idx_events_nocol ON events (nocol);", "E_COLUMN_NOT_FOUND"),
    ("CREATE INDEX idx_events_id ON events (id);", "E_INDEX_EXISTS"),
    ("DROP INDEX nope;", "E_INDEX_NOT_FOUND"),
    ("CREATE INDEX idx_events_id2 ON nosuch (id);", "E_TABLE_NOT_FOUND"),
)


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory):
    """装载演示数据（不开追踪，等价于 CLI 的 --no-trace），返回目录与 Runner。"""

    data_dir = tmp_path_factory.mktemp("v3demo")
    server = DatabaseServer(str(data_dir))
    runner = Runner(server=server, parse=parse, parse_script=parse_script)
    runner.execute_file(str(LOAD_SQL))
    return data_dir, runner


def test_load_script_builds_documented_dataset(demo) -> None:
    data_dir, runner = demo
    handle = DatabaseServer(str(data_dir)).connect("main")
    stats = handle.statistics("events")

    assert (stats.row_count, stats.page_count) == (2000, 25)
    assert sorted(info.name for info in handle.list_indexes("events")) == [
        "idx_events_grp",
        "idx_events_id",
    ]
    assert len(runner.execute("SELECT id FROM labels;").rows) == 2


def test_routing_expectations_match_handbook(demo) -> None:
    """八条场景的选路理由必须与手册一致（分水岭两条尤其重要）。"""

    _data_dir, runner = demo
    for sql, expected in ROUTING_EXPECTATIONS:
        result = runner.execute(sql)
        path = runner.last_access_paths[0]
        assert path.reason == expected, (sql, path.reason)
        assert result.rows is not None

    # 分水岭：9 行走索引、99 行放弃；代价估计必须支持这一判断
    narrow = runner.execute("SELECT id FROM events WHERE id > 1990;")
    wide = runner.execute("SELECT id FROM events WHERE id > 1900;")
    assert (len(narrow.rows), len(wide.rows)) == (9, 99)
    assert runner.last_access_paths[0].reason == "SEQ_CHEAPER"


def test_optimizer_pushdown_and_equivalence(demo) -> None:
    """JOIN 单侧下推 + 优化开/关结果一致（手册第 3 幕）。"""

    _data_dir, runner = demo
    optimized = runner.execute(JOIN_SQL, optimize=True)
    log = runner.last_optimization_log

    assert log is not None
    rules = {application.rule for application in log.applications}
    assert {"fold_constants", "normalize_booleans", "push_join_predicates",
            "prune_and_eliminate"} <= rules
    assert sorted(optimized.rows) == [
        (1991, "odd"), (1993, "odd"), (1995, "odd"), (1997, "odd"), (1999, "odd")
    ]
    assert [(p.table, p.reason) for p in runner.last_access_paths] == [
        ("events", "INDEX_RANGE"),
        ("labels", "NO_MATCHING_INDEX"),
    ]

    plain = runner.execute(JOIN_SQL, optimize=False)
    assert sorted(plain.rows) == sorted(optimized.rows)


def test_error_matrix_codes(demo) -> None:
    """错误矩阵 5 个码与手册一致；脚本本身也是可直接跑的。"""

    _data_dir, runner = demo
    for sql, expected in ERROR_EXPECTATIONS:
        with pytest.raises(SqlError) as exc:
            runner.execute(sql)
        assert exc.value.code == expected, sql

    assert ERRORS_SQL.read_text(encoding="utf-8").strip().count(";") == len(
        ERROR_EXPECTATIONS
    )


def test_forced_index_error_ownership(demo) -> None:
    """强制索引的错误归属：无索引列由 B 抛，无谓词由 C 抛（DV3-12）。"""

    _data_dir, runner = demo
    with pytest.raises(SqlError) as no_index:
        runner.execute("SELECT id FROM events WHERE note = 'n00042';", physical="index")
    assert no_index.value.code == "E_INDEX_NOT_FOUND"

    with pytest.raises(SqlError) as no_predicate:
        runner.execute("SELECT id FROM events;", physical="index")
    assert no_predicate.value.code == "E_BAD_ARG"


def test_three_modes_agree(demo) -> None:
    """同一查询三种物理模式结果一致（手册第 5 幕）。"""

    _data_dir, runner = demo
    sql = "SELECT id FROM events WHERE id = 42;"
    results = {mode: runner.execute(sql, physical=mode).rows for mode in ("auto", "seq", "index")}
    assert results["auto"] == results["seq"] == results["index"] == ((42,),)


def test_dml_keeps_index_in_sync(demo) -> None:
    """DML 与索引同步（手册第 2/6 幕）：改键后旧键消失、新键可查、删后查不到。"""

    _data_dir, runner = demo
    runner.execute("INSERT INTO events VALUES (99999, 7, 1, 'tail');")
    assert len(runner.execute("SELECT id FROM events WHERE id = 99999;", physical="index").rows) == 1

    runner.execute("UPDATE events SET id = 88888 WHERE id = 99999;")
    assert runner.execute("SELECT id FROM events WHERE id = 99999;", physical="index").rows == ()
    assert len(runner.execute("SELECT id FROM events WHERE id = 88888;", physical="index").rows) == 1

    runner.execute("DELETE FROM events WHERE id = 88888;")
    assert runner.execute("SELECT id FROM events WHERE id = 88888;", physical="index").rows == ()


def test_statistics_precision_documented_in_handbook(demo) -> None:
    """契约 3.1：min/max 精确、distinct 为近似——手册第 8 幕的核心结论。"""

    data_dir, _runner = demo
    handle = DatabaseServer(str(data_dir)).connect("main")
    column = handle.statistics("events").columns[0]

    assert (column.min_value, column.max_value) == (0, 1999)
    assert 0 < column.distinct_count < 2000  # 有界采样 → 近似值
