"""五种测量模式的执行函数：把场景跑成"行集 + 取证"。

- SQL 模式走 `Runner.execute(sql, physical=...)`，取证来自
  `Runner.last_access_paths`（理由码与两个代价估计）；
- 存储模式直接调 `BaseStorage` 的公开方法，绕过规划器，只量 B 的成本。

两者的行形状统一为 `tuple[tuple[Value, ...], ...]`（丢掉 row_id），
这样同场景跨模式的**行多重集**才可直接比较。
"""

from __future__ import annotations

from dataclasses import dataclass

from compiler import parse, parse_script
from contracts.ast import Value
from contracts.errors import SqlError
from runner import Runner
from storage import DatabaseServer

from bench.dataset import DatasetSpec
from bench.scenarios import Scenario


SQL_MODES: tuple[str, ...] = ("sql-auto", "sql-seq", "sql-index")
API_MODES: tuple[str, ...] = ("api-seq", "api-index")
ALL_MODES: tuple[str, ...] = SQL_MODES + API_MODES

PREWARMED_MODES: tuple[str, ...] = SQL_MODES
"""需要在计数前调用一次 `statistics()` 的模式（C 设计文档 §7.4）。

统计的首次调用要建立精确极值基线（读满数据页）。不预热就会把这笔成本记到
恰好第一个跑的模式头上，三模式便不可比。存储模式不预热：它们根本不经过
规划器，报的才是纯 B 成本。
"""

_SQL_PHYSICAL = {"sql-auto": "auto", "sql-seq": "seq", "sql-index": "index"}


@dataclass(frozen=True, slots=True)
class Outcome:
    """一次执行的观察结果：行集、选路取证或错误码。"""

    rows: tuple[tuple[Value, ...], ...]
    reason: str | None = None
    selectivity: float | None = None
    estimated_rows: float | None = None
    seq_cost: float | None = None
    index_cost: float | None = None
    error_code: str | None = None
    error_message: str | None = None


def execute_mode(
    server: DatabaseServer,
    spec: DatasetSpec,
    scenario: Scenario,
    mode: str,
) -> Outcome:
    """在给定 server 上按 mode 执行场景；错误按错误码记录而不抛出。"""

    if mode in SQL_MODES:
        return _execute_sql(server, scenario, mode)
    if mode in API_MODES:
        return _execute_api(server, spec, scenario, mode)
    raise ValueError(f"unknown bench mode: {mode!r}")


def _execute_sql(
    server: DatabaseServer,
    scenario: Scenario,
    mode: str,
) -> Outcome:
    """端到端：真解析、真绑定、真选路、真执行。"""

    runner = Runner(server=server, parse=parse, parse_script=parse_script)
    try:
        result = runner.execute(scenario.sql, physical=_SQL_PHYSICAL[mode])
    except SqlError as error:
        return Outcome(rows=(), error_code=error.code, error_message=str(error))
    paths = runner.last_access_paths
    path = paths[0] if paths else None
    return Outcome(
        rows=tuple(result.rows or ()),
        reason=None if path is None else path.reason,
        selectivity=None if path is None else path.selectivity,
        estimated_rows=None if path is None else path.estimated_rows,
        seq_cost=None if path is None else path.seq_cost,
        index_cost=None if path is None else path.index_cost,
    )


def _execute_api(
    server: DatabaseServer,
    spec: DatasetSpec,
    scenario: Scenario,
    mode: str,
) -> Outcome:
    """存储级：不经过 C 的规划器，直接调 B 的公开接口。"""

    storage = server.connect(spec.database)
    position = spec.column_position(scenario.column)
    try:
        if mode == "api-seq":
            rows = tuple(
                values
                for _row_id, values in storage.scan(spec.table)
                if scenario.matches(values, position)
            )
        elif scenario.kind == "lookup":
            rows = tuple(
                values
                for _row_id, values in storage.index_lookup(
                    spec.table, scenario.column, scenario.key
                )
            )
        else:
            rows = tuple(
                values
                for _row_id, values in storage.index_range(
                    spec.table,
                    scenario.column,
                    scenario.lower,
                    scenario.upper,
                    lower_inclusive=scenario.lower_inclusive,
                    upper_inclusive=scenario.upper_inclusive,
                )
            )
    except SqlError as error:
        return Outcome(rows=(), error_code=error.code, error_message=str(error))
    return Outcome(rows=rows)
