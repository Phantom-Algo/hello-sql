"""模块 B：存储层（对外入口就是本文件）。

内部设计规格：.codex/docs/storage/storage_prd.md（D01–D18）+
.codex/docs/storage/storage_prd_v2.md（D19–D26，V2 页式 Catalog）。
对外契约：contracts V2.0 —— 本文件里的 DatabaseServer 与 Storage
是 B 方法契约的唯一代码真相；参数 / 返回 / 错误码以契约为准。

内部架构（D01/D20，单向依赖，禁止反向 import）：

    DatabaseServer（库级：目录 + 每库共享 catalog 注册表 + 每表共享 engine）
      │    Storage 是 connect() 返回的薄视图，不持有独立内存快照
      ├─ catalog.py   每库 schema：内存注册表 ⇄ 两张页式系统表（D20/D22）
      ├─ catalog_migration.py  V1 catalog.json → 页式系统表一次性迁移（D24）
      ├─ syscatalog.py 系统表内置 Schema：建/开两张系统表（D20/D21）
      └─ engine.py    行级执行：记录编解码、row_id、页内空间（D07/D08/D15）
             └─ pager.py   页级原语：页 0、alloc/free、页 I/O（D04/D05）
                    └─ cache.py   BufferPool：LRU、pin/dirty、flush（D09–D11/D16–D18）

红线：本目录禁止 import compiler / runner；只允许 import contracts 与标准库。
B 不认识 SQL / AST / 执行计划；C 不知道 B 的文件格式与内部结构。
可观测性通过构造 `DatabaseServer` 时的可选 `trace_sink` 注入；
回调只接收普通字典，B 仍然不反向导入 UI，也不会为追踪重放业务操作。

错误归属（M0 边界）：
- 公开方法第一道闸：库/表名格式校验 → E_BAD_ARG（D13，先于一切存在性检查）；
- 库级：main 保护 E_DATABASE_IN_USE；目录/有效库判定决定 EXISTS/NOT_FOUND；
- “目录在但 catalog 损坏/缺失”属于存储损坏 → E_STORAGE（由 Catalog 抛）。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Iterator, Sequence

from contracts.ast import ColumnDef, SqlType, Value
from contracts.errors import (
    E_BAD_ARG,
    E_COLUMN_NOT_FOUND,
    E_DATABASE_EXISTS,
    E_DATABASE_IN_USE,
    E_DATABASE_NOT_FOUND,
    E_INDEX_EXISTS,
    E_INDEX_NOT_FOUND,
    E_STORAGE,
    E_TABLE_EXISTS,
    E_TYPE_MISMATCH,
    E_VALUE_COUNT,
    SqlError,
)
from contracts.storage import IndexInfo, Row, RowId, TableInfo, TableStats

from storage.cache import BufferPool
from storage.catalog import Catalog
from storage.catalog_migration import load_or_migrate
from storage.constants import (
    CATALOG_FILE_NAME,
    DEFAULT_CACHE_CAPACITY,
    INDEX_DIR_NAME,
    INDEX_FILE_SUFFIX,
    RESERVED_TABLE_PREFIX,
    SYS_COLUMNS_FILE_NAME,
    SYS_INDEXES_FILE_NAME,
    SYS_TABLES_FILE_NAME,
    TABLE_FILE_SUFFIX,
)
from storage.engine import TableEngine
from storage.index import IndexTree, create_index_file
from storage.pager import create_table_file
from storage.syscatalog import create_empty_system_catalog
from storage.stats import TableStatsProvider
from storage.trace_hooks import StorageTraceSink
from storage.valuecodec import normalize_value


_IDENTIFIER_RE = re.compile(r"[a-z_][a-z0-9_]*\Z")


def _validate_identifier(name: str) -> None:
    """库名/表名边界：非空、小写、匹配 [a-z_][a-z0-9_]*，否则 E_BAD_ARG。"""
    if not isinstance(name, str) or not _IDENTIFIER_RE.fullmatch(name):
        raise SqlError(E_BAD_ARG, f"invalid name: {name!r}")


def _validate_table_name(name: str) -> None:
    """表名边界：普通标识符规则 + __sys_ 前缀为 B 保留（E_BAD_ARG）。"""
    _validate_identifier(name)
    if name.startswith(RESERVED_TABLE_PREFIX):
        raise SqlError(E_BAD_ARG, f"reserved table name: {name}")


def _validate_index_name(name: str) -> None:
    """索引名边界：与表名同一套规则（含 __sys_ 前缀保留，E_BAD_ARG）。"""
    _validate_identifier(name)
    if name.startswith(RESERVED_TABLE_PREFIX):
        raise SqlError(E_BAD_ARG, f"reserved index name: {name}")


def _column_named(
    columns: Sequence[ColumnDef], name: str
) -> ColumnDef:
    """按列名取列定义；不存在 → E_COLUMN_NOT_FOUND。"""
    for column in columns:
        if column.name == name:
            return column
    raise SqlError(E_COLUMN_NOT_FOUND, f"column not found: {name}")


def _materialize_columns(columns: Sequence[ColumnDef]) -> tuple[ColumnDef, ...]:
    """把 columns 物化成元组：调用方传一次性迭代器也能正确取两遍（防御）。"""
    try:
        return tuple(columns)
    except TypeError:
        raise SqlError(E_BAD_ARG, "columns must be an iterable of ColumnDef") from None


def _validate_columns(columns: Sequence[ColumnDef]) -> None:
    """列定义自身边界（拍板：一律 E_BAD_ARG，不新增错误码）。

    列名与库名/表名同属标识符，格式非法归 E_BAD_ARG；type 不是 SqlType
    属于“参数本身非法”，不是入库值放错列（不能用 E_TYPE_MISMATCH）。
    空列/重复列仍由 Catalog.register 抛 E_DUP_COLUMN（契约固定）。
    """
    for column in columns:
        if not isinstance(column, ColumnDef):
            raise SqlError(
                E_BAD_ARG,
                f"invalid column definition: {type(column).__name__}",
            )
        if not isinstance(column.name, str) or not _IDENTIFIER_RE.fullmatch(
            column.name
        ):
            raise SqlError(E_BAD_ARG, f"invalid column name: {column.name!r}")
        if not isinstance(column.type, SqlType):
            raise SqlError(
                E_BAD_ARG,
                f"invalid type for column {column.name!r}: "
                f"{type(column.type).__name__}",
            )


def _normalize_values(
    columns: Sequence[ColumnDef], values: Sequence[Value]
) -> tuple[Value, ...]:
    """公开方法的值边界检查（§3.2）：个数 → 类型 → REAL 归一化为 float。

    返回归一化后的值元组，供 engine 直接编码；校验失败抛契约错误码。
    单值规则抽到 storage/valuecodec.normalize_value，索引键复用同一套，
    避免"表一套、索引一套"两个校验实现。
    """
    if len(values) != len(columns):
        raise SqlError(
            E_VALUE_COUNT,
            f"expected {len(columns)} values, got {len(values)}",
        )
    return tuple(
        normalize_value(column, value) for column, value in zip(columns, values)
    )


class DatabaseServer:
    """库层：管理 data_dir 下的全部数据库，并持有进程级共享状态（D09/A 方案）。

    不变量：
    - 构造时创建 data_dir，并自动创建默认库 main（永存、不可删）；
    - list_databases() 恒包含 main；
    - 每个库 = data_dir/<库名>/ 目录 + 两张页式系统表（D03/D20）；
    - 一个“候选库”= 目录存在且含任一目录工件（页式系统表或 V1 JSON）；
    - 一个库只保留一份内存 Catalog、一张表只保留一份 TableEngine：
      connect() 返回的 Storage 是薄视图，不持有独立快照（方案 A 拍板）；
    - 同一 DatabaseServer 的所有 Storage 共享 BufferPool（D09/D16）。
    """

    def __init__(
        self,
        data_dir: str | Path,
        trace_sink: StorageTraceSink | None = None,
    ) -> None:
        """创建数据目录、共享 BufferPool 与默认 main 数据库。

        Args:
            data_dir: 所有数据库目录的根路径。
            trace_sink: 可选 B 内部调用追踪回调。它被注入进共享
                BufferPool，Catalog、Cache、Pager 与 Engine 均通过该池
                发布记录，存储层本身不导入 UI。

        Raises:
            SqlError: 数据目录或 main 数据库无法创建或校验时抛出。
        """
        self._data_dir = Path(data_dir)
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SqlError(
                E_STORAGE, f"cannot create data dir: {self._data_dir}"
            ) from exc
        self._pool = BufferPool(DEFAULT_CACHE_CAPACITY, trace_sink=trace_sink)
        self._catalogs: dict[Path, Catalog] = {}  # 库目录(绝对) → Catalog
        self._engines: dict[tuple[Path, str], TableEngine] = {}  # (库,表) → engine
        self._index_trees: dict[tuple[Path, str], IndexTree] = {}  # (库,索引) → 树
        self._stats: dict[tuple[Path, str], TableStatsProvider] = {}  # (库,表) → 统计

        main_dir = self._data_dir / "main"
        if main_dir.exists():
            # main 目录已存在：必须有目录工件且可完整校验，损坏不得静默重建。
            if not self._is_valid_db_dir(main_dir):
                raise SqlError(
                    E_STORAGE,
                    f"main database dir has no catalog artifacts: {main_dir}",
                )
            catalog = load_or_migrate(main_dir, self._pool)
            self._catalogs[main_dir.absolute()] = catalog
        else:
            created = False
            try:
                main_dir.mkdir()
                created = True
                create_empty_system_catalog(main_dir)
                catalog = Catalog(main_dir, self._pool)
                catalog.load()
            except SqlError:
                if created:
                    shutil.rmtree(main_dir, ignore_errors=True)
                raise
            except OSError as exc:
                if created:
                    shutil.rmtree(main_dir, ignore_errors=True)
                raise SqlError(
                    E_STORAGE, f"cannot create main db dir: {main_dir}"
                ) from exc
            self._catalogs[main_dir.absolute()] = catalog

    # ---- 内部辅助 ----

    def _db_path(self, name: str) -> Path:
        """把已通过边界校验的数据库名映射为根目录下的路径。"""

        return self._data_dir / name

    @staticmethod
    def _has_catalog_artifacts(db_dir: Path) -> bool:
        """目录工件：三张系统表文件任一，或 V1 catalog.json。"""
        return any(
            (db_dir / file_name).is_file()
            for file_name in (
                SYS_TABLES_FILE_NAME,
                SYS_COLUMNS_FILE_NAME,
                SYS_INDEXES_FILE_NAME,
                CATALOG_FILE_NAME,
            )
        )

    @staticmethod
    def _is_valid_db_dir(db_dir: Path) -> bool:
        """候选库 = 目录存在且含任一目录工件（完整校验在 load 时做）。"""
        return db_dir.is_dir() and DatabaseServer._has_catalog_artifacts(db_dir)

    def _get_catalog(self, db_dir: Path) -> Catalog:
        """返回该库的内存 Catalog；第一次接触时从磁盘载入并缓存。"""
        db_key = db_dir.absolute()
        catalog = self._catalogs.get(db_key)
        if catalog is None:
            catalog = load_or_migrate(db_dir, self._pool)
            self._catalogs[db_key] = catalog
        return catalog

    def _purge_db_state(self, db_dir: Path) -> None:
        """删库时摘掉该库的 catalog 与全部 engine（共享注册表保持一致）。"""
        db_key = db_dir.absolute()
        self._catalogs.pop(db_key, None)
        stale_engines = [key for key in self._engines if key[0] == db_key]
        for key in stale_engines:
            del self._engines[key]
        stale_trees = [key for key in self._index_trees if key[0] == db_key]
        for key in stale_trees:
            del self._index_trees[key]
        stale_stats = [key for key in self._stats if key[0] == db_key]
        for key in stale_stats:
            del self._stats[key]

    # ---- 库级公开方法 ----

    def create_database(self, name: str) -> None:
        """建库：目录 + 两张空系统表；失败时尽量回滚已建目录。"""
        _validate_identifier(name)
        db_dir = self._db_path(name)
        if db_dir.is_dir():
            raise SqlError(E_DATABASE_EXISTS, f"database already exists: {name}")
        try:
            db_dir.mkdir()
        except OSError as exc:
            raise SqlError(
                E_STORAGE, f"cannot create database dir: {db_dir}"
            ) from exc
        try:
            create_empty_system_catalog(db_dir)
            catalog = Catalog(db_dir, self._pool)
            catalog.load()
        except SqlError:
            # 目录建了但系统表没写成 → 回滚目录，保持“库=目录+系统表”不变式。
            try:
                shutil.rmtree(db_dir, ignore_errors=True)
            except OSError:
                pass
            raise
        self._catalogs[db_dir.absolute()] = catalog

    def drop_database(self, name: str) -> None:
        """删库：级联删目录；main 由 B 拦 E_DATABASE_IN_USE。

        M0 无缓存帧；M3 起在删目录前先 discard 该库全部帧（D11）。
        """
        _validate_identifier(name)
        if name == "main":
            raise SqlError(E_DATABASE_IN_USE, "cannot drop default database main")
        db_dir = self._db_path(name)
        if not self._is_valid_db_dir(db_dir):
            raise SqlError(E_DATABASE_NOT_FOUND, f"database not found: {name}")
        try:
            self._pool.discard(db_dir)  # 删库前丢弃该库全部缓存帧（D11）
            self._purge_db_state(db_dir)  # 摘掉共享 catalog/engine，旧句柄随之失效
            shutil.rmtree(db_dir)
        except OSError as exc:
            raise SqlError(E_STORAGE, f"cannot drop database dir: {db_dir}") from exc

    @property
    def cache_stats(self) -> dict[str, int | float]:
        """只读命中统计快照（D18；B 内部属性，不进 13 方法契约）。"""
        return self._pool.stats

    def list_databases(self) -> list[str]:
        """返回所有有效库名（排序；契约不承诺顺序，排序只是稳定输出）。"""
        try:
            names = [
                entry.name
                for entry in self._data_dir.iterdir()
                if self._is_valid_db_dir(entry)
            ]
        except OSError as exc:
            raise SqlError(
                E_STORAGE, f"cannot list databases under {self._data_dir}"
            ) from exc
        return sorted(names)

    def has_database(self, name: str) -> bool:
        """库是否存在（先过名称边界，再查有效库目录）。"""
        _validate_identifier(name)
        return self._is_valid_db_dir(self._db_path(name))

    def connect(self, name: str) -> Storage:
        """连接库：完整校验磁盘系统表，返回共享该库内存真相的薄 Storage。"""
        _validate_identifier(name)
        db_dir = self._db_path(name)
        if not self._is_valid_db_dir(db_dir):
            raise SqlError(E_DATABASE_NOT_FOUND, f"database not found: {name}")
        # 每次 connect 都重扫一遍系统表：外部损坏不许被内存缓存掩盖；
        # 校验结果不替换共享对象——本 server 内存真相优先（M0 语义保留）。
        catalog = self._get_catalog(db_dir)
        Catalog(db_dir, self._pool).load()
        return Storage(self, db_dir.absolute(), catalog)


class Storage:
    """表层：一个实例 = 绑定某个库的薄连接视图（方案 A 拍板）。

    catalog 与 TableEngine 的内存真相都在 DatabaseServer 的共享注册表里，
    connect() 只是取引用；同库多句柄看到的是同一份状态，drop/重建后
    新 schema 立即可见，旧 rid→页 映射不会残留在别的句柄上。
    drop_database 会把共享状态摘除，旧句柄再调用任何方法都会 E_STORAGE。

    不变量（对外可见行为）：
    - 表级方法的 name 一律是“当前库下的小写表名”；
    - A 负责把标识符转小写，B 边界仍按 [a-z_][a-z0-9_]* 校验（D13）；
    - create_table 的列定义自身非法 → E_BAD_ARG（先于建文件，不留孤儿）；
    - 每个“会改数据”的公开方法返回前，本方法涉及的脏页已 flush（D11）；
    - row_id 只在“本次运行、scan 之后、update/delete 之前”有效（契约）；
    - 每张表一个共享 TableEngine（含 rid→页 映射），drop_table 时丢弃（D08）。
    """

    def __init__(self, server: DatabaseServer, db_key: Path, catalog: Catalog) -> None:
        """绑定所属 DatabaseServer、库目录（绝对）与共享 Catalog 引用。"""
        self._server = server
        self._db_path = db_key
        self._catalog = catalog

    @property
    def _pool(self) -> BufferPool:
        """缓存池始终取 server 当前的（库对象共享，不另存引用）。"""
        return self._server._pool

    # ---- 内部辅助 ----

    def _live_catalog(self) -> Catalog:
        """本句柄绑定的 Catalog 必须仍是 server 注册表里的当前真相。"""
        current = self._server._catalogs.get(self._db_path)
        if current is not self._catalog:
            raise SqlError(
                E_STORAGE,
                f"storage handle is stale: database {self._db_path.name} "
                "was dropped or recreated",
            )
        return current

    def _table_file_path(self, name: str) -> Path:
        """把已规范化的表名映射为当前数据库的物理表文件路径。"""

        return self._db_path / f"{name}{TABLE_FILE_SUFFIX}"

    def _engine_for(self, name: str, columns: Sequence[ColumnDef]) -> TableEngine:
        """惰性取得/创建该表的共享引擎；drop_table 会把它从注册表移除。"""
        engine_key = (self._db_path, name)
        engine = self._server._engines.get(engine_key)
        if engine is None:
            engine = TableEngine(self._table_file_path(name), columns, self._pool)
            self._server._engines[engine_key] = engine
        return engine

    def create_table(self, name: str, columns: Sequence[ColumnDef]) -> None:
        """建表：建用户表文件 → 写系统表行 → flush（D23）。"""
        _validate_table_name(name)
        catalog = self._live_catalog()
        if name in catalog.tables:
            raise SqlError(E_TABLE_EXISTS, f"table already exists: {name}")
        columns = _materialize_columns(columns)
        _validate_columns(columns)
        table_path = self._table_file_path(name)
        try:
            create_table_file(table_path)
        except SqlError:
            try:
                table_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        try:
            catalog.register(name, columns)  # 空列/重复列 → E_DUP_COLUMN
        except SqlError:
            # 注册失败按“整表影响范围”回滚：删掉刚建的用户表文件。
            try:
                table_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def drop_table(self, name: str) -> None:
        """删表：先删系统表行，再删用户文件；失败按整表恢复（D23）。"""
        _validate_table_name(name)
        catalog = self._live_catalog()
        columns = catalog.get(name)  # 缺表 → E_TABLE_NOT_FOUND
        table_path = self._table_file_path(name)
        engine_key = (self._db_path, name)
        # 先级联清理该表的索引（登记行 + 文件 + 缓存），再摘表登记；
        # 否则会留下引用已消失 table_id 的索引行，启动校验直接 E_STORAGE。
        for info in catalog.indexes_for_table(name):
            index_path = self._index_file_path(info.name)
            catalog.unregister_index(info.name)
            self._pool.discard(index_path)
            self._server._index_trees.pop((self._db_path, info.name), None)
            try:
                index_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise SqlError(
                    E_STORAGE, f"cannot delete index file: {index_path}"
                ) from exc
        catalog.unregister(name)
        self._pool.discard(table_path)  # 删文件前丢帧（D11）
        try:
            table_path.unlink()
        except FileNotFoundError:
            pass  # 文件缺失视为可清理孤儿，drop 成功
        except OSError as exc:
            # unlink 失败：目录行已删，用内存 schema 整表恢复，保证表仍可用。
            try:
                catalog.register(name, columns)
            except SqlError:
                pass
            raise SqlError(
                E_STORAGE, f"cannot delete table file: {table_path}"
            ) from exc
        self._server._engines.pop(engine_key, None)
        self._server._stats.pop((self._db_path, name), None)

    def list_tables(self) -> list[str]:
        """只读 catalog，返回本库表名（排序稳定，契约不承诺顺序）。"""
        return self._live_catalog().names()

    def describe(self, name: str) -> TableInfo:
        """只读 catalog 返回 TableInfo（C 语义检查的唯一入口，§9.1）。"""
        _validate_table_name(name)
        return TableInfo(name=name, columns=self._live_catalog().get(name))

    def insert(self, name: str, values: Sequence[Value]) -> RowId:
        """追加一行：边界校验 → engine 落页 → 返回新 row_id（§5.3）。"""
        _validate_table_name(name)
        catalog = self._live_catalog()
        columns = catalog.get(name)
        normalized = _normalize_values(columns, values)
        engine = self._engine_for(name, columns)
        row_id = engine.insert(normalized)
        for info, column, position in self._index_targets(name):
            self._index_tree(info, column).insert(normalized[position], row_id)
        self._pool.flush(self._table_file_path(name))  # 方法末 flush（D11）
        self._flush_indexes(name)
        self._note_stats_insert(name, normalized)
        return row_id

    def scan(self, name: str) -> Iterator[Row]:
        """整表行迭代器：调用时立刻校验；行错误在迭代时抛（§10）。"""
        _validate_table_name(name)
        columns = self._live_catalog().get(name)
        engine = self._engine_for(name, columns)
        return engine.scan()

    def update_row(self, name: str, row_id: RowId, values: Sequence[Value]) -> None:
        """整行替换：值边界检查 → engine 定位并更新（D08/D15）。"""
        _validate_table_name(name)
        columns = self._live_catalog().get(name)
        normalized = _normalize_values(columns, values)
        engine = self._engine_for(name, columns)
        old_values = engine.get_row(row_id)[1]
        engine.update(row_id, normalized)
        for info, column, position in self._index_targets(name):
            if old_values[position] != normalized[position]:
                tree = self._index_tree(info, column)
                tree.delete(old_values[position], row_id)
                tree.insert(normalized[position], row_id)
        self._pool.flush(self._table_file_path(name))  # 方法末 flush（D11）
        self._flush_indexes(name)
        self._note_stats_update(name, old_values, normalized)

    def delete_row(self, name: str, row_id: RowId) -> None:
        """删除一行：engine 定位 → 移除槽 → 页内紧凑（D15）。"""
        _validate_table_name(name)
        columns = self._live_catalog().get(name)
        engine = self._engine_for(name, columns)
        old_values = engine.get_row(row_id)[1]
        for info, column, position in self._index_targets(name):
            self._index_tree(info, column).delete(old_values[position], row_id)
        engine.delete(row_id)
        self._pool.flush(self._table_file_path(name))  # 方法末 flush（D11）
        self._flush_indexes(name)
        self._note_stats_delete(name, old_values)

    # ---- 索引（V3 M3/D27–D46） ----

    @property
    def _index_dir(self) -> Path:
        """本库的索引目录（D27：每库一个 indexes/ 子目录）。"""
        return self._db_path / INDEX_DIR_NAME

    def _index_file_path(self, name: str) -> Path:
        return self._index_dir / f"{name}{INDEX_FILE_SUFFIX}"

    def _index_tree(self, info: IndexInfo, column: ColumnDef) -> IndexTree:
        """取（或惰性创建）该索引的共享树；drop 时会从注册表移除。"""
        key = (self._db_path, info.name)
        tree = self._server._index_trees.get(key)
        if tree is None:
            tree = IndexTree(self._pool, self._index_file_path(info.name), column)
            self._server._index_trees[key] = tree
        return tree

    def _index_targets(self, table: str) -> list[tuple[IndexInfo, ColumnDef, int]]:
        """该表全部索引的（定义，列定义，列在行值元组中的位置）。"""
        catalog = self._live_catalog()
        columns = catalog.get(table)
        targets: list[tuple[IndexInfo, ColumnDef, int]] = []
        for info in catalog.indexes_for_table(table):
            for position, column in enumerate(columns):
                if column.name == info.column:
                    targets.append((info, column, position))
                    break
        return targets

    def _flush_indexes(self, table: str) -> None:
        """把该表涉及的全部索引文件落盘（D11 纪律）。"""
        for info, _column, _position in self._index_targets(table):
            self._pool.flush(self._index_file_path(info.name))

    def create_index(self, name: str, table: str, column: str) -> None:
        """建索引：建文件 → 全表扫描灌数据 → 登记系统表（D23 式编排）。

        采用"先灌数据、最后登记"的顺序：登记在前的崩溃会留下"登记存在但
        索引为空"，它会静默返回空结果；登记在后最多留下孤儿文件，由启动
        校验报 E_STORAGE。
        """
        _validate_index_name(name)
        _validate_table_name(table)
        catalog = self._live_catalog()
        columns = catalog.get(table)                 # 表不存在 → E_TABLE_NOT_FOUND
        column_def = _column_named(columns, column)  # 列不存在 → E_COLUMN_NOT_FOUND
        if name in catalog.indexes:
            raise SqlError(E_INDEX_EXISTS, f"index already exists: {name}")
        if any(
            info.table == table and info.column == column
            for info in catalog.indexes.values()
        ):
            raise SqlError(  # D36
                E_INDEX_EXISTS, f"index already exists on {table}.{column}"
            )

        index_path = self._index_file_path(name)
        try:
            create_index_file(index_path, column_def.type)
        except SqlError:
            try:
                index_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        position = [item.name for item in columns].index(column)
        try:
            tree = IndexTree(self._pool, index_path, column_def)
            for row_id, values in self._engine_for(table, columns).scan():
                tree.insert(values[position], row_id)
            self._pool.flush(index_path)
            catalog.register_index(name, table, column)
        except SqlError:
            self._pool.discard(index_path)
            try:
                index_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        self._server._index_trees[(self._db_path, name)] = tree

    def drop_index(self, name: str) -> None:
        """删索引：先摘登记，再丢缓存、删文件；unlink 失败按 D23 恢复登记。"""
        _validate_index_name(name)
        catalog = self._live_catalog()
        info = catalog.get_index(name)  # 不存在 → E_INDEX_NOT_FOUND
        index_path = self._index_file_path(name)
        catalog.unregister_index(name)
        self._pool.discard(index_path)
        self._server._index_trees.pop((self._db_path, name), None)
        try:
            index_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            try:
                catalog.register_index(name, info.table, info.column)
            except SqlError:
                pass
            raise SqlError(
                E_STORAGE, f"cannot delete index file: {index_path}"
            ) from exc

    def list_indexes(self, table: str | None = None) -> list[IndexInfo]:
        """列出索引：不带表名返回全库，带表名则先校验表存在（顺序稳定）。"""
        catalog = self._live_catalog()
        if table is None:
            return sorted(catalog.indexes.values(), key=lambda info: info.name)
        _validate_table_name(table)
        catalog.get(table)  # 表不存在 → E_TABLE_NOT_FOUND
        return catalog.indexes_for_table(table)

    def index_lookup(self, table: str, column: str, key: Value) -> Iterator[Row]:
        """等值查找：返回与 scan() 同形状的行迭代器（逐行回表）。"""
        info, column_def = self._index_target(table, column)
        rids = self._index_tree(info, column_def).lookup(key)
        return self._rows_for_rids(table, rids)

    def index_range(
        self,
        table: str,
        column: str,
        lower: Value | None,
        upper: Value | None,
        *,
        lower_inclusive: bool = True,
        upper_inclusive: bool = True,
    ) -> Iterator[Row]:
        """范围查找：端点为 None 表示无界，默认闭区间。"""
        info, column_def = self._index_target(table, column)
        rids = self._index_tree(info, column_def).range(
            lower,
            upper,
            lower_inclusive=lower_inclusive,
            upper_inclusive=upper_inclusive,
        )
        return self._rows_for_rids(table, rids)

    def _index_target(self, table: str, column: str) -> tuple[IndexInfo, ColumnDef]:
        """定位（表，列）上的索引；没有则 E_INDEX_NOT_FOUND。"""
        _validate_table_name(table)
        catalog = self._live_catalog()
        columns = catalog.get(table)  # 表不存在 → E_TABLE_NOT_FOUND
        for info in catalog.indexes_for_table(table):
            if info.column == column:
                return info, _column_named(columns, column)
        raise SqlError(E_INDEX_NOT_FOUND, f"no index on {table}.{column}")

    def _rows_for_rids(self, table: str, rids: Sequence[RowId]) -> Iterator[Row]:
        """按 rid 逐行回表（生成器：查询是惰性的，与 scan 一致）。"""
        columns = self._live_catalog().get(table)
        engine = self._engine_for(table, columns)
        for row_id in rids:
            yield engine.get_row(row_id)

    # ---- 统计（V3 M5/D37/D38/D41） ----

    def _stats_provider(self, table: str) -> TableStatsProvider:
        """取（或惰性创建）该表的统计提供者；与 engine 同样按库共享。"""
        key = (self._db_path, table)
        provider = self._server._stats.get(key)
        if provider is None:
            columns = self._live_catalog().get(table)
            provider = TableStatsProvider(
                self._engine_for(table, columns), columns, table
            )
            self._server._stats[key] = provider
        return provider

    def _note_stats_insert(self, table: str, values: Sequence[Value]) -> None:
        """写入后通知统计提供者：极值只拓宽、基数失效（D47）。

        提供者尚未建立（本进程还没调用过 statistics）时直接跳过；首次快照
        会自己建立精确基线，因此这里不需要为了统计而提前建任何状态。
        """

        provider = self._server._stats.get((self._db_path, table))
        if provider is not None:
            provider.note_insert(values)

    def _note_stats_update(
        self,
        table: str,
        old_values: Sequence[Value],
        new_values: Sequence[Value],
    ) -> None:
        """写入后通知统计提供者：旧值若正好是极值则转不确定，下次精确重算。"""

        provider = self._server._stats.get((self._db_path, table))
        if provider is not None:
            provider.note_update(old_values, new_values)

    def _note_stats_delete(self, table: str, old_values: Sequence[Value]) -> None:
        """写入后通知统计提供者：删掉极值所在行时才需要重算。"""

        provider = self._server._stats.get((self._db_path, table))
        if provider is not None:
            provider.note_delete(old_values)

    def statistics(self, table: str) -> TableStats:
        """返回表的行数、数据页数与列级统计（规划期只读，D38/D47）。

        `min_value` / `max_value` 精确；`distinct_count` 近似。极值不确定时
        本方法内部做一趟只读全扫建立基线（进程内一次性，缓存后 O(1)）。
        """
        _validate_table_name(table)
        self._live_catalog().get(table)  # 表不存在 → E_TABLE_NOT_FOUND
        return self._stats_provider(table).snapshot()
