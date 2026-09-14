"""单值编解码抽取的护栏测试（V3 M2 第一段）。

抽取是**纯重构**：记录字节格式一个比特都不能变，表级归一化规则原样保留。
这几条用例的作用是让后面所有树代码可以放心地复用同一套编解码，
而不是自己写第二套"索引专用"的键编码。
"""

from __future__ import annotations

import struct

import pytest

from contracts.ast import ColumnDef, SqlType
from contracts.errors import E_STORAGE, E_TYPE_MISMATCH, SqlError
from storage.engine import decode_record, encode_record
from storage.valuecodec import decode_value, encode_value, normalize_value


def _column(sql_type: SqlType) -> ColumnDef:
    return ColumnDef("c", sql_type)


def test_encode_value_layout_is_stable() -> None:
    """每种类型的字节布局：INT=q、REAL=d、TEXT=u32 长度+UTF-8、BOOLEAN=1 字节。"""
    assert encode_value(_column(SqlType.INT), 7) == struct.pack("<q", 7)
    assert encode_value(_column(SqlType.REAL), 1.5) == struct.pack("<d", 1.5)
    assert encode_value(_column(SqlType.TEXT), "ab") == struct.pack("<I", 2) + b"ab"
    assert encode_value(_column(SqlType.BOOLEAN), True) == b"\x01"
    assert encode_value(_column(SqlType.BOOLEAN), False) == b"\x00"


def test_encode_record_bytes_are_stable() -> None:
    """整条记录的字节必须与抽取前完全一致（含行号头）。"""
    columns = (
        ColumnDef("a", SqlType.INT),
        ColumnDef("b", SqlType.REAL),
        ColumnDef("c", SqlType.TEXT),
        ColumnDef("d", SqlType.BOOLEAN),
    )
    expected = (
        struct.pack("<Q", 5)
        + struct.pack("<q", 7)
        + struct.pack("<d", 1.5)
        + struct.pack("<I", 2)
        + b"hi"
        + b"\x00"
    )

    raw = encode_record(5, columns, (7, 1.5, "hi", False))

    assert raw == expected
    assert decode_record(raw, columns) == (5, (7, 1.5, "hi", False))


def test_decode_value_returns_value_and_next_offset() -> None:
    """单值解码要返回新偏移，便于顺序拼出整行。"""
    value, position = decode_value(_column(SqlType.TEXT), b"\x03\x00\x00\x00abc", 0)

    assert (value, position) == ("abc", 7)


def test_decode_value_rejects_truncated_payload() -> None:
    """截断的 TEXT 必须报 E_STORAGE，不能漏出裸异常。"""
    with pytest.raises(SqlError) as exc:
        decode_value(_column(SqlType.TEXT), b"\x05\x00\x00\x00ab", 0)

    assert exc.value.code == E_STORAGE


def test_decode_value_rejects_invalid_boolean_byte() -> None:
    """BOOLEAN 只认 0x00/0x01，其它字节是存储损坏。"""
    with pytest.raises(SqlError) as exc:
        decode_value(_column(SqlType.BOOLEAN), b"\x02", 0)

    assert exc.value.code == E_STORAGE


def test_decode_value_rejects_invalid_utf8() -> None:
    """TEXT 非 UTF-8 → E_STORAGE。"""
    with pytest.raises(SqlError) as exc:
        decode_value(_column(SqlType.TEXT), struct.pack("<I", 2) + b"\xff\xfe", 0)

    assert exc.value.code == E_STORAGE


def test_normalize_value_rejects_bool_for_int_and_real() -> None:
    """bool 是 int 子类，INT/REAL 都必须显式拒绝。"""
    for sql_type in (SqlType.INT, SqlType.REAL):
        with pytest.raises(SqlError) as exc:
            normalize_value(_column(sql_type), True)
        assert exc.value.code == E_TYPE_MISMATCH


def test_normalize_value_converts_int_to_float_for_real() -> None:
    """REAL 收 int 但内部统一成 float（键编码必须用 float 形式）。"""
    normalized = normalize_value(_column(SqlType.REAL), 3)

    assert type(normalized) is float
    assert normalized == 3.0


def test_normalize_value_rejects_non_finite_real() -> None:
    """NaN / ±Inf 不能进存储。"""
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(SqlError) as exc:
            normalize_value(_column(SqlType.REAL), bad)
        assert exc.value.code == E_TYPE_MISMATCH


def test_normalize_value_requires_exact_bool_and_str() -> None:
    """BOOLEAN 只收 bool；TEXT 只收 str。"""
    with pytest.raises(SqlError) as exc:
        normalize_value(_column(SqlType.BOOLEAN), 1)
    assert exc.value.code == E_TYPE_MISMATCH

    with pytest.raises(SqlError) as exc:
        normalize_value(_column(SqlType.TEXT), 1)
    assert exc.value.code == E_TYPE_MISMATCH


def test_normalize_value_rejects_uncodable_text() -> None:
    """孤立代理项无法 UTF-8 编码 → E_TYPE_MISMATCH。"""
    with pytest.raises(SqlError) as exc:
        normalize_value(_column(SqlType.TEXT), "\ud800")

    assert exc.value.code == E_TYPE_MISMATCH


def test_normalize_value_rejects_out_of_range_int() -> None:
    """超出 64 位范围的 INT → E_TYPE_MISMATCH。"""
    with pytest.raises(SqlError) as exc:
        normalize_value(_column(SqlType.INT), 2**63)

    assert exc.value.code == E_TYPE_MISMATCH
