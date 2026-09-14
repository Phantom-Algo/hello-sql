"""V3 DROP INDEX 语句体与 AST 构建测试。

本文件通过编译模块公开的 parse 入口验证索引删除文法。测试关注 A 模块的
职责：完整消费 DROP INDEX 与索引名，构建 DropIndexStmt，并在 AST 边界
将索引名统一转换为小写。索引是否存在以及不存在时返回哪个存储错误属于
B 模块，本测试不连接 Catalog 或 Storage。
"""

from __future__ import annotations

import pytest

from compiler import parse
from contracts.ast import DropIndexStmt


# 两组用例同时覆盖标准写法、可选分号、关键字大小写不敏感和索引名小写化。
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "DROP INDEX idx_users_id;",
            DropIndexStmt(index_name="idx_users_id"),
        ),
        (
            "drop index Idx_Users_ID",
            DropIndexStmt(index_name="idx_users_id"),
        ),
    ],
)
def test_drop_index_builds_normalized_ast(
    source: str,
    expected: DropIndexStmt,
) -> None:
    """验证合法 DROP INDEX 会生成类型正确且索引名已小写化的 AST。

    Args:
        source: 当前用例的完整 SQL，可使用不同大小写并可带结束分号。
        expected: 根据 V3 共享契约预先构建的 DropIndexStmt。
    """
    statement = parse(source)

    assert statement == expected
    assert isinstance(statement, DropIndexStmt)
