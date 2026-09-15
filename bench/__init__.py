"""V3 基准工具（F8）：五种模式跑同一批查询，产出可复现的对比报告。

定位（V3 计划书 DV3-05）：`bench/` 与 `main.py` 一样是**装配层**，允许同时
import A/B/C 三家；但它只准走各家的**公开入口**，不得 import
`storage.engine`、`runner.physical` 这类内部模块，也不得直接读写数据文件。

五个测量模式：

- `sql-auto` / `sql-seq` / `sql-index`：真 SQL 端到端，对应
  `Runner.execute(sql, physical=...)`；`auto` 会经过 C 的代价选路。
- `api-seq` / `api-index`：存储级调用，绕过规划器，只量 B 的成本。

指标口径见 `bench/report.py` 的报告头与 `bench/README.md`。
"""

BENCH_VERSION = "1"
"""报告格式版本：字段增删时递增，便于 `--compare` 判定可比性。"""

__all__ = ["BENCH_VERSION"]
