"""V3 ``CREATE INDEX`` 语句体与 AST 构建测试。

本文件通过编译模块公开的 ``parse`` 入口验证单列非唯一索引
文法。测试关注 Parser 的职责：消费完整 Token 序列、构建
``CreateIndexStmt``，并在 AST 边界将索引名、表名与列名统一转为
小写。索引是否重名以及表、列是否存在不属于 A 模块，因此本文件
不连接 Catalog 或 Storage。
"""

from __future__ import annotations

import pytest

from compiler import parse
from contracts.ast import CreateIndexStmt


# 两组用例同时覆盖标准写法、可选分号、关键字大小写不敏感
# 以及三种用户标识符在 AST 边界的小写化规则。
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "CREATE INDEX idx_users_id ON users (id);",
            CreateIndexStmt(
                index_name="idx_users_id",
                table="users",
                column="id",
            ),
        ),
        (
            "create index Idx_Users_ID on Users (ID)",
            CreateIndexStmt(
                index_name="idx_users_id",
                table="users",
                column="id",
            ),
        ),
    ],
)
def test_create_index_builds_normalized_ast(
    source: str,
    expected: CreateIndexStmt,
) -> None:
    """验证合法 CREATE INDEX 会生成字段完整且名称已小写化的 AST。

    Args:
        source: 当前用例的完整 SQL，可使用不同大小写并可带结束分号。
        expected: 根据 V3 共享契约预先构建的 CreateIndexStmt。
    """
    statement = parse(source)

    assert statement == expected
    assert isinstance(statement, CreateIndexStmt)
