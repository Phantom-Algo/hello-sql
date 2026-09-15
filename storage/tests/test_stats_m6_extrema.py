"""列级极值精确化（D47/D48）：min/max 是真实边界，distinct 仍是近似。

契约（`contracts.storage.ColumnStats`）在 3.1 收紧为：

- `min_value` / `max_value` 是**精确边界**——等于表中该列的真实最小值与
  最大值，C 的"越界即 0 行"推论因此成立；
- `distinct_count` 仍是**近似值**（有界采样），C 不得假设其精确。

实现口径（D47/D48）：

- 插入只可能拓宽极值 → 基线建立后 O(1) 维护，永不重扫；
- 删掉 / 改掉当前极值会让极值不确定 → 下次 `statistics()` 精确重算一次；
- 极值不确定时的首次快照做一趟精确全扫（**只读**，不写盘、不 mark_dirty）；
- `distinct` 采样仍是 ≤ `STATS_SAMPLE_PAGES` 页，但从"页序前缀"改成
  "跨页均匀间隔"，去掉单调插入造成的偏差。

本文件只锁 B 侧语义；C 侧的选路后果由 `tests/test_auto_extrema_regression.py`
端到端锁死。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from storage import DatabaseServer
from storage.constants import STATS_SAMPLE_PAGES
from storage.engine import TableEngine


PAD = "p" * 180
"""填充列：让 22 行左右占满一页，用小行数造出 > STATS_SAMPLE_PAGES 页。"""

ROWS = 400
"""行数：22 页 > 16 页，保证"前缀采样"必然漏掉真实极值。"""


@pytest.fixture
def storage(tmp_path):
    handle = DatabaseServer(str(tmp_path / "data")).connect("main")
    handle.create_table(
        "t",
        (ColumnDef("id", SqlType.INT), ColumnDef("pad", SqlType.TEXT)),
    )
    return handle


def _fill(handle, rows: int = ROWS) -> None:
    """按页填充 id = 0..rows-1 的单调递增行（复现前缀采样偏差的最小场景）。"""

    for value in range(rows):
        handle.insert("t", (value, PAD))


def _stats_id(handle):
    return handle.statistics("t").columns[0]


def _row_id_of(handle, value: int) -> int:
    """按列值找 row_id（用公开 scan，不碰内部结构）。"""

    for row_id, values in handle.scan("t"):
        if values[0] == value:
            return row_id
    raise AssertionError(f"row {value} not found")


# ---------- 精确性 ----------


def test_min_max_exact_beyond_sample_window(storage) -> None:
    """核心用例：真实极值落在采样窗口之外时，min/max 仍必须精确。"""

    _fill(storage)
    stats = storage.statistics("t")
    assert stats.page_count > STATS_SAMPLE_PAGES  # 否则本用例恒真

    column = stats.columns[0]
    assert (column.min_value, column.max_value) == (0, ROWS - 1)


def test_min_max_exact_after_restart(tmp_path) -> None:
    """统计不落盘（D38）：重启后重建基线，精确性不受影响。"""

    data_dir = str(tmp_path / "data")
    first = DatabaseServer(data_dir).connect("main")
    first.create_table(
        "t", (ColumnDef("id", SqlType.INT), ColumnDef("pad", SqlType.TEXT))
    )
    _fill(first)

    reopened = DatabaseServer(data_dir).connect("main")
    column = reopened.statistics("t").columns[0]

    assert (column.min_value, column.max_value) == (0, ROWS - 1)


def test_extrema_exact_for_text_real_boolean(tmp_path) -> None:
    """TEXT / REAL / BOOLEAN 三类列的极值同样精确（含 False < True）。"""

    handle = DatabaseServer(str(tmp_path / "data")).connect("main")
    handle.create_table(
        "mixed",
        (
            ColumnDef("name", SqlType.TEXT),
            ColumnDef("score", SqlType.REAL),
            ColumnDef("ok", SqlType.BOOLEAN),
            ColumnDef("pad", SqlType.TEXT),
        ),
    )
    for index, (name, score, ok) in enumerate(
        (("zulu", 2.5, True), ("alpha", -1.25, False), ("mike", 9.0, True))
    ):
        handle.insert("mixed", (name, score, ok, f"{index}{PAD}"))

    name, score, ok, _pad = handle.statistics("mixed").columns

    assert (name.min_value, name.max_value) == ("alpha", "zulu")
    assert (score.min_value, score.max_value) == (-1.25, 9.0)
    assert (ok.min_value, ok.max_value) == (False, True)


def test_empty_table_extrema_are_certain(storage, monkeypatch) -> None:
    """空表极值确定为 None，之后插入只拓宽，不触发重扫。"""

    assert _stats_id(storage).min_value is None

    def _boom(self):
        raise AssertionError("empty-table baseline must not rescan")

    monkeypatch.setattr(TableEngine, "scan", _boom)
    storage.insert("t", (7, PAD))

    column = _stats_id(storage)
    assert (column.min_value, column.max_value) == (7, 7)


# ---------- 增量维护与重算 ----------


def test_insert_after_baseline_widens_without_rescan(storage, monkeypatch) -> None:
    """基线后插入只拓宽极值：O(1)，不得重扫全表。"""

    _fill(storage)
    assert _stats_id(storage).max_value == ROWS - 1

    def _boom(self):
        raise AssertionError("widening insert must not rescan")

    monkeypatch.setattr(TableEngine, "scan", _boom)
    storage.insert("t", (ROWS + 500, PAD))

    column = _stats_id(storage)
    assert (column.min_value, column.max_value) == (0, ROWS + 500)


def test_non_extreme_delete_does_not_rescan(storage, monkeypatch) -> None:
    """删掉中间值不影响极值：同样不得重扫。"""

    _fill(storage)
    storage.statistics("t")
    middle = _row_id_of(storage, ROWS // 2)  # 取 row_id 本身要 scan，先做完

    def _boom(self):
        raise AssertionError("non-extreme delete must not rescan")

    monkeypatch.setattr(TableEngine, "scan", _boom)
    storage.delete_row("t", middle)

    column = _stats_id(storage)
    assert (column.min_value, column.max_value) == (0, ROWS - 1)


def test_delete_one_of_many_equal_extremes_does_not_rescan(
    storage, monkeypatch
) -> None:
    """低基数列：极值被多行共同持有时，删掉其中一行不该触发重算。

    `pad` 列全表同值（min == max），是"每次都撞极值"的最坏形状；极值持有
    计数让这类删除保持 O(1)。
    """

    _fill(storage)
    storage.statistics("t")
    middle = _row_id_of(storage, ROWS // 2)

    def _boom(self):
        raise AssertionError("removing one of many equal extremes must not rescan")

    monkeypatch.setattr(TableEngine, "scan", _boom)
    storage.delete_row("t", middle)

    stats = storage.statistics("t")
    assert stats.columns[1].min_value == PAD  # pad 列仍然确定
    assert stats.row_count == ROWS - 1


def test_delete_last_holder_of_extreme_recomputes(storage) -> None:
    """删空单一持有者：pad 列只剩 PAD 值，删到最后一行为空表 → 零值。"""

    _fill(storage, 1)
    storage.statistics("t")

    storage.delete_row("t", _row_id_of(storage, 0))

    stats = storage.statistics("t")
    assert (stats.row_count, stats.page_count) == (0, 0)
    assert stats.columns[0].min_value is None
    assert stats.columns[1].max_value is None


def test_update_that_empties_extreme_count_recomputes(storage) -> None:
    """极值持有者被改走且计数归零 → 精确回落到剩余行。"""

    _fill(storage, 3)
    _row_id = _row_id_of(storage, 2)
    storage.statistics("t")

    storage.update_row("t", _row_id, (100, PAD + "x"))

    column = _stats_id(storage)
    assert (column.min_value, column.max_value) == (0, 100)


def test_delete_of_current_max_recomputes_exactly(storage) -> None:
    """删掉当前最大值所在行 → 精确回落到次大值。"""

    _fill(storage)
    storage.statistics("t")

    storage.delete_row("t", _row_id_of(storage, ROWS - 1))

    assert _stats_id(storage).max_value == ROWS - 2


def test_update_away_from_extreme_recomputes_exactly(storage) -> None:
    """把最大值改小 → 极值精确回落到次大值。"""

    _fill(storage)
    storage.statistics("t")

    storage.update_row("t", _row_id_of(storage, ROWS - 1), (0, PAD))

    column = _stats_id(storage)
    assert (column.min_value, column.max_value) == (0, ROWS - 2)


def test_baseline_scan_happens_once_per_process(storage, monkeypatch) -> None:
    """基线全扫是进程内一次性代价：多次 statistics() 只扫一趟。"""

    _fill(storage)
    calls: list[int] = []
    original = TableEngine.scan

    def _spy(self):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(TableEngine, "scan", _spy)
    for _ in range(3):
        storage.statistics("t")

    assert len(calls) == 1


def test_mixed_write_sequence_matches_full_rescan(storage) -> None:
    """混合写序列之后，min/max 必须与"暴力全表重算"逐列一致。"""

    _fill(storage, 120)
    for step in range(40):
        if step % 3 == 0:
            storage.insert("t", (1000 + step, PAD))
        elif step % 3 == 1:
            storage.delete_row("t", _row_id_of(storage, step))
        else:
            storage.update_row("t", _row_id_of(storage, 50 + step), (step, PAD))

    stats = storage.statistics("t")
    expected_ids = [values[0] for _row_id, values in storage.scan("t")]

    assert stats.columns[0].min_value == min(expected_ids)
    assert stats.columns[0].max_value == max(expected_ids)


# ---------- 只读性 ----------


def test_baseline_scan_is_read_only(tmp_path) -> None:
    """精确基线只读：不产生脏页写回，也不改动表文件字节。"""

    server = DatabaseServer(str(tmp_path / "data"))
    handle = server.connect("main")
    handle.create_table(
        "t", (ColumnDef("id", SqlType.INT), ColumnDef("pad", SqlType.TEXT))
    )
    _fill(handle)
    table_path = Path(server._data_dir) / "main" / "t.table"
    before_bytes = table_path.read_bytes()
    before_dirty = dict(server.cache_stats)["dirty_writes"]

    handle.statistics("t")

    assert dict(server.cache_stats)["dirty_writes"] == before_dirty
    assert table_path.read_bytes() == before_bytes


# ---------- 采样口径（D48） ----------


def test_sample_page_numbers_are_evenly_spaced() -> None:
    """采样页跨页均匀间隔：首末页都必须入选（去掉前缀偏差）。"""

    pages = list(range(22))
    picked = TableEngine.sample_page_numbers(pages, STATS_SAMPLE_PAGES)

    assert len(picked) == STATS_SAMPLE_PAGES
    assert picked[0] == pages[0]
    assert picked[-1] == pages[-1]
    assert picked == sorted(picked)
    assert len(set(picked)) == len(picked)


@pytest.mark.parametrize("page_total", [0, 1, 2, 16, 17, 100])
def test_sample_page_selection_is_bounded(page_total) -> None:
    """采样页数恒有上界（D41），页数不足时按原样全取。"""

    pages = list(range(page_total))
    picked = TableEngine.sample_page_numbers(pages, STATS_SAMPLE_PAGES)

    assert len(picked) == min(page_total, STATS_SAMPLE_PAGES)
    assert picked == sorted(set(picked))
    assert set(picked) <= set(pages)


def test_distinct_sampling_still_bounded_on_large_table(storage, monkeypatch) -> None:
    """均匀化之后采样仍调用同一上界，不退化成全表遍历。"""

    _fill(storage)
    seen: list[int] = []
    original = TableEngine.sample_rows

    def _spy(self, max_pages):
        seen.append(max_pages)
        return original(self, max_pages)

    monkeypatch.setattr(TableEngine, "sample_rows", _spy)
    storage.statistics("t")

    assert seen == [STATS_SAMPLE_PAGES]
