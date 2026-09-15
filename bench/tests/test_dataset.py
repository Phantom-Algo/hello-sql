"""数据集：确定性、指纹与清单复用。"""

from __future__ import annotations

from bench.dataset import DatasetSpec, ensure_dataset
from storage import DatabaseServer


def _rows(data_dir, spec):
    handle = DatabaseServer(str(data_dir)).connect(spec.database)
    return [values for _row_id, values in handle.scan(spec.table)]


def test_dataset_is_deterministic(tmp_path) -> None:
    """同参数（含 seed）必须产出逐行相同的数据。"""

    spec = DatasetSpec(rows=50, seed=3)
    first = ensure_dataset(tmp_path / "a", spec)
    second = ensure_dataset(tmp_path / "b", spec)

    assert first.created and second.created
    rows = _rows(first.data_dir, spec)
    assert rows == _rows(second.data_dir, spec)
    assert [row[0] for row in rows] == list(range(50))
    assert rows[0][2] == "name-000000"


def test_dataset_seed_changes_fingerprint_and_data(tmp_path) -> None:
    base = DatasetSpec(rows=20, seed=1)
    other = DatasetSpec(rows=20, seed=2)

    assert base.fingerprint != other.fingerprint
    first = ensure_dataset(tmp_path, base)
    second = ensure_dataset(tmp_path, other)

    assert first.data_dir != second.data_dir
    assert _rows(first.data_dir, base) != _rows(second.data_dir, other)


def test_dataset_manifest_reuse_skips_rebuild(tmp_path) -> None:
    """清单一致且数据可读时复用目录，不重复插入（第二次 created=False）。"""

    spec = DatasetSpec(rows=30)
    first = ensure_dataset(tmp_path, spec)
    second = ensure_dataset(tmp_path, spec)

    assert first.created is True
    assert second.created is False
    assert second.data_dir == first.data_dir
    assert second.cache_id == first.cache_id


def test_dataset_rebuild_forces_new_build(tmp_path) -> None:
    spec = DatasetSpec(rows=30)
    ensure_dataset(tmp_path, spec)

    rebuilt = ensure_dataset(tmp_path, spec, rebuild=True)

    assert rebuilt.created is True


def test_dataset_builds_indexes_through_sql_ddl(tmp_path) -> None:
    """建索引走 SQL：顺带证明 A→C→B 的 DDL 链路在基准里也是通的。"""

    spec = DatasetSpec(rows=30)
    dataset = ensure_dataset(tmp_path, spec)
    handle = DatabaseServer(str(dataset.data_dir)).connect(spec.database)

    names = sorted(info.name for info in handle.list_indexes(spec.table))

    assert names == sorted(name for name, _column in spec.indexes)
