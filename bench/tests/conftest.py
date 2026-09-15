"""共用夹具：一份小规模缓存数据集（几秒钟建好，供多个用例复用）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bench.dataset import Dataset, DatasetSpec, ensure_dataset


SMALL_ROWS = 200
SELECTIVE_ROWS = 1200
"""足够多页、让代价模型真的会为高选择性点查选索引的规模（约 13 页）。"""


@pytest.fixture(scope="session")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Dataset:
    cache_root: Path = tmp_path_factory.mktemp("bench-cache")
    return ensure_dataset(cache_root, DatasetSpec(rows=SMALL_ROWS, seed=7))


@pytest.fixture(scope="session")
def selective_dataset(tmp_path_factory: pytest.TempPathFactory) -> Dataset:
    """页数够多的数据集：用于验证"该走索引时就真的走索引"。"""

    cache_root: Path = tmp_path_factory.mktemp("bench-selective")
    return ensure_dataset(cache_root, DatasetSpec(rows=SELECTIVE_ROWS, seed=7))
