"""Runner 语句编排与会话状态测试。"""

from __future__ import annotations

import io
import unittest
from collections.abc import Iterator, Sequence
from contextlib import redirect_stdout
from unittest.mock import patch

from contracts.ast import (
    ColumnDef,
    CreateTableStmt,
    InsertStmt,
    SelectStmt,
    SqlType,
    SourceSpan,
    Statement,
    TableRef,
    UseDatabaseStmt,
    Value,
)
from contracts.errors import E_DATABASE_NOT_FOUND, SqlError
from contracts.result import QueryResult, ScriptResult, StatementResult
from contracts.storage import Row, RowId, TableInfo
from runner import Runner


class FakeStorage:
    """仅实现当前测试需要的单库公开接口。"""

    def __init__(self, database: str) -> None:
        self.database = database
        self.schemas: dict[str, tuple[ColumnDef, ...]] = {}
        self.rows: dict[str, list[Row]] = {}
        self.describe_calls: list[str] = []

    def create_table(
        self,
        name: str,
        columns: Sequence[ColumnDef],
    ) -> None:
        self.schemas[name] = tuple(columns)
        self.rows[name] = []

    def drop_table(self, name: str) -> None:
        del self.schemas[name]
        del self.rows[name]

    def list_tables(self) -> list[str]:
        return list(self.schemas)

    def describe(self, name: str) -> TableInfo:
        self.describe_calls.append(name)
        return TableInfo(name=name, columns=self.schemas[name])

    def insert(self, name: str, values: Sequence[Value]) -> RowId:
        row_id = len(self.rows[name]) + 1
        self.rows[name].append((row_id, tuple(values)))
        return row_id

    def scan(self, name: str) -> Iterator[Row]:
        return iter(self.rows[name])

    def update_row(
        self,
        name: str,
        row_id: RowId,
        values: Sequence[Value],
    ) -> None:
        for index, (stored_id, _) in enumerate(self.rows[name]):
            if stored_id == row_id:
                self.rows[name][index] = (row_id, tuple(values))
                return
        raise AssertionError(f"row not found: {row_id}")

    def delete_row(self, name: str, row_id: RowId) -> None:
        self.rows[name] = [row for row in self.rows[name] if row[0] != row_id]


class FakeDatabaseServer:
    """记录连接目标，并为每个数据库返回独立 Storage。"""

    def __init__(self, databases: Sequence[str] = ("main",)) -> None:
        self.storages = {name: FakeStorage(name) for name in databases}
        self.connect_calls: list[str] = []

    def create_database(self, name: str) -> None:
        self.storages[name] = FakeStorage(name)

    def drop_database(self, name: str) -> None:
        del self.storages[name]

    def list_databases(self) -> list[str]:
        return list(self.storages)

    def has_database(self, name: str) -> bool:
        return name in self.storages

    def connect(self, name: str) -> FakeStorage:
        self.connect_calls.append(name)
        try:
            return self.storages[name]
        except KeyError:
            raise SqlError(
                E_DATABASE_NOT_FOUND,
                f"database not found: {name}",
            ) from None


class RecordingParser:
    def __init__(self, statements: dict[str, Statement]) -> None:
        self.statements = statements
        self.calls: list[str] = []

    def __call__(self, sql: str) -> Statement:
        self.calls.append(sql)
        return self.statements[sql]


class RunnerTest(unittest.TestCase):
    def test_default_and_explicit_initial_database(self) -> None:
        server = FakeDatabaseServer(("main", "shop"))

        default_runner = Runner(server, RecordingParser({}))
        shop_runner = Runner(server, RecordingParser({}), current_database="shop")

        self.assertEqual(server.connect_calls, ["main", "shop"])
        self.assertEqual(default_runner.current_database, "main")
        self.assertEqual(shop_runner.current_database, "shop")

    def test_initial_connection_error_is_propagated(self) -> None:
        server = FakeDatabaseServer()

        with self.assertRaises(SqlError) as caught:
            Runner(server, RecordingParser({}), current_database="missing")

        self.assertEqual(caught.exception.code, E_DATABASE_NOT_FOUND)
        self.assertEqual(server.connect_calls, ["missing"])

    def test_execute_passes_original_sql_to_parser(self) -> None:
        sql = "CREATE TABLE users (id INT);"
        parser = RecordingParser(
            {
                sql: CreateTableStmt(
                    table="users",
                    columns=(ColumnDef("id", SqlType.INT),),
                )
            }
        )
        runner = Runner(FakeDatabaseServer(), parser)

        result = runner.execute(sql)

        self.assertEqual(parser.calls, [sql])
        self.assertEqual(result.affected_rows, 0)

    def test_use_switches_schema_lookup_and_data_operations(self) -> None:
        columns = (ColumnDef("id", SqlType.INT),)
        server = FakeDatabaseServer(("main", "shop"))
        server.storages["main"].create_table("items", columns)
        server.storages["shop"].create_table("items", columns)
        parser = RecordingParser(
            {
                "USE shop;": UseDatabaseStmt("shop"),
                "INSERT": InsertStmt("items", (7,)),
                "SELECT": SelectStmt(None, TableRef("items"), None),
            }
        )
        runner = Runner(server, parser)

        runner.execute("USE shop;")
        insert_result = runner.execute("INSERT")
        select_result = runner.execute("SELECT")
        runner.execute("SELECT")

        self.assertEqual(runner.current_database, "shop")
        self.assertEqual(server.storages["main"].describe_calls, [])
        self.assertEqual(server.storages["main"].rows["items"], [])
        # INSERT 绑定 1 次；两次 SELECT 各在绑定期 1 次（绑定路径不经过该缓存），
        # 执行器构建期只在第一次反查完整列序，第二次命中的是 (库, 表) 缓存
        self.assertEqual(
            server.storages["shop"].describe_calls,
            ["items", "items", "items", "items"],
        )
        self.assertEqual(server.storages["shop"].rows["items"], [(1, (7,))])
        self.assertEqual(insert_result.affected_rows, 1)
        self.assertEqual(select_result.columns, ("id",))
        self.assertEqual(select_result.rows, ((7,),))

    def test_failed_use_preserves_previous_session(self) -> None:
        server = FakeDatabaseServer()
        parser = RecordingParser({"USE missing;": UseDatabaseStmt("missing")})
        runner = Runner(server, parser)
        original_storage = runner._context.storage

        with self.assertRaises(SqlError) as caught:
            runner.execute("USE missing;")

        self.assertEqual(caught.exception.code, E_DATABASE_NOT_FOUND)
        self.assertEqual(runner.current_database, "main")
        self.assertIs(runner._context.storage, original_storage)

    def test_parser_error_is_propagated_without_storage_access(self) -> None:
        expected = SqlError("E_TEST", "parse failed")

        def failing_parser(sql: str) -> Statement:
            raise expected

        server = FakeDatabaseServer()
        runner = Runner(server, failing_parser)

        with self.assertRaises(SqlError) as caught:
            runner.execute("bad sql")

        self.assertIs(caught.exception, expected)
        self.assertEqual(server.storages["main"].describe_calls, [])

    def test_repl_prints_results_and_sql_errors_then_exits_on_eof(self) -> None:
        runner = Runner(FakeDatabaseServer(), RecordingParser({}))
        expected = SqlError("E_TEST", "expected error")
        results: list[QueryResult | SqlError] = [
            expected,
            QueryResult(columns=("id",), rows=((1,),)),
            QueryResult(affected_rows=2),
        ]

        seen_physical: list[str] = []

        def execute_script(
            sql: str, *, stop_on_error: bool = True, physical: str = "auto"
        ) -> ScriptResult:
            seen_physical.append(physical)
            result = results.pop(0)
            if isinstance(result, SqlError):
                statement = StatementResult(
                    sql=sql,
                    span=SourceSpan(1, 1, 1, len(sql)),
                    error=result,
                )
            else:
                statement = StatementResult(
                    sql=sql,
                    span=SourceSpan(1, 1, 1, len(sql)),
                    result=result,
                )
            return ScriptResult((statement,))

        runner.execute_script = execute_script  # type: ignore[method-assign]
        output = io.StringIO()
        with (
            patch("builtins.input", side_effect=["bad", "select", "update", EOFError]),
            redirect_stdout(output),
        ):
            runner.repl(physical="seq")

        self.assertEqual(
            output.getvalue().splitlines(),
            ["[E_TEST] expected error", "id", "1", "2 row(s) affected"],
        )
        # 会话初值必须逐条透传到执行入口，否则强制模式只会改提示符
        self.assertEqual(seen_physical, ["seq", "seq", "seq"])


if __name__ == "__main__":
    unittest.main()
