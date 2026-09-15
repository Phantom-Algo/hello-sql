"""M6 遗留项收尾：迁移边界、索引目录一致性、页 0 与节点页损坏。

这些用例对应 PRD §8.1.5 里"暂缓"的三类：

1. **V1→V3 / V2→V3 迁移边界**：迁移后第三张系统表必须齐、旧数据必须可读，
   索引生命周期（建/查/重开）必须立刻可用；
2. **索引目录一致性**：`indexes/*.idx` 与 `__sys_indexes` 必须一一对应——
   多一个孤儿文件、少一个文件都必须在**打开时**报 E_STORAGE，不许静默清理；
3. **页 0 与节点页损坏**：height / root / free_head / 条目长度 / 内节点孩子 /
   根页指向空闲页，全部必须是 E_STORAGE，且不能挂死或返回错行。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_STORAGE, SqlError
from storage import DatabaseServer
from storage.constants import (
    CATALOG_FILE_NAME,
    CATALOG_VERSION,
    INDEX_FILE_SUFFIX,
    INTERIOR_NODE,
    JSON_COLUMNS_KEY,
    JSON_NAME_KEY,
    JSON_TABLES_KEY,
    JSON_TYPE_KEY,
    JSON_VERSION_KEY,
    LEAF_NODE,
    SYS_INDEXES_FILE_NAME,
)
from storage.index import (
    IndexTree,
    entry_payloads,
    new_interior_page,
    node_type,
    rebuild_node,
)
from storage.pager import (
    INDEX_FILE_KIND,
    alloc_page,
    free_page,
    read_page,
    write_page,
)
from storage.pager import create_table_file
from storage.tests.index_audit_util import audit_index


COLUMNS = (ColumnDef("id", SqlType.INT), ColumnDef("pad", SqlType.TEXT))


def _expect_storage(call) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == E_STORAGE


def _index_path(root: Path, name: str = "idx_t_id") -> Path:
    return root / "main" / "indexes" / f"{name}{INDEX_FILE_SUFFIX}"


def _build(root: Path, rows: int = 60, *, index: bool = True):
    server = DatabaseServer(str(root))
    handle = server.connect("main")
    handle.create_table("t", COLUMNS)
    for value in range(rows):
        handle.insert("t", (value, "x" * 20))
    if index:
        handle.create_index("idx_t_id", "t", "id")
    return server, handle


# ---------- 1. 迁移边界 ----------


def test_v2_directory_upgrade_adds_empty_index_table(tmp_path) -> None:
    """V2 目录（缺第三张系统表）打开时补建空索引表，旧数据必须可读。"""

    root = tmp_path / "data"
    server, handle = _build(root, rows=5, index=False)
    del server, handle
    (root / "main" / SYS_INDEXES_FILE_NAME).unlink()  # 退回 V2 形状

    reopened = DatabaseServer(str(root)).connect("main")

    assert reopened.list_tables() == ["t"]
    assert reopened.list_indexes() == []
    assert [values for _row_id, values in reopened.scan("t")] == [
        (value, "x" * 20) for value in range(5)
    ]
    # 补建出来的索引系统表立即可用
    reopened.create_index("idx_t_id", "t", "id")
    assert [info.name for info in reopened.list_indexes()] == ["idx_t_id"]


def _write_v1_json_directory(root: Path) -> None:
    """造一个 V1 目录：catalog.json + 对应的 .table 文件。"""

    db_dir = root / "main"
    db_dir.mkdir(parents=True, exist_ok=True)
    columns = [{"name": "id", "type": "INT"}, {"name": "pad", "type": "TEXT"}]
    (db_dir / CATALOG_FILE_NAME).write_text(
        json.dumps(
            {
                JSON_VERSION_KEY: CATALOG_VERSION,
                JSON_TABLES_KEY: {"t": {JSON_COLUMNS_KEY: columns}},
            }
        ),
        encoding="utf-8",
    )
    create_table_file(db_dir / "t.table")


def test_v1_json_migration_creates_index_system_table(tmp_path) -> None:
    """V1 JSON 迁移后三张系统表齐全，且能立刻建索引。"""

    root = tmp_path / "data"
    _write_v1_json_directory(root)

    handle = DatabaseServer(str(root)).connect("main")

    assert (root / "main" / SYS_INDEXES_FILE_NAME).is_file()
    assert handle.list_indexes() == []
    handle.create_index("idx_t_id", "t", "id")
    assert [info.name for info in handle.list_indexes()] == ["idx_t_id"]


def test_migrated_database_supports_full_index_lifecycle(tmp_path) -> None:
    """迁移 + 建索引 + 写入 + 索引查询 + 重开：整条链路可用。"""

    root = tmp_path / "data"
    _write_v1_json_directory(root)
    handle = DatabaseServer(str(root)).connect("main")
    handle.create_index("idx_t_id", "t", "id")
    for value in range(40):
        handle.insert("t", (value, "x" * 20))

    assert [values[0] for _rid, values in handle.index_lookup("t", "id", 7)] == [7]

    reopened = DatabaseServer(str(root)).connect("main")
    assert [info.name for info in reopened.list_indexes("t")] == ["idx_t_id"]
    assert [values[0] for _rid, values in reopened.index_range("t", "id", 38, 39)] == [
        38,
        39,
    ]


# ---------- 2. 索引目录一致性 ----------


def test_orphan_index_file_is_reported_at_open(tmp_path) -> None:
    """目录里有未登记的 .idx：打开即报 E_STORAGE，绝不静默清理。"""

    root = tmp_path / "data"
    server, handle = _build(root, rows=5)
    del server, handle
    orphan = _index_path(root, "idx_orphan")
    orphan.write_bytes(_index_path(root).read_bytes())

    _expect_storage(lambda: DatabaseServer(str(root)))


def test_missing_index_file_is_reported_at_open(tmp_path) -> None:
    """登记了索引却没有文件：同样在打开时报 E_STORAGE。"""

    root = tmp_path / "data"
    server, handle = _build(root, rows=5)
    del server, handle
    _index_path(root).unlink()

    _expect_storage(lambda: DatabaseServer(str(root)))


# ---------- 3. 页 0 与节点页损坏 ----------


def _corrupt_page0(server: DatabaseServer, path: Path, offset: int, raw: bytes) -> None:
    page0 = bytearray(read_page(server._pool, path, 0, kind=INDEX_FILE_KIND))
    page0[offset : offset + len(raw)] = raw
    write_page(server._pool, path, 0, bytes(page0), kind=INDEX_FILE_KIND)


def _open_tree(server: DatabaseServer, path: Path) -> IndexTree:
    return IndexTree(server._pool, path, COLUMNS[0])


def test_index_page0_height_zero_is_storage_error(tmp_path) -> None:
    root = tmp_path / "data"
    server, _handle = _build(root, rows=5)
    path = _index_path(root)
    _corrupt_page0(server, path, 12, (0).to_bytes(4, "little"))  # height = 0

    _expect_storage(lambda: _open_tree(server, path))


def test_index_page0_height_one_with_interior_root_is_storage_error(tmp_path) -> None:
    root = tmp_path / "data"
    server, _handle = _build(root, rows=5)
    path = _index_path(root)
    page0 = read_page(server._pool, path, 0, kind=INDEX_FILE_KIND)
    root_page = int.from_bytes(page0[8:12], "little")
    page = bytearray(read_page(server._pool, path, root_page, kind=INDEX_FILE_KIND))
    page[0] = INTERIOR_NODE  # 页 0 说 height=1，根却是内节点
    write_page(server._pool, path, root_page, bytes(page), kind=INDEX_FILE_KIND)

    _expect_storage(lambda: _open_tree(server, path))


def test_index_page0_free_head_out_of_range_is_storage_error(tmp_path) -> None:
    root = tmp_path / "data"
    server, _handle = _build(root, rows=5)
    path = _index_path(root)
    _corrupt_page0(server, path, 16, (99999).to_bytes(4, "little"))

    _expect_storage(lambda: _open_tree(server, path))


def test_index_leaf_entry_shorter_than_prefix_is_storage_error(tmp_path) -> None:
    """条目短于定长前缀（rid + 页号）必须在读页时被判损坏。"""

    root = tmp_path / "data"
    server, _handle = _build(root, rows=5)
    path = _index_path(root)
    tree = _open_tree(server, path)
    page0 = read_page(server._pool, path, 0, kind=INDEX_FILE_KIND)
    root_page = int.from_bytes(page0[8:12], "little")
    page = bytearray(read_page(server._pool, path, root_page, kind=INDEX_FILE_KIND))
    rebuild_node(page, LEAF_NODE, (b"\x01\x02\x03\x04",))
    write_page(server._pool, path, root_page, bytes(page), kind=INDEX_FILE_KIND)

    _expect_storage(lambda: tree.lookup(1))


def test_index_interior_root_without_child_is_storage_error(tmp_path) -> None:
    """内节点没有 first_child：审计必须报损坏，而不是无限下降。"""

    root = tmp_path / "data"
    server, _handle = _build(root, rows=5)
    path = _index_path(root)
    page0 = read_page(server._pool, path, 0, kind=INDEX_FILE_KIND)
    root_page = int.from_bytes(page0[8:12], "little")
    write_page(
        server._pool,
        path,
        root_page,
        bytes(new_interior_page(first_child=0)),
        kind=INDEX_FILE_KIND,
    )
    _corrupt_page0(server, path, 12, (2).to_bytes(4, "little"))  # height = 2

    _expect_storage(lambda: audit_index(server._pool, path, COLUMNS[0]))


def test_index_root_pointing_into_free_list_is_storage_error(tmp_path) -> None:
    """根页指向空闲页：审计必须发现（该页不属于可达树，也不该被当数据页）。"""

    root = tmp_path / "data"
    server, _handle = _build(root, rows=5)
    path = _index_path(root)
    spare = alloc_page(server._pool, path, kind=INDEX_FILE_KIND)
    write_page(
        server._pool,
        path,
        spare,
        bytes(new_interior_page(first_child=0)),
        kind=INDEX_FILE_KIND,
    )
    free_page(server._pool, path, spare, kind=INDEX_FILE_KIND)
    _corrupt_page0(server, path, 8, spare.to_bytes(4, "little"))  # root = 空闲页
    _corrupt_page0(server, path, 12, (2).to_bytes(4, "little"))

    _expect_storage(lambda: audit_index(server._pool, path, COLUMNS[0]))
