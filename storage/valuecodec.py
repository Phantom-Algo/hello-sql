"""单值编解码与值归一化（V3 M2 从 engine / 门面抽取）。

本模块是"一个列值 ↔ 字节"的**单一真相**：

- `engine.encode_record` / `decode_record`（表记录）与索引键编解码共用它，
  避免出现"表一套、索引一套"的两个编码实现，那必然分叉；
- 归一化规则从 `storage/__init__.py::_normalize_values` 抽出，保证公开方法
  边界与索引键对值的要求完全一致（INT 拒 bool、REAL 收 int 转 float 且拒绝
  NaN/±Inf、TEXT 必须 UTF-8 可编码、BOOLEAN 只收 bool）。

编码布局（与 V1 记录格式一致，一个比特都不能改）：

    INT     8 B 有符号小端（<q）
    REAL    8 B IEEE754 双精度（<d）
    TEXT    4 B 无符号长度（<I）+ UTF-8 字节
    BOOLEAN 1 B：0x00 = False，0x01 = True
"""

from __future__ import annotations

import math
import struct

from contracts.ast import ColumnDef, SqlType, Value
from contracts.errors import E_STORAGE, E_TYPE_MISMATCH, SqlError
from storage.constants import BOOL_FALSE_BYTE, BOOL_SIZE, BOOL_TRUE_BYTE


_INT = struct.Struct("<q")
_REAL = struct.Struct("<d")
_TEXT_LEN = struct.Struct("<I")

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def normalize_value(column: ColumnDef, value: Value) -> Value:
    """按列类型校验并归一化单个值；失败抛契约错误码。

    这是公开方法边界与索引键共用的规则：INT 显式拒 bool 且必须落在 64 位范围；
    REAL 收 int 或 float 并统一成 float、拒绝 NaN/±Inf 与超大整数；TEXT 只收
    可 UTF-8 编码的 str；BOOLEAN 只收 `type(value) is bool`。
    """
    if column.type is SqlType.INT:
        if isinstance(value, bool) or type(value) is not int:
            raise SqlError(E_TYPE_MISMATCH, f"INT column {column.name!r} got {value!r}")
        if not _INT64_MIN <= value <= _INT64_MAX:
            raise SqlError(
                E_TYPE_MISMATCH,
                f"INT column {column.name!r} out of 64-bit range",
            )
        return value
    if column.type is SqlType.REAL:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SqlError(
                E_TYPE_MISMATCH, f"REAL column {column.name!r} got {value!r}"
            )
        try:
            real_value = float(value)
        except OverflowError:
            # 巨 int（如 2**1024）转 double 会抛 OverflowError；
            # 表示不了的数值按范围不符拒绝，不许把裸异常漏给调用方。
            raise SqlError(
                E_TYPE_MISMATCH,
                f"REAL column {column.name!r} out of double range",
            ) from None
        if not math.isfinite(real_value):
            raise SqlError(
                E_TYPE_MISMATCH,
                f"REAL column {column.name!r} must be finite, got {value!r}",
            )
        return real_value
    if column.type is SqlType.BOOLEAN:
        if type(value) is not bool:
            raise SqlError(
                E_TYPE_MISMATCH, f"BOOLEAN column {column.name!r} got {value!r}"
            )
        return value
    # SqlType.TEXT
    if type(value) is not str:
        raise SqlError(E_TYPE_MISMATCH, f"TEXT column {column.name!r} got {value!r}")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise SqlError(
            E_TYPE_MISMATCH,
            f"TEXT column {column.name!r} is not utf-8 encodable",
        ) from None
    return value


def encode_value(column: ColumnDef, value: Value) -> bytes:
    """把一个已归一化的值编码成字节（布局见模块 docstring）。"""
    if column.type is SqlType.INT:
        return _INT.pack(value)
    if column.type is SqlType.REAL:
        return _REAL.pack(value)
    if column.type is SqlType.BOOLEAN:
        return bytes((BOOL_TRUE_BYTE if value else BOOL_FALSE_BYTE,))
    raw = value.encode("utf-8")
    return _TEXT_LEN.pack(len(raw)) + raw


def decode_value(
    column: ColumnDef, raw: bytes, offset: int
) -> tuple[Value, int]:
    """从 `raw` 的 `offset` 处解出一个值，返回 (值, 新偏移)。

    截断 / 非法 BOOLEAN 字节 / 非 UTF-8 TEXT 一律 E_STORAGE（存储损坏）。
    """
    if column.type is SqlType.INT:
        try:
            (value,) = _INT.unpack_from(raw, offset)
        except struct.error as exc:
            raise SqlError(E_STORAGE, "corrupt value: truncated int") from exc
        return value, offset + _INT.size

    if column.type is SqlType.REAL:
        try:
            (value,) = _REAL.unpack_from(raw, offset)
        except struct.error as exc:
            raise SqlError(E_STORAGE, "corrupt value: truncated real") from exc
        return value, offset + _REAL.size

    if column.type is SqlType.BOOLEAN:
        chunk = raw[offset : offset + BOOL_SIZE]
        if len(chunk) < BOOL_SIZE:
            raise SqlError(E_STORAGE, "corrupt value: truncated boolean")
        byte = chunk[0]
        if byte not in (BOOL_FALSE_BYTE, BOOL_TRUE_BYTE):
            raise SqlError(E_STORAGE, "corrupt boolean value")
        return byte == BOOL_TRUE_BYTE, offset + BOOL_SIZE

    # SqlType.TEXT
    try:
        (length,) = _TEXT_LEN.unpack_from(raw, offset)
    except struct.error as exc:
        raise SqlError(E_STORAGE, "corrupt value: truncated text length") from exc
    start = offset + _TEXT_LEN.size
    chunk = raw[start : start + length]
    if len(chunk) < length:
        raise SqlError(E_STORAGE, "corrupt value: truncated text")
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SqlError(E_STORAGE, "corrupt value: invalid utf-8 text") from exc
    return text, start + length
