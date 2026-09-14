"""IndexScanExecutor：行形状、row_id 透传与候选回退的错误归属。

Storage 用假实现注入，因此可以精确制造"候选无索引"与"类型不符"两种错误，
验证只有前者触发回退、后者立即上抛。真实索引的语义由 SQL 级测试覆盖。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import pytest

from contracts.ast import SqlType
from contracts.errors import (
    E_BAD_ARG,
    E_INDEX_NOT_FOUND,
    E_TYPE_MISMATCH,
    SqlError,
)
from contracts.storage import BaseDatabaseServer, BaseStorage, Row
from runner.executor.context import ExecutionContext
from runner.executor.dql import IndexScanExecutor
from runner.logical_plan.base import LogicalColumn, LogicalSchema
from runner.physical.requests import IndexLookup, IndexRange, IndexRequest


TABLE = "items"


class _FakeStorage:
    """按 (接口, 列) 返回预置行或抛出预置错误，并记录每次调用。"""

    def __init__(self, responses: dict[tuple[str, str], object]) -> None:
        self.responses = responses
        self.calls: list[tuple[object, ...]] = []

    def index_lookup(self, table: str, column: str, key: object) -> Iterator[Row]:
        self.calls.append(("lookup", table, column, key))
        return self._resolve(("lookup", column))

    def index_range(
        self,
        table: str,
        column: str,
        lower: object,
        upper: object,
        *,
        lower_inclusive: bool = True,
        upper_inclusive: bool = True,
    ) -> Iterator[Row]:
        self.calls.append(
            ("range", table, column, lower, upper, lower_inclusive, upper_inclusive)
        )
        return self._resolve(("range", column))

    def _resolve(self, key: tuple[str, str]) -> Iterator[Row]:
        response = self.responses[key]
        if isinstance(response, BaseException):
            raise response
        return iter(cast("list[Row]", response))


def _context(storage: _FakeStorage) -> ExecutionContext:
    """只填 storage 的执行上下文：索引扫描不触碰 server 与会话库名。"""

    return ExecutionContext(
        server=cast(BaseDatabaseServer, None),
        storage=cast(BaseStorage, storage),
        current_database="main",
    )


def _schema() -> LogicalSchema:
    """裁剪后的 Scan 输出：只有 name 与 age，index 从 0 重新编号。"""

    return LogicalSchema(
        (
            LogicalColumn.of(TABLE, "name", 0, SqlType.TEXT),
            LogicalColumn.of(TABLE, "age", 1, SqlType.INT),
        )
    )


def _executor(requests: tuple[IndexRequest, ...]) -> IndexScanExecutor:
    """扫描整行、只输出 (name, age)：source_indexes 应映射到整行的第 1、2 位。"""

    return IndexScanExecutor(
        table=TABLE,
        schema=_schema(),
        source_indexes=(1, 2),
        requests=requests,
    )


def _missing(column: str) -> SqlError:
    return SqlError(E_INDEX_NOT_FOUND, f"no index on {TABLE}.{column}")


def _lazy(rows: list[Row], error: SqlError) -> Iterator[Row]:
    """先产出给定行、再抛出错误的惰性迭代器。"""

    def generate() -> Iterator[Row]:
        yield from rows
        raise error

    return generate()


# ---------- 行形状 ----------


def test_rows_are_projected_by_source_indexes() -> None:
    storage = _FakeStorage(
        {("lookup", "id"): [(7, (1, "ann", 30)), (9, (2, "bob", 40))]}
    )

    rows = tuple(_executor((IndexLookup("id", 1),)).rows(_context(storage)))

    assert [row.row_id for row in rows] == [7, 9]
    assert [row.values for row in rows] == [("ann", 30), ("bob", 40)]
    assert storage.calls == [("lookup", TABLE, "id", 1)]


def test_range_request_forwards_endpoints_and_flags() -> None:
    storage = _FakeStorage({("range", "age"): [(3, (3, "cid", 25))]})
    request = IndexRange("age", 20, 40, upper_inclusive=False)

    rows = tuple(_executor((request,)).rows(_context(storage)))

    assert [row.values for row in rows] == [("cid", 25)]
    assert storage.calls == [("range", TABLE, "age", 20, 40, True, False)]


def test_open_ended_range_forwards_none_endpoints() -> None:
    storage = _FakeStorage({("range", "age"): []})

    tuple(_executor((IndexRange("age", None, 40),)).rows(_context(storage)))

    assert storage.calls == [("range", TABLE, "age", None, 40, True, True)]


# ---------- 候选回退 ----------


def test_candidate_without_index_falls_back_to_the_next_one() -> None:
    storage = _FakeStorage(
        {
            ("lookup", "amount"): _missing("amount"),
            ("lookup", "id"): [(5, (5, "eve", 20))],
        }
    )
    requests = (IndexLookup("amount", 100), IndexLookup("id", 5))

    rows = tuple(_executor(requests).rows(_context(storage)))

    assert [row.values for row in rows] == [("eve", 20)]
    assert [call[0] for call in storage.calls] == ["lookup", "lookup"]


def test_all_candidates_without_index_propagate_the_storage_error() -> None:
    storage = _FakeStorage(
        {
            ("lookup", "amount"): _missing("amount"),
            ("lookup", "tag"): _missing("tag"),
        }
    )
    requests = (IndexLookup("amount", 100), IndexLookup("tag", "x"))

    with pytest.raises(SqlError) as info:
        tuple(_executor(requests).rows(_context(storage)))

    assert info.value.code == E_INDEX_NOT_FOUND
    assert info.value.message == _missing("tag").message


def test_type_mismatch_is_not_swallowed_by_the_fallback() -> None:
    storage = _FakeStorage(
        {
            ("lookup", "amount"): SqlError(E_TYPE_MISMATCH, "bad key"),
            ("lookup", "id"): [(5, (5, "eve", 20))],
        }
    )
    requests = (IndexLookup("amount", "100"), IndexLookup("id", 5))

    with pytest.raises(SqlError) as info:
        tuple(_executor(requests).rows(_context(storage)))

    assert info.value.code == E_TYPE_MISMATCH
    # 类型错误必须立即上抛，不能再问下一个候选
    assert [call[0] for call in storage.calls] == ["lookup"]


def test_empty_requests_are_rejected_instead_of_silently_scanning_nothing() -> None:
    storage = _FakeStorage({})

    with pytest.raises(SqlError) as info:
        tuple(_executor(()).rows(_context(storage)))

    assert info.value.code == E_BAD_ARG
    assert storage.calls == []


# ---------- 惰性抛出错误的时序 ----------


def test_index_missing_on_first_pull_still_falls_back() -> None:
    storage = _FakeStorage(
        {
            ("lookup", "amount"): _lazy([], _missing("amount")),
            ("lookup", "id"): [(5, (5, "eve", 20))],
        }
    )
    requests = (IndexLookup("amount", 100), IndexLookup("id", 5))

    rows = tuple(_executor(requests).rows(_context(storage)))

    assert [row.values for row in rows] == [("eve", 20)]


def test_index_missing_after_a_row_is_propagated() -> None:
    # 已经产出过行说明索引存在：此时再回退会造成重复行，必须上抛
    storage = _FakeStorage(
        {
            ("lookup", "id"): _lazy([(5, (5, "eve", 20))], _missing("id")),
        }
    )

    with pytest.raises(SqlError) as info:
        tuple(_executor((IndexLookup("id", 5),)).rows(_context(storage)))

    assert info.value.code == E_INDEX_NOT_FOUND
