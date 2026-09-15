"""B+ 树核心测试（V3 M2 第三段：插入/分裂/等值/范围/删除）。

树是索引文件的唯一使用者：`IndexTree` 以 (pool, 文件路径, 列定义) 构造。
本文件不经过门面（门面接入在 M3），直接驱动树的公开方法。
"""

from __future__ import annotations

import random

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_STORAGE, E_TYPE_MISMATCH, SqlError
from storage.cache import BufferPool
from storage.index import IndexTree, create_index_file, open_index_file
from storage.pager import INDEX_FILE_KIND, free_pages, page_count



# ---- D49a：叶条目多了"行页号"字段 ----
# 这两个文件测的是树机制本身，不关心页号真伪；统一用一个占位页号写入，
# 断言时只看 rid（`rids()` 把 [(rid, page)] 投影成 [rid]）。
_PLACEHOLDER_PAGE = 1


def _insert(tree, key, row_id):
    tree.insert(key, row_id, _PLACEHOLDER_PAGE)


def _rids(entries):
    return [row_id for row_id, _page_no in entries]

def _make(tmp_path, sql_type: SqlType = SqlType.INT, *, capacity: int = 8):
    """建一个索引文件 + 树，返回 (tree, pool, path, column)。"""
    path = tmp_path / f"idx_{sql_type.value}.idx"
    create_index_file(path, sql_type)
    pool = BufferPool(capacity=capacity)
    column = ColumnDef("c", sql_type)
    return IndexTree(pool, path, column), pool, path, column


def _expect_code(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


# ---------- 插入与分裂 ----------


def test_insert_single_entry_then_lookup_returns_rid(tmp_path) -> None:
    tree, pool, path, _ = _make(tmp_path)

    _insert(tree, 42, 7)
    pool.flush(path)

    assert _rids(tree.lookup(42)) == [7]
    assert _rids(tree.lookup(41)) == []


def test_entries_sorted_by_key_then_rid(tmp_path) -> None:
    """同一个键的多个 rid 必须按 rid 升序返回。"""
    tree, _, _, _ = _make(tmp_path)

    _insert(tree, 1, 5)
    _insert(tree, 1, 3)
    _insert(tree, 1, 9)

    assert _rids(tree.lookup(1)) == [3, 5, 9]


def test_leaf_split_links_leaf_chain_in_order(tmp_path) -> None:
    """插入量超过单叶容量后发生分裂，全索引扫描仍按键序返回。"""
    tree, _, path, _ = _make(tmp_path)
    for value in range(200):
        _insert(tree, value, value)

    assert page_count(tree.pool, path, kind=INDEX_FILE_KIND) > 2
    assert _rids(tree.range(None, None)) == list(range(200))


def test_root_split_increases_height_and_updates_page0(tmp_path) -> None:
    """根分裂后页 0 的 root / height 必须同步更新。"""
    tree, pool, path, _ = _make(tmp_path)

    for value in range(200):
        _insert(tree, value, value)
    pool.flush(path)

    header = open_index_file(pool, path)
    assert header.height >= 2
    assert header.root_page > 1


def test_multi_level_tree_still_answers_correctly(tmp_path) -> None:
    """高度 ≥ 3 时等值与范围仍正确（覆盖内节点分裂的再上推）。"""
    tree, _, _, _ = _make(tmp_path)
    for value in range(600):
        _insert(tree, value, value)

    assert _rids(tree.lookup(599)) == [599]
    assert _rids(tree.lookup(-1)) == []
    assert _rids(tree.range(100, 109)) == list(range(100, 110))


def test_split_handles_mixed_text_key_lengths(tmp_path) -> None:
    """变长键按字节预算分裂：长短混合也必须全部可检索。"""
    tree, _, _, _ = _make(tmp_path, SqlType.TEXT)
    keys = [f"{'x' * (i % 40)}{i:04d}" for i in range(300)]

    for index, key in enumerate(keys):
        _insert(tree, key, index)

    for index, key in enumerate(keys):
        assert _rids(tree.lookup(key)) == [index]


# ---------- 等值查找 ----------


def test_lookup_empty_tree_returns_nothing(tmp_path) -> None:
    tree, _, _, _ = _make(tmp_path)

    assert _rids(tree.lookup(0)) == []
    assert _rids(tree.range(None, None)) == []


def test_lookup_returns_all_duplicates_spanning_two_leaves(tmp_path) -> None:
    """同一个键的副本写满一页以上后必须全部取回（最左下降 + 沿叶链续扫）。"""
    tree, _, _, _ = _make(tmp_path)
    for rid in range(250):
        _insert(tree, 7, rid)

    assert _rids(tree.lookup(7)) == list(range(250))
    assert _rids(tree.lookup(8)) == []


def test_lookup_missing_key_between_existing_keys(tmp_path) -> None:
    tree, _, _, _ = _make(tmp_path)
    for value in (10, 20, 30):
        _insert(tree, value, value)

    assert _rids(tree.lookup(25)) == []
    assert _rids(tree.lookup(9)) == []


# ---------- 范围查找 ----------


def test_range_closed_bounds_include_endpoints(tmp_path) -> None:
    tree, _, _, _ = _make(tmp_path)
    for value in range(10):
        _insert(tree, value, value)

    assert _rids(tree.range(3, 6)) == [3, 4, 5, 6]


def test_range_open_bounds_exclude_endpoints(tmp_path) -> None:
    tree, _, _, _ = _make(tmp_path)
    for value in range(10):
        _insert(tree, value, value)

    assert _rids(tree.range(3, 6, lower_inclusive=False)) == [4, 5, 6]
    assert _rids(tree.range(3, 6, upper_inclusive=False)) == [3, 4, 5]
    assert _rids(tree.range(3, 6, lower_inclusive=False, upper_inclusive=False)) == [4, 5]


def test_range_supports_unbounded_sides(tmp_path) -> None:
    tree, _, _, _ = _make(tmp_path)
    for value in range(10):
        _insert(tree, value, value)

    assert _rids(tree.range(None, 3)) == [0, 1, 2, 3]
    assert _rids(tree.range(7, None)) == [7, 8, 9]
    assert _rids(tree.range(None, None)) == list(range(10))


def test_range_lower_greater_than_upper_is_empty(tmp_path) -> None:
    tree, _, _, _ = _make(tmp_path)
    for value in range(10):
        _insert(tree, value, value)

    assert _rids(tree.range(8, 2)) == []
    assert _rids(tree.range(4, 4, lower_inclusive=False)) == []


def test_range_crosses_leaf_boundary(tmp_path) -> None:
    """范围跨多个叶时按全局键序连续返回。"""
    tree, _, _, _ = _make(tmp_path)
    for value in range(300):
        _insert(tree, value, value)

    assert _rids(tree.range(150, 199)) == list(range(150, 200))


def test_range_includes_duplicates_at_bounds(tmp_path) -> None:
    tree, _, _, _ = _make(tmp_path)
    for rid in range(120):
        _insert(tree, 5, rid)
    _insert(tree, 6, 999)

    assert _rids(tree.range(5, 5)) == list(range(120))
    assert _rids(tree.range(5, 6)) == list(range(120)) + [999]


# ---------- 键比较 ----------


def test_int_keys_order_includes_negatives(tmp_path) -> None:
    tree, _, _, _ = _make(tmp_path)
    for row_id, value in enumerate((-5, -1, 0, 3), start=1):
        _insert(tree, value, row_id)

    assert _rids(tree.range(None, None)) == [1, 2, 3, 4]
    assert _rids(tree.lookup(-5)) == [1]


def test_real_negative_zero_matches_positive_zero(tmp_path) -> None:
    """-0.0 与 0.0 必须视为同一个键（否则违反"键序与 C 一致"）。"""
    tree, _, _, _ = _make(tmp_path, SqlType.REAL)

    _insert(tree, -0.0, 1)

    assert _rids(tree.lookup(0.0)) == [1]
    assert _rids(tree.lookup(-0.0)) == [1]


def test_real_int_probe_is_normalized_to_float(tmp_path) -> None:
    """REAL 列收到 int 探针时先归一为 float，才能命中 float 键。"""
    tree, _, _, _ = _make(tmp_path, SqlType.REAL)

    _insert(tree, 2.0, 4)

    assert _rids(tree.lookup(2)) == [4]


def test_text_prefix_sorts_before_longer_key(tmp_path) -> None:
    tree, _, _, _ = _make(tmp_path, SqlType.TEXT)
    for key in ("ab", "a", "b"):
        _insert(tree, key, len(key))

    assert _rids(tree.range(None, None)) == [1, 2, 1]


def test_boolean_keys_support_equality(tmp_path) -> None:
    tree, _, _, _ = _make(tmp_path, SqlType.BOOLEAN)

    _insert(tree, False, 1)
    _insert(tree, True, 2)

    assert _rids(tree.lookup(True)) == [2]
    assert _rids(tree.lookup(False)) == [1]


def test_key_type_mismatch_raises_type_mismatch(tmp_path) -> None:
    tree, _, _, _ = _make(tmp_path, SqlType.INT)

    _expect_code(lambda: _rids(tree.lookup("x")), E_TYPE_MISMATCH)
    _expect_code(lambda: _insert(tree, 1.5, 1), E_TYPE_MISMATCH)


# ---------- 删除与空闲页 ----------


def test_delete_removes_only_target_rid(tmp_path) -> None:
    tree, _, _, _ = _make(tmp_path)
    for rid in (1, 2, 3):
        _insert(tree, 5, rid)

    tree.delete(5, 2)

    assert _rids(tree.lookup(5)) == [1, 3]


def test_delete_last_entry_keeps_empty_root(tmp_path) -> None:
    """根叶被删空后必须保留，树回到"空树 = 空叶根"的状态。"""
    tree, pool, path, _ = _make(tmp_path)
    _insert(tree, 1, 1)

    tree.delete(1, 1)
    pool.flush(path)

    assert _rids(tree.lookup(1)) == []
    header = open_index_file(pool, path)
    assert header.height == 1
    assert header.root_page == 1


def test_delete_emptied_leaf_is_freed_and_chain_relinked(tmp_path) -> None:
    """删空中间叶后：页被回收、叶链仍连续、范围扫描不中断。"""
    tree, pool, path, _ = _make(tmp_path)
    for value in range(250):
        _insert(tree, value, value)

    before_pages = page_count(pool, path, kind=INDEX_FILE_KIND)
    for value in range(250):
        tree.delete(value, value)

    assert _rids(tree.range(None, None)) == []
    assert page_count(pool, path, kind=INDEX_FILE_KIND) == before_pages
    assert len(free_pages(pool, path, kind=INDEX_FILE_KIND)) >= 1


def test_delete_nonexistent_pair_raises_storage(tmp_path) -> None:
    """删除不存在的 (键, rid) 说明索引与数据已分叉 → E_STORAGE。"""
    tree, _, _, _ = _make(tmp_path)
    _insert(tree, 1, 1)

    _expect_code(lambda: tree.delete(1, 2), E_STORAGE)
    _expect_code(lambda: tree.delete(2, 1), E_STORAGE)


def test_delete_then_lookup_and_range_still_correct(tmp_path) -> None:
    tree, _, _, _ = _make(tmp_path)
    for value in range(300):
        _insert(tree, value, value)
    for value in range(0, 300, 3):
        tree.delete(value, value)

    expected = [value for value in range(300) if value % 3]
    assert _rids(tree.range(None, None)) == expected
    assert _rids(tree.lookup(4)) == [4]
    assert _rids(tree.lookup(3)) == []


def test_split_reuses_page_returned_to_free_list(tmp_path) -> None:
    """删除释放的页应被后续分裂复用，文件不因反复写入而无限增长。"""
    tree, pool, path, _ = _make(tmp_path)
    for value in range(250):
        _insert(tree, value, value)
    for value in range(250):
        tree.delete(value, value)
    pages_after_delete = page_count(pool, path, kind=INDEX_FILE_KIND)

    for value in range(250):
        _insert(tree, value, value)

    assert page_count(pool, path, kind=INDEX_FILE_KIND) == pages_after_delete
    assert _rids(tree.range(None, None)) == list(range(250))


# ---------- 持久化与随机模型 ----------


def test_tree_survives_flush_and_reopen(tmp_path) -> None:
    """flush 之后重新构造树（模拟重启）仍能正确检索。"""
    tree, pool, path, column = _make(tmp_path)
    for value in range(250):
        _insert(tree, value, value)
    pool.flush(path)

    reopened = IndexTree(pool, path, column)

    assert _rids(reopened.lookup(123)) == [123]
    assert _rids(reopened.range(10, 12)) == [10, 11, 12]


def test_randomized_model_matches_sorted_reference(tmp_path) -> None:
    """固定种子随机插入/删除后，与 Python 有序参照逐项对照。"""
    tree, _, _, _ = _make(tmp_path)
    rng = random.Random(20260914)
    reference: list[tuple[int, int]] = []

    for _ in range(900):
        key = rng.randrange(60)
        if reference and rng.random() < 0.35:
            key, rid = rng.choice(reference)
            tree.delete(key, rid)
            reference.remove((key, rid))
        else:
            rid = rng.randrange(100000)
            if (key, rid) in reference:
                continue
            _insert(tree, key, rid)
            reference.append((key, rid))

    reference.sort()
    assert _rids(tree.range(None, None)) == [rid for _, rid in reference]
    for key in range(60):
        assert _rids(tree.lookup(key)) == sorted(
            rid for k, rid in reference if k == key
        )
