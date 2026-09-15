"""布局遍历只付一次（D49b）。

`_ensure_layout()` 早就维护了「行数 + 活动数据页集合」的增量基线，但
`scan()` / `_locate()` / `_find_page_for()` 仍在直接调用
`_active_page_numbers()`——它每次都要读遍所有页去判定"哪页是溢出链页"，
于是顺序扫描实际读 2× 数据页，行定位与插入找空间也各多付一遍。

本文件锁两件事：

1. 缓存与每次重算**逐项恒等**（启用缓存的前提）；
2. 三条热路径不再触发重算（红的性能用例：改动前必失败）。
"""

from __future__ import annotations

import pytest

from contracts.ast import ColumnDef, SqlType
from storage import DatabaseServer
from storage.engine import TableEngine
from storage.pager import read_page


COLUMNS = (ColumnDef("id", SqlType.INT), ColumnDef("amount", SqlType.INT))


class PageReadCounter:
    """按 B 的公开观测口统计页读次数（与 bench、追踪器同一条通道）。"""

    def __init__(self) -> None:
        self.reads = 0

    def __call__(self, payload: dict[str, object]) -> None:
        if (
            payload.get("component") == "pager"
            and payload.get("operation") == "read_page"
        ):
            self.reads += 1

    def reset(self) -> None:
        self.reads = 0


@pytest.fixture
def server(tmp_path):
    return DatabaseServer(str(tmp_path / "data"))


def _handle(server: DatabaseServer):
    handle = server.connect("main")
    handle.create_table("t", COLUMNS)
    return handle


def _fill(handle, count: int) -> None:
    for value in range(count):
        handle.insert("t", (value, value % 97))


def _live_engine(server: DatabaseServer, table: str = "t") -> TableEngine:
    """门面自己用的那个 engine 实例（缓存正确性必须在同一实例上看）。"""

    for (_path, name), engine in server._engines.items():
        if name == table:
            return engine
    raise AssertionError("engine not found")


def test_layout_cache_matches_recomputed_after_mixed_writes(tmp_path) -> None:
    """缓存与每次重算必须恒等：这是启用缓存的前提（含删页、溢出、更新）。"""

    big = (ColumnDef("id", SqlType.INT), ColumnDef("pad", SqlType.TEXT))
    server = DatabaseServer(str(tmp_path / "data"))
    handle = server.connect("main")
    handle.create_table("wide", big)
    for index in range(300):
        handle.insert("wide", (index, "x" * (2000 if index % 7 == 0 else 20)))
    engine = _live_engine(server, "wide")

    def _assert_identical(label: str) -> None:
        engine.data_pages()
        assert set(engine._data_pages or ()) == set(engine._active_page_numbers()), label
        assert engine.row_count() == handle.statistics("wide").row_count, label

    _assert_identical("初始")
    row_ids = [row_id for row_id, _values in handle.scan("wide")]
    for row_id in row_ids[::2]:
        handle.delete_row("wide", row_id)
    _assert_identical("删一半")
    for index in range(50):
        handle.insert("wide", (10000 + index, "y" * (3000 if index % 5 == 0 else 30)))
    _assert_identical("补插（含溢出行）")
    for row_id, values in list(handle.scan("wide"))[:20]:
        handle.update_row("wide", row_id, (values[0], "z" * 2500))
    _assert_identical("更新撑成溢出")


def test_second_scan_does_not_repeat_layout_sweep(server) -> None:
    """布局基线建好之后，再扫描不该重复遍历整表判定页类型。"""

    handle = _handle(server)
    _fill(handle, 400)
    pages = handle.statistics("t").page_count
    counter = PageReadCounter()
    server._pool._trace_sink = counter  # noqa: SLF001 - 测试直接挂观测口

    counter.reset()
    list(handle.scan("t"))
    first = counter.reads
    counter.reset()
    list(handle.scan("t"))
    second = counter.reads

    assert first >= second
    assert second <= pages + 2, f"第二次扫描读了 {second} 页，数据页只有 {pages}"


def test_locate_does_not_recompute_layout(server, monkeypatch) -> None:
    """`_locate` 的退化分支必须走缓存页表，而不是每次重算。

    注意前提：`statistics()` 的极值基线走 `scan()`，会顺手填好 rid→页 映射；
    要构造"映射冷、布局热"的状态得直接用 `data_pages()` 建基线。
    """

    handle = _handle(server)
    _fill(handle, 300)
    handle.create_index("idx_t_id", "t", "id")

    fresh = DatabaseServer(str(server._data_dir)).connect("main")
    engine = fresh._engine_for("t", fresh.describe("t").columns)  # noqa: SLF001
    engine.data_pages()  # 建布局基线（不填 rid→页 映射）
    assert not engine._rid_to_page

    def _boom(self):
        raise AssertionError("layout must not be recomputed on the locate path")

    monkeypatch.setattr(TableEngine, "_active_page_numbers", _boom)

    rows = list(fresh.index_lookup("t", "id", 150))

    assert [values[0] for _row_id, values in rows] == [150]


def test_insert_path_does_not_recompute_layout(server, monkeypatch) -> None:
    """插入找空间同样只该读缓存页表。"""

    handle = _handle(server)
    _fill(handle, 50)
    handle.statistics("t")

    def _boom(self):
        raise AssertionError("layout must not be recomputed on the insert path")

    monkeypatch.setattr(TableEngine, "_active_page_numbers", _boom)
    handle.insert("t", (999, 1))

    assert handle.statistics("t").row_count == 51


def test_overflow_rows_do_not_pollute_layout_cache(tmp_path) -> None:
    """溢出链页绝不允许进活动数据页集合（否则扫描会读到链页）。"""

    server = DatabaseServer(str(tmp_path / "data"))
    handle = server.connect("main")
    handle.create_table("big", (ColumnDef("pad", SqlType.TEXT),))
    for index in range(40):
        handle.insert("big", ("q" * (3000 if index % 3 == 0 else 50),))
    engine = _live_engine(server, "big")

    engine.data_pages()
    cached = set(engine._data_pages or ())
    for page_no in cached:
        page = read_page(server._pool, engine._path, page_no)
        assert page[:4] != b"OVFL"
    assert cached == set(engine._active_page_numbers())
