#!/usr/bin/env python3
"""V3 证据面板：把「选路决策」和「优化器改写」直接打印到终端。

背景：终端里的 ``/inspect`` 只给出 14 个阶段的名称、状态与事件条数，
选路理由（reason）和代价估计要看浏览器查看器的节点详情。本脚本用 Runner
的公开属性 ``last_access_paths`` 与 ``last_optimization_log`` 把同一份
数据打印成表格，方便在终端里直接对照。

脚本只读公开接口，不修改任何模块，也不改变 SQL 语义。

用法：

    python docs/zjt-docs/show/v3/v3_evidence.py --data-dir /tmp/v3demo
    python docs/zjt-docs/show/v3/v3_evidence.py --data-dir /tmp/v3demo --bench

``--bench`` 需要先用 ``v3_perf_load.sql`` 装入数据。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from statistics import median
from time import perf_counter

# 允许从仓库任意位置直接运行本脚本
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from compiler import parse, parse_script  # noqa: E402
from contracts.errors import SqlError  # noqa: E402
from runner import Runner  # noqa: E402
from storage import DatabaseServer  # noqa: E402


# 覆盖全部七种选路理由的查询；每条都写清「期望看到什么」
DEMO_QUERIES: tuple[tuple[str, str], ...] = (
    ("SELECT id FROM wide_facts WHERE id = 42;", "等值 + 高选择性 → 索引胜出"),
    ("SELECT id FROM wide_facts WHERE grp = 1;", "有索引但命中一半 → 放弃索引"),
    ("SELECT id FROM wide_facts WHERE id > 10;", "范围太宽 → 放弃索引"),
    ("SELECT id FROM wide_facts WHERE id > 1000;", "范围键越界 → 估算 0 行，走索引"),
    ("SELECT id FROM wide_facts WHERE m1 = 500;", "列上没有索引 → 顺序扫描"),
    ("SELECT id, m1 FROM wide_facts;", "没有 WHERE → 顺序扫描"),
    ("SELECT id FROM wide_facts WHERE id = 42 AND grp = 0;", "两个条件都可下推"),
)

BENCH_QUERIES: tuple[tuple[str, str], ...] = (
    ("SELECT id FROM wide_facts WHERE id = 42;", "高选择性"),
    ("SELECT id FROM wide_facts WHERE id > 80;", "低选择性"),
)


def build_runner(data_dir: Path, reset: bool) -> Runner:
    """创建装配好的 Runner；reset 为真时先清空数据目录。"""

    if reset and data_dir.exists():
        shutil.rmtree(data_dir)
    server = DatabaseServer(data_dir)
    return Runner(server=server, parse=parse, parse_script=parse_script)


def describe_requests(requests: tuple[object, ...]) -> str:
    """把索引请求压成一行短文本，空元组表示没有下推任何条件。"""

    if not requests:
        return "-"
    return ", ".join(type(item).__name__ + "(" + _request_column(item) + ")" for item in requests)


def _request_column(request: object) -> str:
    """取出索引请求的目标列名。"""

    return str(getattr(request, "column", "?"))


def print_decisions(runner: Runner) -> None:
    """在 auto 模式下逐条执行演示查询，打印选路决策表。"""

    print("=" * 100)
    print("选路决策表（physical=auto）：reason 是稳定标识，代价单位是「预计数据页读取次数」")
    print("=" * 100)
    header = f"{'reason':<18}{'列':<6}{'选择性':>10}{'估计行数':>10}{'扫描代价':>10}{'索引代价':>10}  下推条件"
    print(header)
    print("-" * 100)
    for sql, note in DEMO_QUERIES:
        runner.execute(sql)
        paths = runner.last_access_paths
        if not paths:
            print(f"{'(无 Scan)':<18}{'-':<6}{'-':>10}{'-':>10}{'-':>10}{'-':>10}  {note}")
            continue
        for path in paths:
            selectivity = "-" if path.selectivity is None else f"{path.selectivity:.4f}"
            estimated = "-" if path.estimated_rows is None else f"{path.estimated_rows:.2f}"
            seq = "-" if path.seq_cost is None else f"{path.seq_cost:.2f}"
            index = "-" if path.index_cost is None else f"{path.index_cost:.2f}"
            print(
                f"{path.reason:<18}{str(path.column or '-'):<6}{selectivity:>10}"
                f"{estimated:>10}{seq:>10}{index:>10}  {describe_requests(path.requests)}"
            )
        print(f"{'':<18}SQL: {sql}")
        print(f"{'':<18}期望: {note}")
        print("-" * 100)


def print_optimizer_log(runner: Runner) -> None:
    """打印优化器规则命中日志，并核对优化开 / 关的结果是否逐行一致。"""

    from runner.logical_plan.optimizer.optimizer import render_plan

    sql = (
        "SELECT w.id, g.grp FROM wide_facts w "
        "INNER JOIN wide_facts g ON w.id = g.id "
        "WHERE w.id = 42 AND g.grp = 0 AND 1 = 1;"
    )
    print()
    print("=" * 100)
    print("优化器规则命中日志（optimize=True）")
    print("=" * 100)
    print(f"SQL: {sql}")
    optimized = runner.execute(sql, optimize=True)
    log = runner.last_optimization_log
    if log is None:
        print("未拿到优化日志。")
        return
    print(f"迭代轮数={log.rounds}  触顶={log.hit_limit}  规则命中次数={len(log.applications)}")
    print(f"原始计划: {render_plan(log.original)}")
    print(f"优化计划: {render_plan(log.optimized)}")
    for item in log.applications:
        print(f"  [{item.rule}] {item.summary}")
        print(f"      {item.plan_before}")
        print(f"   -> {item.plan_after}")

    plain = runner.execute(sql, optimize=False)
    print()
    print(f"优化开的结果行数={len(optimized.rows)}  优化关的结果行数={len(plain.rows)}")
    print(f"两种模式结果逐行一致: {optimized == plain}")


def print_bench(runner: Runner) -> None:
    """对比 auto / seq / index 三种物理模式的耗时，并核对结果一致。"""

    print()
    print("=" * 100)
    print("三模式耗时对比（每格取 9 次的中位数；结果一致性同时校验）")
    print("=" * 100)
    for sql, note in BENCH_QUERIES:
        print(f"SQL: {sql}   （{note}）")
        outcomes = {}
        for mode in ("auto", "seq", "index"):
            samples: list[float] = []
            result = None
            for _ in range(9):
                started = perf_counter()
                result = runner.execute(sql, physical=mode)
                samples.append((perf_counter() - started) * 1000)
            outcomes[mode] = result
            path = runner.last_access_paths[0]
            print(
                f"  {mode:<6} reason={path.reason:<16} "
                f"中位耗时={median(samples):7.2f} ms   返回行数={len(result.rows)}"
            )
        identical = outcomes["auto"] == outcomes["seq"] == outcomes["index"]
        print(f"  三模式结果一致: {identical}")
        print()


def main(argv: list[str] | None = None) -> int:
    """解析参数并打印选路、优化器与可选的耗时证据。"""

    parser = argparse.ArgumentParser(description="V3 选路与优化器证据面板")
    parser.add_argument("--data-dir", type=Path, required=True, help="数据目录")
    parser.add_argument("--bench", action="store_true", help="额外跑三模式耗时对比")
    args = parser.parse_args(argv)

    runner = build_runner(args.data_dir.expanduser(), reset=False)
    try:
        runner.describe_table("wide_facts")
    except SqlError:
        print(
            "未找到 wide_facts 表。请先执行：\n"
            "  python main.py --data-dir <数据目录> -f docs/zjt-docs/show/v3/v3_demo_load.sql",
            file=sys.stderr,
        )
        return 2

    print_decisions(runner)
    print_optimizer_log(runner)
    if args.bench:
        print_bench(runner)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
