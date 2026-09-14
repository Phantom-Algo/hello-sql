"""索引一致性（M3）：DML 联动、级联清理与目录损坏校验。

契约 §7.2 要求"任何完成的写入之后，索引与表数据保持一致"——
本文件用 DML 序列与随机模型把它变成可执行的断言。
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_STORAGE, SqlError
from storage import DatabaseServer
from storage.constants import INDEX_DIR_NAME, INDEX_FILE_SUFFIX


@pytest.fixture
def server(tmp_path) -> DatabaseServer:
    return DatabaseServer(str(tmp_path / "data"))


@pytest.fixture
def storage(server: DatabaseServer):
    handle = server.connect("main")
    handle.create_table(
        "users",
        (ColumnDef("id", SqlType.INT), ColumnDef("age", SqlType.INT)),
    )
    return handle


def _expect_code(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


def _index_rids(storage, table: str, column: str, key) -> list[int]:
    return [row_id for row_id, _values in storage.index_lookup(table, column, key)]


def _scan_rids(storage, table: str, position: int, key) -> list[int]:
    return [
        row_id
        for row_id, values in storage.scan(table)
        if values[position] == key
    ]


# ---------- DML 联动 ----------


def test_insert_updates_all_indexes(storage) -> None:
    storage.create_index("idx_id", "users", "id")
    storage.create_index("idx_age", "users", "age")

    row_id = storage.insert("users", (7, 30))

    assert _index_rids(storage, "users", "id", 7) == [row_id]
    assert _index_rids(storage, "users", "age", 30) == [row_id]


def test_delete_updates_all_indexes(storage) -> None:
    storage.create_index("idx_id", "users", "id")
    row_id = storage.insert("users", (7, 30))

    storage.delete_row("users", row_id)

    assert _index_rids(storage, "users", "id", 7) == []
    assert list(storage.index_range("users", "id", None, None)) == []


def test_update_reindexes_only_when_key_changes(storage) -> None:
    storage.create_index("idx_id", "users", "id")
    storage.create_index("idx_age", "users", "age")
    row_id = storage.insert("users", (7, 30))

    storage.update_row("users", row_id, (7, 31))  # id 未变、age 变了

    assert _index_rids(storage, "users", "id", 7) == [row_id]
    assert _index_rids(storage, "users", "age", 30) == []
    assert _index_rids(storage, "users", "age", 31) == [row_id]


def test_update_moves_row_between_keys(storage) -> None:
    storage.create_index("idx_id", "users", "id")
    row_id = storage.insert("users", (7, 30))

    storage.update_row("users", row_id, (8, 30))

    assert _index_rids(storage, "users", "id", 7) == []
    assert _index_rids(storage, "users", "id", 8) == [row_id]


def test_index_matches_scan_after_mixed_dml(storage) -> None:
    storage.create_index("idx_age", "users", "age")
    ids = [storage.insert("users", (value, value % 5)) for value in range(20)]

    storage.delete_row("users", ids[0])
    storage.update_row("users", ids[1], (1, 4))

    for key in range(5):
        assert sorted(_index_rids(storage, "users", "age", key)) == sorted(
            _scan_rids(storage, "users", 1, key)
        )


def test_drop_table_removes_index_files_and_rows(server, storage) -> None:
    storage.create_index("idx_id", "users", "id")
    index_path = Path(server._data_dir) / "main" / INDEX_DIR_NAME / "idx_id.idx"
    assert index_path.is_file()

    storage.drop_table("users")

    assert not index_path.exists()
    assert storage.list_indexes() == []
    # 重新建同名表后不应残留旧索引登记。
    storage.create_table("users", (ColumnDef("id", SqlType.INT),))
    assert storage.list_indexes() == []


def test_index_and_dml_survive_restart(tmp_path) -> None:
    data_dir = str(tmp_path / "data")
    first = DatabaseServer(data_dir).connect("main")
    first.create_table(
        "users", (ColumnDef("id", SqlType.INT), ColumnDef("age", SqlType.INT))
    )
    first.create_index("idx_age", "users", "age")
    for value in range(30):
        first.insert("users", (value, value % 4))
    first.delete_row("users", 2)
    first.update_row("users", 3, (3, 9))

    reopened = DatabaseServer(data_dir).connect("main")

    # 删除 rid=2、把 rid=3 的 age 改成 9（age 原本只取 0..3，9 只可能来自这次更新）
    assert sorted(row_id for row_id, _ in reopened.index_lookup("users", "age", 9)) == [3]
    assert len(list(reopened.index_range("users", "age", None, None))) == 29


def test_random_dml_keeps_index_consistent_with_scan(storage) -> None:
    """固定种子随机 DML 后，索引查询结果与 scan 过滤逐项一致。"""
    storage.create_index("idx_age", "users", "age")
    rng = random.Random(20260914)
    live: set[int] = set()

    for _ in range(300):
        choice = rng.random()
        if choice < 0.5 or not live:
            row_id = storage.insert("users", (rng.randrange(50), rng.randrange(6)))
            live.add(row_id)
        elif choice < 0.75:
            row_id = rng.choice(sorted(live))
            values = (rng.randrange(50), rng.randrange(6))
            storage.update_row("users", row_id, values)
        else:
            row_id = rng.choice(sorted(live))
            storage.delete_row("users", row_id)
            live.discard(row_id)

    for key in range(6):
        assert sorted(_index_rids(storage, "users", "age", key)) == sorted(
            _scan_rids(storage, "users", 1, key)
        )


# ---------- 目录损坏校验 ----------


def _indexes_engine(storage):
    return storage._live_catalog()._systems.indexes


def test_load_rejects_missing_index_file(server, storage) -> None:
    storage.create_index("idx_id", "users", "id")
    path = Path(server._data_dir) / "main" / INDEX_DIR_NAME / "idx_id.idx"
    path.unlink()

    _expect_code(lambda: DatabaseServer(str(server._data_dir)), E_STORAGE)


def test_load_rejects_orphan_index_file(server, storage) -> None:
    storage.create_index("idx_id", "users", "id")
    orphan = (
        Path(server._data_dir)
        / "main"
        / INDEX_DIR_NAME
        / f"orphan{INDEX_FILE_SUFFIX}"
    )
    orphan.write_bytes(bytes(8192))

    _expect_code(lambda: DatabaseServer(str(server._data_dir)), E_STORAGE)


def test_load_rejects_index_row_with_unknown_table_id(server, storage) -> None:
    engine = _indexes_engine(storage)
    engine.insert(("idx_ghost", 999, "id", f"idx_ghost{INDEX_FILE_SUFFIX}"))
    storage._pool.flush(storage._live_catalog().db_dir / "sys_indexes.db")

    _expect_code(lambda: DatabaseServer(str(server._data_dir)), E_STORAGE)


def test_load_rejects_duplicate_index_on_same_table_column(server, storage) -> None:
    storage.create_index("idx_a", "users", "id")
    engine = _indexes_engine(storage)
    engine.insert(("idx_b", 1, "id", f"idx_b{INDEX_FILE_SUFFIX}"))
    storage._pool.flush(storage._live_catalog().db_dir / "sys_indexes.db")

    _expect_code(lambda: DatabaseServer(str(server._data_dir)), E_STORAGE)


def test_load_rejects_file_name_mismatch(server, storage) -> None:
    storage.create_index("idx_id", "users", "id")
    engine = _indexes_engine(storage)
    for row_id, values in list(engine.scan()):
        if values[0] == "idx_id":
            engine.delete(row_id)
    engine.insert(("idx_id", 1, "id", "wrong_name.idx"))
    storage._pool.flush(storage._live_catalog().db_dir / "sys_indexes.db")

    _expect_code(lambda: DatabaseServer(str(server._data_dir)), E_STORAGE)
