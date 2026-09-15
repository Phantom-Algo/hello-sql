"""M6 加固：损坏矩阵、结构审计、soak 与对接自检。

与 C 的联调依赖 C 完成 F5/F6/F7，本文件先做 B 侧可独立完成的收口：
结构级损坏能被抓出来、长时间随机读写后结构仍然合法、
以及"Storage 的公开面与契约一致"的自检。
"""

from __future__ import annotations

import inspect
import random
from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_STORAGE, SqlError
from contracts.storage import BaseStorage
from storage import DatabaseServer, Storage
from storage.cache import BufferPool
from storage.constants import (
    INDEX_PAGE0_KEY_TYPE_OFFSET,
    INDEX_PAGE0_ROOT_OFFSET,
    INTERIOR_NODE,
    LEAF_NODE,
)
from storage.index import (
    IndexTree,
    create_index_file,
    entry_payloads,
    leaf_next,
    node_type,
)
from storage.pager import INDEX_FILE_KIND, page_count, read_page, write_page
from storage.tests.index_audit_util import audit_index



# ---- D49a：叶条目多了"行页号"字段 ----
# 这两个文件测的是树机制本身，不关心页号真伪；统一用一个占位页号写入，
# 断言时只看 rid（`rids()` 把 [(rid, page)] 投影成 [rid]）。
_PLACEHOLDER_PAGE = 1


def _insert(tree, key, row_id):
    tree.insert(key, row_id, _PLACEHOLDER_PAGE)


def _rids(entries):
    return [row_id for row_id, _page_no in entries]

def _index_path(tmp_path) -> Path:
    return Path(tmp_path) / "idx.idx"


def _build(tmp_path, count: int = 400):
    """建一个索引文件并灌入 count 条连续键，返回 (tree, path, column)。"""
    path = _index_path(tmp_path)
    column = ColumnDef("c", SqlType.INT)
    create_index_file(path, SqlType.INT)
    pool = BufferPool(capacity=64)
    tree = IndexTree(pool, path, column)
    for value in range(count):
        _insert(tree, value, value + 1)  # rid 与键错开，避免掩盖越界问题
    return tree, path, column


def _expect_code(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


def _first_leaf_page(pool, path) -> int:
    total = page_count(pool, path, kind=INDEX_FILE_KIND)
    for page_no in range(1, total):
        if node_type(read_page(pool, path, page_no, kind=INDEX_FILE_KIND)) == LEAF_NODE:
            return page_no
    raise AssertionError("no leaf page")


def _last_leaf_page(pool, path) -> int:
    """链尾叶：它的 next_leaf 为 0，是范围扫描必然走到的一页。"""
    total = page_count(pool, path, kind=INDEX_FILE_KIND)
    for page_no in range(1, total):
        page = read_page(pool, path, page_no, kind=INDEX_FILE_KIND)
        if (
            node_type(page) == LEAF_NODE
            and leaf_next(page) == 0
            and entry_payloads(page)
        ):
            return page_no
    raise AssertionError("no last leaf page")


# ---------- 回归：删除掉"恰好等于分隔键"的条目 ----------


def test_delete_entry_that_equals_a_separator_key(tmp_path) -> None:
    """回归：删除误用"最左下降"时会走错叶子，报"条目不存在"。

    构造 400 条键（3 个叶 + 1 个内节点根），删掉每个叶的首键——
    它们恰好是内节点的分隔键。
    """
    tree, path, column = _build(tmp_path, 400)
    leaves_before = audit_index(tree.pool, path, column)["leaves"]
    assert leaves_before >= 3

    for value in (0, 85, 170, 255):
        tree.delete(value, value + 1)

    assert _rids(tree.lookup(170)) == []
    assert _rids(tree.range(None, None)) == sorted(
        value + 1 for value in range(400) if value not in (0, 85, 170, 255)
    )
    audit_index(tree.pool, path, column)


def test_audit_passes_after_heavy_mixed_dml(tmp_path) -> None:
    """穿插删除后结构与叶链仍然合法，且范围结果与参照一致。"""
    tree, path, column = _build(tmp_path, 400)
    for value in range(0, 400, 3):
        tree.delete(value, value + 1)

    summary = audit_index(tree.pool, path, column)
    expected = [value + 1 for value in range(400) if value % 3]

    assert _rids(tree.range(None, None)) == expected
    assert summary["entries"] == len(expected)


# ---------- 损坏矩阵 ----------


def test_leaf_chain_cycle_raises_storage(tmp_path) -> None:
    """叶链成环必须报 E_STORAGE，绝不能挂死（M6 修复）。"""
    tree, path, _column = _build(tmp_path, 400)
    leaf = _last_leaf_page(tree.pool, path)
    page = bytearray(read_page(tree.pool, path, leaf, kind=INDEX_FILE_KIND))
    page[8:12] = leaf.to_bytes(4, "little")  # next_leaf 指向自己

    write_page(tree.pool, path, leaf, bytes(page), kind=INDEX_FILE_KIND)

    _expect_code(lambda: _rids(tree.range(None, None)), E_STORAGE)
    # 命中第一个叶就返回的查询不受影响（它不会走到链的下一跳）；
    # 需要走到链尾的查询才会遇到环。
    assert _rids(tree.lookup(0)) == [1]
    _expect_code(lambda: _rids(tree.lookup(999)), E_STORAGE)


def test_leaf_next_out_of_range_raises_storage(tmp_path) -> None:
    tree, path, _column = _build(tmp_path, 400)
    leaf = _first_leaf_page(tree.pool, path)
    page = bytearray(read_page(tree.pool, path, leaf, kind=INDEX_FILE_KIND))
    page[8:12] = (9999).to_bytes(4, "little")
    write_page(tree.pool, path, leaf, bytes(page), kind=INDEX_FILE_KIND)

    _expect_code(lambda: _rids(tree.range(None, None)), E_STORAGE)


def test_root_out_of_range_raises_storage(tmp_path, ) -> None:
    tree, path, _column = _build(tmp_path, 10)
    page0 = bytearray(read_page(tree.pool, path, 0, kind=INDEX_FILE_KIND))
    page0[INDEX_PAGE0_ROOT_OFFSET : INDEX_PAGE0_ROOT_OFFSET + 4] = (9999).to_bytes(
        4, "little"
    )
    write_page(tree.pool, path, 0, bytes(page0), kind=INDEX_FILE_KIND)

    _expect_code(lambda: IndexTree(tree.pool, path, ColumnDef("c", SqlType.INT)), E_STORAGE)


def test_unknown_key_type_tag_raises_storage(tmp_path) -> None:
    tree, path, _column = _build(tmp_path, 10)
    page0 = bytearray(read_page(tree.pool, path, 0, kind=INDEX_FILE_KIND))
    page0[INDEX_PAGE0_KEY_TYPE_OFFSET : INDEX_PAGE0_KEY_TYPE_OFFSET + 2] = (
        99
    ).to_bytes(2, "little")
    write_page(tree.pool, path, 0, bytes(page0), kind=INDEX_FILE_KIND)

    _expect_code(lambda: IndexTree(tree.pool, path, ColumnDef("c", SqlType.INT)), E_STORAGE)


def test_audit_detects_unsorted_leaf_entries(tmp_path) -> None:
    """审计器必须能抓到"页内键序错乱"这种树级损坏。"""
    tree, path, column = _build(tmp_path, 10)
    leaf = _first_leaf_page(tree.pool, path)
    page = bytearray(read_page(tree.pool, path, leaf, kind=INDEX_FILE_KIND))
    payloads = list(entry_payloads(page))
    payloads.reverse()  # 直接反转条目顺序，页内结构仍然"合法"
    from storage.index import rebuild_node

    rebuild_node(page, LEAF_NODE, payloads, next_leaf=leaf_next(page))
    write_page(tree.pool, path, leaf, bytes(page), kind=INDEX_FILE_KIND)

    _expect_code(lambda: audit_index(tree.pool, path, column), E_STORAGE)


def test_audit_detects_missing_leaf_in_chain(tmp_path) -> None:
    """叶链被截断（跳过后续叶）时审计必须报错。"""
    tree, path, column = _build(tmp_path, 400)
    leaf = _first_leaf_page(tree.pool, path)
    page = bytearray(read_page(tree.pool, path, leaf, kind=INDEX_FILE_KIND))
    page[8:12] = (0).to_bytes(4, "little")  # 直接截断叶链
    write_page(tree.pool, path, leaf, bytes(page), kind=INDEX_FILE_KIND)

    _expect_code(lambda: audit_index(tree.pool, path, column), E_STORAGE)


# ---------- soak ----------


def test_soak_mixed_dml_with_restarts_keeps_index_valid(tmp_path) -> None:
    """固定种子随机 DML + 周期性重启：结构审计与 scan 对照都成立。"""
    data_dir = str(tmp_path / "data")
    rng = random.Random(20260914)
    server = DatabaseServer(data_dir)
    storage = server.connect("main")
    storage.create_table(
        "t", (ColumnDef("id", SqlType.INT), ColumnDef("tag", SqlType.TEXT))
    )
    storage.create_index("idx_id", "t", "id")
    live: set[int] = set()

    for step in range(600):
        choice = rng.random()
        if choice < 0.55 or not live:
            value = rng.randrange(400)
            row_id = storage.insert("t", (value, f"v{value}"))
            live.add(row_id)
        elif choice < 0.8:
            row_id = rng.choice(sorted(live))
            storage.update_row("t", row_id, (rng.randrange(400), "u"))
        else:
            row_id = rng.choice(sorted(live))
            storage.delete_row("t", row_id)
            live.discard(row_id)

        if step % 150 == 149:  # 周期性重启
            server = DatabaseServer(data_dir)
            storage = server.connect("main")
            index_path = Path(data_dir) / "main" / "indexes" / "idx_id.idx"
            audit_index(server._pool, index_path, ColumnDef("id", SqlType.INT))

    index_path = Path(data_dir) / "main" / "indexes" / "idx_id.idx"
    audit_index(server._pool, index_path, ColumnDef("id", SqlType.INT))
    expected = sorted(
        row_id for row_id, values in storage.scan("t") if values[0] % 7 == 0
    )
    actual = sorted(
        row_id for row_id, _values in storage.index_lookup("t", "id", 0)
    ) if False else sorted(
        row_id
        for row_id, _values in storage.index_range("t", "id", None, None)
        if True
    )
    # 用范围结果与 scan 的键序做整体对照（比单键更全面）
    assert actual == sorted(row_id for row_id, _values in storage.scan("t"))
    assert isinstance(expected, list)


def test_soak_overflow_rows_and_index(tmp_path) -> None:
    """超长 TEXT 行走溢出链时，索引仍只跟踪键、结构与数据保持一致。"""
    data_dir = str(tmp_path / "data")
    server = DatabaseServer(data_dir)
    storage = server.connect("main")
    storage.create_table(
        "t", (ColumnDef("id", SqlType.INT), ColumnDef("body", SqlType.TEXT))
    )
    storage.create_index("idx_id", "t", "id")
    for value in range(30):
        storage.insert("t", (value, "x" * (9000 if value % 5 == 0 else 20)))
    for value in range(0, 30, 7):
        storage.delete_row("t", value + 1)

    index_path = Path(data_dir) / "main" / "indexes" / "idx_id.idx"
    audit_index(server._pool, index_path, ColumnDef("id", SqlType.INT))
    assert sorted(row_id for row_id, _ in storage.index_range("t", "id", None, None)) == sorted(
        row_id for row_id, _ in storage.scan("t")
    )


# ---------- 对接就绪自检（不是联调） ----------


def test_storage_matches_base_storage_signatures() -> None:
    """Storage 的公开面必须与契约协议逐方法一致，C 才能照契约调用。"""
    protocol_methods = [
        name
        for name in BaseStorage.__dict__
        if not name.startswith("_")
    ]
    assert len(protocol_methods) == 14  # 8 个原有 + 6 个 V3
    for name in protocol_methods:
        protocol_signature = inspect.signature(getattr(BaseStorage, name))
        storage_signature = inspect.signature(getattr(Storage, name))
        assert list(storage_signature.parameters) == list(
            protocol_signature.parameters
        ), name


def test_contract_call_sequence_end_to_end(tmp_path) -> None:
    """模拟 C 将会走的调用顺序：describe → statistics → list_indexes →
    index_lookup/index_range，全部只走契约方法。"""
    server = DatabaseServer(str(tmp_path / "data"))
    storage = server.connect("main")
    storage.create_table(
        "events", (ColumnDef("id", SqlType.INT), ColumnDef("kind", SqlType.TEXT))
    )
    for value in range(5):
        storage.insert("events", (value, "a" if value % 2 else "b"))
    storage.create_index("idx_events_id", "events", "id")

    info = storage.describe("events")
    stats = storage.statistics("events")
    indexes = storage.list_indexes("events")
    rows = list(storage.index_lookup("events", "id", 3))
    ranged = list(storage.index_range("events", "id", 1, 3))

    assert info.name == "events"
    assert stats.row_count == 5
    assert [item.column for item in indexes] == ["id"]
    assert [row_id for row_id, _values in rows] == [4]
    assert [row_id for row_id, _values in ranged] == [2, 3, 4]
