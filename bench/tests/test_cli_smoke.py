"""CLI 冒烟：小规模端到端跑通并落两份产物。"""

from __future__ import annotations

import json

from bench.cli import main


def test_cli_run_smoke(tmp_path) -> None:
    json_path = tmp_path / "bench.json"
    md_path = tmp_path / "bench.md"

    code = main(
        [
            "run",
            "--rows",
            "120",
            "--repeat",
            "1",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--json",
            str(json_path),
            "--markdown",
            str(md_path),
        ]
    )

    assert code == 0
    result = json.loads(json_path.read_text(encoding="utf-8"))
    assert result["meta"]["rows"] == 120
    assert result["verdict"]["rows_match_across_modes"] is True
    assert md_path.read_text(encoding="utf-8").startswith("# V3 基准报告")


def test_cli_rejects_unknown_mode(tmp_path) -> None:
    import pytest

    with pytest.raises(SystemExit):
        main(["run", "--rows", "20", "--repeat", "1", "--modes", "sql-nope"])
