"""报告渲染：JSON（机读证据）+ Markdown（进仓库的对比表）。"""

from __future__ import annotations

import json
from pathlib import Path

from bench.measure import BUCKETS


_MODE_LAYER = {
    "sql-auto": "A+B+C",
    "sql-seq": "A+B+C",
    "sql-index": "A+B+C",
    "api-seq": "B",
    "api-index": "B",
}


def write_json(result: dict[str, object], path: Path) -> None:
    """落一份机读证据；`--compare` 之类的后续工具都读它。"""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def write_markdown(result: dict[str, object], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(result), encoding="utf-8")


def render_markdown(result: dict[str, object]) -> str:
    """渲染报告：口径 → 冷/热主表 → 选路 → 错误矩阵 → 一致性 → 结论。"""

    meta = result["meta"]
    lines: list[str] = [
        "# V3 基准报告（索引 · 代价选路 · 优化器）",
        "",
        f"> 生成时间（UTC）：{meta['generated_at_utc']}　|　"
        f"Python {meta['python']}　|　{meta['platform']}",
        f"> 数据集：`{meta['database']}.{meta['table']}`，"
        f"{meta['rows']} 行，seed={meta['seed']}，"
        f"索引 {', '.join(f'{name}({column})' for name, column in meta['indexes'])}",
        f"> 采样：每种模式冷 {meta['repeat']} 次 + 热 {meta['repeat']} 次；"
        f"数据集指纹 `{meta['dataset_fingerprint']}`"
        f"（{'本次新建' if meta['dataset_created'] else '复用缓存'}）",
        f"> 复现：`{meta['reproduce']}`",
        "",
        "## 口径（先读这几条，再读数字）",
        "",
        "- **冷** = 每次采样新建 `DatabaseServer`：新 Buffer Pool、新 rid→页 映射。",
        "- **热** = 同一个 server 第二次及以后执行，反映稳态。",
        f"- **SQL 模式先预热 `statistics()` 再归零计数**（{', '.join(meta['prewarmed_modes'])}）："
        "统计基线会读满数据页，不预热就会把这笔成本记到第一个跑的模式头上。",
        "- **存储模式（api-seq / api-index）不预热也不经过规划器**，报的是纯 B 成本。",
        "- 逻辑页读 = `pager.read_page` 调用数，按 `用户表 / 索引 / 系统目录` 分桶；"
        "物理读盘 = 同一批调用里 `cache.misses` 的增量。",
        "- **加速比按逻辑页读总数算**：SQL 行以 `sql-seq` 为基线、存储行以 `api-seq` 为基线"
        "（同一层的口径才可比）。SQL 行因预热已把页读进池子，物理读盘接近 0，"
        "所以那一列对 SQL 行只作参考。",
        "- 冷启动的**第一次**访问要付一次 D37 惰性布局基线（把整表读一遍，建立行数与"
        "活动数据页集合）；此后所有扫描只按数据页读一遍（B 的 D49b），不再重复遍历"
        "整表判定页类型。两种 seq 模式同等承担这笔一次性成本。",
        "- 耗时只作辅证：本报告的所有判断都能用页读计数复现（CI 不断言墙钟）。",
        "- 跨模式等价性用**行多重集**（排序后哈希）比对，不用行序——索引按键序返回行。",
        "",
        "## 主表（冷启动）",
        "",
    ]
    lines.extend(_table(result, phase="cold"))
    lines.extend(["", "## 主表（同进程热）", ""])
    lines.extend(_table(result, phase="hot"))
    lines.extend(["", "## 选路决策（sql-auto，冷启动）", ""])
    lines.extend(_routing_table(result))
    lines.extend(["", "## 错误矩阵（强制物理模式的失败码归属）", ""])
    lines.extend(_error_table(result))
    lines.extend(["", "## 跨模式一致性", ""])
    for scenario in result["scenarios"]:
        consistency = scenario["consistency"]
        mark = "一致" if consistency["consistent"] else "见下"
        lines.append(
            f"- `{scenario['name']}`：{mark}（"
            + "、".join(f"{mode}={digest}" for mode, digest in consistency["digests"].items())
            + "）"
        )
    lines.extend(["", "## 结论", ""])
    lines.extend(_conclusions(result))
    lines.extend(
        [
            "",
            "## 已知限制",
            "",
            "- **冷启动回表**：D49a 起索引叶条目自带行页号，冷进程回表也是每行一页；"
            "页号只是提示，读侧校验不符会退回全表定位，因此正确性不依赖它。",
            "- **布局基线**：冷进程的第一次访问仍要付一次 D37 惰性布局基线"
            "（建立行数与活动数据页集合），此后扫描只按数据页读一遍（D49b）。",
            "- **统计基线**：极值精确化后，`statistics()` 的首次调用会读满数据页"
            "（进程内一次性）；这也会顺带预热 rid 映射，使 `auto` 与强制 `index` "
            "在同一进程里的成本不对称——这正是 `api-*` 两列存在的原因。",
            "- **索引文件版本**：D49a 起为 2；旧目录（v1）里的索引在**首次被访问**"
            "时自动重建一次（O(n log n)），bench 已在数据集准备阶段完成这件事。",
            "- **基数近似**：`distinct_count` 仍是有界采样（最多 16 页），"
            "大表会偏低；`min/max` 已精确。",
            "- C 的优化器与选路只覆盖单表谓词下推；JOIN 重排不在本版范围。",
            "",
            "## 复现",
            "",
            "```bash",
            meta["reproduce"],
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _table(result: dict[str, object], *, phase: str) -> list[str]:
    header = (
        "| 场景 | 模式 | 层次 | 返回行 | 用户表页读 | 索引页读 | 系统目录页读 | "
        "逻辑页读合计 | 物理读盘 | 耗时中位数(ms) | 页读加速比 | 选路 reason |"
    )
    separator = "|---|---|---|---|---|---|---|---|---|---|---|---|"
    lines = [header, separator]
    for scenario in result["scenarios"]:
        modes = scenario["modes"]
        baselines = {
            "sql": int(modes["sql-seq"][phase]["logical_reads"]["total"]),
            "api": int(modes["api-seq"][phase]["logical_reads"]["total"]),
        }
        for mode, data in modes.items():
            metrics = data[phase]
            logical = metrics["logical_reads"]
            physical = metrics["physical_reads"]
            layer = "api" if mode.startswith("api-") else "sql"
            speedup = _speedup(baselines[layer], logical["total"])
            reason = metrics["reason"] or ("错误" if metrics["error_code"] else "—")
            lines.append(
                f"| `{scenario['name']}` | `{mode}` | {_MODE_LAYER[mode]} | "
                f"{metrics['returned_rows']} | {logical['user_table']} | "
                f"{logical['index']} | {logical['syscatalog']} | {logical['total']} | "
                f"{physical['total']} | {metrics['elapsed_ms_median']:.2f} | "
                f"{speedup} | {reason} |"
            )
    return lines


def _speedup(baseline: int, value: int) -> str:
    if value <= 0 or baseline <= 0:
        return "—"
    return f"{baseline / value:.2f}×"


def _routing_table(result: dict[str, object]) -> list[str]:
    lines = [
        "| 场景 | reason | 选择列 | 选择性 | 估计行数 | 扫描代价 | 索引代价 | 实际行数 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for scenario in result["scenarios"]:
        metrics = scenario["modes"]["sql-auto"]["cold"]
        if metrics["error_code"] is not None:
            lines.append(
                f"| `{scenario['name']}` | 错误 | — | — | — | — | — | — |"
            )
            continue
        lines.append(
            f"| `{scenario['name']}` | {metrics['reason']} | `{scenario['column']}` | "
            f"{_number(metrics['selectivity'])} | {_number(metrics['estimated_rows'])} | "
            f"{_number(metrics['seq_cost'])} | {_number(metrics['index_cost'])} | "
            f"{metrics['returned_rows']} |"
        )
    return lines


def _error_table(result: dict[str, object]) -> list[str]:
    errors = result["errors"]
    if not errors:
        return ["（本批场景没有错误码）"]
    lines = ["| 场景 | 模式 | 错误码 | 是否符合预期 |", "|---|---|---|---|"]
    for item in errors:
        lines.append(
            f"| `{item['scenario']}` | `{item['mode']}` | `{item['error_code']}` | "
            f"{'是' if item['expected'] else '否（需要检查）'} |"
        )
    return lines


def _conclusions(result: dict[str, object]) -> list[str]:
    """用冷启动页读算出的结论；数字全部来自报告自己。"""

    lines: list[str] = []
    verdict = result["verdict"]
    lines.append(
        f"- 跨模式行集一致性：{'全部一致' if verdict['rows_match_across_modes'] else '存在不一致 ' + str(verdict['inconsistent_scenarios'])}；"
        f"计数可复现：{'是' if verdict['counters_stable'] else '否 ' + str(verdict['unstable_samples'])}。"
    )
    sql_lines: list[str] = []
    for scenario in result["scenarios"]:
        modes = scenario["modes"]
        if modes["sql-index"]["cold"]["error_code"] is not None:
            continue  # 错误矩阵单独成段，不参与"索引快多少"的比较
        seq = int(modes["sql-seq"]["cold"]["logical_reads"]["total"])
        index = int(modes["sql-index"]["cold"]["logical_reads"]["total"])
        auto = modes["sql-auto"]["cold"]
        auto_total = int(auto["logical_reads"]["total"])
        sql_lines.append(
            f"  - `{scenario['name']}`：seq {seq} → index {index} 逻辑页读"
            f"（{seq / index:.2f}×）；`auto` 选 `{auto['reason'] or auto['error_code']}`，"
            f"实际 {auto_total} 页"
        )
    lines.append("- SQL 层（含 C 的选路，冷启动、已统一预热）：")
    lines.extend(sql_lines or ["  - （无数据）"])

    storage_lines: list[str] = []
    for scenario in result["scenarios"]:
        modes = scenario["modes"]
        cold_seq = int(modes["api-seq"]["cold"]["logical_reads"]["total"])
        cold_index = int(modes["api-index"]["cold"]["logical_reads"]["total"])
        hot_seq = int(modes["api-seq"]["hot"]["logical_reads"]["total"])
        hot_index = int(modes["api-index"]["hot"]["logical_reads"]["total"])
        storage_lines.append(
            f"  - `{scenario['name']}`：冷 seq {cold_seq} / index {cold_index}；"
            f"热 seq {hot_seq} / index {hot_index}"
        )
    lines.append(
        "- 存储层（纯 B，不经规划器）：冷启动下索引被回表定位拖累（D49），"
        "热态才体现索引收益："
    )
    lines.extend(storage_lines or ["  - （无数据）"])
    return lines


def _number(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}" if abs(value) < 1 else f"{value:.2f}"
    return str(value)
