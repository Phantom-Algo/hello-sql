"""谓词 → 索引请求的翻译：运算符映射、镜像翻转与无损值归纳。

翻译是纯计算，本文件只依赖绑定表达式与请求形状，不接触 Storage；"选出的请求
能否取到正确的行"由执行器层与 SQL 级测试负责。
"""

from __future__ import annotations

import pytest

from contracts.ast import SqlType
from runner.logical_plan.base import LogicalColumn, LogicalSchema
from runner.logical_plan.expressions import (
    ArithOp,
    BoundArith,
    BoundCast,
    BoundColumnRef,
    BoundComparison,
    BoundLiteral,
    BoundLogical,
    BoundUnaryNot,
    ComparisonOp,
    LogicOp,
)
from runner.physical.requests import (
    CAST_UNSAFE,
    NE_UNSUPPORTED,
    NOT_BARE_COLUMN,
    NOT_COMPARISON,
    NOT_CONJUNCT,
    IndexCandidate,
    IndexLookup,
    IndexRange,
    Unsupported,
    translate,
)

TABLE = "items"


def _schema(table: str = TABLE) -> LogicalSchema:
    """items(id INT, name TEXT, active BOOLEAN)，index 与建表列序一致。"""
    return LogicalSchema(
        (
            LogicalColumn.of(table, "id", 0, SqlType.INT),
            LogicalColumn.of(table, "name", 1, SqlType.TEXT),
            LogicalColumn.of(table, "active", 2, SqlType.BOOLEAN),
        )
    )


def _column(name: str, table: str = TABLE) -> BoundColumnRef:
    return BoundColumnRef(_schema(table).resolve(name))


def _int(value: int) -> BoundLiteral:
    return BoundLiteral(value, SqlType.INT)


def _cmp(left: object, op: ComparisonOp, right: object) -> BoundComparison:
    return BoundComparison(left, op, right)  # type: ignore[arg-type]


def _translated(conjunct: object, table: str = TABLE) -> IndexCandidate:
    """断言可翻译并取出候选，避免每个用例重复 isinstance 判断。"""
    result = translate(conjunct, table)  # type: ignore[arg-type]
    assert isinstance(result, IndexCandidate), result
    return result


def _reason(conjunct: object, table: str = TABLE) -> str:
    """断言不可翻译并取出理由码。"""
    result = translate(conjunct, table)  # type: ignore[arg-type]
    assert isinstance(result, Unsupported), result
    return result.reason


# ---------- 运算符翻译表 ----------


@pytest.mark.parametrize(
    ("op", "expected"),
    [
        (ComparisonOp.EQ, IndexLookup("id", 5)),
        (ComparisonOp.LT, IndexRange("id", None, 5, upper_inclusive=False)),
        (ComparisonOp.LE, IndexRange("id", None, 5, upper_inclusive=True)),
        (ComparisonOp.GT, IndexRange("id", 5, None, lower_inclusive=False)),
        (ComparisonOp.GE, IndexRange("id", 5, None, lower_inclusive=True)),
    ],
)
def test_column_on_left_maps_every_operator(
    op: ComparisonOp, expected: object
) -> None:
    candidate = _translated(_cmp(_column("id"), op, _int(5)))

    assert candidate.column.name == "id"
    assert candidate.request == expected
    assert candidate.is_lookup is isinstance(expected, IndexLookup)


@pytest.mark.parametrize(
    ("op", "expected"),
    [
        # 镜像翻转：k < col ≡ col > k，其余同构
        (ComparisonOp.EQ, IndexLookup("id", 5)),
        (ComparisonOp.LT, IndexRange("id", 5, None, lower_inclusive=False)),
        (ComparisonOp.LE, IndexRange("id", 5, None, lower_inclusive=True)),
        (ComparisonOp.GT, IndexRange("id", None, 5, upper_inclusive=False)),
        (ComparisonOp.GE, IndexRange("id", None, 5, upper_inclusive=True)),
    ],
)
def test_literal_on_left_is_mirrored(op: ComparisonOp, expected: object) -> None:
    candidate = _translated(_cmp(_int(5), op, _column("id")))

    assert candidate.request == expected


@pytest.mark.parametrize("mirrored", [False, True])
def test_not_equal_is_never_translated(mirrored: bool) -> None:
    comparison = (
        _cmp(_int(5), ComparisonOp.NE, _column("id"))
        if mirrored
        else _cmp(_column("id"), ComparisonOp.NE, _int(5))
    )

    assert _reason(comparison) == NE_UNSUPPORTED


def test_range_endpoints_keep_open_closed_flags() -> None:
    strict = _translated(_cmp(_column("id"), ComparisonOp.LT, _int(5))).request
    loose = _translated(_cmp(_column("id"), ComparisonOp.LE, _int(5))).request

    assert isinstance(strict, IndexRange) and isinstance(loose, IndexRange)
    assert (strict.lower, strict.upper) == (None, 5)
    assert strict.upper_inclusive is False
    assert loose.upper_inclusive is True


# ---------- 不可翻译形态 ----------


def test_bare_boolean_column_is_not_a_comparison() -> None:
    assert _reason(_column("active")) == NOT_COMPARISON


def test_boolean_literal_conjunct_is_not_a_comparison() -> None:
    assert _reason(BoundLiteral(True, SqlType.BOOLEAN)) == NOT_COMPARISON


def test_not_wrapped_condition_is_not_a_conjunct() -> None:
    conjunct = BoundUnaryNot(_cmp(_column("id"), ComparisonOp.EQ, _int(5)))

    assert _reason(conjunct) == NOT_CONJUNCT


def test_or_branch_is_not_a_conjunct() -> None:
    conjunct = BoundLogical(
        LogicOp.OR,
        (
            _cmp(_column("id"), ComparisonOp.EQ, _int(5)),
            _cmp(_column("id"), ComparisonOp.EQ, _int(6)),
        ),
    )

    assert _reason(conjunct) == NOT_CONJUNCT


def test_column_inside_arithmetic_is_not_a_bare_column() -> None:
    conjunct = _cmp(
        BoundArith(_column("id"), ArithOp.ADD, _int(1)),
        ComparisonOp.EQ,
        _int(5),
    )

    assert _reason(conjunct) == NOT_BARE_COLUMN


def test_two_columns_are_not_translatable() -> None:
    conjunct = _cmp(_column("id"), ComparisonOp.EQ, _column("id"))

    assert _reason(conjunct) == NOT_BARE_COLUMN


def test_column_of_another_table_is_not_translatable() -> None:
    conjunct = _cmp(_column("id", "orders"), ComparisonOp.EQ, _int(5))

    assert _reason(conjunct) == NOT_BARE_COLUMN


# ---------- 无损值归纳 ----------


def _cast_column(name: str, target: SqlType) -> BoundCast:
    return BoundCast(_column(name), target)


def test_real_literal_with_integral_value_collapses_to_int() -> None:
    conjunct = _cmp(
        _cast_column("id", SqlType.REAL),
        ComparisonOp.EQ,
        BoundLiteral(1.0, SqlType.REAL),
    )

    candidate = _translated(conjunct)

    assert candidate.request == IndexLookup("id", 1)
    key = candidate.request.key  # type: ignore[union-attr]
    assert type(key) is int


def test_real_literal_on_the_left_of_cast_column_is_also_normalized() -> None:
    conjunct = _cmp(
        BoundLiteral(2.0, SqlType.REAL),
        ComparisonOp.LE,
        _cast_column("id", SqlType.REAL),
    )

    # 2.0 <= CAST(id) ≡ id >= 2
    assert _translated(conjunct).request == IndexRange(
        "id", 2, None, lower_inclusive=True
    )


def test_real_literal_with_fraction_is_cast_unsafe() -> None:
    conjunct = _cmp(
        _cast_column("id", SqlType.REAL),
        ComparisonOp.EQ,
        BoundLiteral(1.5, SqlType.REAL),
    )

    assert _reason(conjunct) == CAST_UNSAFE


def test_cast_of_non_int_column_is_cast_unsafe() -> None:
    conjunct = _cmp(
        _cast_column("name", SqlType.REAL),
        ComparisonOp.EQ,
        BoundLiteral(1.0, SqlType.REAL),
    )

    assert _reason(conjunct) == CAST_UNSAFE


def test_cast_to_non_numeric_target_is_cast_unsafe() -> None:
    conjunct = _cmp(
        _cast_column("id", SqlType.TEXT),
        ComparisonOp.EQ,
        BoundLiteral("1", SqlType.TEXT),
    )

    assert _reason(conjunct) == CAST_UNSAFE


def test_cast_column_against_another_column_is_not_bare_column() -> None:
    conjunct = _cmp(
        _cast_column("id", SqlType.REAL),
        ComparisonOp.EQ,
        _column("id"),
    )

    # 另一侧不是字面量：没有键值可取，属于"没有裸列对字面量"的形态
    assert _reason(conjunct) == NOT_BARE_COLUMN


# ---------- 非数值键 ----------


def test_text_equality_and_range_are_translated() -> None:
    equality = _translated(
        _cmp(_column("name"), ComparisonOp.EQ, BoundLiteral("abc", SqlType.TEXT))
    )
    lower = _translated(
        _cmp(_column("name"), ComparisonOp.GE, BoundLiteral("m", SqlType.TEXT))
    )
    mirrored = _translated(
        _cmp(BoundLiteral("a", SqlType.TEXT), ComparisonOp.LT, _column("name"))
    )

    assert equality.request == IndexLookup("name", "abc")
    assert lower.request == IndexRange("name", "m", None)
    assert mirrored.request == IndexRange("name", "a", None, lower_inclusive=False)


def test_boolean_equality_is_translated() -> None:
    conjunct = _cmp(
        _column("active"), ComparisonOp.EQ, BoundLiteral(True, SqlType.BOOLEAN)
    )

    assert _translated(conjunct).request == IndexLookup("active", True)
