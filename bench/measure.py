"""采样循环与页读计数：把"跑一次"变成可复现的确定性指标。

指标来源是 B 的公开观测口——`DatabaseServer(data_dir, trace_sink=...)`，
与全链路追踪器同一条通道，bench 不 import 任何存储内部模块。

- 逻辑页读 = `pager.read_page` 的调用次数（可细分为用户表 / 索引 / 系统目录）；
- 物理读盘 = 同一批调用里 `cache.misses` 的增量（真实缺页读盘）；
- 脏页写回 = `cache.dirty_writes` 的增量；
- 耗时只作辅证：CI 断言只看计数（V3 计划书 §11）。

冷 / 热两种口径：

- **cold**：每次采样都新建 `DatabaseServer`（新 BufferPool + 新 rid 映射），
  计数可复现；
- **hot**：同一个 server 先跑一次（丢弃），再连续采样 `repeat` 次，
  反映稳态。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from time import perf_counter

from contracts.ast import Value
from contracts.errors import SqlError
from storage import DatabaseServer

from bench.dataset import DatasetSpec
from bench.modes import PREWARMED_MODES, Outcome, execute_mode
from bench.scenarios import Scenario


CATALOG_PREFIX = "__sys_"
"""B 保留的系统表前缀（契约与 v3 计划书 §4.6 都写明）；用于页读分桶。"""

BUCKETS: tuple[str, ...] = ("user_table", "index", "syscatalog")


def classify(path: Path) -> str:
    """把一次页读归到三类文件之一：用户表 / 索引 / 系统目录。"""

    if path.suffix == ".idx":
        return "index"
    if path.name.startswith(CATALOG_PREFIX):
        return "syscatalog"
    return "user_table"


@dataclass(frozen=True, slots=True)
class Metrics:
    """一次采样的确定性计数。"""

    logical_reads: dict[str, int]
    physical_reads: dict[str, int]
    dirty_writes: int

    def logical(self, bucket: str = "total") -> int:
        return self.logical_reads.get(bucket, 0)

    def physical(self, bucket: str = "total") -> int:
        return self.physical_reads.get(bucket, 0)


@dataclass(frozen=True, slots=True)
class Sample:
    """一次采样：计数 + 耗时 + 执行观察。"""

    elapsed_ms: float
    metrics: Metrics
    outcome: Outcome

    @property
    def rows(self) -> tuple[tuple[Value, ...], ...]:
        return self.outcome.rows

    @property
    def digest(self) -> str:
        """行多重集指纹（排序后哈希）：跨模式比对用。"""

        return row_digest(self.rows)


def row_digest(rows: tuple[tuple[Value, ...], ...]) -> str:
    """行集的规范指纹：排序后哈希，抵消跨模式行序差异。"""

    canonical = repr(sorted(rows))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class PageCounter:
    """可直接作为 `DatabaseServer(trace_sink=...)` 的页读计数器。"""

    def __init__(self) -> None:
        self.reset()

    def __call__(self, payload: dict[str, object]) -> None:
        if payload.get("component") != "pager":
            return
        if payload.get("operation") != "read_page":
            return
        arguments = payload.get("arguments") or ()
        path = Path(str(arguments[0])) if arguments else Path("")
        bucket = classify(path)
        self._logical[bucket] += 1
        self._logical["total"] += 1
        before = payload.get("cache_stats_before") or {}
        after = payload.get("cache_stats_after") or {}
        misses = int(after.get("misses", 0)) - int(before.get("misses", 0))
        if misses:
            self._physical[bucket] += misses
            self._physical["total"] += misses
        if self._dirty_base is None:
            self._dirty_base = int(after.get("dirty_writes", 0))
        self._dirty_now = int(after.get("dirty_writes", self._dirty_now))

    def reset(self) -> None:
        """清零计数（每次采样前调用；预热成本因此被排除在测量窗口外）。"""

        self._logical = {bucket: 0 for bucket in (*BUCKETS, "total")}
        self._physical = {bucket: 0 for bucket in (*BUCKETS, "total")}
        self._dirty_base: int | None = None
        self._dirty_now = 0

    def metrics(self) -> Metrics:
        base = self._dirty_base if self._dirty_base is not None else self._dirty_now
        return Metrics(
            logical_reads=dict(self._logical),
            physical_reads=dict(self._physical),
            dirty_writes=max(0, self._dirty_now - base),
        )


def measure_mode(
    data_dir: Path,
    spec: DatasetSpec,
    scenario: Scenario,
    mode: str,
    *,
    repeat: int = 3,
) -> dict[str, list[Sample]]:
    """跑一个（场景，模式）：返回 cold / hot 两组采样。"""

    if repeat <= 0:
        raise ValueError("repeat must be positive")
    if mode not in PREWARMED_MODES and mode not in ("api-seq", "api-index"):
        raise ValueError(f"unknown bench mode: {mode!r}")
    prewarm = mode in PREWARMED_MODES
    cold = [
        _sample_once(data_dir, spec, scenario, mode, prewarm_stats=prewarm)
        for _ in range(repeat)
    ]

    counter = PageCounter()
    server = DatabaseServer(str(data_dir), trace_sink=counter)
    if prewarm:
        _prewarm_statistics(server, spec)
    execute_mode(server, spec, scenario, mode)  # 第一次丢弃：让缓存与映射热起来
    hot: list[Sample] = []
    for _ in range(repeat):
        counter.reset()
        started = perf_counter()
        outcome = execute_mode(server, spec, scenario, mode)
        hot.append(
            Sample(
                elapsed_ms=(perf_counter() - started) * 1000,
                metrics=counter.metrics(),
                outcome=outcome,
            )
        )
    return {"cold": cold, "hot": hot}


def _sample_once(
    data_dir: Path,
    spec: DatasetSpec,
    scenario: Scenario,
    mode: str,
    *,
    prewarm_stats: bool,
) -> Sample:
    """一次冷采样：新 server（新缓存 + 新 rid 映射）→ 可选预热 → 计数执行。"""

    counter = PageCounter()
    server = DatabaseServer(str(data_dir), trace_sink=counter)
    if prewarm_stats:
        _prewarm_statistics(server, spec)
        counter.reset()
    started = perf_counter()
    outcome = execute_mode(server, spec, scenario, mode)
    return Sample(
        elapsed_ms=(perf_counter() - started) * 1000,
        metrics=counter.metrics(),
        outcome=outcome,
    )


def _prewarm_statistics(server: DatabaseServer, spec: DatasetSpec) -> None:
    """预热统计基线；只对 SQL 模式调用（口径见 `PREWARMED_MODES`）。"""

    try:
        server.connect(spec.database).statistics(spec.table)
    except SqlError:
        pass  # 场景本身可能在探测错误语义，预热失败不影响测量


def summarize(samples: list[Sample]) -> dict[str, object]:
    """把一组采样压成报告行：计数取首样本，并标注计数是否稳定。"""

    if not samples:
        raise ValueError("no samples to summarize")
    first = samples[0]
    stable = all(
        sample.metrics == first.metrics and sample.digest == first.digest
        for sample in samples
    )
    return {
        "samples": len(samples),
        "elapsed_ms_median": round(
            median(sample.elapsed_ms for sample in samples), 3
        ),
        "logical_reads": first.metrics.logical_reads,
        "physical_reads": first.metrics.physical_reads,
        "dirty_writes": first.metrics.dirty_writes,
        "returned_rows": len(first.rows),
        "row_digest": first.digest,
        "reason": first.outcome.reason,
        "selectivity": first.outcome.selectivity,
        "estimated_rows": first.outcome.estimated_rows,
        "seq_cost": first.outcome.seq_cost,
        "index_cost": first.outcome.index_cost,
        "error_code": first.outcome.error_code,
        "counters_stable": stable,
    }
