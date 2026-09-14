"""引入 V3 INDEX 保留字后的 V1/V2 精确 AST 回归测试。

新增 Token 和 DDL 分派最容易误伤 CREATE/DROP 的旧分支，或改变 SELECT 的
既有递归下降入口。本文件选取一条 V1 CREATE TABLE 和一条覆盖别名、JOIN、
限定列、NOT、BOOLEAN 的 V2 SELECT，直接比较完整 AST，作为现有 Golden 与
专项测试之外的 V3 邻接回归保护。
"""

from __future__ import annotations

from compiler import parse
from contracts.ast import (
    Cmp,
    Column,
    ColumnDef,
    CreateTableStmt,
    JoinClause,
    JoinType,
    Literal,
    Not,
    SelectStmt,
    SqlType,
    TableRef,
)


# 该测试保护与 CREATE INDEX 共用一级分派的 V1 CREATE TABLE 语法。
def test_v1_create_table_ast_is_unchanged_by_index_ddl() -> None:
    """验证 CREATE TABLE 仍生成字段顺序和类型完全一致的 V1 AST。"""
    statement = parse("CREATE TABLE Users (ID INT, Name TEXT);")

    assert statement == CreateTableStmt(
        table="users",
        columns=(
            ColumnDef(name="id", type=SqlType.INT),
            ColumnDef(name="name", type=SqlType.TEXT),
        ),
    )


# 该测试保护不经过索引 DDL 分派的 V2 SELECT 表达式与 JOIN 解析链。
def test_v2_select_ast_is_unchanged_by_index_ddl() -> None:
    """验证 V2 限定列、别名、INNER JOIN、NOT 和 BOOLEAN AST 保持不变。"""
    statement = parse(
        "SELECT u.id FROM Users u "
        "INNER JOIN Orders o ON u.id = o.user_id "
        "WHERE NOT u.enabled = FALSE;"
    )

    assert statement == SelectStmt(
        columns=(Column(name="id", qualifier="u"),),
        table=TableRef(name="users", alias="u"),
        where=Not(
            operand=Cmp(
                left=Column(name="enabled", qualifier="u"),
                op="=",
                right=Literal(value=False),
            )
        ),
        joins=(
            JoinClause(
                right=TableRef(name="orders", alias="o"),
                on=Cmp(
                    left=Column(name="id", qualifier="u"),
                    op="=",
                    right=Column(name="user_id", qualifier="o"),
                ),
                kind=JoinType.INNER,
            ),
        ),
    )
