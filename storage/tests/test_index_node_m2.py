"""索引节点页原语与页 0 键类型的测试（V3 M2 第二段）。

节点页是 B+ 树的地基：条目区连续、槽目录自页尾向前、结构损坏一律 E_STORAGE。
"""

from __future__ import annotations

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_STORAGE, SqlError
from storage.cache import BufferPool
from storage.constants import (
    INDEX_KEY_TYPE_TEXT,
    INDEX_NODE_HEADER_SIZE,
    INDEX_RID_SIZE,
    INDEX_SLOT_SIZE,
    INTERIOR_NODE,
    LEAF_NODE,
    MAX_INDEX_KEY_BYTES,
    PAGE_SIZE,
)
from storage.index import (
    create_index_file,
    encode_key,
    entry_count,
    entry_payloads,
    first_child,
    free_ptr,
    free_space,
    leaf_next,
    leaf_prev,
    new_interior_page,
    new_leaf_page,
    node_fits,
    node_type,
    open_index_file,
    rebuild_node,
    slot,
    validate_node,
)


def _expect_code(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


@pytest.fixture
def pool() -> BufferPool:
    return BufferPool(capacity=8)


def test_new_leaf_page_is_empty_with_contiguous_free_space() -> None:
    """空叶页：条目数为 0、free_ptr 指向页头之后、前后叶指针为 0。"""
    page = new_leaf_page()

    assert node_type(page) == LEAF_NODE
    assert entry_count(page) == 0
    assert free_ptr(page) == INDEX_NODE_HEADER_SIZE
    assert leaf_next(page) == 0
    assert leaf_prev(page) == 0
    assert free_space(page) == PAGE_SIZE - INDEX_NODE_HEADER_SIZE


def test_new_interior_page_has_only_first_child() -> None:
    """空内节点：没有分隔键，只有一个最左子页号。"""
    page = new_interior_page(first_child=9)

    assert node_type(page) == INTERIOR_NODE
    assert entry_count(page) == 0
    assert first_child(page) == 9


def test_rebuild_leaf_writes_entries_and_slots() -> None:
    """重建后条目顺序不变，且条目区保持连续（free_ptr 紧跟最后一条）。"""
    page = new_leaf_page()
    entries = (b"A" * INDEX_RID_SIZE, b"B" * (INDEX_RID_SIZE + 5))

    rebuild_node(page, LEAF_NODE, entries, next_leaf=7, prev_leaf=3)

    assert entry_count(page) == 2
    assert entry_payloads(page) == entries
    assert slot(page, 0)[0] == INDEX_NODE_HEADER_SIZE
    assert slot(page, 1)[0] == INDEX_NODE_HEADER_SIZE + len(entries[0])
    assert free_ptr(page) == INDEX_NODE_HEADER_SIZE + sum(len(e) for e in entries)
    assert leaf_next(page) == 7
    assert leaf_prev(page) == 3
    assert free_space(page) == (
        PAGE_SIZE
        - INDEX_NODE_HEADER_SIZE
        - INDEX_SLOT_SIZE * 2
        - sum(len(e) for e in entries)
    )


def test_node_fits_accounts_for_slots() -> None:
    """容量判断必须同时算上条目负载与每条 8 B 的槽。"""
    biggest = b"x" * (PAGE_SIZE - INDEX_NODE_HEADER_SIZE - INDEX_SLOT_SIZE)
    assert node_fits((biggest,))
    assert not node_fits((biggest, b"y"))


def test_validate_node_rejects_unknown_node_type() -> None:
    page = new_leaf_page()
    page[0] = 99

    _expect_code(lambda: validate_node(page, context="test"), E_STORAGE)


def test_validate_node_rejects_free_ptr_out_of_range() -> None:
    page = new_leaf_page()
    page[4:8] = (5).to_bytes(4, "little")

    _expect_code(lambda: validate_node(page, context="test"), E_STORAGE)


def test_validate_node_rejects_slot_count_overflow() -> None:
    page = new_leaf_page()
    page[1:3] = (999).to_bytes(2, "little")

    _expect_code(lambda: validate_node(page, context="test"), E_STORAGE)


def test_validate_node_rejects_slot_pointing_past_free_ptr() -> None:
    page = new_leaf_page()
    payload = b"k" * INDEX_RID_SIZE
    rebuild_node(page, LEAF_NODE, (payload,))
    base = PAGE_SIZE - INDEX_SLOT_SIZE
    page[base + 4 : base + 8] = (PAGE_SIZE).to_bytes(4, "little")

    _expect_code(lambda: validate_node(page, context="test"), E_STORAGE)


def test_create_index_file_records_key_type_tag(tmp_path, pool: BufferPool) -> None:
    """页 0 必须自描述键类型（M2 决策 3）。"""
    path = tmp_path / "idx.idx"

    create_index_file(path, SqlType.TEXT)

    header = open_index_file(pool, path)
    assert header.key_type_tag == INDEX_KEY_TYPE_TEXT


def test_open_index_file_rejects_key_type_mismatch(tmp_path, pool: BufferPool) -> None:
    """期望类型与文件记录不符 → E_STORAGE（避免 INT/REAL 静默解错序）。"""
    path = tmp_path / "idx.idx"
    create_index_file(path, SqlType.REAL)

    _expect_code(
        lambda: open_index_file(pool, path, expected_key_type=SqlType.INT),
        E_STORAGE,
    )


def test_encode_key_rejects_oversized_text_value() -> None:
    """超过单条目容量的键无法被索引 → E_STORAGE（M2 决策 2）。"""
    column = ColumnDef("c", SqlType.TEXT)
    too_long = "x" * (MAX_INDEX_KEY_BYTES + 1)

    _expect_code(lambda: encode_key(column, too_long), E_STORAGE)


def test_encode_key_accepts_value_exactly_at_limit() -> None:
    """边界值应通过：编码后长度恰好等于上限。"""
    column = ColumnDef("c", SqlType.TEXT)
    at_limit = "x" * (MAX_INDEX_KEY_BYTES - 4)  # 4 B 长度前缀

    assert len(encode_key(column, at_limit)) == MAX_INDEX_KEY_BYTES
