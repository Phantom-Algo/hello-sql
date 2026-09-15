"""基准数据集：确定性生成 + 清单指纹复用。

数据集形状刻意贴近真实负载，并且**复现统计采样偏差的历史场景**：

- `id` 按 0..N-1 单调插入（等价于自增主键）；
- `amount` 低基数（0..96 循环），用于"命中多行"的对照；
- 建表与灌数据走 `storage` 公开 API，建索引走 **SQL**（顺带证明 A→C→B 的
  DDL 全链路可用）。

插入速度约 1.1 ms/行（每次写入同步 flush），所以数据集按指纹缓存在
`bench/.cache/` 下，除非显式 `--rebuild`，否则复用同一份目录。
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
from dataclasses import dataclass
from pathlib import Path

from compiler import parse, parse_script
from contracts.ast import ColumnDef, SqlType
from runner import Runner
from storage import DatabaseServer


BENCH_DATASET_VERSION = 1
DEFAULT_TABLE = "events"
DEFAULT_DATABASE = "main"
DEFAULT_COLUMNS: tuple[ColumnDef, ...] = (
    ColumnDef("id", SqlType.INT),
    ColumnDef("amount", SqlType.INT),
    ColumnDef("name", SqlType.TEXT),
)
DEFAULT_INDEXES: tuple[tuple[str, str], ...] = (
    ("idx_events_id", "id"),
    ("idx_events_amount", "amount"),
)
MANIFEST_NAME = "bench-dataset.json"


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    """数据集参数；指纹只由这些参数决定，保证同参数即同数据。"""

    rows: int
    seed: int = 1
    table: str = DEFAULT_TABLE
    database: str = DEFAULT_DATABASE
    columns: tuple[ColumnDef, ...] = DEFAULT_COLUMNS
    indexes: tuple[tuple[str, str], ...] = DEFAULT_INDEXES

    @property
    def fingerprint(self) -> str:
        """参数指纹：数据集目录名与清单校验都用它。"""

        payload = json.dumps(
            {
                "version": BENCH_DATASET_VERSION,
                "table": self.table,
                "database": self.database,
                "rows": self.rows,
                "seed": self.seed,
                "columns": [
                    [column.name, column.type.value] for column in self.columns
                ],
                "indexes": [list(item) for item in self.indexes],
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def row_at(self, index: int) -> tuple[object, ...]:
        """第 index 行（插入顺序即 id 顺序，amount/name 由 seed 决定）。"""

        rng = random.Random(f"{self.seed}:{index}")
        return (index, rng.randrange(0, 97), f"name-{index:06d}")

    def column_position(self, name: str) -> int:
        """列在行元组里的位置（建表列序）。"""

        for index, column in enumerate(self.columns):
            if column.name == name:
                return index
        raise KeyError(name)

    def manifest(self) -> dict[str, object]:
        return {
            "version": BENCH_DATASET_VERSION,
            "fingerprint": self.fingerprint,
            "table": self.table,
            "database": self.database,
            "rows": self.rows,
            "seed": self.seed,
            "columns": [[column.name, column.type.value] for column in self.columns],
            "indexes": [list(item) for item in self.indexes],
        }


@dataclass(frozen=True, slots=True)
class Dataset:
    """已就绪的数据集：目录、参数与是否本次新建。"""

    spec: DatasetSpec
    data_dir: Path
    created: bool

    @property
    def cache_id(self) -> str:
        return self.spec.fingerprint


def ensure_dataset(
    cache_root: Path,
    spec: DatasetSpec,
    *,
    rebuild: bool = False,
) -> Dataset:
    """取（或构建）数据集目录；返回是否本次新建，便于报告标注。"""

    cache_root = Path(cache_root)
    data_dir = cache_root / f"ds-{spec.fingerprint}"
    if not rebuild and _is_reusable(data_dir, spec):
        _prepare_indexes(data_dir, spec)
        return Dataset(spec=spec, data_dir=data_dir, created=False)
    if data_dir.exists():
        # 只清理本基准自己的缓存子目录（按指纹派生，不含用户数据）。
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    _build(data_dir, spec)
    _prepare_indexes(data_dir, spec)
    return Dataset(spec=spec, data_dir=data_dir, created=True)


def _prepare_indexes(data_dir: Path, spec: DatasetSpec) -> None:
    """让全部索引在**测量之前**完成打开/迁移，避免污染采样窗口。

    B 的 D49a 起索引文件有版本概念（v1 无行页号、v2 有）。旧缓存目录第一次
    被访问时会在打开索引的那一刻做一次 O(n log n) 重建——如果这件事发生在
    第一个采样里，那一格的数据就既包含重建又包含查询，三模式也就不可比。
    这里用公开接口把每个索引"碰"一次：只需要打开树，不必取回任何行。
    """

    handle = DatabaseServer(str(data_dir)).connect(spec.database)
    for _name, column in spec.indexes:
        handle.index_range(spec.table, column, None, None)


def _is_reusable(data_dir: Path, spec: DatasetSpec) -> bool:
    """清单一致且数据可读（行数、索引数对上）才复用。"""

    manifest_path = data_dir / MANIFEST_NAME
    if not manifest_path.exists():
        return False
    try:
        stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if stored != spec.manifest():
        return False
    try:
        handle = DatabaseServer(str(data_dir)).connect(spec.database)
        stats = handle.statistics(spec.table)
        indexes = handle.list_indexes(spec.table)
    except Exception:  # 数据目录损坏：当作不可复用，由调用方重建
        return False
    return stats.row_count == spec.rows and len(indexes) == len(spec.indexes)


def _build(data_dir: Path, spec: DatasetSpec) -> None:
    """建表、灌数据（storage API）与建索引（SQL DDL）。"""

    handle = DatabaseServer(str(data_dir)).connect(spec.database)
    handle.create_table(spec.table, spec.columns)
    for index in range(spec.rows):
        handle.insert(spec.table, spec.row_at(index))
    runner = Runner(
        server=DatabaseServer(str(data_dir)), parse=parse, parse_script=parse_script
    )
    for name, column in spec.indexes:
        runner.execute(f"CREATE INDEX {name} ON {spec.table} ({column});")
    (data_dir / MANIFEST_NAME).write_text(
        json.dumps(spec.manifest(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
