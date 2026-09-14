"""AST 可视化转换器的纯逻辑测试。

这些测试不创建 tkinter 窗口，因此可在无图形界面的环境运行。它们验证展示树
确实来自真实 AST 的类型、字段和嵌套表达式，而不是在可视化层手写 SQL 规则。
"""

from __future__ import annotations

import pytest

from compiler import parse
from contracts.ast import Cmp, Column, Literal, SelectStmt, TableRef
from tests.A_tests.ast_tree_adapter import TreeNode, ast_to_tree


# 此辅助函数递归收集可视化树标签，便于核对 V2 深层 AST 节点是否全部出现。
def _collect_labels(node: TreeNode) -> tuple[str, ...]:
    """按先序遍历返回当前节点及全部后代节点的标签。

    Args:
        node: 需要检查的通用可视化树根节点。

    Returns:
        按“根节点在前、子树从左到右”排列的不可变标签元组。
    """
    labels = [node.label]
    for child in node.children:
        labels.extend(_collect_labels(child))
    return tuple(labels)


# 此测试确认 Statement 根节点、字段节点和嵌套 Cmp 条件都会被完整保留。
def test_ast_to_tree_preserves_select_statement_structure() -> None:
    """将带 WHERE 的 SelectStmt 转换后，应保留类型、字段和值的树形层级。"""
    statement = SelectStmt(
        columns=(Column("name"),),
        table=TableRef("users"),
        where=Cmp(left=Column("age"), op=">=", right=Literal(18)),
    )

    tree = ast_to_tree(statement)
    assert tree.label == "SelectStmt"
    assert [child.label for child in tree.children] == [
        "columns",
        "table",
        "where",
        "joins",
    ]
    assert tree.children[0].children[0].label == "tuple[1]"
    assert tree.children[1].children[0].label == "TableRef"
    assert tree.children[2].children[0].label == "Cmp"
    assert tree.children[3].children[0].label == "tuple[0]"


# 此测试通过真实 Parser 构造复杂 SELECT，验证可视化器同步支持全部 V2 新字段。
def test_ast_to_tree_displays_v2_select_fields_and_expression_nodes() -> None:
    """确认别名、限定列、BOOLEAN、逻辑表达式和 JOIN 均进入展示树。"""
    statement = parse(
        "SELECT u.id, o.user_id "
        "FROM users u "
        "INNER JOIN orders o ON u.id = o.user_id AND NOT o.deleted "
        "WHERE u.active = TRUE OR o.total > 100;"
    )

    tree = ast_to_tree(statement)
    labels = _collect_labels(tree)

    assert tree.label == "SelectStmt"
    assert [child.label for child in tree.children] == [
        "columns",
        "table",
        "where",
        "joins",
    ]
    assert labels.count("Column") == 7
    assert labels.count("TableRef") == 2
    assert labels.count("JoinClause") == 1
    assert "JoinType.INNER" in labels
    assert "And" in labels
    assert "Or" in labels
    assert "Not" in labels
    assert "True" in labels


# 两个参数确认旧版 Canvas 适配器也通过通用 dataclass 逻辑展示 V3 索引节点。
@pytest.mark.parametrize(
    ("sql", "root_label", "field_labels", "value_labels"),
    [
        (
            "CREATE INDEX idx_users_id ON users (id);",
            "CreateIndexStmt",
            ["index_name", "table", "column"],
            {"'idx_users_id'", "'users'", "'id'"},
        ),
        (
            "DROP INDEX idx_users_id;",
            "DropIndexStmt",
            ["index_name"],
            {"'idx_users_id'"},
        ),
    ],
)
def test_ast_to_tree_displays_v3_index_nodes(
    sql: str,
    root_label: str,
    field_labels: list[str],
    value_labels: set[str],
) -> None:
    """验证索引 AST 的根类型、字段分支和值叶子都进入可视化树。

    Args:
        sql: 用真实 Parser 生成索引 AST 的 SQL。
        root_label: 可视化根节点应显示的 AST 类型名。
        field_labels: 根节点下按 dataclass 定义顺序展示的字段名。
        value_labels: 整棵树中必须出现的索引、表或列名称叶子。
    """
    tree = ast_to_tree(parse(sql))
    labels = set(_collect_labels(tree))

    assert tree.label == root_label
    assert [child.label for child in tree.children] == field_labels
    assert value_labels <= labels
