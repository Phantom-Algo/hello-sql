"""物理层：索引请求翻译、代价模型与选路器。

- requests.py：索引请求形状与谓词翻译（纯函数）；
- cost_model.py：选择性估算与代价公式（纯函数 + 标定常量）；
- planner.py：PhysicalPlanner、AccessPath、BuildContext 与理由码。

本包只消费契约里的数据类型，不 import storage：对 Storage 的调用留在执行器。
"""

from runner.physical.cost_model import (
    INDEX_FANOUT,
    RANGE_DEFAULT_SELECTIVITY,
    REF_PAGE_COST,
    equality_selectivity,
    index_cost,
    range_selectivity,
    seq_cost,
    tree_height,
)
from runner.physical.planner import (
    FORCED_INDEX,
    FORCED_SEQ,
    INDEX_EQUALITY,
    INDEX_RANGE,
    NO_MATCHING_INDEX,
    NO_PREDICATE,
    NO_STATS,
    PHYSICAL_MODES,
    SEQ_CHEAPER,
    AccessPath,
    BuildContext,
    ListIndexesOfTable,
    PhysicalMode,
    PhysicalPlanner,
    StatisticsOfTable,
    validate_physical_mode,
)
from runner.physical.requests import (
    CAST_UNSAFE,
    NE_UNSUPPORTED,
    NOT_BARE_COLUMN,
    NOT_COMPARISON,
    NOT_CONJUNCT,
    IndexCandidate,
    IndexLookup,
    IndexRange,
    IndexRequest,
    TranslationResult,
    Unsupported,
    translate,
)

__all__ = [
    # requests
    "IndexLookup",
    "IndexRange",
    "IndexRequest",
    "IndexCandidate",
    "TranslationResult",
    "Unsupported",
    "translate",
    "NE_UNSUPPORTED",
    "CAST_UNSAFE",
    "NOT_COMPARISON",
    "NOT_BARE_COLUMN",
    "NOT_CONJUNCT",
    # cost_model
    "INDEX_FANOUT",
    "REF_PAGE_COST",
    "RANGE_DEFAULT_SELECTIVITY",
    "tree_height",
    "seq_cost",
    "index_cost",
    "equality_selectivity",
    "range_selectivity",
    # planner
    "PhysicalMode",
    "PHYSICAL_MODES",
    "validate_physical_mode",
    "AccessPath",
    "BuildContext",
    "PhysicalPlanner",
    "StatisticsOfTable",
    "ListIndexesOfTable",
    "FORCED_SEQ",
    "FORCED_INDEX",
    "NO_PREDICATE",
    "NO_MATCHING_INDEX",
    "NO_STATS",
    "SEQ_CHEAPER",
    "INDEX_EQUALITY",
    "INDEX_RANGE",
]
