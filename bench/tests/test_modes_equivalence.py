"""五种模式的等价性与取证：行多重集一致、错误码按契约归属、不越界调规划器。"""

from __future__ import annotations

import storage

from bench.dataset import Dataset
from bench.measure import measure_mode
from bench.modes import API_MODES, SQL_MODES
from bench.scenarios import build_scenarios


def _scenario(dataset: Dataset, name: str):
    return next(
        scenario
        for scenario in build_scenarios(dataset.spec)
        if scenario.name == name
    )


def _cold(dataset: Dataset, name: str, mode: str):
    scenario = _scenario(dataset, name)
    return measure_mode(dataset.data_dir, dataset.spec, scenario, mode, repeat=1)[
        "cold"
    ][0]


def test_sql_modes_agree_on_rows(dataset: Dataset) -> None:
    """auto / seq / index 必须返回同一行多重集（行序不承诺）。"""

    for name in ("point_hit", "range_selective", "dup_lookup"):
        digests = {mode: _cold(dataset, name, mode).digest for mode in SQL_MODES}
        assert len(set(digests.values())) == 1, (name, digests)
        assert _cold(dataset, name, "sql-auto").rows


def test_api_modes_agree_on_rows(dataset: Dataset) -> None:
    """存储级两模式等价：索引查找 = 顺序扫描 + 等价过滤。"""

    for name in ("point_hit", "point_miss", "range_selective", "dup_lookup"):
        scan = _cold(dataset, name, "api-seq")
        index = _cold(dataset, name, "api-index")
        assert scan.digest == index.digest, name
        assert scan.rows == index.rows


def test_sql_and_api_layers_agree_on_rows(dataset: Dataset) -> None:
    """端到端与存储级必须给出同样的行集（否则报告不可信）。"""

    for name in ("point_hit", "range_selective", "range_wide"):
        assert (
            _cold(dataset, name, "sql-seq").digest
            == _cold(dataset, name, "api-seq").digest
        )


def test_api_modes_never_touch_planner(dataset: Dataset, monkeypatch) -> None:
    """存储模式是纯 B 成本：不许调用 statistics / list_indexes。"""

    def _boom(*_args, **_kwargs):
        raise AssertionError("api modes must not consult the planner")

    monkeypatch.setattr(storage.Storage, "statistics", _boom)
    monkeypatch.setattr(storage.Storage, "list_indexes", _boom)

    for mode in API_MODES:
        assert _cold(dataset, "point_hit", mode).rows


def test_sql_auto_records_access_reason(selective_dataset: Dataset) -> None:
    """报告里的 reason 来自 C 的公开取证属性，不是 bench 猜的。

    小数据集（200 行 / 2 页）上"树高 3 > 扫描 2 页"，代价模型会一律放弃
    索引——这是正确行为，所以本用例用页数足够的夹具。
    """

    hit = _cold(selective_dataset, "point_hit", "sql-auto")
    wide = _cold(selective_dataset, "range_wide", "sql-auto")

    assert hit.outcome.reason == "INDEX_EQUALITY"
    assert hit.outcome.seq_cost is not None and hit.outcome.index_cost is not None
    assert hit.outcome.index_cost < hit.outcome.seq_cost
    assert wide.outcome.reason == "SEQ_CHEAPER"
    assert wide.outcome.index_cost >= wide.outcome.seq_cost


def test_index_mode_without_index_records_b_error(dataset: Dataset) -> None:
    """目标列没有索引：两种 index 模式都必须记下 B 的 E_INDEX_NOT_FOUND。"""

    assert (
        _cold(dataset, "no_index_column", "sql-index").outcome.error_code
        == "E_INDEX_NOT_FOUND"
    )
    assert (
        _cold(dataset, "no_index_column", "api-index").outcome.error_code
        == "E_INDEX_NOT_FOUND"
    )


def test_forced_seq_never_consults_index(dataset: Dataset) -> None:
    """强制顺序扫描的页读全部落在用户表上，索引页读为 0。"""

    metrics = _cold(dataset, "point_hit", "sql-seq").metrics

    assert metrics.logical("index") == 0
    assert metrics.logical("user_table") > 0
