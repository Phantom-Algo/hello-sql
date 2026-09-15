"""计数器与采样聚合：分桶、物理读、指纹与稳定性标注。"""

from __future__ import annotations

from pathlib import Path

from bench.measure import PageCounter, Sample, classify, row_digest, summarize
from bench.modes import Outcome


def _payload(path: str, *, before: int, after: int, dirty: int = 0):
    return {
        "component": "pager",
        "operation": "read_page",
        "arguments": (Path(path), 1),
        "cache_stats_before": {"misses": before, "dirty_writes": 0},
        "cache_stats_after": {"misses": after, "dirty_writes": dirty},
    }


def test_classify_splits_user_table_index_and_catalog() -> None:
    assert classify(Path("/data/main/events.table")) == "user_table"
    assert classify(Path("/data/main/indexes/idx_events_id.idx")) == "index"
    assert classify(Path("/data/main/__sys_tables.table")) == "syscatalog"


def test_page_counter_counts_logical_and_physical_reads() -> None:
    counter = PageCounter()
    counter(_payload("/data/main/events.table", before=7, after=8))
    counter(_payload("/data/main/events.table", before=8, after=8))  # 缓存命中
    counter(_payload("/data/main/indexes/idx_events_id.idx", before=8, after=10))

    metrics = counter.metrics()

    assert metrics.logical_reads["user_table"] == 2
    assert metrics.logical_reads["index"] == 1
    assert metrics.logical_reads["total"] == 3
    assert metrics.physical_reads["user_table"] == 1
    assert metrics.physical_reads["index"] == 2
    assert metrics.physical_reads["total"] == 3


def test_page_counter_ignores_other_components_and_resets() -> None:
    counter = PageCounter()
    counter({"component": "engine", "operation": "scan"})
    counter(_payload("/data/main/events.table", before=0, after=1))

    assert counter.metrics().logical_reads["total"] == 1
    counter.reset()
    assert counter.metrics().logical_reads["total"] == 0
    assert counter.metrics().physical_reads["total"] == 0


def test_row_digest_is_order_insensitive_multiset() -> None:
    assert row_digest(((1, "a"), (2, "b"))) == row_digest(((2, "b"), (1, "a")))
    assert row_digest(((1, "a"),)) != row_digest(((2, "a"),))
    assert row_digest(((1, "a"), (1, "a"))) != row_digest(((1, "a"),))


def _sample(elapsed: float, reads: int, rows=((1,),)) -> Sample:
    from bench.measure import Metrics

    return Sample(
        elapsed_ms=elapsed,
        metrics=Metrics(
            logical_reads={"user_table": reads, "index": 0, "syscatalog": 0, "total": reads},
            physical_reads={"user_table": reads, "index": 0, "syscatalog": 0, "total": reads},
            dirty_writes=0,
        ),
        outcome=Outcome(rows=tuple(rows)),
    )


def test_summarize_marks_counters_stable_across_repeats() -> None:
    stable = summarize([_sample(1.0, 5), _sample(9.0, 5)])
    unstable = summarize([_sample(1.0, 5), _sample(1.0, 6)])

    assert stable["counters_stable"] is True
    assert stable["elapsed_ms_median"] == 5.0  # 耗时只聚合，不参与稳定性判定
    assert unstable["counters_stable"] is False
