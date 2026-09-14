"""模块 A 的 V3 索引关键字词法测试。

本文件只验收 V3 步骤一的 Token 层契约：``INDEX`` 必须被视为
不区分大小写的 SQL 保留字，同时 Token 仍应保留用户输入的原始文本、
行列位置与源码偏移。完整索引 DDL 的 Token 顺序也在本文件验证；AST 构建
由 Parser 专用测试负责，避免把词法分类和语法结构混为同一层职责。
"""

from __future__ import annotations

import pytest

from compiler.lexer import tokenize
from compiler.tokens import TokenType


# 该参数化测试分别使用全大写、全小写和混合大小写输入，
# 用一组统一断言锁定 SQL 关键字大小写不敏感的词法契约。
@pytest.mark.parametrize("source", ["INDEX", "index", "Index"])
def test_index_keyword_is_reserved_case_insensitively(source: str) -> None:
    """验证三种大小写形式都生成 ``KW_INDEX`` 并保留完整源位置。

    Lexer 通过将扫描到的标识符转为大写后查询 ``KEYWORDS``，
    所以三种输入应共享一个 TokenType；``lexeme`` 则不能被标准化，
    否则错误提示和 SourceSpan 将无法还原用户真实输入。该测试还检查
    EOF 偏移，确保新关键字没有破坏已有的源码定位机制。

    Args:
        source: 当前用例使用的 ``INDEX`` 大小写形式。
    """
    keyword, eof = tokenize(source)

    assert keyword.type is TokenType.KW_INDEX
    assert keyword.lexeme == source
    assert keyword.position.line == 1
    assert keyword.position.column == 1
    assert keyword.start_offset == 0
    assert keyword.end_offset == len(source)
    assert eof.type is TokenType.EOF
    assert eof.start_offset == eof.end_offset == len(source)


# 该测试使用同一输入中的 CREATE/DROP INDEX，验证完整词法流顺序和偏移连续性。
def test_index_ddl_token_stream_preserves_order_and_offsets() -> None:
    """验证两条索引 DDL 被拆为预期 Token，并保留原始文本与源码偏移。

    该用例不仅检查 INDEX 本身，还确认 CREATE、ON、括号、分号和 DROP 与其
    组合后不会发生错误合并。Lexer 应保留混合大小写 lexeme，关键字分类则
    统一为 KW_*；每个 Token 的半开偏移必须能从原 SQL 精确切回原文。
    """
    source = "CREATE INDEX Idx ON Users (ID); DROP INDEX Idx;"

    tokens = tokenize(source)

    assert [token.type for token in tokens] == [
        TokenType.KW_CREATE,
        TokenType.KW_INDEX,
        TokenType.IDENTIFIER,
        TokenType.KW_ON,
        TokenType.IDENTIFIER,
        TokenType.LPAREN,
        TokenType.IDENTIFIER,
        TokenType.RPAREN,
        TokenType.SEMICOLON,
        TokenType.KW_DROP,
        TokenType.KW_INDEX,
        TokenType.IDENTIFIER,
        TokenType.SEMICOLON,
        TokenType.EOF,
    ]
    assert [token.lexeme for token in tokens[:-1]] == [
        "CREATE", "INDEX", "Idx", "ON", "Users", "(", "ID", ")", ";",
        "DROP", "INDEX", "Idx", ";",
    ]
    assert all(
        source[token.start_offset:token.end_offset] == token.lexeme
        for token in tokens
    )
    assert tokens[-1].start_offset == tokens[-1].end_offset == len(source)
