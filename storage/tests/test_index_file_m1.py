"""索引文件页 0 与空叶根页测试（V3 M1）。

覆盖：建文件的页面形态、页 0 身份校验、损坏路径，以及与表文件的互斥。
B+ 树的节点操作与树算法属于 M2，本文件不涉及。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.errors import E_STORAGE, SqlError
from storage.cache import BufferPool
from storage.constants import (
    INDEX_FILE_VERSION,
    INDEX_MAGIC,
    INDEX_NODE_HEADER_SIZE,
    INDEX_PAGE0_FREE_HEAD_OFFSET,
    INDEX_PAGE0_HEIGHT_OFFSET,
    INDEX_PAGE0_MAGIC_OFFSET,
    INDEX_PAGE0_ROOT_OFFSET,
    INDEX_PAGE0_VERSION_OFFSET,
    LEAF_NODE,
    PAGE_SIZE,
    TABLE_FILE_MAGIC,
)
from storage.index import create_index_file, open_index_file
from storage.pager import (
    INDEX_FILE_KIND,
    create_table_file,
    page_count,
    read_page,
)


@pytest.fixture
def pool() -> BufferPool:
    return BufferPool(capacity=8)


def _expect_code(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


def _u16(raw: bytes, offset: int) -> int:
    return int.from_bytes(raw[offset : offset + 2], "little")


def _u32(raw: bytes, offset: int) -> int:
    return int.from_bytes(raw[offset : offset + 4], "little")


def test_create_index_file_writes_page0_and_empty_leaf_root(tmp_path, pool) -> None:
    """新建索引文件 = 页 0 头 + 一个空叶根页，且文件整页对齐。"""
    path = tmp_path / "idx_users_age.idx"

    create_index_file(path)

    assert path.is_file()
    assert path.stat().st_size == 2 * PAGE_SIZE
    assert page_count(pool, path, kind=INDEX_FILE_KIND) == 2

    page0 = read_page(pool, path, 0, kind=INDEX_FILE_KIND)
    assert page0[INDEX_PAGE0_MAGIC_OFFSET : INDEX_PAGE0_MAGIC_OFFSET + 4] == INDEX_MAGIC
    assert _u16(page0, INDEX_PAGE0_VERSION_OFFSET) == INDEX_FILE_VERSION
    assert _u32(page0, INDEX_PAGE0_ROOT_OFFSET) == 1
    assert _u32(page0, INDEX_PAGE0_HEIGHT_OFFSET) == 1
    assert _u32(page0, INDEX_PAGE0_FREE_HEAD_OFFSET) == 0

    leaf = read_page(pool, path, 1, kind=INDEX_FILE_KIND)
    assert leaf[0] == LEAF_NODE
    assert _u16(leaf, 1) == 0
    assert _u32(leaf, 4) == INDEX_NODE_HEADER_SIZE
    assert _u32(leaf, 8) == 0


def test_open_index_file_returns_root_and_height(tmp_path, pool) -> None:
    """打开索引文件应读回页 0 的 root / height / free_head。"""
    path = tmp_path / "idx_users_age.idx"
    create_index_file(path)

    header = open_index_file(pool, path)

    assert (header.root_page, header.height, header.free_head) == (1, 1, 0)


def test_index_page0_rejects_wrong_magic(tmp_path, pool) -> None:
    """页 0 magic 不是 HSIX（这里改成 HSQL）→ E_STORAGE。"""
    path = tmp_path / "idx_users_age.idx"
    create_index_file(path)
    raw = bytearray(path.read_bytes())
    raw[INDEX_PAGE0_MAGIC_OFFSET : INDEX_PAGE0_MAGIC_OFFSET + 4] = TABLE_FILE_MAGIC
    path.write_bytes(bytes(raw))

    _expect_code(lambda: open_index_file(pool, path), E_STORAGE)


def test_index_page0_rejects_unsupported_version(tmp_path, pool) -> None:
    """页 0 版本号不认识 → E_STORAGE。"""
    path = tmp_path / "idx_users_age.idx"
    create_index_file(path)
    raw = bytearray(path.read_bytes())
    raw[INDEX_PAGE0_VERSION_OFFSET : INDEX_PAGE0_VERSION_OFFSET + 2] = (
        INDEX_FILE_VERSION + 98
    ).to_bytes(2, "little")
    path.write_bytes(bytes(raw))

    _expect_code(lambda: open_index_file(pool, path), E_STORAGE)


def test_index_file_rejects_half_page(tmp_path, pool) -> None:
    """文件长度不是页大小整数倍 → E_STORAGE。"""
    path = tmp_path / "idx_users_age.idx"
    create_index_file(path)
    path.write_bytes(path.read_bytes()[: PAGE_SIZE + 100])

    _expect_code(lambda: open_index_file(pool, path), E_STORAGE)


def test_index_file_rejects_short_page0(tmp_path, pool) -> None:
    """文件短于一个整页（连页 0 都不完整）→ E_STORAGE。"""
    path = tmp_path / "idx_users_age.idx"
    path.write_bytes(b"HSIX")

    _expect_code(lambda: open_index_file(pool, path), E_STORAGE)


def test_create_index_file_creates_parent_directory(tmp_path, pool) -> None:
    """indexes/ 目录不存在时应自动创建（D27 每库一个索引目录）。"""
    path = tmp_path / "indexes" / "idx_users_age.idx"

    create_index_file(path)

    assert path.is_file()
    assert path.parent.is_dir()


def test_table_file_is_not_accepted_as_index_file(tmp_path, pool) -> None:
    """表文件交给索引打开必须被拒绝，避免两类文件互相冒充。"""
    path = tmp_path / "users.table"
    create_table_file(path)

    _expect_code(lambda: open_index_file(pool, path), E_STORAGE)


def test_index_file_is_not_accepted_as_table_file(tmp_path, pool) -> None:
    """反向隔离：索引文件按表文件读也必须被拒绝（证明 kind 参数生效）。"""
    path = tmp_path / "idx_users_age.idx"
    create_index_file(path)

    _expect_code(lambda: read_page(pool, path, 0), E_STORAGE)


def test_create_index_file_overwrites_existing_file(tmp_path, pool) -> None:
    """重建索引文件应覆盖旧内容（与 create_table_file 的孤儿覆盖一致）。"""
    path = tmp_path / "idx_users_age.idx"
    create_index_file(path)
    path.write_bytes(path.read_bytes() + bytes(PAGE_SIZE))

    create_index_file(path)

    assert path.stat().st_size == 2 * PAGE_SIZE
    assert open_index_file(pool, path).root_page == 1
