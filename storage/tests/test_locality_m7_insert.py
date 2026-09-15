"""插入页提示（D49c）。

插入要落在"最低的、装得下的活动数据页"。改动前的实现每次都从第 1 页开始
逐页读去找那一页；改动后用 `_insert_hint` 记住"它以下都已满"的下界，纯追加
负载下每行只读一页，而落点语义**逐字节不变**。

本文件锁两件事：页读上界（红的性能用例）与落点等价（绿的语义用例）。
"""

from __future__ import annotations

from contracts.ast import ColumnDef, SqlType
from storage import DatabaseServer
from storage.constants import PAGE_SIZE, SLOT_SIZE
from storage.engine import TableEngine, _parse_page_header, encode_record
from storage.pager import read_page


COLUMNS = (ColumnDef("id", SqlType.INT), ColumnDef("amount", SqlType.INT))


class PageReadCounter:
    """按 B 的公开观测口统计页读次数。"""

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


def _handle(server: DatabaseServer):
    handle = server.connect("main")
    handle.create_table("t", COLUMNS)
    return handle


def _fill(handle, count: int) -> None:
    for value in range(count):
        handle.insert("t", (value, value % 97))


def _live_engine(server: DatabaseServer, table: str = "t") -> TableEngine:
    for (_path, name), engine in server._engines.items():
        if name == table:
            return engine
    raise AssertionError("engine not found")


def _room(page: bytes | bytearray) -> int:
    """该页还能放下多少字节的记录（与 `_find_page_for` 同一公式）。"""

    count, _flags, free_ptr = _parse_page_header(page)
    return PAGE_SIZE - SLOT_SIZE * (count + 1) - free_ptr


def test_insert_lands_on_lowest_page_with_room(tmp_path) -> None:
    """落点语义不变：新行落在最低的、装得下的活动页。"""

    server = DatabaseServer(str(tmp_path / "data"))
    handle = _handle(server)
    _fill(handle, 400)
    engine = _live_engine(server)
    first_page = engine._active_page_numbers()[0]
    victim = None
    for row_id, _values in handle.scan("t"):
        if engine._locate(row_id)[0] == first_page:
            victim = row_id
            break
    assert victim is not None
    handle.delete_row("t", victim)

    new_row = handle.insert("t", (12345, 5))

    assert engine._locate(new_row)[0] == first_page


def test_insert_page_reads_are_bounded(tmp_path) -> None:
    """纯追加装载必须接近 O(1) 页读/行（改动前每行都要扫全表找空间）。"""

    server = DatabaseServer(str(tmp_path / "data"))
    handle = _handle(server)
    counter = PageReadCounter()
    server._pool._trace_sink = counter  # noqa: SLF001

    counter.reset()
    _fill(handle, 400)

    assert counter.reads / 400 <= 3, f"每行读了 {counter.reads / 400:.1f} 页"


def test_freed_page_is_not_reused_as_data_page_without_allocation(tmp_path) -> None:
    """整页删空后该页进空闲链表：不参与最低可用页选择，直到被真正分配。"""

    server = DatabaseServer(str(tmp_path / "data"))
    handle = _handle(server)
    _fill(handle, 400)
    engine = _live_engine(server)
    pages = engine._active_page_numbers()
    victim_page = pages[len(pages) // 2]
    victims = [
        row_id
        for row_id, _values in handle.scan("t")
        if engine._locate(row_id)[0] == victim_page
    ]
    for row_id in victims:
        handle.delete_row("t", row_id)

    engine.data_pages()
    assert victim_page not in set(engine._data_pages or ())

    new_row = handle.insert("t", (777, 3))
    landing = engine._locate(new_row)[0]

    assert landing in set(engine._data_pages or ())


def test_random_write_sequence_matches_first_fit_model(tmp_path) -> None:
    """随机写序列下，新行落点必须等于"重算页表 + first-fit"的模型结果。"""

    server = DatabaseServer(str(tmp_path / "data"))
    handle = _handle(server)
    _fill(handle, 300)
    engine = _live_engine(server)
    next_value = 10_000

    for step in range(80):
        rows = list(handle.scan("t"))
        if step % 2 == 0 and len(rows) > 20:
            handle.delete_row("t", rows[(step * 7) % len(rows)][0])
            continue
        needed = len(encode_record(0, COLUMNS, (next_value, 1)))
        model = None
        for page_no in engine._active_page_numbers():
            page = read_page(server._pool, engine._path, page_no)
            if _room(page) >= needed:
                model = page_no
                break
        row_id = handle.insert("t", (next_value, 1))
        next_value += 1
        if model is not None:
            assert engine._locate(row_id)[0] == model, f"第 {step} 步落点偏离 first-fit"


def test_scan_order_is_stable_across_sessions(tmp_path) -> None:
    """行序不承诺，但本次改动不该改变它：跨进程重开后的扫描序列必须一致。"""

    root = tmp_path / "data"
    first = DatabaseServer(str(root)).connect("main")
    first.create_table("t", COLUMNS)
    _fill(first, 300)
    expected = list(first.scan("t"))

    second = DatabaseServer(str(root)).connect("main")
    second.statistics("t")  # 先建布局基线，再扫描
    assert list(second.scan("t")) == expected
