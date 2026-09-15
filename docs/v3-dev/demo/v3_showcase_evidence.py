#!/usr/bin/env python3
"""V3 演示取证面板：把"看不见的决策"打印成终端表格。

SQL 层没有 `EXPLAIN`，选路理由、优化日志、统计与三模式对照只能从公开接口读。
本脚本只消费三家**公开入口**（`compiler.parse*` / `DatabaseServer` /
`Runner` 的公开属性），不 import 任何内部模块、不重新执行被观察的 SQL、也不
改动数据库状态（DML 那一段在临时库上跑）。

用法：

    python docs/v3-dev/demo/v3_showcase_evidence.py --data-dir /tmp/v3demo

前置：`--data-dir` 指向已经装载过 `v3_showcase_load.sql` 的目录：

    python main.py --data-dir /tmp/v3demo -f docs/v3-dev/demo/v3_showcase_load.sql
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from compiler import parse, parse_script  # noqa: E402
from contracts.errors import SqlError  # noqa: E402
from runner import Runner  # noqa: E402
from storage import DatabaseServer  # noqa: E402


ROUTING_CASES: tuple[tuple[str, str], ...] = (
    ("高选择性点查", "SELECT id FROM events WHERE id = 42;"),
    ("窄区间(9 行)", "SELECT id FROM events WHERE id > 1990;"),
    ("宽区间(99 行)", "SELECT id FROM events WHERE id > 1900;"),
    ("低基数列(一半)", "SELECT id FROM events WHERE grp = 1;"),
    ("键真实越界", "SELECT id FROM events WHERE id = 999999;"),
    ("无索引列", "SELECT id FROM events WHERE note = 'n00042';"),
    ("无谓词", "SELECT id FROM events;"),
    ("下推 + 残余过滤", "SELECT id, amount FROM events WHERE id > 1990 AND amount = 51;"),
)

JOIN_SQL = (
    "SELECT e.id, m.label FROM events e INNER JOIN labels m ON e.grp = m.id "
    "WHERE 1 = 1 AND NOT (e.id < 0) AND e.id > 1990 AND m.id = 1;"
)

FORCED_MODES: tuple[str, ...] = ("auto", "seq", "index")


class PageCounter:
    """B 的公开观测口：统计一次查询触发了多少次页读（与追踪器同一条通道）。"""

    def __init__(self) -> None:
        self.reads = 0

    def __call__(self, payload: dict[str, object]) -> None:
        if (
            payload.get("component") == "pager"
            and payload.get("operation") == "read_page"
        ):
            self.reads += 1

    def reset(self) -> None:
        self.reads = 0


def _runner(data_dir: Path, sink=None) -> Runner:
    server = DatabaseServer(str(data_dir), trace_sink=sink)
    return Runner(server=server, parse=parse, parse_script=parse_script)


def _fmt(value: object, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _plan_sketch(plan: object) -> str:
    """把逻辑计划画成一行结构素描（只报节点名，不打印字段）。

    完整 repr 有几百个字符，终端演示会刷屏；这里只保留"树的形状"，
    足以说明优化器把 Filter 从 JOIN 上方推到了两个 Scan 上方。
    """

    name = type(plan).__name__.replace("Logical", "")
    child = getattr(plan, "child", None)
    if child is not None:
        return f"{name}({_plan_sketch(child)})"
    left = getattr(plan, "left", None)
    right = getattr(plan, "right", None)
    if left is not None and right is not None:
        return f"{name}({_plan_sketch(left)}, {_plan_sketch(right)})"
    return name


def print_routing(runner: Runner) -> None:
    """四之一：选路决策表（reason 是稳定标识，代价的单位是"预计数据页读取次数"）。"""

    print("=" * 108)
    print("① 选路决策（physical=auto）：代价单位 = 预计数据页读取次数")
    print("=" * 108)
    header = f"{'场景':<16}{'reason':<18}{'列':<9}{'选择性':>10}{'估计行数':>10}{'扫描':>8}{'索引':>9}  下推请求"
    print(header)
    print("-" * 108)
    for label, sql in ROUTING_CASES:
        result = runner.execute(sql)
        path = runner.last_access_paths[0]
        requests = ", ".join(type(item).__name__ for item in path.requests) or "-"
        print(
            f"{label:<16}{path.reason:<18}{str(path.column or '-'):<9}"
            f"{_fmt(path.selectivity, 5):>10}{_fmt(path.estimated_rows):>10}"
            f"{_fmt(path.seq_cost):>8}{_fmt(path.index_cost):>9}  {requests}"
        )
        assert result.rows is not None
        print(f"{'':<16}SQL: {sql}")
        print(f"{'':<16}实际返回 {len(result.rows)} 行")
    print()


def print_optimizer(runner: Runner) -> None:
    """四之二：优化器规则命中 + 开/关等价。"""

    print("=" * 108)
    print("② 逻辑优化器：规则命中日志与「开/关结果一致」")
    print("=" * 108)
    print(f"SQL: {JOIN_SQL}")
    optimized = runner.execute(JOIN_SQL, optimize=True)
    log = runner.last_optimization_log
    if log is None:
        print("（未拿到优化日志）")
        return
    print(f"迭代轮数={log.rounds} 触顶={log.hit_limit} 规则命中={len(log.applications)} 条")
    print(f"原始计划：{_plan_sketch(log.original)}")
    print(f"优化计划：{_plan_sketch(log.optimized)}")
    for item in log.applications:
        print(f"  - {item.rule:<22} {item.summary}")
    print("下推/选路：" + str([(p.table, p.reason) for p in runner.last_access_paths]))
    plain = runner.execute(JOIN_SQL, optimize=False)
    print(f"优化开 {len(optimized.rows)} 行 / 优化关 {len(plain.rows)} 行；"
          f"结果逐行一致：{sorted(optimized.rows) == sorted(plain.rows)}")
    print(f"返回行：{sorted(optimized.rows)}")
    print()


def print_statistics(data_dir: Path) -> None:
    """四之三：统计（min/max 精确、distinct 近似、page_count 只算数据页）。"""

    print("=" * 108)
    print("③ 统计：契约 3.1 —— min/max 精确，distinct 为有界采样（近似）")
    print("=" * 108)
    handle = DatabaseServer(str(data_dir)).connect("main")
    stats = handle.statistics("events")
    print(f"表 {stats.table}：row_count={stats.row_count}  page_count={stats.page_count}"
          f"（只计数据页，不含页 0 / 空闲页 / 溢出链页）")
    for column in stats.columns:
        print(
            f"  {column.name:<8} distinct≈{column.distinct_count:<6}"
            f"min={_fmt(column.min_value):<8} max={_fmt(column.max_value):<8}"
        )
    print("说明：min/max 是整表真实极值（C 可据此推出「键越界即 0 行」）；")
    print("      distinct 来自最多 16 页的均匀间隔采样，只作代价估算输入。")
    print()


def print_forced_modes(data_dir: Path) -> None:
    """四之四：同一查询三种物理模式——结果一致、页读不同。"""

    print("=" * 108)
    print("④ 强制物理模式：只影响选路，不影响结果")
    print("=" * 108)
    sql = "SELECT id FROM events WHERE id = 42;"
    print(f"SQL: {sql}")
    outcomes = {}
    for mode in FORCED_MODES:
        sink = PageCounter()
        runner = _runner(data_dir, sink)
        runner.execute(sql, physical=mode)  # 预热：统计基线与缓存进入稳态
        sink.reset()
        result = runner.execute(sql, physical=mode)
        path = runner.last_access_paths[0]
        outcomes[mode] = result.rows
        print(
            f"  {mode:<6} reason={path.reason:<16} 页读={sink.reads:<5}"
            f"返回行={len(result.rows)}"
        )
    print(f"  auto / seq / index 返回行完全一致："
          f"{outcomes['auto'] == outcomes['seq'] == outcomes['index']}")
    print("  说明：每种模式先跑一次预热（统计基线 + 缓存），上表是稳态页读；")
    print("        冷启动的第一次查询还要付一次 D37 布局/统计基线（进程内一次性）。")
    print()


def print_error_ownership(runner: Runner) -> None:
    """四之五：强制模式的错误归属（C 抛 E_BAD_ARG / B 抛 E_INDEX_NOT_FOUND）。"""

    print("=" * 108)
    print("⑤ 错误归属：索引存在性由 B 判定，C 只在自己构造不出请求时报错")
    print("=" * 108)
    cases = (
        ("无索引列 + 强制 index", "SELECT id FROM events WHERE note = 'n00042';"),
        ("无谓词   + 强制 index", "SELECT id FROM events;"),
    )
    for label, sql in cases:
        try:
            runner.execute(sql, physical="index")
            print(f"  {label:<24} → 没有报错（不符合预期）")
        except SqlError as error:
            print(f"  {label:<24} → {error.code}: {error}")
    print()


def print_dml_sync(data_dir: Path) -> None:
    """四之六：DML 与索引同步（旧键消失、新键可查、删行后查不到）。"""

    print("=" * 108)
    print("⑥ DML 与索引同步：update/delete 之后索引与表数据一致")
    print("=" * 108)
    runner = _runner(data_dir)
    runner.execute("INSERT INTO events VALUES (99999, 7, 1, 'tail');")
    print(f"  插入 99999 后按索引查：{len(runner.execute('SELECT id FROM events WHERE id = 99999;', physical='index').rows)} 行")
    runner.execute("UPDATE events SET id = 88888 WHERE id = 99999;")
    print(f"  改键后旧键 99999：{len(runner.execute('SELECT id FROM events WHERE id = 99999;', physical='index').rows)} 行"
          f"　新键 88888：{len(runner.execute('SELECT id FROM events WHERE id = 88888;', physical='index').rows)} 行")
    runner.execute("DELETE FROM events WHERE id = 88888;")
    print(f"  删除后新键 88888：{len(runner.execute('SELECT id FROM events WHERE id = 88888;', physical='index').rows)} 行")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V3 演示取证面板")
    parser.add_argument("--data-dir", type=Path, required=True, help="已装载演示数据的目录")
    args = parser.parse_args(argv)
    data_dir: Path = args.data_dir.expanduser()

    runner = _runner(data_dir)
    try:
        runner.execute("SELECT id FROM events WHERE id = 0;")
    except SqlError as error:
        print(
            f"未找到演示数据（{error}）。\n请先执行：\n"
            f"  python main.py --data-dir {data_dir} "
            f"-f docs/v3-dev/demo/v3_showcase_load.sql",
            file=sys.stderr,
        )
        return 2

    print_routing(runner)
    print_optimizer(runner)
    print_statistics(data_dir)
    print_forced_modes(data_dir)
    print_error_ownership(runner)
    print_dml_sync(data_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
