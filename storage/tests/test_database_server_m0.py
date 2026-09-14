"""DatabaseServer 库级测试（V2 页式 Catalog）：目录工件、main 保护、错误边界。

V3 起新库创建 sys_tables.db / sys_columns.db / sys_indexes.db，不再创建 catalog.json。
重启 = 在同一个 data_dir 上新建 DatabaseServer。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import (
    E_BAD_ARG,
    E_DATABASE_EXISTS,
    E_DATABASE_IN_USE,
    E_DATABASE_NOT_FOUND,
    E_STORAGE,
    SqlError,
)
from storage import DatabaseServer, Storage
from storage.constants import (
    CATALOG_FILE_NAME,
    SYS_COLUMNS_FILE_NAME,
    SYS_TABLES_FILE_NAME,
)


@pytest.fixture
def data_dir(tmp_path) -> str:
    return str(tmp_path / "data")


@pytest.fixture
def server(data_dir) -> DatabaseServer:
    return DatabaseServer(data_dir)


def _reopen(data_dir: str) -> DatabaseServer:
    """重启模拟：同一数据目录上新建 DatabaseServer。"""
    return DatabaseServer(data_dir)


def _db_dir(data_dir: str, db_name: str) -> Path:
    return Path(data_dir) / db_name


def _sys_tables(data_dir: str, db_name: str) -> Path:
    return _db_dir(data_dir, db_name) / SYS_TABLES_FILE_NAME


def _sys_columns(data_dir: str, db_name: str) -> Path:
    return _db_dir(data_dir, db_name) / SYS_COLUMNS_FILE_NAME


def _expect_code(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


# ---- main 初始化与系统表工件 ----


def test_init_creates_main_system_tables_without_json(data_dir):
    """构造必须建 main + 两张页式系统表，且不再创建 catalog.json。

    断言的改动：仍写 JSON 权威目录、漏建系统表、list 漏 main。
    """
    DatabaseServer(data_dir)

    assert _sys_tables(data_dir, "main").is_file()
    assert _sys_columns(data_dir, "main").is_file()
    assert not (_db_dir(data_dir, "main") / CATALOG_FILE_NAME).exists()
    assert DatabaseServer(data_dir).list_databases() == ["main"]


def test_init_detects_corrupt_main_system_catalog(data_dir):
    """main 系统表损坏时启动必须 E_STORAGE，不得静默重建。

    断言的改动：忽略损坏的系统表、直接覆盖成空库。
    """
    DatabaseServer(data_dir)
    _sys_tables(data_dir, "main").write_bytes(b"{broken")

    _expect_code(lambda: DatabaseServer(data_dir), E_STORAGE)


def test_init_raises_storage_when_main_dir_has_no_artifacts(data_dir):
    """main 目录存在但没有任何目录工件视为损坏（E_STORAGE）。"""
    (_db_dir(data_dir, "main")).mkdir(parents=True)

    _expect_code(lambda: DatabaseServer(data_dir), E_STORAGE)


def test_init_raises_storage_when_main_system_catalog_partial(data_dir):
    """main 只有一张系统表文件（半成品）也视为损坏。"""
    DatabaseServer(data_dir)
    _sys_columns(data_dir, "main").unlink()

    _expect_code(lambda: DatabaseServer(data_dir), E_STORAGE)


# ---- create_database ----


def test_create_database_creates_system_tables_and_survives_restart(data_dir):
    server = DatabaseServer(data_dir)

    server.create_database("shop")

    assert _sys_tables(data_dir, "shop").is_file()
    assert _sys_columns(data_dir, "shop").is_file()
    assert not (_db_dir(data_dir, "shop") / CATALOG_FILE_NAME).exists()
    assert server.list_databases() == ["main", "shop"]
    assert _reopen(data_dir).list_databases() == ["main", "shop"]


def test_create_database_existing_raises_exists(data_dir):
    server = DatabaseServer(data_dir)
    server.create_database("shop")

    _expect_code(lambda: server.create_database("shop"), E_DATABASE_EXISTS)


@pytest.mark.parametrize("name", ["", "Shop", "1abc", "a-b", "a b", "库"])
def test_database_methods_reject_invalid_names(data_dir, name):
    """非法库名必须先抛 E_BAD_ARG，且不能留下目录副作用。"""
    server = DatabaseServer(data_dir)

    _expect_code(lambda: server.create_database(name), E_BAD_ARG)
    _expect_code(lambda: server.drop_database(name), E_BAD_ARG)
    _expect_code(lambda: server.has_database(name), E_BAD_ARG)
    _expect_code(lambda: server.connect(name), E_BAD_ARG)
    assert server.list_databases() == ["main"]


# ---- drop_database ----


def test_drop_database_removes_system_tables_and_survives_restart(data_dir):
    server = DatabaseServer(data_dir)
    server.create_database("shop")

    server.drop_database("shop")

    assert not _db_dir(data_dir, "shop").exists()
    assert server.list_databases() == ["main"]
    assert _reopen(data_dir).list_databases() == ["main"]


def test_drop_database_main_raises_in_use(data_dir):
    server = DatabaseServer(data_dir)

    _expect_code(lambda: server.drop_database("main"), E_DATABASE_IN_USE)


def test_drop_database_missing_raises_not_found(data_dir):
    server = DatabaseServer(data_dir)

    _expect_code(lambda: server.drop_database("nope"), E_DATABASE_NOT_FOUND)


def test_drop_database_removes_partial_system_catalog(data_dir):
    """半成品系统表属于“存在但损坏”，drop 仍应能删除该库。"""
    server = DatabaseServer(data_dir)
    server.create_database("shop")
    _sys_columns(data_dir, "shop").unlink()

    server.drop_database("shop")

    assert not _db_dir(data_dir, "shop").exists()


# ---- has / list / connect ----


def test_has_database_true_after_create_false_after_drop(data_dir):
    server = DatabaseServer(data_dir)

    server.create_database("shop")
    assert server.has_database("shop") is True

    server.drop_database("shop")
    assert server.has_database("shop") is False


def test_partial_database_is_candidate_but_connect_raises_storage(data_dir):
    """只有一张系统表文件时，库算存在但 connect 必须 E_STORAGE。"""
    server = DatabaseServer(data_dir)
    server.create_database("shop")
    _sys_columns(data_dir, "shop").unlink()
    reopened = DatabaseServer(data_dir)

    assert reopened.has_database("shop") is True
    assert "shop" in reopened.list_databases()
    _expect_code(lambda: reopened.connect("shop"), E_STORAGE)


def test_connect_returns_storage_bound_to_existing_db(data_dir):
    server = DatabaseServer(data_dir)

    assert isinstance(server.connect("main"), Storage)


def test_connect_missing_db_raises_not_found(data_dir):
    server = DatabaseServer(data_dir)

    _expect_code(lambda: server.connect("nope"), E_DATABASE_NOT_FOUND)


def test_connect_detects_external_system_catalog_corruption(data_dir):
    """外部把系统表截断后，connect 必须 E_STORAGE（不替换共享内存真相）。"""
    server = DatabaseServer(data_dir)
    server.create_database("shop")
    path = _sys_tables(data_dir, "shop")
    path.write_bytes(path.read_bytes()[: 4096 // 2])

    _expect_code(lambda: server.connect("shop"), E_STORAGE)


def test_list_databases_ignores_stray_dirs_without_artifacts(data_dir):
    """只有目录、没有任何 catalog 工件的杂目录不进列表。"""
    server = DatabaseServer(data_dir)
    (_db_dir(data_dir, "stray")).mkdir()
    (Path(data_dir) / "notes.txt").write_text("x", encoding="utf-8")
    server.create_database("shop")

    assert server.list_databases() == ["main", "shop"]


# ---- 端到端：用户表经页式 Catalog 重启可读 ----


def test_user_table_survives_restart_with_page_catalog(data_dir):
    """建表/插入后重启，describe 与 scan 必须来自页式目录恢复结果。"""
    server = DatabaseServer(data_dir)
    storage = server.connect("main")
    storage.create_table(
        "users",
        (ColumnDef("id", SqlType.INT), ColumnDef("flag", SqlType.BOOLEAN)),
    )
    row_id = storage.insert("users", (1, True))

    reopened = DatabaseServer(data_dir).connect("main")
    assert reopened.describe("users").columns == (
        ColumnDef("id", SqlType.INT),
        ColumnDef("flag", SqlType.BOOLEAN),
    )
    assert list(reopened.scan("users")) == [(row_id, (1, True))]
