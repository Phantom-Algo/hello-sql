"""系统目录 Catalog（V2 D20/D22/D25）。

定位：B 的私有内部记忆。M2 起权威持久化是两张页式系统表
``__sys_tables`` / ``__sys_columns``（物理文件 sys_tables.db / sys_columns.db）；
V1 catalog.json 只作为迁移输入（见 catalog_migration.py）。

不变量：
- 内存形态：tables: dict[表名, tuple[ColumnDef, ...]]，列序 = ordinal 0..N-1；
- 系统表 Schema 由 syscatalog 内置常量定义，不查询 Catalog 自身；
- table_id == __sys_tables 行 row_id（D22）；
- file_name == <表名>.table；系统表不出现在本注册表中；
- 任何结构非法 / 记录与文件不一致 → E_STORAGE；
- 本层不做 SQL 语义检查（D13），只做注册表增删查与系统表持久化。

追踪：公开和回滚操作都经由共享 BufferPool 发送事件，使界面能
同时解释一次 DDL 的内存 Schema 改变和系统表写入。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

from contracts.ast import ColumnDef, SqlType
from contracts.errors import (
    E_DUP_COLUMN,
    E_STORAGE,
    E_TABLE_EXISTS,
    E_TABLE_NOT_FOUND,
    SqlError,
)
from storage.cache import BufferPool
from storage.constants import RESERVED_TABLE_PREFIX, TABLE_FILE_SUFFIX
from storage.syscatalog import open_system_tables, system_table_paths
from storage.trace_hooks import trace_storage_operation


_IDENTIFIER_RE = re.compile(r"[a-z_][a-z0-9_]*\Z")


class Catalog:
    """本库 schema 的内存注册表 + 两张页式系统表持久化（M2）。"""

    def __init__(self, db_dir: str | Path, pool: BufferPool) -> None:
        """绑定数据库目录和共享缓存池，打开但尚未加载系统表。

        Args:
            db_dir: 包含系统目录页文件的单个数据库目录。
            pool: 由 DatabaseServer 持有的进程级共享 BufferPool；
                它同时承载可选的 B 追踪回调。

        Notes:
            构造只建立引用；内存 Schema 由 ``load`` 校验并重建。
        """

        self.db_dir = Path(db_dir)
        self._pool = pool
        self._systems = open_system_tables(self.db_dir, pool)
        self.tables: dict[str, tuple[ColumnDef, ...]] = {}
        self._table_row_ids: dict[str, int] = {}

    # ---- 加载与校验 ----

    @trace_storage_operation("catalog", "load")
    def load(self) -> None:
        """扫描两张系统表并校验，重建内存注册表；任何损坏都抛 E_STORAGE。"""
        by_name: dict[str, int] = {}
        by_id: dict[int, tuple[str, str]] = {}
        for row_id, values in self._systems.tables.scan():
            table_id, table_name, file_name = values
            if type(table_id) is not int:
                raise SqlError(E_STORAGE, "corrupt system catalog: table_id not int")
            if table_id != row_id:
                raise SqlError(
                    E_STORAGE,
                    "corrupt system catalog: table_id does not match row_id",
                )
            self._check_stored_name(table_name)
            if table_name.startswith(RESERVED_TABLE_PREFIX):
                raise SqlError(
                    E_STORAGE,
                    f"corrupt system catalog: reserved table {table_name!r}",
                )
            expected_file = f"{table_name}{TABLE_FILE_SUFFIX}"
            if file_name != expected_file:
                raise SqlError(
                    E_STORAGE,
                    f"corrupt system catalog: bad file_name {file_name!r}",
                )
            if table_name in by_name or table_id in by_id:
                raise SqlError(
                    E_STORAGE,
                    f"corrupt system catalog: duplicate table {table_name!r}",
                )
            by_name[table_name] = table_id
            by_id[table_id] = (table_name, file_name)

        columns_by_id: dict[int, list[tuple[int, str, SqlType]]] = {}
        for _row_id, values in self._systems.columns.scan():
            table_id, ordinal, column_name, type_name = values
            if table_id not in by_id:
                raise SqlError(
                    E_STORAGE,
                    "corrupt system catalog: column row has unknown table_id",
                )
            if type(ordinal) is not int or ordinal < 0:
                raise SqlError(
                    E_STORAGE,
                    "corrupt system catalog: invalid ordinal",
                )
            self._check_stored_name(column_name)
            if not isinstance(type_name, str):
                raise SqlError(
                    E_STORAGE,
                    "corrupt system catalog: column_type not a string",
                )
            try:
                sql_type = SqlType(type_name)
            except ValueError as exc:
                raise SqlError(
                    E_STORAGE,
                    f"corrupt system catalog: unknown type {type_name!r}",
                ) from exc
            columns_by_id.setdefault(table_id, []).append(
                (ordinal, column_name, sql_type)
            )

        loaded: dict[str, tuple[ColumnDef, ...]] = {}
        for table_id, (table_name, _file_name) in by_id.items():
            entries = columns_by_id.get(table_id)
            if not entries:
                raise SqlError(
                    E_STORAGE,
                    f"corrupt system catalog: table {table_name!r} has no columns",
                )
            entries.sort(key=lambda item: item[0])
            if [ordinal for ordinal, _name, _type in entries] != list(
                range(len(entries))
            ):
                raise SqlError(
                    E_STORAGE,
                    f"corrupt system catalog: bad ordinal order for {table_name!r}",
                )
            seen: set[str] = set()
            columns: list[ColumnDef] = []
            for _ordinal, column_name, sql_type in entries:
                if column_name in seen:
                    raise SqlError(
                        E_STORAGE,
                        f"corrupt system catalog: duplicate column {column_name!r}",
                    )
                seen.add(column_name)
                columns.append(ColumnDef(column_name, sql_type))
            loaded[table_name] = tuple(columns)

        expected_files = {file_name for _name, file_name in by_id.values()}
        actual_files = {
            path.name
            for path in self.db_dir.glob(f"*{TABLE_FILE_SUFFIX}")
            if path.is_file()
        }
        if expected_files != actual_files:
            raise SqlError(
                E_STORAGE,
                "corrupt system catalog: table files do not match catalog",
            )

        self.tables = loaded
        self._table_row_ids = by_name

    # ---- 注册表增删查 ----

    @trace_storage_operation("catalog", "register")
    def register(self, name: str, columns: Sequence[ColumnDef]) -> None:
        """登记新表：写两张系统表并 flush；失败时回滚系统行。"""
        if name in self.tables:
            raise SqlError(E_TABLE_EXISTS, f"table already exists: {name}")
        if not columns:
            raise SqlError(E_DUP_COLUMN, f"table {name!r} has no columns")
        seen: set[str] = set()
        for column in columns:
            if column.name in seen:
                raise SqlError(
                    E_DUP_COLUMN,
                    f"table {name!r} has duplicate column {column.name!r}",
                )
            seen.add(column.name)

        table_id = self._insert_table_rows(name, columns)
        self.tables[name] = tuple(columns)
        self._table_row_ids[name] = table_id

    @trace_storage_operation("catalog", "unregister")
    def unregister(self, name: str) -> None:
        """注销表：删两张系统表行并 flush；失败时按快照恢复。"""
        if name not in self.tables:
            raise SqlError(E_TABLE_NOT_FOUND, f"table not found: {name}")
        columns = self.tables[name]
        table_id = self._table_row_ids[name]
        try:
            self._delete_table_rows(table_id)
        except SqlError:
            self._restore_table(name, columns)
            raise
        del self.tables[name]
        del self._table_row_ids[name]

    @trace_storage_operation("catalog", "get")
    def get(self, name: str) -> tuple[ColumnDef, ...]:
        """查表结构（表不存在抛 E_TABLE_NOT_FOUND）。"""
        try:
            return self.tables[name]
        except KeyError as exc:
            raise SqlError(E_TABLE_NOT_FOUND, f"table not found: {name}") from exc

    @trace_storage_operation("catalog", "names")
    def names(self) -> list[str]:
        """返回全部用户表名（稳定排序，契约不承诺顺序）。"""
        return sorted(self.tables)

    @trace_storage_operation("catalog", "flush")
    def flush(self) -> None:
        """把两张系统表的脏页写回磁盘。"""
        for path in system_table_paths(self.db_dir):
            self._pool.flush(path)

    # ---- 内部：系统行写入与回滚 ----

    @trace_storage_operation("catalog", "insert_system_rows")
    def _insert_table_rows(
        self, name: str, columns: Sequence[ColumnDef]
    ) -> int:
        """插入 1 条表行 + N 条列行；失败时清理本次写入，原样抛错。"""
        table_id: int | None = None
        column_row_ids: list[int] = []
        try:
            table_id = self._systems.tables.insert(
                (0, name, f"{name}{TABLE_FILE_SUFFIX}")
            )
            self._systems.tables.update(
                table_id, (table_id, name, f"{name}{TABLE_FILE_SUFFIX}")
            )
            for ordinal, column in enumerate(columns):
                column_row_ids.append(
                    self._systems.columns.insert(
                        (table_id, ordinal, column.name, column.type.value)
                    )
                )
            self.flush()
            return table_id
        except SqlError:
            self._best_effort_delete_rows(table_id, column_row_ids)
            raise

    @trace_storage_operation("catalog", "delete_system_rows")
    def _delete_table_rows(self, table_id: int) -> None:
        """删掉一个表的全部列行与表行，然后 flush。"""
        for row_id, values in list(self._systems.columns.scan()):
            if values[0] == table_id:
                self._systems.columns.delete(row_id)
        self._systems.tables.delete(table_id)
        self.flush()

    @trace_storage_operation("catalog", "restore_table")
    def _restore_table(
        self, name: str, columns: tuple[ColumnDef, ...]
    ) -> None:
        """尽量把注销失败的表恢复回系统表；内存保持原注册状态。"""
        try:
            self._cleanup_rows_by_name(name)
            table_id = self._insert_table_rows(name, columns)
        except SqlError:
            return
        self._table_row_ids[name] = table_id

    @trace_storage_operation("catalog", "cleanup_rows_by_name")
    def _cleanup_rows_by_name(self, name: str) -> None:
        """清掉系统表里某个表名的任何残留行（用于恢复前清场）。"""
        stale_ids: set[int] = set()
        for row_id, values in list(self._systems.tables.scan()):
            if values[1] == name:
                stale_ids.add(row_id)
                self._systems.tables.delete(row_id)
        for row_id, values in list(self._systems.columns.scan()):
            if values[0] in stale_ids:
                self._systems.columns.delete(row_id)
        self.flush()

    @trace_storage_operation("catalog", "rollback_system_rows")
    def _best_effort_delete_rows(
        self, table_id: int | None, column_row_ids: Sequence[int]
    ) -> None:
        """注册失败后的尽力回滚；回滚自身的错误不覆盖原始错误。"""
        for row_id in reversed(list(column_row_ids)):
            try:
                self._systems.columns.delete(row_id)
            except SqlError:
                pass
        if table_id is not None:
            try:
                self._systems.tables.delete(table_id)
            except SqlError:
                pass
        try:
            self.flush()
        except SqlError:
            pass

    # ---- 内部：名字校验 ----

    @staticmethod
    def _check_stored_name(name: str) -> None:
        """持久化数据里的标识符必须是合法小写标识符，否则视为文件损坏。"""
        if not isinstance(name, str) or not _IDENTIFIER_RE.fullmatch(name):
            raise SqlError(E_STORAGE, f"corrupt system catalog: invalid name {name!r}")
