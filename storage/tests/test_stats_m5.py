"""表级统计（M5）：计数、采样、失效、只读性与闸门。

契约要求（contracts.storage.TableStats）：任何存在的表都返回统计；空表零值；
page_count 只计数据页；columns 完整且按建表列序；规划期只读；
返回值不得早于最近一次已完成的写操作（D37/D38/D41）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_BAD_ARG, E_TABLE_NOT_FOUND, SqlError
from storage import DatabaseServer
from storage.constants import STATS_SAMPLE_PAGES
from storage.engine import TableEngine
from storage.pager import page_count


@pytest.fixture
def server(tmp_path) -> DatabaseServer:
    return DatabaseServer(str(tmp_path / "data"))


@pytest.fixture
def storage(server: DatabaseServer):
    handle = server.connect("main")
    handle.create_table(
        "t",
        (
            ColumnDef("id", SqlType.INT),
            ColumnDef("name", SqlType.TEXT),
            ColumnDef("score", SqlType.REAL),
            ColumnDef("ok", SqlType.BOOLEAN),
        ),
    )
    return handle


def _expect_code(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


def _fill(handle, rows) -> None:
    for row in rows:
        handle.insert("t", row)


# ---------- 计数 ----------


def test_statistics_empty_table_zero_values(storage) -> None:
    stats = storage.statistics("t")

    assert (stats.table, stats.row_count, stats.page_count) == ("t", 0, 0)
    assert [column.name for column in stats.columns] == ["id", "name", "score", "ok"]
    for column in stats.columns:
        assert column.distinct_count == 0
        assert column.min_value is None
        assert column.max_value is None


def test_statistics_row_and_page_counts_after_inserts(storage) -> None:
    _fill(storage, [(index, "x", 1.5, True) for index in range(10)])

    stats = storage.statistics("t")

    assert stats.row_count == 10
    assert stats.page_count == 1


def test_statistics_row_count_after_delete(storage) -> None:
    ids = [storage.insert("t", (index, "x", 1.0, False)) for index in range(5)]

    storage.delete_row("t", ids[0])

    assert storage.statistics("t").row_count == 4


def test_statistics_page_count_excludes_overflow_chain_pages(server, storage) -> None:
    """溢出行会占多个链页，但链页不是数据页（DV3-09）。"""
    storage.create_table("big", (ColumnDef("text", SqlType.TEXT),))
    storage.insert("big", ("x" * 9000,))

    stats = storage.statistics("big")
    table_path = Path(server._data_dir) / "main" / "big.table"

    assert stats.row_count == 1
    assert stats.page_count == 1
    assert page_count(server._pool, table_path) > 3


def test_statistics_page_count_excludes_freed_pages(server, storage) -> None:
    """整页删空后该页归还空闲链表，数据页数要减、文件总页数不变。"""
    storage.create_table("big", (ColumnDef("text", SqlType.TEXT),))
    for _ in range(120):
        storage.insert("big", ("y" * 100,))
    table_path = Path(server._data_dir) / "main" / "big.table"
    pages_before = page_count(server._pool, table_path)
    data_before = storage.statistics("big").page_count
    assert data_before > 1

    for row_id, _values in list(storage.scan("big")):
        storage.delete_row("big", row_id)

    stats = storage.statistics("big")
    assert stats.row_count == 0
    assert stats.page_count == 0  # 全部数据页已归还
    assert page_count(server._pool, table_path) == pages_before  # 文件只增不减


def test_statistics_counters_are_rebuilt_after_restart(tmp_path) -> None:
    data_dir = str(tmp_path / "data")
    first = DatabaseServer(data_dir).connect("main")
    first.create_table("t", (ColumnDef("id", SqlType.INT),))
    for index in range(7):
        first.insert("t", (index,))

    reopened = DatabaseServer(data_dir).connect("main")
    stats = reopened.statistics("t")

    assert (stats.row_count, stats.page_count) == (7, 1)


def test_statistics_does_not_rescan_all_pages(storage, monkeypatch) -> None:
    """统计必须 O(1) 可读：基线之后不得再触发全表页遍历。"""
    _fill(storage, [(index, "x", 1.0, True) for index in range(5)])
    storage.statistics("t")  # 建立基线

    def _boom(self):
        raise AssertionError("statistics rescanned all pages")

    monkeypatch.setattr(TableEngine, "_active_page_numbers", _boom)

    assert storage.statistics("t").row_count == 5


def test_statistics_after_update_growing_to_overflow(server, storage) -> None:
    storage.create_table("big", (ColumnDef("text", SqlType.TEXT),))
    row_id = storage.insert("big", ("short",))

    storage.update_row("big", row_id, ("z" * 9000,))

    stats = storage.statistics("big")
    assert stats.row_count == 1
    assert stats.page_count == 1


# ---------- 列级统计 ----------


def test_statistics_columns_are_complete_and_in_declaration_order(storage) -> None:
    _fill(storage, [(1, "a", 1.0, True)])

    stats = storage.statistics("t")

    assert [(column.name,) for column in stats.columns] == [
        ("id",),
        ("name",),
        ("score",),
        ("ok",),
    ]


def test_statistics_int_distinct_min_max(storage) -> None:
    _fill(storage, [(value, "x", 1.0, True) for value in (5, 3, 9, 3)])

    column = storage.statistics("t").columns[0]

    assert (column.distinct_count, column.min_value, column.max_value) == (3, 3, 9)


def test_statistics_text_and_real_columns(storage) -> None:
    _fill(
        storage,
        [(1, "b", 2.5, True), (2, "a", 0.5, False), (3, "a", 1.5, True)],
    )

    stats = storage.statistics("t")
    name, score = stats.columns[1], stats.columns[2]

    assert (name.distinct_count, name.min_value, name.max_value) == (2, "a", "b")
    assert (score.distinct_count, score.min_value, score.max_value) == (3, 0.5, 2.5)


def test_statistics_boolean_column(storage) -> None:
    _fill(storage, [(1, "a", 1.0, True), (2, "b", 2.0, False)])

    column = storage.statistics("t").columns[3]

    assert column.distinct_count == 2
    assert column.min_value is False
    assert column.max_value is True


def test_statistics_reflects_data_after_writes(storage) -> None:
    _fill(storage, [(1, "a", 1.0, True)])
    assert storage.statistics("t").columns[0].max_value == 1

    storage.insert("t", (42, "b", 2.0, True))

    assert storage.statistics("t").columns[0].max_value == 42


# ---------- 缓存与失效 ----------


def test_statistics_cache_is_reused_without_writes(storage, monkeypatch) -> None:
    _fill(storage, [(1, "a", 1.0, True)])
    storage.statistics("t")

    def _boom(self, max_pages):
        raise AssertionError("sampled twice without a write")

    monkeypatch.setattr(TableEngine, "sample_rows", _boom)

    assert storage.statistics("t").columns[0].max_value == 1


def test_statistics_invalidated_by_insert(storage) -> None:
    storage.statistics("t")
    storage.insert("t", (7, "a", 1.0, True))

    assert storage.statistics("t").columns[0].max_value == 7


def test_statistics_invalidated_by_update(storage) -> None:
    row_id = storage.insert("t", (1, "a", 1.0, True))
    storage.statistics("t")

    storage.update_row("t", row_id, (99, "a", 1.0, True))

    assert storage.statistics("t").columns[0].max_value == 99


def test_statistics_invalidated_by_delete(storage) -> None:
    row_id = storage.insert("t", (10, "a", 1.0, True))
    storage.insert("t", (20, "b", 2.0, True))
    storage.statistics("t")

    storage.delete_row("t", row_id)

    assert storage.statistics("t").columns[0].max_value == 20


def test_statistics_after_drop_and_recreate_table(storage) -> None:
    storage.insert("t", (5, "a", 1.0, True))
    storage.statistics("t")

    storage.drop_table("t")
    storage.create_table("t", (ColumnDef("id", SqlType.INT),))

    stats = storage.statistics("t")
    assert stats.row_count == 0
    assert stats.columns[0].max_value is None


# ---------- 只读性 ----------


def test_statistics_does_not_increase_dirty_writes(server, storage) -> None:
    _fill(storage, [(index, "a", 1.0, True) for index in range(5)])
    before = dict(server.cache_stats)

    storage.statistics("t")

    assert server.cache_stats["dirty_writes"] == before["dirty_writes"]


def test_statistics_does_not_change_table_file_bytes(server, storage) -> None:
    _fill(storage, [(index, "a", 1.0, True) for index in range(5)])
    table_path = Path(server._data_dir) / "main" / "t.table"
    before = table_path.read_bytes()

    storage.statistics("t")

    assert table_path.read_bytes() == before


# ---------- 闸门与采样边界 ----------


def test_statistics_rejects_bad_identifier(storage) -> None:
    _expect_code(lambda: storage.statistics("Bad Name"), E_BAD_ARG)


def test_statistics_rejects_reserved_prefix(storage) -> None:
    _expect_code(lambda: storage.statistics("__sys_tables"), E_BAD_ARG)


def test_statistics_rejects_missing_table(storage) -> None:
    _expect_code(lambda: storage.statistics("missing"), E_TABLE_NOT_FOUND)


def test_statistics_samples_at_most_configured_pages(storage, monkeypatch) -> None:
    """采样页数必须有上界（D41），不能退化成全表遍历。"""
    storage.create_table("wide", (ColumnDef("text", SqlType.TEXT),))
    for index in range(300):
        storage.insert("wide", (f"{index:04d}" + "p" * 40,))

    seen: list[int] = []
    original = TableEngine.sample_rows

    def _spy(self, max_pages):
        seen.append(max_pages)
        return original(self, max_pages)

    monkeypatch.setattr(TableEngine, "sample_rows", _spy)
    storage.statistics("wide")

    assert seen == [STATS_SAMPLE_PAGES]


def test_statistics_min_max_come_from_real_rows(storage) -> None:
    _fill(storage, [(value, "x", 1.0, True) for value in (4, 8, 15)])

    column = storage.statistics("t").columns[0]

    assert column.min_value in {4, 8, 15}
    assert column.max_value in {4, 8, 15}
    assert column.min_value <= column.max_value
