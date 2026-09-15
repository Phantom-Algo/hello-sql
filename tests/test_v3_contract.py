"""V3 公共契约的形状测试（contracts 3.1）。

本文件锁定 V3 新增的公共契约面：索引 DDL 的 AST 节点、索引与统计的
共享数据类、BaseStorage 协议新增的五个方法，以及新增错误码。

它只验证契约本身的形状，不测试任何模块的实现——A/B/C 的功能由各自
模块内的测试覆盖。契约同步的原则是"形状先于实现"：本文件落地时
对应的 A/B/C 能力尚未实现，调用方拿到的是未实现错误，而不是契约漂移。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
import inspect
from typing import get_args

import pytest

import contracts
from contracts.ast import CreateIndexStmt, DropIndexStmt, Statement
from contracts.errors import (
    E_BAD_ARG,
    E_DATABASE_IN_USE,
    E_INDEX_EXISTS,
    E_INDEX_NOT_FOUND,
    E_INPUT_FILE,
    E_SYNTAX,
    E_TABLE_NOT_FOUND,
)
from contracts.storage import (
    BaseStorage,
    ColumnStats,
    IndexInfo,
    TableStats,
)


# ---------- 版本与语句联合 ----------


def test_contract_version_is_3_1() -> None:
    """契约版本号是三方的兼容性开关，必须随语义变化一起升级。

    3.1（DV3-12）：ColumnStats 的 min/max 收紧为精确边界、distinct 保持近似。
    """

    assert contracts.__version__ == "3.1"


def test_statement_union_includes_index_ddl() -> None:
    """索引 DDL 是 V3 新增的两种语句，且不挤掉任何既有语句。"""
    members = get_args(Statement)
    assert CreateIndexStmt in members
    assert DropIndexStmt in members
    assert len(members) == 11


def test_create_index_stmt_is_frozen_with_expected_fields() -> None:
    """CreateIndexStmt 字段顺序即契约：索引名、表名、列名。"""
    statement = CreateIndexStmt(
        index_name="idx_users_age",
        table="users",
        column="age",
    )
    assert [field.name for field in fields(statement)] == [
        "index_name",
        "table",
        "column",
    ]
    with pytest.raises(FrozenInstanceError):
        statement.index_name = "other"  # type: ignore[misc]


def test_drop_index_stmt_is_frozen_with_expected_fields() -> None:
    """DropIndexStmt 只带索引名：索引名在同一数据库内唯一，无需表名。"""
    statement = DropIndexStmt(index_name="idx_users_age")
    assert [field.name for field in fields(statement)] == ["index_name"]
    with pytest.raises(FrozenInstanceError):
        statement.index_name = "other"  # type: ignore[misc]


# ---------- 索引与统计的共享数据形状 ----------


def test_index_info_shape_without_unique_field() -> None:
    """本轮只做单列非唯一索引，IndexInfo 不预留 unique 字段。"""
    info = IndexInfo(name="idx_users_age", table="users", column="age")
    assert [field.name for field in fields(info)] == ["name", "table", "column"]


def test_column_stats_allows_empty_value_range() -> None:
    """空表语义：distinct_count 为 0，min/max 为空。"""
    stats = ColumnStats(name="age", distinct_count=0, min_value=None, max_value=None)
    assert stats.distinct_count == 0
    assert stats.min_value is None
    assert stats.max_value is None


def test_table_stats_columns_is_tuple_of_column_stats() -> None:
    """TableStats.columns 是 tuple，元素为 ColumnStats。"""
    stats = TableStats(
        table="users",
        row_count=3,
        page_count=1,
        columns=(
            ColumnStats(name="id", distinct_count=3, min_value=1, max_value=3),
        ),
    )
    assert isinstance(stats.columns, tuple)
    assert isinstance(stats.columns[0], ColumnStats)


def test_contract_dataclasses_are_hashable() -> None:
    """C 可能把契约数据放进 set/dict，冻结数据类必须可哈希。"""
    column = ColumnStats(name="age", distinct_count=1, min_value=1, max_value=1)
    values = (
        IndexInfo(name="idx_users_age", table="users", column="age"),
        column,
        TableStats(table="users", row_count=1, page_count=1, columns=(column,)),
    )
    for value in values:
        assert isinstance(hash(value), int)


# ---------- BaseStorage 协议 ----------


_ORIGINAL_STORAGE_METHODS = (
    "create_table",
    "drop_table",
    "list_tables",
    "describe",
    "insert",
    "scan",
    "update_row",
    "delete_row",
)

_V3_STORAGE_METHODS = (
    "create_index",
    "drop_index",
    "list_indexes",
    "statistics",
    "index_lookup",
    "index_range",
)


def test_base_storage_keeps_v1_v2_methods() -> None:
    """V3 只增不减：原有八个表级方法必须一个不少。"""
    for name in _ORIGINAL_STORAGE_METHODS:
        assert callable(getattr(BaseStorage, name, None)), name


def test_base_storage_public_method_set_is_locked() -> None:
    """公开方法集合即契约：增删方法都必须是一次显式决定。"""
    public_methods = {
        name for name in BaseStorage.__dict__ if not name.startswith("_")
    }
    assert public_methods == set(_ORIGINAL_STORAGE_METHODS) | set(_V3_STORAGE_METHODS)


def test_base_storage_declares_v3_methods() -> None:
    """新增方法的参数名、默认值与返回注解即契约。"""
    expected_parameters = {
        "create_index": ["self", "name", "table", "column"],
        "drop_index": ["self", "name"],
        "list_indexes": ["self", "table"],
        "statistics": ["self", "table"],
        "index_lookup": ["self", "table", "column", "key"],
        "index_range": [
            "self",
            "table",
            "column",
            "lower",
            "upper",
            "lower_inclusive",
            "upper_inclusive",
        ],
    }
    for name, parameters in expected_parameters.items():
        method = getattr(BaseStorage, name)
        assert list(inspect.signature(method).parameters) == parameters, name

    assert (
        inspect.signature(BaseStorage.list_indexes).parameters["table"].default is None
    )
    assert inspect.signature(BaseStorage.statistics).return_annotation == "TableStats"
    assert (
        inspect.signature(BaseStorage.index_lookup).return_annotation
        == "Iterator[Row]"
    )
    assert (
        inspect.signature(BaseStorage.index_range).return_annotation
        == "Iterator[Row]"
    )


def test_index_range_bounds_are_keyword_only_with_inclusive_defaults() -> None:
    """区间端点默认闭区间，且必须只能按关键字传入，避免位置参数写错。"""
    parameters = inspect.signature(BaseStorage.index_range).parameters
    for name in ("lower_inclusive", "upper_inclusive"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert parameters[name].default is True, name


def test_storage_contract_carries_no_comparison_operator() -> None:
    """比较语义归 C：B 的索引接口不得出现操作符类参数。"""
    forbidden = {"op", "operator", "cmp", "comparison"}
    for name in _V3_STORAGE_METHODS:
        parameters = inspect.signature(getattr(BaseStorage, name)).parameters
        assert not forbidden & set(parameters), name


# ---------- 错误码 ----------


def test_new_error_codes_have_exact_values() -> None:
    """错误码字符串本身即契约，值必须与设计文档一致。"""
    assert E_INDEX_EXISTS == "E_INDEX_EXISTS"
    assert E_INDEX_NOT_FOUND == "E_INDEX_NOT_FOUND"


def test_existing_error_codes_unchanged() -> None:
    """新增错误码不得改动任何既有错误码的值。"""
    assert E_SYNTAX == "E_SYNTAX"
    assert E_TABLE_NOT_FOUND == "E_TABLE_NOT_FOUND"
    assert E_DATABASE_IN_USE == "E_DATABASE_IN_USE"
    assert E_BAD_ARG == "E_BAD_ARG"
    assert E_INPUT_FILE == "E_INPUT_FILE"
