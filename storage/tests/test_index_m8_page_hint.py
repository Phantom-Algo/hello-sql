"""D49a：索引叶条目携带行所在页号，回表 O(1)。

三组用例：

- **格式/语义**：叶条目里的页号必须等于行真实所在页；重复键跨叶时每个副本
  都要正确；审计器要能抓到"页号写错"这种损坏；
- **性能**：冷进程（不预热统计、rid 映射为空）下，回表读页数必须与命中行数
  同阶，而不是与数据页数相乘；用索引扫出 rid 之后接着 UPDATE/DELETE 也应是
  O(1)（回表时顺手补齐了 rid→页 映射）；
- **迁移**：v1 索引文件（叶条目不带页号）在首次访问时自动重建；未知版本
  一律报 E_STORAGE，不静默重建。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_STORAGE, SqlError
from storage import DatabaseServer
from storage.constants import (
    INDEX_FILE_VERSION,
    INDEX_LEAF_PREFIX_SIZE,
    INDEX_PAGE0_VERSION_OFFSET,
    INDEX_RID_SIZE,
    LEAF_NODE,
)
from storage.engine import TableEngine
from storage.index import (
    IndexTree,
    entry_payloads,
    leaf_next,
    node_type,
    rebuild_node,
)
from storage.pager import INDEX_FILE_KIND, read_page, write_page
from storage.tests.index_audit_util import audit_index


COLUMNS = (ColumnDef("id", SqlType.INT), ColumnDef("pad", SqlType.TEXT))


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


def _build(root: Path, rows: int = 300, *, pad: int = 20) -> tuple[DatabaseServer, object]:
    server = DatabaseServer(str(root))
    handle = server.connect("main")
    handle.create_table("t", COLUMNS)
    for value in range(rows):
        handle.insert("t", (value, "x" * pad))
    handle.create_index("idx_t_id", "t", "id")
    return server, handle


def _live_engine(server: DatabaseServer, table: str = "t") -> TableEngine:
    for (_path, name), engine in server._engines.items():
        if name == table:
            return engine
    raise AssertionError("engine not found")


def _leaf_pages(server: DatabaseServer, path: Path) -> list[int]:
    """从根沿叶链收集全部叶页号（单层/多层树都适用）。"""

    page0 = read_page(server._pool, path, 0, kind=INDEX_FILE_KIND)
    root = int.from_bytes(page0[8:12], "little")
    page_no = root
    pages: list[int] = []
    while page_no != 0:
        page = read_page(server._pool, path, page_no, kind=INDEX_FILE_KIND)
        if node_type(page) == LEAF_NODE:
            pages.append(page_no)
            page_no = leaf_next(page)
            continue
        page_no = int.from_bytes(page[12:16], "little")  # first_child
    return pages


def _decode_hint(payload: bytes) -> tuple[int, int]:
    """叶条目 = u64 rid + u32 页号 + 键编码（D49a）。"""

    row_id = int.from_bytes(payload[:INDEX_RID_SIZE], "little")
    page_no = int.from_bytes(
        payload[INDEX_RID_SIZE:INDEX_LEAF_PREFIX_SIZE], "little"
    )
    return row_id, page_no


# ---------- 格式与语义 ----------


def test_leaf_entry_carries_row_page(tmp_path) -> None:
    """叶条目里的页号必须等于该行真实所在页（每一页、每一条都查）。"""

    server, handle = _build(tmp_path / "data", rows=300)
    engine = _live_engine(server)
    path = Path(server._data_dir) / "main" / "indexes" / "idx_t_id.idx"

    checked = 0
    for page_no in _leaf_pages(server, path):
        page = read_page(server._pool, path, page_no, kind=INDEX_FILE_KIND)
        for payload in entry_payloads(page):
            row_id, hint = _decode_hint(payload)
            assert hint == engine._locate(row_id)[0]
            checked += 1

    assert checked == 300


def test_lookup_and_range_return_row_pages(tmp_path) -> None:
    """`IndexTree` 返回的 (rid, page) 与真实页一致，并覆盖范围查询。"""

    server, handle = _build(tmp_path / "data", rows=300)
    engine = _live_engine(server)
    rid_of_150 = next(
        row_id for row_id, values in handle.scan("t") if values[0] == 150
    )
    tree = IndexTree(
        server._pool,
        Path(server._data_dir) / "main" / "indexes" / "idx_t_id.idx",
        COLUMNS[0],
    )

    lookup = tree.lookup(150)
    ranged = tree.range(10, 12)

    assert [rid for rid, _page in lookup] == [rid_of_150]
    assert [(rid, page) for rid, page in lookup] == [
        (rid_of_150, engine._locate(rid_of_150)[0])
    ]
    assert len(ranged) == 3
    for rid, page_no in ranged:
        assert page_no == engine._locate(rid)[0]


def test_duplicate_keys_across_leaves_keep_pages(tmp_path) -> None:
    """重复键跨叶时，每个副本的页号都必须正确（最易写错的分裂路径）。"""

    server = DatabaseServer(str(tmp_path / "data"))
    handle = server.connect("main")
    handle.create_table("d", (ColumnDef("k", SqlType.INT), ColumnDef("pad", SqlType.TEXT)))
    for index in range(500):
        handle.insert("d", (7, "p" * 400))  # 单键值 + 长行 → 索引叶必然分裂
    handle.create_index("idx_d_k", "d", "k")
    engine = _live_engine(server, "d")
    path = Path(server._data_dir) / "main" / "indexes" / "idx_d_k.idx"

    rows = list(handle.index_lookup("d", "k", 7))

    assert len(rows) == 500
    audit_index(
        server._pool,
        path,
        ColumnDef("k", SqlType.INT),
        expect_page_of=engine.page_of,
    )
    for row_id, _values in rows:
        assert engine.page_of(row_id) == engine._locate(row_id)[0]


def test_audit_verifies_page_hints(tmp_path) -> None:
    """审计器必须抓到"页号写错"这种损坏（提示错了就退化成全表探测）。"""

    server, _handle = _build(tmp_path / "data", rows=120)
    engine = _live_engine(server)
    path = Path(server._data_dir) / "main" / "indexes" / "idx_t_id.idx"
    column = COLUMNS[0]
    # 健康的索引先过一遍
    audit_index(server._pool, path, column, expect_page_of=engine.page_of)

    leaf = _leaf_pages(server, path)[0]
    page = bytearray(read_page(server._pool, path, leaf, kind=INDEX_FILE_KIND))
    payloads = list(entry_payloads(page))
    broken = bytearray(payloads[0])
    broken[INDEX_RID_SIZE:INDEX_LEAF_PREFIX_SIZE] = (9999).to_bytes(4, "little")
    payloads[0] = bytes(broken)
    rebuild_node(
        page,
        LEAF_NODE,
        payloads,
        next_leaf=leaf_next(page),
    )
    write_page(server._pool, path, leaf, bytes(page), kind=INDEX_FILE_KIND)

    with pytest.raises(SqlError) as exc:
        audit_index(server._pool, path, column, expect_page_of=engine.page_of)
    assert exc.value.code == E_STORAGE


# ---------- 性能：回表 O(1) ----------


def test_cold_index_range_reads_one_page_per_row(tmp_path) -> None:
    """冷进程下 99 行区间的页读必须与行数同阶（改动前与数据页数相乘）。"""

    _build(tmp_path / "data", rows=4000)
    counter = PageReadCounter()
    fresh = DatabaseServer(str(tmp_path / "data"), trace_sink=counter).connect("main")

    counter.reset()
    rows = list(fresh.index_range("t", "id", 3900, None, lower_inclusive=False))

    assert len(rows) == 99
    assert counter.reads <= len(rows) + 20, f"读了 {counter.reads} 页"


def test_cold_index_lookup_does_not_scale_with_pages(tmp_path) -> None:
    """点查的页读不随表变大而增长（回表按提示页直取）。"""

    server = DatabaseServer(str(tmp_path / "data"))
    handle = server.connect("main")
    handle.create_table("small", (ColumnDef("id", SqlType.INT),))
    handle.create_table("big", COLUMNS)
    for value in range(5):
        handle.insert("small", (value,))
    for value in range(4000):
        handle.insert("big", (value, "x" * 20))
    handle.create_index("idx_small_id", "small", "id")
    handle.create_index("idx_big_id", "big", "id")

    def _cold_reads(table: str, key: int) -> int:
        counter = PageReadCounter()
        fresh = DatabaseServer(str(tmp_path / "data"), trace_sink=counter).connect("main")
        counter.reset()
        assert list(fresh.index_lookup(table, "id", key))
        return counter.reads

    small = _cold_reads("small", 2)
    big = _cold_reads("big", 2000)

    assert big <= small + 3, f"小表 {small} 页、大表 {big} 页：回表仍随表增长"


def test_cold_dml_by_indexed_rid_is_constant(tmp_path) -> None:
    """索引扫出的 rid 接着 UPDATE/DELETE 也应是 O(1)：回表时已补齐映射。"""

    _build(tmp_path / "data", rows=3000)
    counter = PageReadCounter()
    fresh = DatabaseServer(str(tmp_path / "data"), trace_sink=counter).connect("main")
    rows = list(fresh.index_range("t", "id", 100, 120))
    assert len(rows) == 21

    counter.reset()
    for index, (row_id, values) in enumerate(rows):
        fresh.update_row("t", row_id, (values[0], values[1]))
        if index == 0:
            # 第一次写入会顺带建立布局基线（D49b 的一次性 O(页数) 成本），
            # 不计入"索引 rid → UPDATE 是 O(1)"的稳态口径。
            counter.reset()

    assert counter.reads <= 3 * (len(rows) - 1), f"读了 {counter.reads} 页"


# ---------- 正确性：搬家、删除、随机写 ----------


def test_row_relocation_refreshes_page_hint(tmp_path) -> None:
    """更新把行挪到别的页之后，冷查询仍要 1 页命中（提示必须被刷新）。"""

    server, handle = _build(tmp_path / "data", rows=600)
    engine = _live_engine(server)
    path = Path(server._data_dir) / "main" / "indexes" / "idx_t_id.idx"
    victim, values = list(handle.scan("t"))[300]

    handle.update_row("t", victim, (values[0], "z" * 3000))  # 撑大到溢出行 → 换页

    audit_index(server._pool, path, COLUMNS[0], expect_page_of=engine.page_of)
    counter = PageReadCounter()
    fresh = DatabaseServer(str(server._data_dir), trace_sink=counter).connect("main")
    counter.reset()
    found = list(fresh.index_lookup("t", "id", values[0]))
    assert len(found) == 1
    assert counter.reads <= 12, f"读了 {counter.reads} 页"


def test_delete_leaves_no_dangling_hint(tmp_path) -> None:
    """删行后条目与页号一起消失，审计仍通过。"""

    server, handle = _build(tmp_path / "data", rows=300)
    engine = _live_engine(server)
    path = Path(server._data_dir) / "main" / "indexes" / "idx_t_id.idx"
    victim = next(row_id for row_id, values in handle.scan("t") if values[0] == 150)

    handle.delete_row("t", victim)

    assert list(handle.index_lookup("t", "id", 150)) == []
    audit_index(server._pool, path, COLUMNS[0], expect_page_of=engine.page_of)


def test_index_matches_scan_after_random_dml(tmp_path) -> None:
    """随机写序列之后：索引结果与 scan+过滤逐行一致，且审计通过。"""

    server, handle = _build(tmp_path / "data", rows=300)
    engine = _live_engine(server)
    path = Path(server._data_dir) / "main" / "indexes" / "idx_t_id.idx"
    next_value = 10_000
    for step in range(120):
        rows = list(handle.scan("t"))
        if step % 3 == 0 and len(rows) > 30:
            handle.delete_row("t", rows[(step * 11) % len(rows)][0])
        elif step % 3 == 1 and rows:
            row_id, values = rows[(step * 5) % len(rows)]
            handle.update_row("t", row_id, (values[0] + 1, values[1]))
        else:
            handle.insert("t", (next_value, "y" * 30))
            next_value += 1

    for probe in (0, 5, 150, 10_001):
        expected = sorted(
            values for _row_id, values in handle.scan("t") if values[0] == probe
        )
        got = sorted(
            values for _row_id, values in handle.index_lookup("t", "id", probe)
        )
        assert got == expected, probe
    audit_index(server._pool, path, COLUMNS[0], expect_page_of=engine.page_of)


# ---------- 迁移：v1 → v2 ----------


def _downgrade_index_to_v1(server: DatabaseServer, path: Path, column: ColumnDef) -> None:
    """把 v2 索引文件就地降级成 v1（叶条目去掉页号 + 页 0 版本写 1）。

    单叶树，够造出"合法但旧格式"的文件：内节点布局两版相同，所以只需改叶页。
    """

    page0 = bytearray(read_page(server._pool, path, 0, kind=INDEX_FILE_KIND))
    root = int.from_bytes(page0[8:12], "little")
    page = bytearray(read_page(server._pool, path, root, kind=INDEX_FILE_KIND))
    legacy: list[bytes] = []
    for payload in entry_payloads(page):
        row_id = payload[:INDEX_RID_SIZE]
        legacy.append(bytes(row_id) + bytes(payload[INDEX_LEAF_PREFIX_SIZE:]))
    rebuild_node(page, LEAF_NODE, legacy, next_leaf=leaf_next(page))
    write_page(server._pool, path, root, bytes(page), kind=INDEX_FILE_KIND)
    page0[
        INDEX_PAGE0_VERSION_OFFSET : INDEX_PAGE0_VERSION_OFFSET + 2
    ] = (1).to_bytes(2, "little")
    write_page(server._pool, path, 0, bytes(page0), kind=INDEX_FILE_KIND)
    server._pool.flush(path)  # 让降级结果真正落盘（否则新进程读到旧版本号）


def test_legacy_index_file_is_rebuilt_on_first_use(tmp_path) -> None:
    """v1 索引在首次访问时自动重建：查询正确，且文件版本升到当前版本。"""

    server, handle = _build(tmp_path / "data", rows=120)
    path = Path(server._data_dir) / "main" / "indexes" / "idx_t_id.idx"
    _downgrade_index_to_v1(server, path, COLUMNS[0])
    del server, handle  # 新进程：缓存为空

    fresh_server = DatabaseServer(str(tmp_path / "data"))
    fresh = fresh_server.connect("main")
    rows = list(fresh.index_lookup("t", "id", 42))

    assert [values[0] for _row_id, values in rows] == [42]
    page0 = read_page(fresh_server._pool, path, 0, kind=INDEX_FILE_KIND)
    version = int.from_bytes(page0[4:6], "little")
    assert version == INDEX_FILE_VERSION
    engine = _live_engine(fresh_server)
    audit_index(
        fresh_server._pool, path, COLUMNS[0], expect_page_of=engine.page_of
    )


def test_unknown_index_version_is_storage_error(tmp_path) -> None:
    """未知版本不许当旧版本静默重建：直接 E_STORAGE。"""

    server, handle = _build(tmp_path / "data", rows=30)
    path = Path(server._data_dir) / "main" / "indexes" / "idx_t_id.idx"
    page0 = bytearray(read_page(server._pool, path, 0, kind=INDEX_FILE_KIND))
    page0[INDEX_PAGE0_VERSION_OFFSET : INDEX_PAGE0_VERSION_OFFSET + 2] = (99).to_bytes(
        2, "little"
    )
    write_page(server._pool, path, 0, bytes(page0), kind=INDEX_FILE_KIND)
    server._pool.flush(path)
    del server, handle

    fresh = DatabaseServer(str(tmp_path / "data")).connect("main")
    with pytest.raises(SqlError) as exc:
        list(fresh.index_lookup("t", "id", 5))
    assert exc.value.code == E_STORAGE


def test_rebuild_keeps_catalog_registration_and_dml(tmp_path) -> None:
    """重建后索引登记不变，DML 联动照常（新写入的行也能查到）。"""

    server, handle = _build(tmp_path / "data", rows=60)
    path = Path(server._data_dir) / "main" / "indexes" / "idx_t_id.idx"
    _downgrade_index_to_v1(server, path, COLUMNS[0])
    del server, handle

    fresh_server = DatabaseServer(str(tmp_path / "data"))
    fresh = fresh_server.connect("main")
    assert [info.name for info in fresh.list_indexes("t")] == ["idx_t_id"]

    fresh.insert("t", (999, "n"))
    assert [values[0] for _row_id, values in fresh.index_lookup("t", "id", 999)] == [999]
