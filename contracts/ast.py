"""契约 V3.1：SQL 编译模块输出、运行模块输入的共享 AST。

V2 在 V1.1 的基础上增加：
- BOOLEAN 类型与布尔字面量；
- AND / OR / NOT、括号优先级对应的表达式树；
- 列与列比较、表限定列和表别名；
- INNER JOIN；
- 带源码范围的多语句解析结果。

V3 在 V2 的基础上增加：
- 索引 DDL：CREATE INDEX / DROP INDEX 的语句节点（仅单列、非唯一索引）。

不变式：
- database / table / column / alias 名均已转为小写且非空；
- 数字和布尔字面量已经转换为 Python 原生值；
- 括号不单独保留节点，其作用体现在表达式树结构中；
- V2 只支持 INNER JOIN，不包含 ORDER BY、LIMIT、NULL、聚合和子查询；
- V3 不含 UNIQUE 与多列组合索引，AST 不为此预留字段。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias


class SqlType(Enum):
    """V2 支持的四种 SQL 列类型。"""

    INT = "INT"
    TEXT = "TEXT"
    REAL = "REAL"
    BOOLEAN = "BOOLEAN"


Value: TypeAlias = int | str | float | bool
"""SQL 字面量与存储值的 Python 表示；V2 不允许 NULL。"""


@dataclass(frozen=True)
class ColumnDef:
    """CREATE TABLE 中按声明顺序保存的一列。"""

    name: str
    type: SqlType


@dataclass(frozen=True)
class Assignment:
    """UPDATE SET 中的一项；同列重复赋值仍采用后者覆盖前者。"""

    column: str
    value: Value


# ---------- 表达式 ----------


@dataclass(frozen=True)
class Column:
    """列引用；qualifier 是可选的表名或表别名。"""

    name: str
    qualifier: str | None = None


@dataclass(frozen=True)
class Literal:
    """已经转换为 Python 原生值的 SQL 字面量。"""

    value: Value


ScalarExpr: TypeAlias = Column | Literal


@dataclass(frozen=True)
class Cmp:
    """比较表达式；两侧均可为列引用或字面量。"""

    left: ScalarExpr
    op: str
    right: ScalarExpr


@dataclass(frozen=True)
class And:
    """逻辑与；左右子树都必须在语义绑定后得到 BOOLEAN。"""

    left: Expr
    right: Expr


@dataclass(frozen=True)
class Or:
    """逻辑或；左右子树都必须在语义绑定后得到 BOOLEAN。"""

    left: Expr
    right: Expr


@dataclass(frozen=True)
class Not:
    """逻辑非；operand 必须在语义绑定后得到 BOOLEAN。"""

    operand: Expr


Expr: TypeAlias = Column | Literal | Cmp | And | Or | Not


# ---------- 查询来源 ----------


class JoinType(Enum):
    """V2 仅开放 INNER JOIN，保留枚举以便后续兼容扩展。"""

    INNER = "INNER"


@dataclass(frozen=True)
class TableRef:
    """FROM 或 JOIN 中的表引用。"""

    name: str
    alias: str | None = None

    # 此属性返回名称绑定阶段使用的有效限定符，存在别名时优先使用别名。
    @property
    def qualifier(self) -> str:
        """返回名称绑定时使用的限定符：别名优先，否则使用表名。"""

        return self.alias or self.name


@dataclass(frozen=True)
class JoinClause:
    """按 SQL 书写顺序保存的一项 INNER JOIN。"""

    right: TableRef
    on: Expr
    kind: JoinType = JoinType.INNER


# ---------- 数据库语句 ----------


@dataclass(frozen=True)
class CreateDatabaseStmt:
    name: str


@dataclass(frozen=True)
class DropDatabaseStmt:
    name: str


@dataclass(frozen=True)
class UseDatabaseStmt:
    name: str


# ---------- 表语句 ----------


@dataclass(frozen=True)
class CreateTableStmt:
    table: str
    columns: tuple[ColumnDef, ...]


@dataclass(frozen=True)
class DropTableStmt:
    table: str


@dataclass(frozen=True)
class InsertStmt:
    table: str
    values: tuple[Value, ...]


@dataclass(frozen=True)
class SelectStmt:
    """SELECT 查询。

    字段顺序沿用 V1.1，便于 A、C 在各自分支渐进迁移。V2 完成后，显式
    columns 必须由 Column 组成，table 必须是 TableRef。迁移期旧实现传入
    str 不会触发运行时构造错误，但不属于最终 V2 合规输出。
    """

    columns: tuple[Column, ...] | None
    table: TableRef
    where: Expr | None
    joins: tuple[JoinClause, ...] = ()


@dataclass(frozen=True)
class UpdateStmt:
    table: str
    assignments: tuple[Assignment, ...]
    where: Expr | None


@dataclass(frozen=True)
class DeleteStmt:
    table: str
    where: Expr | None


# ---------- 索引语句（V3） ----------


@dataclass(frozen=True)
class CreateIndexStmt:
    """CREATE INDEX：单列、非唯一索引。

    本轮不支持 UNIQUE 与多列组合索引；索引名在同一数据库内唯一，
    与表名、列名共用标识符规则并在进入 AST 前统一小写。
    """

    index_name: str
    table: str
    column: str


@dataclass(frozen=True)
class DropIndexStmt:
    """DROP INDEX：索引名在数据库内唯一，因此不需要表名。"""

    index_name: str


Statement: TypeAlias = (
    CreateDatabaseStmt
    | DropDatabaseStmt
    | UseDatabaseStmt
    | CreateTableStmt
    | DropTableStmt
    | CreateIndexStmt
    | DropIndexStmt
    | InsertStmt
    | SelectStmt
    | UpdateStmt
    | DeleteStmt
)


# ---------- 多语句输入 ----------


@dataclass(frozen=True)
class SourceSpan:
    """一条语句在完整 SQL 输入中的一基闭区间位置。

    parse_script 忽略语句两侧的分隔空白；源码中存在结束分号时，闭区间包含
    分号，否则结束位置指向语句最后一个 Token 的最后一个字符。
    """

    start_line: int
    start_col: int
    end_line: int
    end_col: int


@dataclass(frozen=True)
class ParsedStatement:
    """parse_script 的单条输出，保留 AST、原文和全局源码范围。

    sql 与 span 表示同一段连续源码：不包含语句前后的分隔空白，但保留语句
    内部的原始大小写和空白；原输入带结束分号时也保留该分号。
    """

    statement: Statement
    sql: str
    span: SourceSpan


Script: TypeAlias = tuple[ParsedStatement, ...]
