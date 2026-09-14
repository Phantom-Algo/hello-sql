"""Catalog 加载/迁移入口（V2 D24/D25）。

M2 起页式系统表是权威目录；V1 catalog.json 只作为一次性迁移输入。
本文件集中放“页式权威判定 + V1 JSON 迁移状态机”，catalog.py 不碰 JSON。

状态机（load_or_migrate）：
1. 两张系统表齐全且校验通过 → 页式权威；若有残留 JSON，改名收尾；
2. 系统表齐全但校验失败 + 有可读 JSON → 视为中断迁移，清理系统表后重迁；
3. 系统表不齐 + 有 JSON → 清理半成品后从 JSON 迁移；
4. 没有可用 JSON 且系统表不完整/损坏 → E_STORAGE。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_STORAGE, SqlError

from storage.cache import BufferPool
from storage.catalog import Catalog
from storage.constants import (
    CATALOG_FILE_NAME,
    CATALOG_VERSION,
    JSON_COLUMNS_KEY,
    JSON_NAME_KEY,
    JSON_TABLES_KEY,
    JSON_TYPE_KEY,
    JSON_VERSION_KEY,
    LEGACY_MIGRATED_FILE_NAME,
    RESERVED_TABLE_PREFIX,
)
from storage.syscatalog import (
    create_empty_system_catalog,
    system_table_paths,
)
from storage.pager import create_table_file


_IDENTIFIER_RE = re.compile(r"[a-z_][a-z0-9_]*\Z")


def load_or_migrate(db_dir: str | Path, pool: BufferPool) -> Catalog:
    """加载页式目录；必要时执行 V1 JSON 一次性迁移。"""
    root = Path(db_dir)
    sys_paths = system_table_paths(root)
    tables_path, columns_path = sys_paths.tables, sys_paths.columns
    json_path = root / CATALOG_FILE_NAME
    system_complete = tables_path.is_file() and columns_path.is_file()

    if system_complete:
        if not sys_paths.indexes.is_file():
            # V2 目录（只有两张系统表）：补建空的索引系统表（D39）。
            # 放在 load 之前，使"缺第三张表"在同一个加载路径里被消化。
            create_table_file(sys_paths.indexes)
        try:
            catalog = Catalog(root, pool)
            catalog.load()
        except SqlError:
            if not json_path.is_file():
                raise
            # 中断的迁移：页式半成品不可信，清掉后用 JSON 重建。
            _drop_system_files(root, pool)
            return _migrate_from_json(root, pool, json_path)
        if json_path.is_file():
            _rename_legacy_json(json_path, root)
        return catalog

    if (tables_path.exists() or columns_path.exists()) and not json_path.is_file():
        raise SqlError(E_STORAGE, f"system catalog incomplete: {root}")
    if json_path.is_file():
        _drop_system_files(root, pool)
        return _migrate_from_json(root, pool, json_path)
    raise SqlError(E_STORAGE, f"system catalog missing: {root}")


def _migrate_from_json(
    root: Path, pool: BufferPool, json_path: Path
) -> Catalog:
    """从 V1 JSON 建页式系统表；失败清理半成品并保留 JSON。"""
    legacy = _load_v1_json(json_path)
    create_empty_system_catalog(root)
    try:
        catalog = Catalog(root, pool)
        for name, columns in legacy.items():
            catalog.register(name, columns)
        reloaded = Catalog(root, pool)
        reloaded.load()
        if reloaded.tables != legacy:
            raise SqlError(E_STORAGE, "migrated catalog does not match V1 JSON")
    except SqlError:
        _drop_system_files(root, pool)
        raise
    _rename_legacy_json(json_path, root)
    return reloaded


def _rename_legacy_json(json_path: Path, root: Path) -> None:
    """迁移成功后把 JSON 改名；目标已存在时按 os.replace 覆盖（幂等）。"""
    try:
        os.replace(json_path, root / LEGACY_MIGRATED_FILE_NAME)
    except OSError as exc:
        raise SqlError(E_STORAGE, f"cannot archive legacy catalog: {json_path}") from exc


def _drop_system_files(root: Path, pool: BufferPool) -> None:
    """清理三张系统表文件与其缓存帧（用于失败回滚/中断重试）。"""
    for path in system_table_paths(root).all():
        pool.discard(path)
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise SqlError(E_STORAGE, f"cannot remove system catalog: {path}") from exc


def _load_v1_json(path: Path) -> dict[str, tuple[ColumnDef, ...]]:
    """读取并严格校验 V1 catalog.json；任何非法形态都 E_STORAGE。"""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SqlError(E_STORAGE, f"cannot read legacy catalog {path}") from exc

    if not isinstance(doc, dict):
        raise SqlError(E_STORAGE, f"corrupt legacy catalog {path}: not an object")
    if doc.get(JSON_VERSION_KEY) != CATALOG_VERSION:
        raise SqlError(E_STORAGE, f"corrupt legacy catalog {path}: bad version")
    raw_tables = doc.get(JSON_TABLES_KEY)
    if not isinstance(raw_tables, dict):
        raise SqlError(E_STORAGE, f"corrupt legacy catalog {path}: tables invalid")

    loaded: dict[str, tuple[ColumnDef, ...]] = {}
    for table_name, meta in raw_tables.items():
        _check_stored_name(table_name)
        if table_name.startswith(RESERVED_TABLE_PREFIX):
            raise SqlError(
                E_STORAGE,
                f"corrupt legacy catalog {path}: reserved table {table_name!r}",
            )
        if not isinstance(meta, dict):
            raise SqlError(
                E_STORAGE, f"corrupt legacy catalog {path}: table meta invalid"
            )
        raw_columns = meta.get(JSON_COLUMNS_KEY)
        if not isinstance(raw_columns, list) or not raw_columns:
            raise SqlError(
                E_STORAGE, f"corrupt legacy catalog {path}: empty columns"
            )
        seen: set[str] = set()
        columns: list[ColumnDef] = []
        for item in raw_columns:
            if not isinstance(item, dict):
                raise SqlError(
                    E_STORAGE, f"corrupt legacy catalog {path}: column invalid"
                )
            try:
                column_name = item[JSON_NAME_KEY]
                type_name = item[JSON_TYPE_KEY]
            except KeyError as exc:
                raise SqlError(
                    E_STORAGE, f"corrupt legacy catalog {path}: column field missing"
                ) from exc
            _check_stored_name(column_name)
            if column_name in seen:
                raise SqlError(
                    E_STORAGE, f"corrupt legacy catalog {path}: duplicate column"
                )
            seen.add(column_name)
            try:
                sql_type = SqlType(type_name)
            except (TypeError, ValueError) as exc:
                raise SqlError(
                    E_STORAGE, f"corrupt legacy catalog {path}: unknown type"
                ) from exc
            columns.append(ColumnDef(column_name, sql_type))
        loaded[table_name] = tuple(columns)
    return loaded


def _check_stored_name(name: str) -> None:
    """V1 JSON 里的标识符必须是合法小写标识符，否则视为损坏。"""
    if not isinstance(name, str) or not _IDENTIFIER_RE.fullmatch(name):
        raise SqlError(E_STORAGE, f"corrupt legacy catalog: invalid name {name!r}")
