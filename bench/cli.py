"""bench 命令行入口：`python -m bench run [...]`。

默认参数面向"能进仓库的报告"：4000 行、seed=1、每模式冷热各 3 次，
报告写 `docs/v3-dev/benchmark-report.md`、机读证据写 `bench/results/`。
小规模自检用 `--rows 400 --repeat 1`。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from bench.dataset import DatasetSpec, ensure_dataset
from bench.modes import ALL_MODES
from bench.report import write_json, write_markdown
from bench.suite import run_suite


DEFAULT_CACHE_DIR = Path("bench") / ".cache"
DEFAULT_JSON = Path("bench") / "results" / "v3-benchmark.json"
DEFAULT_MARKDOWN = Path("docs") / "v3-dev" / "benchmark-report.md"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m bench",
        description="hello-sql V3 基准：五种模式跑同一批查询，产出对比报告",
    )
    subparsers = parser.add_subparsers(dest="command")
    run = subparsers.add_parser("run", help="跑基准并产出报告")
    run.add_argument("--rows", type=int, default=4000, help="数据集行数（默认 4000）")
    run.add_argument("--seed", type=int, default=1, help="数据集随机种子（默认 1）")
    run.add_argument("--repeat", type=int, default=3, help="冷/热各采样次数（默认 3）")
    run.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help="数据集缓存根目录（默认 bench/.cache，已 gitignore）",
    )
    run.add_argument(
        "--json", type=Path, default=DEFAULT_JSON, help="机读证据输出路径"
    )
    run.add_argument(
        "--markdown", type=Path, default=DEFAULT_MARKDOWN, help="报告输出路径"
    )
    run.add_argument(
        "--rebuild", action="store_true", help="忽略缓存，强制重建数据集"
    )
    run.add_argument(
        "--modes",
        default=",".join(ALL_MODES),
        help=f"逗号分隔的模式子集（默认全部：{', '.join(ALL_MODES)}）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """解析参数、跑基准、落两份产物；返回进程退出码。"""

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command not in (None, "run"):
        parser.error(f"unknown command: {args.command}")
    if args.rows <= 0 or args.repeat <= 0:
        parser.error("--rows and --repeat must be positive")
    modes = tuple(item.strip() for item in args.modes.split(",") if item.strip())
    unknown = [mode for mode in modes if mode not in ALL_MODES]
    if unknown:
        parser.error(f"unknown modes: {', '.join(unknown)}")

    spec = DatasetSpec(rows=args.rows, seed=args.seed)
    dataset = ensure_dataset(args.cache_dir, spec, rebuild=args.rebuild)
    result = run_suite(dataset, repeat=args.repeat, modes=modes)
    write_json(result, args.json)
    write_markdown(result, args.markdown)

    print(f"数据集：{dataset.data_dir}（{'新建' if dataset.created else '复用'}）")
    print(f"结果：{'一致' if result['verdict']['rows_match_across_modes'] else '不一致'}"
          f"　计数稳定：{'是' if result['verdict']['counters_stable'] else '否'}")
    print(f"报告：{args.markdown}")
    print(f"证据：{args.json}")
    return 0 if result["verdict"]["rows_match_across_modes"] else 1


__all__ = ["main", "build_parser"]
