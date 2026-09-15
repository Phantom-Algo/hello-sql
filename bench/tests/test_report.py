"""报告产物：结果结构、Markdown 列与机读证据落盘。"""

from __future__ import annotations

import json
from pathlib import Path

from bench.dataset import Dataset
from bench.report import render_markdown, write_json, write_markdown
from bench.suite import run_suite


def _result(dataset: Dataset) -> dict:
    return run_suite(dataset, repeat=1)


def test_result_has_documented_fields(dataset: Dataset) -> None:
    result = _result(dataset)

    assert result["bench_version"]
    meta = result["meta"]
    for key in (
        "generated_at_utc",
        "python",
        "platform",
        "dataset_fingerprint",
        "rows",
        "seed",
        "repeat",
        "modes",
        "prewarmed_modes",
        "reproduce",
    ):
        assert key in meta, key
    assert len(result["scenarios"]) == 6
    sample = result["scenarios"][0]["modes"]["sql-auto"]["cold"]
    for key in (
        "logical_reads",
        "physical_reads",
        "returned_rows",
        "row_digest",
        "reason",
        "counters_stable",
        "elapsed_ms_median",
    ):
        assert key in sample, key
    assert set(sample["logical_reads"]) >= {"user_table", "index", "syscatalog", "total"}


def test_verdict_reports_consistency_and_error_matrix(dataset: Dataset) -> None:
    result = _result(dataset)

    assert result["verdict"]["rows_match_across_modes"] is True
    assert result["verdict"]["counters_stable"] is True
    errors = {(item["scenario"], item["mode"]): item for item in result["errors"]}
    assert errors[("no_index_column", "sql-index")]["error_code"] == "E_INDEX_NOT_FOUND"
    assert errors[("no_index_column", "sql-index")]["expected"] is True


def test_markdown_has_reads_speedup_and_routing_columns(dataset: Dataset) -> None:
    markdown = render_markdown(_result(dataset))

    assert "用户表页读" in markdown
    assert "物理读盘" in markdown
    assert "页读加速比" in markdown
    assert "选路 reason" in markdown
    assert "## 口径" in markdown
    assert "冷启动回表" in markdown
    assert "python -m bench run" in markdown


def test_reports_are_written_to_disk(dataset: Dataset, tmp_path) -> None:
    result = _result(dataset)
    json_path = tmp_path / "results" / "v3.json"
    md_path = tmp_path / "report.md"

    write_json(result, json_path)
    write_markdown(result, md_path)

    stored = json.loads(json_path.read_text(encoding="utf-8"))
    assert stored["meta"]["rows"] == dataset.spec.rows
    assert md_path.read_text(encoding="utf-8").startswith("# V3 基准报告")
    assert Path(md_path).exists()
