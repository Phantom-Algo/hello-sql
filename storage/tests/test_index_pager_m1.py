"""索引文件的页原语与空闲页链表复用测试（V3 M1/D29）。

pager 被泛化成"页文件层"：同一套 alloc / free / free_pages / read / write
通过 PageFileKind 同时服务表文件与索引文件。本文件验证索引侧的行为，
并反向验证两类文件不会互相冒充。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.ast import SqlType
from contracts.errors import E_STORAGE, SqlError
from storage.cache import BufferPool
from storage.constants import INDEX_PAGE0_FREE_HEAD_OFFSET, PAGE_SIZE
from storage.index import create_index_file, open_index_file
from storage.pager import (
    INDEX_FILE_KIND,
    alloc_page,
    create_table_file,
    free_page,
    free_pages,
    read_page,
    write_page,
)


@pytest.fixture
def pool() -> BufferPool:
    return BufferPool(capacity=8)


@pytest.fixture
def index_path(tmp_path) -> Path:
    path = tmp_path / "idx_users_age.idx"
    create_index_file(path, SqlType.INT)
    return path


def _expect_code(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


def _free_head(pool: BufferPool, path: Path) -> int:
    page0 = read_page(pool, path, 0, kind=INDEX_FILE_KIND)
    return int.from_bytes(
        page0[INDEX_PAGE0_FREE_HEAD_OFFSET : INDEX_PAGE0_FREE_HEAD_OFFSET + 4],
        "little",
    )


def test_index_alloc_page_appends_when_free_list_empty(pool, index_path) -> None:
    """空闲链为空时在文件末尾追加（D06 文件只增不减）。"""
    assert alloc_page(pool, index_path, kind=INDEX_FILE_KIND) == 2
    assert alloc_page(pool, index_path, kind=INDEX_FILE_KIND) == 3
    assert index_path.stat().st_size == 4 * PAGE_SIZE


def test_index_free_page_then_alloc_reuses_same_page(pool, index_path) -> None:
    """释放的页必须能被下一次分配复用（D29）。"""
    alloc_page(pool, index_path, kind=INDEX_FILE_KIND)
    free_page(pool, index_path, 2, kind=INDEX_FILE_KIND)

    assert alloc_page(pool, index_path, kind=INDEX_FILE_KIND) == 2
    assert index_path.stat().st_size == 3 * PAGE_SIZE


def test_index_page0_free_head_tracks_freed_and_allocated_page(
    pool, index_path
) -> None:
    """free 后页 0 指向该页；再分配后回到链尾。"""
    alloc_page(pool, index_path, kind=INDEX_FILE_KIND)

    free_page(pool, index_path, 2, kind=INDEX_FILE_KIND)
    assert _free_head(pool, index_path) == 2

    alloc_page(pool, index_path, kind=INDEX_FILE_KIND)
    assert _free_head(pool, index_path) == 0


def test_index_free_pages_lists_chain_in_reverse_release_order(
    pool, index_path
) -> None:
    """free_pages 返回链序（最新释放在前）。"""
    alloc_page(pool, index_path, kind=INDEX_FILE_KIND)
    alloc_page(pool, index_path, kind=INDEX_FILE_KIND)

    free_page(pool, index_path, 2, kind=INDEX_FILE_KIND)
    free_page(pool, index_path, 3, kind=INDEX_FILE_KIND)

    assert free_pages(pool, index_path, kind=INDEX_FILE_KIND) == [3, 2]


def test_index_free_list_cycle_is_rejected(pool, index_path) -> None:
    """空闲链自环 → E_STORAGE，不能被当成正常链表无限循环。"""
    alloc_page(pool, index_path, kind=INDEX_FILE_KIND)
    free_page(pool, index_path, 2, kind=INDEX_FILE_KIND)
    page = bytearray(read_page(pool, index_path, 2, kind=INDEX_FILE_KIND))
    page[:4] = (2).to_bytes(4, "little")
    write_page(pool, index_path, 2, bytes(page), kind=INDEX_FILE_KIND)

    _expect_code(
        lambda: free_pages(pool, index_path, kind=INDEX_FILE_KIND), E_STORAGE
    )


def test_table_file_kind_is_rejected_by_index_operations(tmp_path, pool) -> None:
    """表文件走索引 kind 时必须被拒绝，避免两类文件互相污染。"""
    path = tmp_path / "users.table"
    create_table_file(path)

    _expect_code(lambda: alloc_page(pool, path, kind=INDEX_FILE_KIND), E_STORAGE)
    _expect_code(lambda: free_pages(pool, path, kind=INDEX_FILE_KIND), E_STORAGE)
    _expect_code(lambda: open_index_file(pool, path), E_STORAGE)
