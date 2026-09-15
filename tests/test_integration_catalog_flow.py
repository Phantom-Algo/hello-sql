"""三方集成 + 页式 Catalog 观察（根 tests；允许同时 import 三家）。

范围说明：真实 SQL → AST → 计划/执行 → 页式 Catalog 全链路。V2 起
BOOLEAN/TRUE/FALSE 已贯通到 SQL 层，因此本文件既覆盖 INT/TEXT/REAL，
也覆盖 BOOLEAN 的建表、插入、过滤、索引与重启恢复。
"""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import random
import subprocess
import sys

import pytest

from compiler import parse
from contracts.ast import ColumnDef, SqlType
from contracts.errors import (
    E_BAD_ARG,
    E_DATABASE_IN_USE,
    E_TABLE_EXISTS,
    E_TABLE_NOT_FOUND,
    SqlError,
)
from runner import Runner
from storage import DatabaseServer
from storage.cache import BufferPool
from storage.constants import (
    CATALOG_FILE_NAME,
    CATALOG_VERSION,
    JSON_COLUMNS_KEY,
    JSON_NAME_KEY,
    JSON_TABLES_KEY,
    JSON_TYPE_KEY,
    JSON_VERSION_KEY,
    LEGACY_MIGRATED_FILE_NAME,
    SYS_COLUMNS_FILE_NAME,
    SYS_TABLES_FILE_NAME,
)
from storage.engine import TableEngine
from storage.pager import create_table_file


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _server_runner(data_dir: str) -> tuple[DatabaseServer, Runner]:
    server = DatabaseServer(data_dir)
    return server, Runner(server=server, parse=parse, current_database="main")


def _expect_code(call, code: str) -> None:
    with pytest.raises(SqlError) as exc:
        call()
    assert exc.value.code == code


def _db_dir(data_dir: str, name: str = "main") -> Path:
    return Path(data_dir) / name


# ---- 固定集成场景 ----


def test_integrated_catalog_lifecycle_and_restart(tmp_path):
    """真实 SQL 建表/插入/查询后，页式 Catalog 落地且重启可恢复。"""
    data_dir = str(tmp_path / "data")
    server, runner = _server_runner(data_dir)

    runner.execute("CREATE TABLE users (id INT, name TEXT, age REAL);")
    assert runner.execute("INSERT INTO users VALUES (1, 'alice', 18);").affected_rows == 1
    assert runner.execute("INSERT INTO users VALUES (2, 'bob', 5.0);").affected_rows == 1

    result = runner.execute("SELECT * FROM users;")
    assert result.columns == ("id", "name", "age")
    assert Counter(result.rows) == Counter([(1, "alice", 18.0), (2, "bob", 5.0)])

    root = _db_dir(data_dir)
    assert (root / SYS_TABLES_FILE_NAME).is_file()
    assert (root / SYS_COLUMNS_FILE_NAME).is_file()
    assert not (root / CATALOG_FILE_NAME).exists()
    stats = server.cache_stats
    assert stats["misses"] > 0
    assert stats["dirty_writes"] > 0

    _server2, runner2 = _server_runner(data_dir)
    assert Counter(runner2.execute("SELECT * FROM users;").rows) == Counter(
        [(1, "alice", 18.0), (2, "bob", 5.0)]
    )


def test_integrated_boolean_column_through_sql_and_index(tmp_path):
    """BOOLEAN 的 SQL 全链路：建表 → 插入 TRUE/FALSE → 过滤 → 索引 → 重启。"""

    data_dir = str(tmp_path / "data")
    _server, runner = _server_runner(data_dir)
    runner.execute("CREATE TABLE flags (id INT, ok BOOLEAN);")
    runner.execute("INSERT INTO flags VALUES (1, TRUE);")
    runner.execute("INSERT INTO flags VALUES (2, FALSE);")

    selected = runner.execute("SELECT * FROM flags WHERE ok = TRUE;")
    assert selected.rows == ((1, True),)

    runner.execute("CREATE INDEX idx_flags_ok ON flags (ok);")
    assert Counter(runner.execute("SELECT * FROM flags WHERE ok = FALSE;").rows) == Counter(
        [(2, False)]
    )
    forced = runner.execute(
        "SELECT * FROM flags WHERE ok = TRUE;", physical="index"
    )
    assert Counter(forced.rows) == Counter([(1, True)])

    _server2, runner2 = _server_runner(data_dir)
    assert Counter(runner2.execute("SELECT * FROM flags;").rows) == Counter(
        [(1, True), (2, False)]
    )
    after_restart = runner2.execute(
        "SELECT * FROM flags WHERE ok = TRUE;", physical="index"
    )
    assert Counter(after_restart.rows) == Counter([(1, True)])


def test_integrated_drop_recreate_and_reserved_prefix(tmp_path):
    """drop/同名重建经真实 SQL 生效；__sys_ 前缀经 describe 被拦。"""
    data_dir = str(tmp_path / "data")
    _server, runner = _server_runner(data_dir)
    runner.execute("CREATE TABLE t (id INT);")
    runner.execute("INSERT INTO t VALUES (1);")
    runner.execute("DROP TABLE t;")
    runner.execute("CREATE TABLE t (id INT, tag TEXT);")
    runner.execute("INSERT INTO t VALUES (2, 'x');")
    assert runner.execute("SELECT * FROM t;").rows == ((2, "x"),)

    root = _db_dir(data_dir)
    before = (
        (root / SYS_TABLES_FILE_NAME).read_bytes(),
        (root / SYS_COLUMNS_FILE_NAME).read_bytes(),
    )
    _expect_code(
        lambda: runner.execute("CREATE TABLE t (id INT);"), E_TABLE_EXISTS
    )
    _expect_code(
        lambda: runner.execute("CREATE TABLE __sys_x (id INT);"), E_BAD_ARG
    )
    _expect_code(lambda: runner.execute("SELECT * FROM __sys_tables;"), E_BAD_ARG)
    after = (
        (root / SYS_TABLES_FILE_NAME).read_bytes(),
        (root / SYS_COLUMNS_FILE_NAME).read_bytes(),
    )
    assert before == after


def test_integrated_multi_database_catalog_isolation(tmp_path):
    """USE/CREATE/DROP DATABASE 下，多库 Catalog 互相隔离且各自页式落地。"""
    data_dir = str(tmp_path / "data")
    _server, runner = _server_runner(data_dir)
    runner.execute("CREATE DATABASE shop;")
    runner.execute("USE shop;")
    runner.execute("CREATE TABLE orders (oid INT, item TEXT);")
    runner.execute("INSERT INTO orders VALUES (10, 'book');")
    assert runner.current_database == "shop"
    assert runner.execute("SELECT * FROM orders;").rows == ((10, "book"),)

    runner.execute("USE main;")
    _expect_code(lambda: runner.execute("SELECT * FROM orders;"), E_TABLE_NOT_FOUND)
    runner.execute("USE shop;")
    _expect_code(
        lambda: runner.execute("DROP DATABASE main;"), E_DATABASE_IN_USE
    )
    _expect_code(
        lambda: runner.execute("DROP DATABASE shop;"), E_DATABASE_IN_USE
    )
    runner.execute("USE main;")
    runner.execute("DROP DATABASE shop;")

    assert not _db_dir(data_dir, "shop").exists()
    for db_name in ("main",):
        root = _db_dir(data_dir, db_name)
        assert (root / SYS_TABLES_FILE_NAME).is_file()
        assert not (root / CATALOG_FILE_NAME).exists()


def _write_v1_database(root: Path) -> None:
    """构造 V1 目录：catalog.json + users.table（INT/TEXT/REAL 一行）。"""
    root.mkdir(parents=True)
    columns = (
        ColumnDef("id", SqlType.INT),
        ColumnDef("name", SqlType.TEXT),
        ColumnDef("age", SqlType.REAL),
    )
    create_table_file(root / "users.table")
    pool = BufferPool(capacity=16)
    TableEngine(root / "users.table", columns, pool).insert((1, "alice", 18.0))
    pool.flush(root / "users.table")
    payload = {
        JSON_VERSION_KEY: CATALOG_VERSION,
        JSON_TABLES_KEY: {
            "users": {
                JSON_COLUMNS_KEY: [
                    {JSON_NAME_KEY: "id", JSON_TYPE_KEY: "INT"},
                    {JSON_NAME_KEY: "name", JSON_TYPE_KEY: "TEXT"},
                    {JSON_NAME_KEY: "age", JSON_TYPE_KEY: "REAL"},
                ]
            }
        },
    }
    (root / CATALOG_FILE_NAME).write_text(json.dumps(payload), encoding="utf-8")


def test_integrated_v1_migration_is_sql_visible(tmp_path):
    """V1 目录经真实 SQL 访问时完成迁移，数据仍可查询。"""
    data_dir = str(tmp_path / "data")
    root = _db_dir(data_dir)
    _write_v1_database(root)

    _server, runner = _server_runner(data_dir)
    result = runner.execute("SELECT * FROM users;")

    assert result.columns == ("id", "name", "age")
    assert Counter(result.rows) == Counter([(1, "alice", 18.0)])
    assert not (root / CATALOG_FILE_NAME).exists()
    assert (root / LEGACY_MIGRATED_FILE_NAME).is_file()
    assert (root / SYS_TABLES_FILE_NAME).is_file()


# ---- 真实服务进程（CLI） ----


def test_cli_process_persists_page_catalog(tmp_path):
    """三个独立 hello-sql 进程连续执行 SQL，Catalog 跨进程可恢复。"""
    pytest.importorskip("rich")
    data_dir = str(tmp_path / "service-data")

    def run(sql: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "main.py"),
                "--data-dir",
                data_dir,
                "-e",
                sql,
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )

    first = run("CREATE TABLE users (id INT, name TEXT);")
    assert first.returncode == 0, first.stderr
    second = run("INSERT INTO users VALUES (1, 'alice');")
    assert second.returncode == 0, second.stderr
    third = run("SELECT * FROM users;")
    assert third.returncode == 0, third.stderr
    assert "alice" in third.stdout

    root = _db_dir(data_dir)
    assert (root / SYS_TABLES_FILE_NAME).is_file()
    assert (root / SYS_COLUMNS_FILE_NAME).is_file()
    assert not (root / CATALOG_FILE_NAME).exists()

    duplicate = run("CREATE TABLE users (id INT);")
    assert duplicate.returncode == 1
    assert "E_TABLE_EXISTS" in duplicate.stderr


# ---- 随机出题：V1 SQL 子集 + Catalog 模型 ----


_RANDOM_TYPES = (SqlType.INT, SqlType.TEXT, SqlType.REAL)
_TEXT_VALUES = ("alpha", "beta", "gamma")
_REAL_VALUES = (1.5, 2.0, 3.25)


def _random_columns(rng: random.Random) -> tuple[ColumnDef, ...]:
    count = rng.randint(1, 3)
    return tuple(
        ColumnDef(f"c{index}", rng.choice(_RANDOM_TYPES))
        for index in range(count)
    )


def _random_value(rng: random.Random, sql_type: SqlType):
    if sql_type is SqlType.INT:
        return rng.randint(0, 9)
    if sql_type is SqlType.REAL:
        return rng.choice(_REAL_VALUES)
    return rng.choice(_TEXT_VALUES)


def _literal_sql(value) -> str:
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _row_sql(row: tuple) -> str:
    return "(" + ", ".join(_literal_sql(value) for value in row) + ")"


def _assert_catalog_matches(storage, model: dict) -> None:
    assert storage.list_tables() == sorted(model)
    for name, entry in model.items():
        assert storage.describe(name).columns == entry["columns"]


@pytest.mark.parametrize("seed", range(5))
def test_randomized_integration_catalog_model(seed, tmp_path):
    """随机 DDL/DML（V1 类型）经真实 SQL 执行，Catalog 始终与模型一致。"""
    rng = random.Random(seed)
    data_dir = str(tmp_path / f"data-{seed}")
    server, runner = _server_runner(data_dir)
    storage = server.connect("main")
    model: dict[str, dict] = {}

    for step in range(40):
        name = f"t{rng.randrange(3)}"
        action = rng.choice(("create", "drop", "insert", "select", "update", "delete"))

        if action == "create":
            columns = _random_columns(rng)
            sql = (
                f"CREATE TABLE {name} "
                f"({', '.join(f'{c.name} {c.type.value}' for c in columns)});"
            )
            if name in model:
                _expect_code(lambda s=sql: runner.execute(s), E_TABLE_EXISTS)
            else:
                runner.execute(sql)
                model[name] = {"columns": columns, "rows": []}
        elif action == "drop":
            if name in model:
                runner.execute(f"DROP TABLE {name};")
                del model[name]
            else:
                _expect_code(
                    lambda s=f"DROP TABLE {name};": runner.execute(s),
                    E_TABLE_NOT_FOUND,
                )
        elif action == "insert" and model:
            target = rng.choice(list(model))
            entry = model[target]
            row = tuple(
                _random_value(rng, column.type) for column in entry["columns"]
            )
            runner.execute(f"INSERT INTO {target} VALUES {_row_sql(row)};")
            entry["rows"].append(row)
        elif action == "select" and model:
            target = rng.choice(list(model))
            result = runner.execute(f"SELECT * FROM {target};")
            assert result.columns == tuple(
                column.name for column in model[target]["columns"]
            )
            assert Counter(result.rows) == Counter(model[target]["rows"])
        elif action in ("update", "delete") and model:
            candidates = [n for n, e in model.items() if e["rows"]]
            if candidates:
                target = rng.choice(candidates)
                entry = model[target]
                where_index = rng.randrange(len(entry["columns"]))
                where_value = rng.choice(entry["rows"])[where_index]
                if action == "update":
                    set_index = rng.randrange(len(entry["columns"]))
                    new_value = _random_value(
                        rng, entry["columns"][set_index].type
                    )
                    runner.execute(
                        f"UPDATE {target} "
                        f"SET c{set_index} = {_literal_sql(new_value)} "
                        f"WHERE c{where_index} = {_literal_sql(where_value)};"
                    )
                    entry["rows"] = [
                        tuple(
                            new_value if index == set_index else value
                            for index, value in enumerate(row)
                        )
                        if row[where_index] == where_value
                        else row
                        for row in entry["rows"]
                    ]
                else:
                    runner.execute(
                        f"DELETE FROM {target} "
                        f"WHERE c{where_index} = {_literal_sql(where_value)};"
                    )
                    entry["rows"] = [
                        row for row in entry["rows"] if row[where_index] != where_value
                    ]

        _assert_catalog_matches(storage, model)

        if step % 15 == 14:
            server, runner = _server_runner(data_dir)
            storage = server.connect("main")
            _assert_catalog_matches(storage, model)
            for target, entry in model.items():
                assert Counter(runner.execute(f"SELECT * FROM {target};").rows) == Counter(
                    entry["rows"]
                )
