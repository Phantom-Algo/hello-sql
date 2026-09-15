"""跨模块回归：统计极值精确化之后的 `auto` 选路（D47 + 契约 3.1）。

背景（修复前的真实故障）：B 的列级极值来自"页序前 16 页"的采样，单调插入的
大表会报出远低于真实值的 `max`；C 的选择性估算把 `max` 当硬边界，于是
`WHERE id > 350`（旧采样的 `max` 只到 289 左右，真实 `max` 是 399）被估成
0 行、`index_cost < seq_cost`，选出一条实测比顺序扫描更差的索引路径。

本文件锁的是**装配后的可观察行为**（真 SQL、真存储、自动选路）：

- 真实越界（键 > 真实 max）→ 仍走索引，且返回空集；
- 真实命中但落在旧采样窗口之外 → 必须放弃索引，理由码 `SEQ_CHEAPER`；
- 高选择性点查 → 仍走索引，理由码 `INDEX_EQUALITY`（防过度保守）。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from contracts.ast import ColumnDef, SqlType
from compiler import parse, parse_script
from runner import Runner
from storage import DatabaseServer


PAD = "p" * 180
ROWS = 400
"""22 行左右一页 → 22 页 > 16 页采样窗口，保证旧口径漏掉真实极值。"""


@pytest.fixture
def runner(tmp_path) -> Iterator[Runner]:
    """400 行单调插入 + id 索引，装配一个真实 Runner（非 inspector 路径）。"""

    root = tmp_path / "data"
    setup = DatabaseServer(str(root)).connect("main")
    setup.create_table(
        "events",
        (ColumnDef("id", SqlType.INT), ColumnDef("pad", SqlType.TEXT)),
    )
    for value in range(ROWS):
        setup.insert("events", (value, PAD))
    Runner(
        server=DatabaseServer(str(root)),
        parse=parse,
        parse_script=parse_script,
    ).execute("CREATE INDEX idx_events_id ON events (id);")

    yield Runner(
        server=DatabaseServer(str(root)),
        parse=parse,
        parse_script=parse_script,
    )


def _only_path(runner: Runner):
    paths = runner.last_access_paths
    assert len(paths) == 1
    return paths[0]


def test_auto_abandons_index_when_range_exceeds_stale_sample_window(
    runner: Runner,
) -> None:
    """核心回归：`id > 350` 超出旧采样窗口（max≈289）但真实命中 49 行。

    修复前：采样 max≈289 → 选择性 0 → 选索引（错误）。
    修复后：精确 max=399 → 选择性 0.12 → 索引比顺序扫描贵 → `SEQ_CHEAPER`。
    """

    result = runner.execute("SELECT * FROM events WHERE id > 350;", physical="auto")
    path = _only_path(runner)

    assert path.reason == "SEQ_CHEAPER"
    assert path.selectivity is not None and path.selectivity > 0
    assert path.estimated_rows is not None and path.estimated_rows > 0
    assert path.index_cost is not None and path.seq_cost is not None
    assert path.index_cost >= path.seq_cost
    assert len(result.rows) == ROWS - 351


def test_auto_still_uses_index_for_true_out_of_range_key(runner: Runner) -> None:
    """真实越过 max 的键：估算 0 行是正确推论，索引仍然最快。"""

    result = runner.execute(
        "SELECT * FROM events WHERE id = 99999;", physical="auto"
    )
    path = _only_path(runner)

    assert path.reason == "INDEX_EQUALITY"
    assert path.estimated_rows == 0
    assert result.rows == ()


def test_auto_still_uses_index_for_high_selectivity_point(runner: Runner) -> None:
    """高选择性点查不能被"精确化"误伤：仍然选索引。"""

    result = runner.execute(
        "SELECT * FROM events WHERE id = 200;", physical="auto"
    )
    path = _only_path(runner)

    assert path.reason == "INDEX_EQUALITY"
    assert path.estimated_rows is not None and path.estimated_rows < 2
    assert len(result.rows) == 1


def test_three_physical_modes_stay_equivalent(runner: Runner) -> None:
    """选路变化不得改变结果：三模式行多重集一致。"""

    sql = "SELECT * FROM events WHERE id > 350;"
    auto = runner.execute(sql, physical="auto")
    seq = runner.execute(sql, physical="seq")
    index = runner.execute(sql, physical="index")

    assert sorted(auto.rows) == sorted(seq.rows) == sorted(index.rows)
