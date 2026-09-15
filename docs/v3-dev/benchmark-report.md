# V3 基准报告（索引 · 代价选路 · 优化器）

> 生成时间（UTC）：2026-09-15T04:21:27+00:00　|　Python 3.11.15　|　Linux-6.18.33.2-microsoft-standard-WSL2-x86_64-with-glibc2.39
> 数据集：`main.events`，4000 行，seed=1，索引 idx_events_id(id), idx_events_amount(amount)
> 采样：每种模式冷 3 次 + 热 3 次；数据集指纹 `0f34160918fc2da9`（复用缓存）
> 复现：`python -m bench run --rows 4000 --seed 1 --repeat 3`

## 口径（先读这几条，再读数字）

- **冷** = 每次采样新建 `DatabaseServer`：新 Buffer Pool、新 rid→页 映射。
- **热** = 同一个 server 第二次及以后执行，反映稳态。
- **SQL 模式先预热 `statistics()` 再归零计数**（sql-auto, sql-seq, sql-index）：统计基线会读满数据页，不预热就会把这笔成本记到第一个跑的模式头上。
- **存储模式（api-seq / api-index）不预热也不经过规划器**，报的是纯 B 成本。
- 逻辑页读 = `pager.read_page` 调用数，按 `用户表 / 索引 / 系统目录` 分桶；物理读盘 = 同一批调用里 `cache.misses` 的增量。
- **加速比按逻辑页读总数算**：SQL 行以 `sql-seq` 为基线、存储行以 `api-seq` 为基线（同一层的口径才可比）。SQL 行因预热已把页读进池子，物理读盘接近 0，所以那一列对 SQL 行只作参考。
- 冷启动的**第一次**访问要付一次 D37 惰性布局基线（把整表读一遍，建立行数与活动数据页集合）；此后所有扫描只按数据页读一遍（B 的 D49b），不再重复遍历整表判定页类型。两种 seq 模式同等承担这笔一次性成本。
- 耗时只作辅证：本报告的所有判断都能用页读计数复现（CI 不断言墙钟）。
- 跨模式等价性用**行多重集**（排序后哈希）比对，不用行序——索引按键序返回行。

## 主表（冷启动）

| 场景 | 模式 | 层次 | 返回行 | 用户表页读 | 索引页读 | 系统目录页读 | 逻辑页读合计 | 物理读盘 | 耗时中位数(ms) | 页读加速比 | 选路 reason |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `point_hit` | `sql-auto` | A+B+C | 1 | 13 | 6 | 0 | 19 | 3 | 3.83 | 3.11× | INDEX_EQUALITY |
| `point_hit` | `sql-seq` | A+B+C | 1 | 59 | 0 | 0 | 59 | 0 | 34.25 | 1.00× | FORCED_SEQ |
| `point_hit` | `sql-index` | A+B+C | 1 | 13 | 6 | 0 | 19 | 3 | 3.36 | 3.11× | FORCED_INDEX |
| `point_hit` | `api-seq` | B | 1 | 166 | 0 | 0 | 166 | 54 | 25.20 | 1.00× | — |
| `point_hit` | `api-index` | B | 1 | 25 | 6 | 0 | 31 | 10 | 1.93 | 5.35× | — |
| `point_miss` | `sql-auto` | A+B+C | 0 | 12 | 6 | 0 | 18 | 3 | 3.89 | 3.28× | INDEX_EQUALITY |
| `point_miss` | `sql-seq` | A+B+C | 0 | 59 | 0 | 0 | 59 | 0 | 37.37 | 1.00× | FORCED_SEQ |
| `point_miss` | `sql-index` | A+B+C | 0 | 12 | 6 | 0 | 18 | 3 | 4.31 | 3.28× | FORCED_INDEX |
| `point_miss` | `api-seq` | B | 0 | 166 | 0 | 0 | 166 | 54 | 22.03 | 1.00× | — |
| `point_miss` | `api-index` | B | 0 | 24 | 6 | 0 | 30 | 9 | 1.82 | 5.53× | — |
| `range_selective` | `sql-auto` | A+B+C | 99 | 59 | 0 | 0 | 59 | 0 | 38.42 | 1.00× | SEQ_CHEAPER |
| `range_selective` | `sql-seq` | A+B+C | 99 | 59 | 0 | 0 | 59 | 0 | 42.27 | 1.00× | FORCED_SEQ |
| `range_selective` | `sql-index` | A+B+C | 99 | 111 | 6 | 0 | 117 | 3 | 19.73 | 0.50× | FORCED_INDEX |
| `range_selective` | `api-seq` | B | 99 | 166 | 0 | 0 | 166 | 54 | 24.23 | 1.00× | — |
| `range_selective` | `api-index` | B | 99 | 123 | 6 | 0 | 129 | 11 | 14.02 | 1.29× | — |
| `range_wide` | `sql-auto` | A+B+C | 2999 | 59 | 0 | 0 | 59 | 0 | 46.06 | 1.00× | SEQ_CHEAPER |
| `range_wide` | `sql-seq` | A+B+C | 2999 | 59 | 0 | 0 | 59 | 0 | 47.16 | 1.00× | FORCED_SEQ |
| `range_wide` | `sql-index` | A+B+C | 2999 | 3011 | 46 | 0 | 3057 | 77 | 542.58 | 0.02× | FORCED_INDEX |
| `range_wide` | `api-seq` | B | 2999 | 166 | 0 | 0 | 166 | 54 | 25.65 | 1.00× | — |
| `range_wide` | `api-index` | B | 2999 | 3023 | 46 | 0 | 3069 | 85 | 458.64 | 0.05× | — |
| `dup_lookup` | `sql-auto` | A+B+C | 43 | 55 | 6 | 0 | 61 | 3 | 9.59 | 0.97× | INDEX_EQUALITY |
| `dup_lookup` | `sql-seq` | A+B+C | 43 | 59 | 0 | 0 | 59 | 0 | 35.11 | 1.00× | FORCED_SEQ |
| `dup_lookup` | `sql-index` | A+B+C | 43 | 55 | 6 | 0 | 61 | 3 | 11.77 | 0.97× | FORCED_INDEX |
| `dup_lookup` | `api-seq` | B | 43 | 166 | 0 | 0 | 166 | 54 | 25.58 | 1.00× | — |
| `dup_lookup` | `api-index` | B | 43 | 67 | 6 | 0 | 73 | 35 | 10.59 | 2.27× | — |
| `no_index_column` | `sql-auto` | A+B+C | 1 | 59 | 0 | 0 | 59 | 0 | 41.14 | 1.00× | NO_MATCHING_INDEX |
| `no_index_column` | `sql-seq` | A+B+C | 1 | 59 | 0 | 0 | 59 | 0 | 39.93 | 1.00× | FORCED_SEQ |
| `no_index_column` | `sql-index` | A+B+C | 0 | 12 | 0 | 0 | 12 | 0 | 2.42 | 4.92× | 错误 |
| `no_index_column` | `api-seq` | B | 1 | 166 | 0 | 0 | 166 | 54 | 31.51 | 1.00× | — |
| `no_index_column` | `api-index` | B | 0 | 24 | 0 | 0 | 24 | 6 | 1.39 | 6.92× | 错误 |

## 主表（同进程热）

| 场景 | 模式 | 层次 | 返回行 | 用户表页读 | 索引页读 | 系统目录页读 | 逻辑页读合计 | 物理读盘 | 耗时中位数(ms) | 页读加速比 | 选路 reason |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `point_hit` | `sql-auto` | A+B+C | 1 | 13 | 4 | 0 | 17 | 0 | 3.02 | 3.47× | INDEX_EQUALITY |
| `point_hit` | `sql-seq` | A+B+C | 1 | 59 | 0 | 0 | 59 | 0 | 35.41 | 1.00× | FORCED_SEQ |
| `point_hit` | `sql-index` | A+B+C | 1 | 13 | 4 | 0 | 17 | 0 | 3.34 | 3.47× | FORCED_INDEX |
| `point_hit` | `api-seq` | B | 1 | 59 | 0 | 0 | 59 | 0 | 15.84 | 1.00× | — |
| `point_hit` | `api-index` | B | 1 | 13 | 4 | 0 | 17 | 0 | 1.62 | 3.47× | — |
| `point_miss` | `sql-auto` | A+B+C | 0 | 12 | 4 | 0 | 16 | 0 | 2.07 | 3.69× | INDEX_EQUALITY |
| `point_miss` | `sql-seq` | A+B+C | 0 | 59 | 0 | 0 | 59 | 0 | 36.71 | 1.00× | FORCED_SEQ |
| `point_miss` | `sql-index` | A+B+C | 0 | 12 | 4 | 0 | 16 | 0 | 2.22 | 3.69× | FORCED_INDEX |
| `point_miss` | `api-seq` | B | 0 | 59 | 0 | 0 | 59 | 0 | 15.38 | 1.00× | — |
| `point_miss` | `api-index` | B | 0 | 12 | 4 | 0 | 16 | 0 | 1.99 | 3.69× | — |
| `range_selective` | `sql-auto` | A+B+C | 99 | 59 | 0 | 0 | 59 | 0 | 40.41 | 1.00× | SEQ_CHEAPER |
| `range_selective` | `sql-seq` | A+B+C | 99 | 59 | 0 | 0 | 59 | 0 | 43.69 | 1.00× | FORCED_SEQ |
| `range_selective` | `sql-index` | A+B+C | 99 | 111 | 4 | 0 | 115 | 0 | 17.23 | 0.51× | FORCED_INDEX |
| `range_selective` | `api-seq` | B | 99 | 59 | 0 | 0 | 59 | 0 | 17.81 | 1.00× | — |
| `range_selective` | `api-index` | B | 99 | 111 | 4 | 0 | 115 | 0 | 14.09 | 0.51× | — |
| `range_wide` | `sql-auto` | A+B+C | 2999 | 59 | 0 | 0 | 59 | 0 | 50.18 | 1.00× | SEQ_CHEAPER |
| `range_wide` | `sql-seq` | A+B+C | 2999 | 59 | 0 | 0 | 59 | 0 | 54.21 | 1.00× | FORCED_SEQ |
| `range_wide` | `sql-index` | A+B+C | 2999 | 3011 | 44 | 0 | 3055 | 85 | 523.36 | 0.02× | FORCED_INDEX |
| `range_wide` | `api-seq` | B | 2999 | 59 | 0 | 0 | 59 | 0 | 20.27 | 1.00× | — |
| `range_wide` | `api-index` | B | 2999 | 3011 | 44 | 0 | 3055 | 85 | 470.61 | 0.02× | — |
| `dup_lookup` | `sql-auto` | A+B+C | 43 | 55 | 4 | 0 | 59 | 0 | 9.26 | 1.00× | INDEX_EQUALITY |
| `dup_lookup` | `sql-seq` | A+B+C | 43 | 59 | 0 | 0 | 59 | 0 | 37.20 | 1.00× | FORCED_SEQ |
| `dup_lookup` | `sql-index` | A+B+C | 43 | 55 | 4 | 0 | 59 | 0 | 10.42 | 1.00× | FORCED_INDEX |
| `dup_lookup` | `api-seq` | B | 43 | 59 | 0 | 0 | 59 | 0 | 18.29 | 1.00× | — |
| `dup_lookup` | `api-index` | B | 43 | 55 | 4 | 0 | 59 | 0 | 8.88 | 1.00× | — |
| `no_index_column` | `sql-auto` | A+B+C | 1 | 59 | 0 | 0 | 59 | 0 | 40.44 | 1.00× | NO_MATCHING_INDEX |
| `no_index_column` | `sql-seq` | A+B+C | 1 | 59 | 0 | 0 | 59 | 0 | 42.03 | 1.00× | FORCED_SEQ |
| `no_index_column` | `sql-index` | A+B+C | 0 | 12 | 0 | 0 | 12 | 0 | 2.13 | 4.92× | 错误 |
| `no_index_column` | `api-seq` | B | 1 | 59 | 0 | 0 | 59 | 0 | 19.36 | 1.00× | — |
| `no_index_column` | `api-index` | B | 0 | 12 | 0 | 0 | 12 | 0 | 2.11 | 4.92× | 错误 |

## 选路决策（sql-auto，冷启动）

| 场景 | reason | 选择列 | 选择性 | 估计行数 | 扫描代价 | 索引代价 | 实际行数 |
|---|---|---|---|---|---|---|---|
| `point_hit` | INDEX_EQUALITY | `id` | 0.0007 | 3.00 | 47.00 | 6.00 | 1 |
| `point_miss` | INDEX_EQUALITY | `id` | 0.0000 | 0.0000 | 47.00 | 3.00 | 0 |
| `range_selective` | SEQ_CHEAPER | `id` | 0.0248 | 99.02 | 47.00 | 102.02 | 99 |
| `range_wide` | SEQ_CHEAPER | `id` | 0.7499 | 2999.75 | 47.00 | 3002.75 | 2999 |
| `dup_lookup` | INDEX_EQUALITY | `amount` | 0.0103 | 41.24 | 47.00 | 44.24 | 43 |
| `no_index_column` | NO_MATCHING_INDEX | `name` | — | — | — | — | 1 |

## 错误矩阵（强制物理模式的失败码归属）

| 场景 | 模式 | 错误码 | 是否符合预期 |
|---|---|---|---|
| `no_index_column` | `sql-index` | `E_INDEX_NOT_FOUND` | 是 |
| `no_index_column` | `api-index` | `E_INDEX_NOT_FOUND` | 是 |

## 跨模式一致性

- `point_hit`：一致（sql-auto=09feae41c02a878a、sql-seq=09feae41c02a878a、sql-index=09feae41c02a878a、api-seq=09feae41c02a878a、api-index=09feae41c02a878a）
- `point_miss`：一致（sql-auto=4f53cda18c2baa0c、sql-seq=4f53cda18c2baa0c、sql-index=4f53cda18c2baa0c、api-seq=4f53cda18c2baa0c、api-index=4f53cda18c2baa0c）
- `range_selective`：一致（sql-auto=a8b90081d8b44e46、sql-seq=a8b90081d8b44e46、sql-index=a8b90081d8b44e46、api-seq=a8b90081d8b44e46、api-index=a8b90081d8b44e46）
- `range_wide`：一致（sql-auto=05653facc5e99a4e、sql-seq=05653facc5e99a4e、sql-index=05653facc5e99a4e、api-seq=05653facc5e99a4e、api-index=05653facc5e99a4e）
- `dup_lookup`：一致（sql-auto=3c986ca4671368a3、sql-seq=3c986ca4671368a3、sql-index=3c986ca4671368a3、api-seq=3c986ca4671368a3、api-index=3c986ca4671368a3）
- `no_index_column`：一致（sql-auto=9358f8b88b3cd82b、sql-seq=9358f8b88b3cd82b、api-seq=9358f8b88b3cd82b）

## 结论

- 跨模式行集一致性：全部一致；计数可复现：是。
- SQL 层（含 C 的选路，冷启动、已统一预热）：
  - `point_hit`：seq 59 → index 19 逻辑页读（3.11×）；`auto` 选 `INDEX_EQUALITY`，实际 19 页
  - `point_miss`：seq 59 → index 18 逻辑页读（3.28×）；`auto` 选 `INDEX_EQUALITY`，实际 18 页
  - `range_selective`：seq 59 → index 117 逻辑页读（0.50×）；`auto` 选 `SEQ_CHEAPER`，实际 59 页
  - `range_wide`：seq 59 → index 3057 逻辑页读（0.02×）；`auto` 选 `SEQ_CHEAPER`，实际 59 页
  - `dup_lookup`：seq 59 → index 61 逻辑页读（0.97×）；`auto` 选 `INDEX_EQUALITY`，实际 61 页
- 存储层（纯 B，不经规划器）：冷启动下索引被回表定位拖累（D49），热态才体现索引收益：
  - `point_hit`：冷 seq 166 / index 31；热 seq 59 / index 17
  - `point_miss`：冷 seq 166 / index 30；热 seq 59 / index 16
  - `range_selective`：冷 seq 166 / index 129；热 seq 59 / index 115
  - `range_wide`：冷 seq 166 / index 3069；热 seq 59 / index 3055
  - `dup_lookup`：冷 seq 166 / index 73；热 seq 59 / index 59
  - `no_index_column`：冷 seq 166 / index 24；热 seq 59 / index 12

## 已知限制

- **冷启动回表**：D49a 起索引叶条目自带行页号，冷进程回表也是每行一页；页号只是提示，读侧校验不符会退回全表定位，因此正确性不依赖它。
- **布局基线**：冷进程的第一次访问仍要付一次 D37 惰性布局基线（建立行数与活动数据页集合），此后扫描只按数据页读一遍（D49b）。
- **统计基线**：极值精确化后，`statistics()` 的首次调用会读满数据页（进程内一次性）；这也会顺带预热 rid 映射，使 `auto` 与强制 `index` 在同一进程里的成本不对称——这正是 `api-*` 两列存在的原因。
- **索引文件版本**：D49a 起为 2；旧目录（v1）里的索引在**首次被访问**时自动重建一次（O(n log n)），bench 已在数据集准备阶段完成这件事。
- **基数近似**：`distinct_count` 仍是有界采样（最多 16 页），大表会偏低；`min/max` 已精确。
- C 的优化器与选路只覆盖单表谓词下推；JOIN 重排不在本版范围。

## 复现

```bash
python -m bench run --rows 4000 --seed 1 --repeat 3
```
