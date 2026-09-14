"""索引门面（M3）：公开方法的闸门、行为、回滚、缓存与旧目录升级。

覆盖契约 §4.6 里"谁抛什么"的责任表，以及 D36（同表同列只允许一个索引）、
D39（V2 目录自动补建第三张系统表）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import (
    E_BAD_ARG,
    E_COLUMN_NOT_FOUND,
    E_INDEX_EXISTS,
    E_INDEX_NOT_FOUND,
    E_STORAGE,
    E_TABLE_NOT_FOUND,
    E_TYPE_MISMATCH,
    SqlError,
)
from storage import DatabaseServer
from storage.constants import SYS_INDEXES_FILE_NAME


@pytest.fixture
def server(tmp_path) -> DatabaseServer:
    return DatabaseServer(str(tmp_path / "data"))


@pytest.fixture
def storage(server: DatabaseServer):
    handle = server.connect("main")
    handle.create_table(
        "users",
        (
            ColumnDef("id", SqlType.INT),
            ColumnDef("name", SqlType.TEXT),
        ),
    )
    for row_id, name in ((1, "alice"), (2, "bob"), (3, "carol")):
        handle.insert("users", (row_id, name))
    return handle


def _expect_code(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


# ---------- 闸门与错误码 ----------


def test_create_index_rejects_bad_identifier(storage) -> None:
    _expect_code(lambda: storage.create_index("Bad Name", "users", "id"), E_BAD_ARG)


def test_create_index_rejects_reserved_prefix(storage) -> None:
    _expect_code(
        lambda: storage.create_index("__sys_idx", "users", "id"), E_BAD_ARG
    )


def test_create_index_rejects_missing_table(storage) -> None:
    _expect_code(
        lambda: storage.create_index("idx", "missing", "id"), E_TABLE_NOT_FOUND
    )


def test_create_index_rejects_missing_column(storage) -> None:
    _expect_code(
        lambda: storage.create_index("idx", "users", "missing"), E_COLUMN_NOT_FOUND
    )


def test_create_index_rejects_duplicate_name(storage) -> None:
    storage.create_index("idx_users_id", "users", "id")

    _expect_code(
        lambda: storage.create_index("idx_users_id", "users", "name"),
        E_INDEX_EXISTS,
    )


def test_create_index_rejects_same_table_and_column_twice(storage) -> None:
    """D36：同一表同一列只允许一个索引。"""
    storage.create_index("idx_a", "users", "id")

    _expect_code(
        lambda: storage.create_index("idx_b", "users", "id"), E_INDEX_EXISTS
    )


def test_drop_index_rejects_missing(storage) -> None:
    _expect_code(lambda: storage.drop_index("nope"), E_INDEX_NOT_FOUND)


def test_index_lookup_without_index_raises(storage) -> None:
    _expect_code(lambda: list(storage.index_lookup("users", "id", 1)), E_INDEX_NOT_FOUND)


def test_index_lookup_rejects_wrong_key_type(storage) -> None:
    storage.create_index("idx_users_id", "users", "id")

    _expect_code(
        lambda: list(storage.index_lookup("users", "id", "x")), E_TYPE_MISMATCH
    )


def test_index_methods_reject_unknown_table(storage) -> None:
    _expect_code(
        lambda: list(storage.index_lookup("missing", "id", 1)), E_TABLE_NOT_FOUND
    )


# ---------- 行为正确性 ----------


def test_create_index_builds_from_existing_rows(storage) -> None:
    storage.create_index("idx_users_id", "users", "id")

    assert [row_id for row_id, _values in storage.index_lookup("users", "id", 2)] == [2]


def test_index_lookup_matches_scan_filter(storage) -> None:
    """索引结果必须与 scan 过滤逐行一致（形状也要与 scan 相同）。"""
    storage.create_index("idx_users_name", "users", "name")
    expected = {
        (row_id, values)
        for row_id, values in storage.scan("users")
        if values[1] == "bob"
    }

    actual = {
        (row_id, values)
        for row_id, values in storage.index_lookup("users", "name", "bob")
    }

    assert actual == expected


def test_index_range_matches_scan_filter(storage) -> None:
    storage.create_index("idx_users_id", "users", "id")
    expected = sorted(
        row_id for row_id, values in storage.scan("users") if 2 <= values[0] <= 3
    )

    actual = [
        row_id
        for row_id, _values in storage.index_range("users", "id", 2, 3)
    ]

    assert actual == expected


def test_index_range_supports_no_index_bounds(storage) -> None:
    storage.create_index("idx_users_id", "users", "id")

    assert [
        row_id for row_id, _values in storage.index_range("users", "id", None, None)
    ] == [1, 2, 3]


def test_list_indexes_returns_registered(storage) -> None:
    storage.create_index("idx_users_id", "users", "id")

    infos = storage.list_indexes()

    assert [(info.name, info.table, info.column) for info in infos] == [
        ("idx_users_id", "users", "id")
    ]


def test_list_indexes_filters_by_table(storage) -> None:
    storage.create_table("orders", (ColumnDef("id", SqlType.INT),))
    storage.create_index("idx_users_id", "users", "id")
    storage.create_index("idx_orders_id", "orders", "id")

    assert [info.name for info in storage.list_indexes("orders")] == ["idx_orders_id"]
    assert len(storage.list_indexes()) == 2


def test_drop_index_removes_file_and_registration(server, storage) -> None:
    storage.create_index("idx_users_id", "users", "id")
    index_path = Path(server._data_dir) / "main" / "indexes" / "idx_users_id.idx"
    assert index_path.is_file()

    storage.drop_index("idx_users_id")

    assert not index_path.exists()
    assert storage.list_indexes() == []
    _expect_code(lambda: list(storage.index_lookup("users", "id", 1)), E_INDEX_NOT_FOUND)


def test_index_survives_restart(tmp_path) -> None:
    data_dir = str(tmp_path / "data")
    first = DatabaseServer(data_dir)
    handle = first.connect("main")
    handle.create_table("users", (ColumnDef("id", SqlType.INT),))
    handle.insert("users", (7,))
    handle.create_index("idx_users_id", "users", "id")

    reopened = DatabaseServer(data_dir)

    assert [row_id for row_id, _ in reopened.connect("main").index_lookup("users", "id", 7)] == [1]


def test_multi_handle_shares_index_registry(server) -> None:
    first = server.connect("main")
    first.create_table("users", (ColumnDef("id", SqlType.INT),))
    first.insert("users", (1,))
    first.create_index("idx_users_id", "users", "id")

    second = server.connect("main")

    assert [info.name for info in second.list_indexes()] == ["idx_users_id"]
    assert [row_id for row_id, _ in second.index_lookup("users", "id", 1)] == [1]


# ---------- V2 目录升级（D39） ----------


def _make_v2_directory(tmp_path) -> Path:
    """建库后删掉第三张系统表，模拟 V2 时代的目录。"""
    data_dir = tmp_path / "data"
    server = DatabaseServer(str(data_dir))
    handle = server.connect("main")
    handle.create_table("users", (ColumnDef("id", SqlType.INT),))
    handle.insert("users", (1,))
    (data_dir / "main" / SYS_INDEXES_FILE_NAME).unlink()
    return data_dir


def test_v2_directory_gains_empty_index_system_table(tmp_path) -> None:
    data_dir = _make_v2_directory(tmp_path)

    reopened = DatabaseServer(str(data_dir))
    handle = reopened.connect("main")

    assert (data_dir / "main" / SYS_INDEXES_FILE_NAME).is_file()
    assert handle.list_indexes() == []
    assert handle.list_tables() == ["users"]


def test_v2_bootstrap_is_idempotent(tmp_path) -> None:
    data_dir = _make_v2_directory(tmp_path)

    DatabaseServer(str(data_dir))
    reopened = DatabaseServer(str(data_dir))

    assert reopened.connect("main").list_tables() == ["users"]


def test_v2_bootstrap_preserves_existing_rows(tmp_path) -> None:
    data_dir = _make_v2_directory(tmp_path)

    handle = DatabaseServer(str(data_dir)).connect("main")

    assert [row_id for row_id, _values in handle.scan("users")] == [1]


def test_directory_with_only_index_system_table_is_not_a_database(tmp_path) -> None:
    """只有第三张表的目录既不是合法库，也不能被当成新库静默接管。"""
    data_dir = tmp_path / "data"
    db_dir = data_dir / "ghost"
    db_dir.mkdir(parents=True)
    (db_dir / SYS_INDEXES_FILE_NAME).write_bytes(bytes(4096))

    server = DatabaseServer(str(data_dir))

    _expect_code(lambda: server.connect("ghost"), E_STORAGE)


# ---------- 回滚与缓存 ----------


def test_create_index_register_failure_removes_file(server, storage, monkeypatch) -> None:
    """登记阶段失败必须删掉已建好的索引文件，不留下"文件在、登记无"的孤儿。"""
    from storage.catalog import Catalog

    def _boom(self, *args, **kwargs):
        raise SqlError(E_STORAGE, "injected register failure")

    monkeypatch.setattr(Catalog, "register_index", _boom)
    index_path = Path(server._data_dir) / "main" / "indexes" / "idx_users_id.idx"

    _expect_code(lambda: storage.create_index("idx_users_id", "users", "id"), E_STORAGE)

    assert not index_path.exists()
    assert storage.list_indexes() == []


def test_drop_index_unlink_failure_restores_registration(
    server, storage, monkeypatch
) -> None:
    """unlink 失败时按 D23 恢复登记行，索引仍可用。"""
    storage.create_index("idx_users_id", "users", "id")
    index_path = Path(server._data_dir) / "main" / "indexes" / "idx_users_id.idx"
    original_unlink = Path.unlink

    def _failing_unlink(self, *args, **kwargs):
        if self == index_path:
            raise OSError("injected unlink failure")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _failing_unlink)

    _expect_code(lambda: storage.drop_index("idx_users_id"), E_STORAGE)

    monkeypatch.undo()
    assert index_path.is_file()
    assert [info.name for info in storage.list_indexes()] == ["idx_users_id"]
    assert [row_id for row_id, _ in storage.index_lookup("users", "id", 1)] == [1]


def test_index_pages_count_in_cache_stats(server, storage) -> None:
    before = dict(server.cache_stats)

    storage.create_index("idx_users_id", "users", "id")
    list(storage.index_lookup("users", "id", 1))

    after = dict(server.cache_stats)
    assert after["misses"] + after["hits"] > before["misses"] + before["hits"]


def test_drop_index_then_recreate_works(storage) -> None:
    """删除后重建同名索引必须从头开始，不受缓存残留影响。"""
    storage.create_index("idx_users_id", "users", "id")
    storage.drop_index("idx_users_id")

    storage.create_index("idx_users_id", "users", "id")

    assert [row_id for row_id, _ in storage.index_lookup("users", "id", 3)] == [3]
