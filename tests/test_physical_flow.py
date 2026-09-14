"""V3-T6 / V3-T7：代价选路与强制物理模式的 SQL 级验收（真实 Parser + Storage）。

- V3-T6：高选择性走索引、低选择性放弃索引，无索引/无统计时安全退化；
- V3-T7：seq / index / auto 三模式的结果（行多重集合、表头、影响行数）一致。

等价性比对的是**行多重集合**：索引按键序返回行，无 ORDER BY 时 SQL 不承诺行序，
逐位置比对会得到假失败。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from compiler import parse, parse_script
from contracts.errors import E_BAD_ARG, E_INDEX_NOT_FOUND, SqlError
from contracts.result import QueryResult, ScriptResult
from runner import Runner
from runner.physical.planner import (
    FORCED_INDEX,
    INDEX_EQUALITY,
    NO_MATCHING_INDEX,
    NO_PREDICATE,
    SEQ_CHEAPER,
)
from runner.physical.requests import IndexLookup, IndexRange
from storage import DatabaseServer


# 宽 TEXT 列让每页行数变少，使"命中一行"的索引访问在 300 行规模上就能胜出
TAG_PREFIX = "item-tag-" + "x" * 100
ROW_COUNT = 300


def _tag(group: int) -> str:
    return f"{TAG_PREFIX}{group}"


SCHEMA_SQL = """
CREATE TABLE items (id INT, amount REAL, tag TEXT, active BOOLEAN, note TEXT);
CREATE TABLE other (id INT, note TEXT);
CREATE TABLE empty_items (id INT, note TEXT);
"""

DATA_SQL = "\n".join(
    [
        *(
            "INSERT INTO items VALUES "
            f"({index}, {index // 2 - 75}.5, '{_tag(index % 5)}', "
            f"{'TRUE' if index % 2 == 0 else 'FALSE'}, 'n{index % 3}');"
            for index in range(ROW_COUNT)
        ),
        *(
            f"INSERT INTO other VALUES ({index}, 'other{index}');"
            for index in range(20)
        ),
    ]
)

INDEX_SQL = """
CREATE INDEX idx_items_id ON items (id);
CREATE INDEX idx_items_amount ON items (amount);
CREATE INDEX idx_items_tag ON items (tag);
CREATE INDEX idx_items_active ON items (active);
CREATE INDEX idx_other_id ON other (id);
CREATE INDEX idx_empty_id ON empty_items (id);
"""

# note 列没有索引：用于断言"auto 退化"与"index 模式下由 B 抛 E_INDEX_NOT_FOUND"
SETUP_SQL = f"{SCHEMA_SQL}{DATA_SQL}{INDEX_SQL}"


def _session(tmp_path: Path) -> tuple[DatabaseServer, Runner]:
    """建一个含 300 行 items 与 4 个索引的会话，返回（server, runner）。"""
    server = DatabaseServer(tmp_path)
    runner = Runner(server, parse, parse_script=parse_script)
    runner.execute_script(SETUP_SQL)
    return server, runner


def _multiset(result: QueryResult) -> Counter[tuple[object, ...]]:
    """把结果行折叠成多重集合，用于跨模式比对。"""
    assert result.rows is not None
    return Counter(result.rows)


def _assert_same_result(left: QueryResult, right: QueryResult) -> None:
    """表头与行多重集合逐项一致；无结果集的语句只比对表头。"""
    assert left.columns == right.columns
    if left.rows is None or right.rows is None:
        assert left.rows is None and right.rows is None
        return
    assert _multiset(left) == _multiset(right)


def _only_path(runner: Runner):
    """取最近一条语句唯一的选路记录。"""
    paths = runner.last_access_paths
    assert len(paths) == 1, paths
    return paths[0]


# ---------- V3-T6：两个方向 ----------


def test_high_selectivity_predicate_takes_the_index(tmp_path: Path) -> None:
    _, runner = _session(tmp_path)

    result = runner.execute("SELECT tag FROM items WHERE id = 7")

    path = _only_path(runner)
    assert path.reason == INDEX_EQUALITY
    assert path.requests == (IndexLookup("id", 7),)
    assert path.estimated_rows == pytest.approx(1.0)
    assert path.index_cost is not None and path.seq_cost is not None
    assert path.index_cost < path.seq_cost
    assert result.rows == ((_tag(2),),)


def test_low_selectivity_predicate_gives_up_the_index(tmp_path: Path) -> None:
    _, runner = _session(tmp_path)

    result = runner.execute("SELECT id FROM items WHERE amount > 0")

    path = _only_path(runner)
    assert path.reason == SEQ_CHEAPER
    assert path.requests == ()
    assert path.index_cost is not None and path.seq_cost is not None
    assert path.index_cost >= path.seq_cost
    assert len(result.rows) == 150


def test_selective_range_predicate_takes_the_index(tmp_path: Path) -> None:
    _, runner = _session(tmp_path)

    # 命中 4 行：索引代价 3 + 4 = 7，低于 12 个数据页的顺序扫描
    runner.execute("SELECT id FROM items WHERE id > 295")

    path = _only_path(runner)
    assert path.reason == "INDEX_RANGE"
    assert path.requests == (IndexRange("id", 295, None, lower_inclusive=False),)


def test_auto_degrades_without_a_matching_index(tmp_path: Path) -> None:
    _, runner = _session(tmp_path)

    result = runner.execute("SELECT id FROM items WHERE note = 'n1'")

    path = _only_path(runner)
    assert path.reason == NO_MATCHING_INDEX
    assert path.requests == ()
    assert path.seq_cost is None and path.index_cost is None
    assert len(result.rows) == 100


def test_auto_degrades_on_an_empty_table(tmp_path: Path) -> None:
    _, runner = _session(tmp_path)

    result = runner.execute("SELECT id FROM empty_items WHERE id = 1")

    # 空表：C_seq = 0 < C_index = 1，即使列上有索引也取顺序扫描
    assert _only_path(runner).reason == SEQ_CHEAPER
    assert result.rows == ()


def test_forced_index_still_visits_an_empty_table(tmp_path: Path) -> None:
    _, runner = _session(tmp_path)

    result = runner.execute(
        "SELECT id FROM empty_items WHERE id = 1", physical="index"
    )

    assert _only_path(runner).reason == FORCED_INDEX
    assert result.rows == ()


# ---------- 强制模式 ----------


def test_seq_mode_never_takes_an_index(tmp_path: Path) -> None:
    _, runner = _session(tmp_path)
    indexed = runner.execute("SELECT tag FROM items WHERE id = 7")

    forced = runner.execute("SELECT tag FROM items WHERE id = 7", physical="seq")

    assert _only_path(runner).reason == "FORCED_SEQ"
    assert runner.last_access_paths[0].requests == ()
    _assert_same_result(indexed, forced)


def test_index_mode_overrides_the_cost_decision(tmp_path: Path) -> None:
    _, runner = _session(tmp_path)
    automatic = runner.execute("SELECT id FROM items WHERE amount > 0")

    forced = runner.execute("SELECT id FROM items WHERE amount > 0", physical="index")

    assert _only_path(runner).reason == FORCED_INDEX
    _assert_same_result(automatic, forced)


def test_index_mode_falls_back_across_candidates(tmp_path: Path) -> None:
    _, runner = _session(tmp_path)
    # note 优先（等值排在区间之前）但它没有索引，执行器必须回退到 amount
    sql = "SELECT id FROM items WHERE note = 'n1' AND amount > 0"
    expected = runner.execute(sql, physical="seq")

    forced = runner.execute(sql, physical="index")

    path = _only_path(runner)
    assert path.reason == FORCED_INDEX
    assert path.column == "note"
    assert path.requests == (
        IndexLookup("note", "n1"),
        IndexRange("amount", 0.0, None, lower_inclusive=False),
    )
    _assert_same_result(expected, forced)


def test_index_mode_runs_statements_without_scan_positions(tmp_path: Path) -> None:
    _, runner = _session(tmp_path)

    result = runner.execute_script(
        """
CREATE TABLE scratch (id INT);
INSERT INTO scratch VALUES (1);
INSERT INTO scratch VALUES (2);
DROP TABLE scratch;
""",
        physical="index",
    )

    assert [statement.error for statement in result.statements] == [None] * 4
    assert result.statements[1].result is not None
    assert result.statements[1].result.affected_rows == 1
    # 不含 Scan 的语句没有选路对象
    assert runner.last_access_paths == ()


def test_two_scan_positions_are_planned_independently(tmp_path: Path) -> None:
    _, runner = _session(tmp_path)
    sql = (
        "SELECT i.id FROM items i JOIN other o ON i.id = o.id "
        "WHERE i.id = 7 AND o.id = 7"
    )

    result = runner.execute(sql, physical="index")

    assert [path.reason for path in runner.last_access_paths] == [
        FORCED_INDEX,
        FORCED_INDEX,
    ]
    assert result.rows == ((7,),)


def test_scan_position_without_predicate_keeps_sequence_scan(tmp_path: Path) -> None:
    _, runner = _session(tmp_path)
    sql = "SELECT i.id FROM items i JOIN other o ON i.id = o.id WHERE i.id = 7"

    runner.execute(sql, physical="index")

    # 右侧没有可翻译的谓词：那里没有键值可交给 B，只能顺序扫描
    assert [path.reason for path in runner.last_access_paths] == [
        FORCED_INDEX,
        NO_PREDICATE,
    ]


# ---------- 错误归属 ----------


def test_index_mode_without_predicate_is_rejected_by_c(tmp_path: Path) -> None:
    _, runner = _session(tmp_path)

    with pytest.raises(SqlError) as info:
        runner.execute("SELECT * FROM items", physical="index")

    assert info.value.code == E_BAD_ARG


def test_index_mode_with_untranslatable_predicate_is_rejected_by_c(
    tmp_path: Path,
) -> None:
    _, runner = _session(tmp_path)

    with pytest.raises(SqlError) as info:
        runner.execute("SELECT id FROM items WHERE id <> 7", physical="index")

    assert info.value.code == E_BAD_ARG


def test_index_mode_uses_the_translatable_term_of_a_mixed_predicate(
    tmp_path: Path,
) -> None:
    _, runner = _session(tmp_path)
    sql = "SELECT id FROM items WHERE id <> 7 AND amount > 0"
    expected = runner.execute(sql, physical="seq")

    forced = runner.execute(sql, physical="index")

    assert _only_path(runner).reason == FORCED_INDEX
    _assert_same_result(expected, forced)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM items WHERE active",              # 裸布尔列
        "SELECT id FROM items WHERE NOT (id = 7)",        # NOT 下的条件
        "SELECT id FROM items WHERE id = 7 OR id = 8",    # OR 分支
    ],
)
def test_untranslatable_predicates_are_rejected_only_by_the_forced_mode(
    tmp_path: Path, sql: str
) -> None:
    _, runner = _session(tmp_path)

    sequential = runner.execute(sql, physical="seq")
    automatic = runner.execute(sql, physical="auto")

    # 不翻译不等于错误：条件继续由 Filter 求值，结果不受影响
    assert _only_path(runner).reason == NO_PREDICATE
    _assert_same_result(sequential, automatic)
    with pytest.raises(SqlError) as info:
        runner.execute(sql, physical="index")
    assert info.value.code == E_BAD_ARG


def test_out_of_range_equality_key_takes_the_index_with_zero_rows(
    tmp_path: Path,
) -> None:
    _, runner = _session(tmp_path)

    result = runner.execute("SELECT id FROM items WHERE id = 9999")

    # 选择性为 0：索引代价只剩树高，必然胜出，真实结果同样是零行
    path = _only_path(runner)
    assert path.reason == INDEX_EQUALITY
    assert path.selectivity == 0.0
    assert path.estimated_rows == 0.0
    assert result.rows == ()


def test_index_mode_asks_storage_when_the_column_has_no_index(tmp_path: Path) -> None:
    _, runner = _session(tmp_path)

    with pytest.raises(SqlError) as info:
        runner.execute("SELECT id FROM items WHERE note = 'n1'", physical="index")

    # C 不判断索引是否存在：请求照常交给 B，由 B 报"没有这个索引"
    assert info.value.code == E_INDEX_NOT_FOUND


def test_index_mode_without_a_single_side_filter_is_rejected(tmp_path: Path) -> None:
    _, runner = _session(tmp_path)
    sql = "SELECT i.id FROM items i JOIN other o ON i.id = o.id WHERE i.id = 7"

    with pytest.raises(SqlError) as info:
        runner.execute(sql, optimize=False, physical="index")

    # 优化器关闭时条件下推不发生，WHERE 停在 JOIN 之上：扫描位置没有候选
    assert info.value.code == E_BAD_ARG


def test_unknown_physical_mode_is_rejected_before_parsing(tmp_path: Path) -> None:
    _, runner = _session(tmp_path)

    with pytest.raises(SqlError) as info:
        runner.execute("THIS IS NOT SQL", physical="fast")  # type: ignore[arg-type]

    assert info.value.code == E_BAD_ARG


def test_unknown_physical_mode_is_rejected_by_script_entry(tmp_path: Path) -> None:
    _, runner = _session(tmp_path)

    with pytest.raises(SqlError) as info:
        runner.execute_script("SELECT 1;", physical="fast")  # type: ignore[arg-type]

    assert info.value.code == E_BAD_ARG


# ---------- V3-T7：三模式一致 ----------


EQUIVALENCE_SQL = f"""
{SETUP_SQL}
SELECT tag FROM items WHERE id = 7;
SELECT id FROM items WHERE amount > 0;
SELECT id FROM items WHERE amount >= -75.5 AND amount <= 0;
SELECT id FROM items WHERE id > 290;
SELECT id FROM items WHERE active = TRUE;
SELECT id FROM items WHERE tag >= '{_tag(3)}';
UPDATE items SET amount = 1.5 WHERE id = 7;
SELECT id, amount FROM items WHERE id = 7;
DELETE FROM items WHERE id = 299;
SELECT id FROM items WHERE id >= 299;
SELECT id FROM items WHERE id = 299;
"""


def _run_modes(tmp_path: Path) -> dict[str, ScriptResult]:
    """在三个独立数据库上跑同一段脚本，避免写操作互相影响。"""
    results: dict[str, ScriptResult] = {}
    for mode in ("auto", "seq", "index"):
        directory = tmp_path / mode
        server = DatabaseServer(directory)
        runner = Runner(server, parse, parse_script=parse_script)
        results[mode] = runner.execute_script(EQUIVALENCE_SQL, physical=mode)
    return results


def test_three_modes_produce_the_same_results(tmp_path: Path) -> None:
    results = _run_modes(tmp_path)
    baseline = results["seq"]

    for mode in ("auto", "index"):
        current = results[mode]
        assert len(current.statements) == len(baseline.statements)
        for expected, actual in zip(baseline.statements, current.statements):
            assert actual.error is None, (mode, expected.sql, actual.error)
            assert expected.error is None, expected.sql
            assert expected.result is not None and actual.result is not None
            assert actual.result.affected_rows == expected.result.affected_rows
            _assert_same_result(expected.result, actual.result)


def test_index_mode_is_actually_used_by_the_equivalence_script(
    tmp_path: Path,
) -> None:
    # 取证：等价性不能只靠结果相同，还要证明索引路径确实生效
    _, runner = _session(tmp_path)
    sql = "SELECT id FROM items WHERE id = 7"

    runner.execute(sql, physical="index")

    path = _only_path(runner)
    assert path.reason == FORCED_INDEX
    assert path.requests == (IndexLookup("id", 7),)


def test_last_access_paths_are_reset_by_every_statement(tmp_path: Path) -> None:
    _, runner = _session(tmp_path)
    runner.execute("SELECT tag FROM items WHERE id = 7")
    assert _only_path(runner).reason == INDEX_EQUALITY

    result = runner.execute("INSERT INTO items VALUES (1000, 1.5, 'z', TRUE, 'n0')")

    assert result.affected_rows == 1
    # 不含扫描的语句把上一条的选路记录清空
    assert runner.last_access_paths == ()


def test_last_access_paths_keep_one_entry_per_scan(tmp_path: Path) -> None:
    _, runner = _session(tmp_path)

    runner.execute("SELECT tag FROM items WHERE amount > 0")

    assert len(runner.last_access_paths) == 1
    assert runner.last_access_paths[0].seq_cost is not None


def test_use_database_reads_metadata_from_the_new_connection(tmp_path: Path) -> None:
    _, runner = _session(tmp_path)
    indexed = runner.execute("SELECT tag FROM items WHERE id = 7")
    assert _only_path(runner).reason == INDEX_EQUALITY

    runner.execute_script(
        "CREATE DATABASE other;"
        "USE other;"
        "CREATE TABLE items (id INT, tag TEXT);"
        "INSERT INTO items VALUES (7, 'a');"
    )
    unindexed = runner.execute("SELECT tag FROM items WHERE id = 7")

    # 同名表在另一个库没有索引：索引清单必须来自 USE 之后的连接
    assert _only_path(runner).reason == NO_MATCHING_INDEX
    assert indexed.rows == ((_tag(2),),)
    assert unindexed.rows == (("a",),)


# ---------- 边界值：三模式逐条对照 ----------


BOUNDARY_SQL = [
    f"SELECT id FROM items WHERE id = 0",                    # 端点等于 min
    f"SELECT id FROM items WHERE id = {ROW_COUNT - 1}",      # 端点等于 max
    f"SELECT id FROM items WHERE id < 0",                    # 越界：无行
    f"SELECT id FROM items WHERE id > {ROW_COUNT - 1}",      # 越界：无行
    f"SELECT id FROM items WHERE id = 9999",                 # 等值键越界：无行
    "SELECT id FROM items WHERE amount = -75.5",             # 重复键：命中两行
    "SELECT id FROM items WHERE amount < -75.5",
    "SELECT id FROM items WHERE amount <= -75.5",
    "SELECT id FROM items WHERE amount > 74.5",
    "SELECT id FROM items WHERE amount >= 74.5",
    "SELECT id FROM items WHERE amount > -75.5 AND amount < -74.5",
    "SELECT id FROM items WHERE amount >= -75.5 AND amount <= -74.5",
    "SELECT id FROM items WHERE id >= 100 AND id < 110",     # 双侧闭开区间
    "SELECT id FROM items WHERE id > 100 AND id <= 110",
    "SELECT id FROM items WHERE amount < 0",                 # 负数值
    "SELECT id FROM items WHERE amount >= 0 AND amount <= 0",
    f"SELECT id FROM items WHERE tag = '{_tag(3)}'",         # TEXT 键：重复值
    f"SELECT id FROM items WHERE tag > '{_tag(3)}'",
    f"SELECT id FROM items WHERE tag >= '{_tag(0)}' AND tag < '{_tag(1)}'",
    "SELECT id FROM items WHERE active = TRUE",              # BOOLEAN 键
    "SELECT id FROM items WHERE active = FALSE",
    "SELECT id FROM items WHERE id = 7.0",                   # REAL 字面量的无损归一
]


@pytest.mark.parametrize("sql", BOUNDARY_SQL)
def test_boundary_predicates_agree_across_modes(tmp_path: Path, sql: str) -> None:
    _, runner = _session(tmp_path)

    sequential = runner.execute(sql, physical="seq")
    automatic = runner.execute(sql, physical="auto")
    forced = runner.execute(sql, physical="index")

    _assert_same_result(sequential, automatic)
    _assert_same_result(sequential, forced)


def test_boundary_endpoints_are_reachable_through_the_index(tmp_path: Path) -> None:
    _, runner = _session(tmp_path)

    forced = runner.execute("SELECT id FROM items WHERE amount <= -75.5", physical="index")

    assert _only_path(runner).reason == FORCED_INDEX
    assert sorted(row[0] for row in forced.rows) == [0, 1]


def test_unreducible_cast_literal_stays_a_sequential_predicate(tmp_path: Path) -> None:
    _, runner = _session(tmp_path)
    # id 是 INT 而 7.5 无法无损归一回 INT：这个条件不产生索引候选
    sql = "SELECT id FROM items WHERE id > 7.5"

    sequential = runner.execute(sql, physical="seq")
    automatic = runner.execute(sql, physical="auto")

    _assert_same_result(sequential, automatic)
    with pytest.raises(SqlError) as info:
        runner.execute(sql, physical="index")
    assert info.value.code == E_BAD_ARG
