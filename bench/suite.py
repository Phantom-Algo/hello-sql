"""基准编排：把数据集、场景、五种模式与采样循环串成一份结果数据。"""

from __future__ import annotations

import platform
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from bench import BENCH_VERSION
from bench.dataset import Dataset, DatasetSpec
from bench.measure import measure_mode, summarize
from bench.modes import ALL_MODES, PREWARMED_MODES
from bench.scenarios import Scenario, build_scenarios


def run_suite(
    dataset: Dataset,
    *,
    repeat: int = 3,
    modes: tuple[str, ...] = ALL_MODES,
) -> dict[str, object]:
    """跑完整套场景 × 模式，返回可直接序列化的结果字典。"""

    spec = dataset.spec
    scenario_list = build_scenarios(spec)
    scenarios: list[dict[str, object]] = []
    for scenario in scenario_list:
        entry: dict[str, object] = {
            "name": scenario.name,
            "sql": scenario.sql,
            "expectation": scenario.expectation,
            "column": scenario.column,
            "has_index": scenario.has_index,
            "modes": {},
        }
        per_mode: dict[str, dict[str, object]] = {}
        for mode in modes:
            samples = measure_mode(
                dataset.data_dir, spec, scenario, mode, repeat=repeat
            )
            per_mode[mode] = {
                "cold": summarize(samples["cold"]),
                "hot": summarize(samples["hot"]),
            }
        entry["modes"] = per_mode
        entry["consistency"] = _check_consistency(per_mode)
        scenarios.append(entry)

    result: dict[str, object] = {
        "bench_version": BENCH_VERSION,
        "meta": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "data_dir": str(dataset.data_dir),
            "dataset_fingerprint": spec.fingerprint,
            "dataset_created": dataset.created,
            "table": spec.table,
            "database": spec.database,
            "rows": spec.rows,
            "seed": spec.seed,
            "indexes": [list(item) for item in spec.indexes],
            "repeat": repeat,
            "modes": list(modes),
            "prewarmed_modes": list(PREWARMED_MODES),
            "reproduce": _reproduce_command(spec, repeat),
        },
        "scenarios": scenarios,
        "errors": _collect_errors(scenarios),
        "verdict": _verdict(scenarios),
    }
    return result


def _reproduce_command(spec: DatasetSpec, repeat: int) -> str:
    return f"python -m bench run --rows {spec.rows} --seed {spec.seed} --repeat {repeat}"


def _check_consistency(per_mode: dict[str, dict[str, object]]) -> dict[str, object]:
    """跨模式一致性：只比较成功执行的模式，且用行多重集指纹（非行序）。"""

    digests: dict[str, str] = {}
    for mode, data in per_mode.items():
        cold = data["cold"]
        if cold["error_code"] is None:
            digests[mode] = str(cold["row_digest"])
    reference = next(iter(digests.values()), None)
    return {
        "digests": digests,
        "consistent": None if reference is None else all(
            digest == reference for digest in digests.values()
        ),
    }


def _collect_errors(scenarios: list[dict[str, object]]) -> list[dict[str, object]]:
    """错误矩阵：哪些（场景，模式）按预期抛了什么错误码。"""

    errors: list[dict[str, object]] = []
    for scenario in scenarios:
        for mode, data in scenario["modes"].items():
            cold = data["cold"]
            if cold["error_code"] is None:
                continue
            errors.append(
                {
                    "scenario": scenario["name"],
                    "mode": mode,
                    "error_code": cold["error_code"],
                    "expected": not scenario["has_index"],
                }
            )
    return errors


def _verdict(scenarios: list[dict[str, object]]) -> dict[str, object]:
    """把"结果一致 / 计数稳定"这类可断言的口径汇总成结论。"""

    inconsistent = [
        scenario["name"]
        for scenario in scenarios
        if scenario["consistency"]["consistent"] is False
    ]
    unstable = [
        f"{scenario['name']}/{mode}"
        for scenario in scenarios
        for mode, data in scenario["modes"].items()
        if not (data["cold"]["counters_stable"] and data["hot"]["counters_stable"])
    ]
    return {
        "rows_match_across_modes": not inconsistent,
        "inconsistent_scenarios": inconsistent,
        "counters_stable": not unstable,
        "unstable_samples": unstable,
    }
