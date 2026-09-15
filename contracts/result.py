"""契约 V3.1：单语句与多语句执行结果。"""

from __future__ import annotations

from dataclasses import dataclass

from contracts.ast import SourceSpan, Value
from contracts.errors import SqlError


@dataclass
class QueryResult:
    """一次执行的结果。三种形态互斥：
    - CREATE / DROP：affected_rows = 0，其余为 None；
    - INSERT / UPDATE / DELETE：affected_rows = 实际影响行数（>=0）；
    - SELECT：columns + rows，affected_rows = None。
    """

    columns: tuple[str, ...] | None = None
    rows: tuple[tuple[Value, ...], ...] | None = None
    affected_rows: int | None = None


@dataclass
class StatementResult:
    """脚本中一条语句的执行结果。

    result 与 error 恰有一个非 None；elapsed_ms 包含该语句从语义绑定到执行
    完成或失败的耗时，不包含用户在 TUI 中的编辑时间。
    """

    sql: str
    span: SourceSpan
    result: QueryResult | None = None
    error: SqlError | None = None
    elapsed_ms: float = 0.0

    def __post_init__(self) -> None:
        if (self.result is None) == (self.error is None):
            raise ValueError("StatementResult requires exactly one of result or error")
        if self.elapsed_ms < 0:
            raise ValueError("elapsed_ms must be non-negative")


@dataclass
class ScriptResult:
    """一次多语句执行的汇总结果，语句顺序与源码顺序一致。"""

    statements: tuple[StatementResult, ...]
    stopped_early: bool = False
