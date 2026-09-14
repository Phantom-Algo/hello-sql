"""页式系统表自举 M1 测试：文件形态、内置 Schema、空表与损坏路径。

M1 只实现 storage/syscatalog.py 的自举能力，不接入 DatabaseServer；
接入与权威切换在 M2。测试直接使用真实 pager/TableEngine/BufferPool。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.errors import E_STORAGE, SqlError
from storage.cache import BufferPool
from storage.constants import (
    PAGE_SIZE,
    SYS_COLUMNS_FILE_NAME,
    SYS_INDEXES_FILE_NAME,
    SYS_TABLES_FILE_NAME,
    TABLE_FILE_MAGIC,
)
from storage.pager import page_count, read_page
from storage.syscatalog import (
    create_empty_system_catalog,
    open_system_tables,
    system_table_paths,
)


@pytest.fixture
def db_dir(tmp_path) -> Path:
    path = tmp_path / "main"
    path.mkdir()
    return path


@pytest.fixture
def pool() -> BufferPool:
    return BufferPool(capacity=8)


def _expect_code(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


def test_create_empty_system_catalog_writes_three_page_zero_files(db_dir, pool):
    """自举必须建出三张页式文件，各自只有页 0 且 magic 正确。

    断言的改动：只建一张表、用 JSON 文件、忘写页 0 或页 0 身份错。
    """
    paths = system_table_paths(db_dir)
    assert paths.tables == db_dir / SYS_TABLES_FILE_NAME
    assert paths.columns == db_dir / SYS_COLUMNS_FILE_NAME
    assert paths.indexes == db_dir / SYS_INDEXES_FILE_NAME

    create_empty_system_catalog(db_dir)

    for path in (paths.tables, paths.columns, paths.indexes):
        assert path.is_file()
        assert path.stat().st_size == PAGE_SIZE
        assert page_count(pool, path) == 1
        assert read_page(pool, path, 0)[:4] == TABLE_FILE_MAGIC


def test_system_table_paths_returns_three_named_paths(db_dir):
    """路径以命名字段返回，避免三个同类型元组写反。"""
    paths = system_table_paths(db_dir)

    assert (paths.tables.name, paths.columns.name, paths.indexes.name) == (
        SYS_TABLES_FILE_NAME,
        SYS_COLUMNS_FILE_NAME,
        SYS_INDEXES_FILE_NAME,
    )


def test_open_system_tables_includes_empty_indexes_table(db_dir, pool):
    """第三张系统表按内置 Schema 打开后应为空表。"""
    create_empty_system_catalog(db_dir)

    opened = open_system_tables(db_dir, pool)

    assert list(opened.indexes.scan()) == []


def test_indexes_system_table_uses_table_magic_not_index_magic(db_dir, pool):
    """系统表本身是普通表文件（HSQL），不是索引文件（HSIX）。"""
    create_empty_system_catalog(db_dir)

    indexes_path = system_table_paths(db_dir).indexes

    assert read_page(pool, indexes_path, 0)[:4] == TABLE_FILE_MAGIC


def test_open_system_tables_scans_empty_after_bootstrap(db_dir, pool):
    """自举后按内置 Schema 打开，三张系统表都应是空表。

    断言的改动：Schema 错位导致把空页解码成垃圾行/报损坏。
    """
    create_empty_system_catalog(db_dir)

    opened = open_system_tables(db_dir, pool)

    assert list(opened.tables.scan()) == []
    assert list(opened.columns.scan()) == []
    assert list(opened.indexes.scan()) == []


def test_system_tables_accept_documented_row_shapes(db_dir, pool):
    """内置 Schema 必须能接收契约规定的列与值形状。

    断言的改动：表字段数量/顺序/类型与 D20 不一致。
    """
    create_empty_system_catalog(db_dir)
    opened = open_system_tables(db_dir, pool)

    table_id = opened.tables.insert((7, "users", "users.table"))
    column_id = opened.columns.insert((table_id, 0, "id", "INT"))
    opened.columns.insert((table_id, 1, "flag", "BOOLEAN"))

    assert list(opened.tables.scan()) == [
        (table_id, (7, "users", "users.table"))
    ]
    assert list(opened.columns.scan()) == [
        (column_id, (table_id, 0, "id", "INT")),
        (column_id + 1, (table_id, 1, "flag", "BOOLEAN")),
    ]


def test_open_system_tables_rejects_truncated_file(db_dir, pool):
    """系统表文件被截成半页后，扫描必须报 E_STORAGE。

    断言的改动：把半页文件当空表返回、或静默忽略长度异常。
    """
    create_empty_system_catalog(db_dir)
    tables_path = system_table_paths(db_dir).tables
    tables_path.write_bytes(tables_path.read_bytes()[: PAGE_SIZE // 2])

    opened = open_system_tables(db_dir, pool)

    _expect_code(lambda: list(opened.tables.scan()), E_STORAGE)
