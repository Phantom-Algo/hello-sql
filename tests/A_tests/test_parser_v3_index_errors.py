"""V3 索引 DDL 错误语法与源码位置测试。

本文件锁定模块 A 对非法 CREATE INDEX、DROP INDEX 的公开错误契约：所有
结构缺失或不支持的 UNIQUE 写法都必须抛出 ParseError，其错误码固定为
E_SYNTAX，行列位置来自 Lexer 在完整 SQL 中记录的一基坐标。测试不判断
索引、表和列是否真实存在，因为这些属于 C/B 的语义与存储职责。
"""

from __future__ import annotations

import pytest

from compiler import parse
from contracts.errors import E_SYNTAX, ParseError


# 每个用例对应计划中要求拒绝的一条 SQL，并锁定首个不符合文法的 Token 列号。
@pytest.mark.parametrize(
    ("source", "expected_col", "expected_message"),
    [
        ("CREATE INDEX;", 13, "expected an identifier"),
        ("CREATE INDEX idx;", 17, "expected ON after CREATE INDEX name"),
        ("CREATE INDEX idx ON users;", 26, "expected '(' before CREATE INDEX column"),
        ("CREATE INDEX idx ON users ();", 28, "expected an identifier"),
        ("DROP INDEX;", 11, "expected an identifier"),
        (
            "CREATE UNIQUE INDEX idx ON users (id);",
            8,
            "expected DATABASE, TABLE, or INDEX after CREATE",
        ),
    ],
)
def test_invalid_index_ddl_reports_syntax_error_at_first_bad_token(
    source: str,
    expected_col: int,
    expected_message: str,
) -> None:
    """验证指定非法索引 SQL 返回 E_SYNTAX 和准确的一基源码位置。

    Parser 的 expect 与 parse_identifier 都直接使用当前 Token 的
    SourcePosition 构造 ParseError，因此错误应落在分号、右括号或不支持的
    UNIQUE 上，而不是笼统指向语句开头。逐条固定列号可以防止后续重构在
    报错前多消费一个 Token，导致答辩界面高亮错误字符。

    Args:
        source: 当前需要拒绝的完整索引 DDL SQL。
        expected_col: 第一个错误 Token 在第一行中的一基列号。
        expected_message: 能说明缺失结构或不支持目标的稳定错误提示片段。
    """
    with pytest.raises(ParseError) as error_info:
        parse(source)

    error = error_info.value
    assert error.code == E_SYNTAX
    assert (error.line, error.col) == (1, expected_col)
    assert expected_message in error.message


# 该测试将错误放在第二行，证明位置不是针对单行 SQL 重新计算的局部列号。
def test_invalid_index_ddl_preserves_multiline_global_position() -> None:
    """验证跨行 CREATE INDEX 的错误位置相对于完整输入而不是局部片段。

    表名位于第二行，Lexer 会继续维护全局行列状态；空列列表中的右括号应
    被报告为第 2 行第 8 列。这同时验证 Parser 没有自行估算或覆盖 Token
    已携带的位置。
    """
    source = "CREATE INDEX idx ON\nusers ();"

    with pytest.raises(ParseError) as error_info:
        parse(source)

    error = error_info.value
    assert error.code == E_SYNTAX
    assert (error.line, error.col) == (2, 8)
