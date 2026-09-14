"""公开 API 完整矩阵（B 侧综合验收）：
13 个公开方法（含 cache_stats）按“成功/失败码”逐项核对，覆盖契约第 3 节。

重点不是重复里程碑单测，而是把“方法 × 错误码”整体过一遍，防漏网组合。
"""

from __future__ import annotations

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import (
    E_BAD_ARG,
    E_DATABASE_EXISTS,
    E_DATABASE_IN_USE,
    E_DATABASE_NOT_FOUND,
    E_DUP_COLUMN,
    E_ROW_NOT_FOUND,
    E_STORAGE,
    E_TABLE_EXISTS,
    E_TABLE_NOT_FOUND,
    E_TYPE_MISMATCH,
    E_VALUE_COUNT,
    SqlError,
)
from storage import DatabaseServer


def _columns() -> tuple[ColumnDef, ColumnDef, ColumnDef]:
    return (
        ColumnDef("id", SqlType.INT),
        ColumnDef("name", SqlType.TEXT),
        ColumnDef("score", SqlType.REAL),
    )


@pytest.fixture
def data_dir(tmp_path) -> str:
    return str(tmp_path / "data")


@pytest.fixture
def server(data_dir) -> DatabaseServer:
    return DatabaseServer(data_dir)


def _expect_code(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


# ---- 数据库层：成功路径 ----


def test_database_server_happy_path(server):
    """create→has→list→connect→drop 全链路成功且可观察。"""
    server.create_database("shop")

    assert server.has_database("shop") is True
    assert server.list_databases() == ["main", "shop"]
    storage = server.connect("shop")
    assert storage is not None
    assert "capacity" in server.cache_stats

    server.drop_database("shop")
    assert server.has_database("shop") is False


@pytest.mark.parametrize(
    "name", ["", "Shop", "1abc", "a-b", "a b", "库"]
)
def test_database_methods_reject_invalid_names(server, name):
    """五个库级方法对非法名全部 E_BAD_ARG，先于存在性检查。"""
    _expect_code(lambda: server.create_database(name), E_BAD_ARG)
    _expect_code(lambda: server.drop_database(name), E_BAD_ARG)
    _expect_code(lambda: server.has_database(name), E_BAD_ARG)
    _expect_code(lambda: server.connect(name), E_BAD_ARG)


def test_database_error_codes_cover_contract(server):
    """库层三个错误码各自命中：EXISTS / NOT_FOUND / IN_USE(main)。"""
    server.create_database("shop")
    _expect_code(lambda: server.create_database("shop"), E_DATABASE_EXISTS)
    _expect_code(lambda: server.connect("ghost"), E_DATABASE_NOT_FOUND)
    _expect_code(lambda: server.drop_database("ghost"), E_DATABASE_NOT_FOUND)
    _expect_code(lambda: server.drop_database("main"), E_DATABASE_IN_USE)


def test_database_server_init_corrupt_main_catalog_raises_storage(tmp_path):
    """坏 main 系统目录在构造时即 E_STORAGE（不静默重建）。"""
    from storage.constants import SYS_TABLES_FILE_NAME

    data_dir = str(tmp_path / "data")
    DatabaseServer(data_dir)
    catalog = tmp_path / "data" / "main" / SYS_TABLES_FILE_NAME
    catalog.write_bytes(b"{broken")

    _expect_code(lambda: DatabaseServer(data_dir), E_STORAGE)


# ---- Storage：成功路径（8 个表方法一次串完）----


def test_storage_eight_methods_happy_path_roundtrip(server, data_dir):
    """create/list/describe/insert/scan/update/delete/drop 全链路可观察。"""
    storage = server.connect("main")
    storage.create_table("users", _columns())

    assert storage.list_tables() == ["users"]
    desc = storage.describe("users")
    assert desc.name == "users"
    assert tuple(col.name for col in desc.columns) == ("id", "name", "score")

    assert storage.insert("users", (1, "alice", 1)) == 1
    assert storage.insert("users", (2, "bob", 2.5)) == 2
    assert sorted(storage.scan("users"), key=lambda r: r[0]) == [
        (1, (1, "alice", 1.0)),
        (2, (2, "bob", 2.5)),
    ]

    storage.update_row("users", 1, (10, "ALICE", 10.5))
    storage.delete_row("users", 2)
    assert list(storage.scan("users")) == [(1, (10, "ALICE", 10.5))]

    storage.drop_table("users")
    assert storage.list_tables() == []
    reopened = DatabaseServer(data_dir).connect("main")
    _expect_code(lambda: reopened.scan("users"), E_TABLE_NOT_FOUND)


def test_storage_table_error_codes_cover_contract(storage):
    """表级 6 个错误码（BAD_ARG/EXISTS/DUP/NOT_FOUND/COUNT/TYPE/ROW）全命中。"""
    _expect_code(lambda: storage.describe("ghost"), E_TABLE_NOT_FOUND)
    _expect_code(lambda: storage.drop_table("ghost"), E_TABLE_NOT_FOUND)
    _expect_code(lambda: storage.insert("ghost", (1, "a", 1.0)), E_TABLE_NOT_FOUND)
    _expect_code(lambda: storage.scan("ghost"), E_TABLE_NOT_FOUND)
    _expect_code(
        lambda: storage.update_row("ghost", 1, (1, "a", 1.0)),
        E_TABLE_NOT_FOUND,
    )
    _expect_code(lambda: storage.delete_row("ghost", 1), E_TABLE_NOT_FOUND)

    storage.create_table("users", _columns())
    _expect_code(lambda: storage.create_table("users", _columns()), E_TABLE_EXISTS)
    _expect_code(lambda: storage.create_table("bad", ()), E_DUP_COLUMN)
    _expect_code(
        lambda: storage.insert("users", (1, "a")), E_VALUE_COUNT
    )
    _expect_code(
        lambda: storage.insert("users", (True, "a", 1.0)), E_TYPE_MISMATCH
    )
    storage.insert("users", (1, "a", 1.0))
    _expect_code(
        lambda: storage.update_row("users", 99, (1, "a", 1.0)),
        E_ROW_NOT_FOUND,
    )
    _expect_code(lambda: storage.delete_row("users", 99), E_ROW_NOT_FOUND)


@pytest.fixture
def storage(server):
    return server.connect("main")


@pytest.mark.parametrize(
    "method_name",
    [
        "create_table",
        "drop_table",
        "describe",
        "insert",
        "scan",
        "update_row",
        "delete_row",
    ],
)
def test_table_methods_reject_invalid_names(storage, method_name):
    """七个带表名的方法对非法表名全部 E_BAD_ARG，先于表存在性检查。"""
    calls = {
        "create_table": lambda: storage.create_table("Bad", _columns()),
        "drop_table": lambda: storage.drop_table("Bad"),
        "describe": lambda: storage.describe("Bad"),
        "insert": lambda: storage.insert("Bad", (1, "a", 1.0)),
        "scan": lambda: storage.scan("Bad"),
        "update_row": lambda: storage.update_row("Bad", 1, (1, "a", 1.0)),
        "delete_row": lambda: storage.delete_row("Bad", 1),
    }
    _expect_code(calls[method_name], E_BAD_ARG)


@pytest.mark.parametrize(
    "method_name",
    [
        "create_index",
        "drop_index",
        "list_indexes",
        "index_lookup",
        "index_range",
        "statistics",
    ],
)
def test_index_methods_join_the_public_matrix(storage, method_name):
    """V3：公开方法由 13 个扩到 19 个，五个索引方法与 statistics 必须都在。

    断言的改动：方法名拼错、只在契约里声明而未在 Storage 上实现。
    """
    assert callable(getattr(storage, method_name, None))


def test_index_methods_reject_reserved_prefix(storage):
    """索引方法与表方法一样，`__sys_` 前缀属于 B 保留（E_BAD_ARG）。"""
    _expect_code(lambda: storage.create_index("__sys_x", "users", "id"), E_BAD_ARG)
    _expect_code(lambda: storage.drop_index("__sys_x"), E_BAD_ARG)
