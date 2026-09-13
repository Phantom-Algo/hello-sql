"""HELLO-SQL 全链路可视化包。

本包是 A（编译）、B（存储）和 C（运行）三方共同使用的观察层，不参与 SQL
语义，也不替代任何现有公共接口。当前公开查询追踪契约、C 使用的
``TraceHub``、A 的编译追踪、B 的存储追踪以及 C 的执行追踪。
新增的 ``QueryInspector`` 在同一次真实语句中合并三方阶段，
``InspectionSnapshot`` 则为 ``/inspect`` 提供 A/B/C/ALL 只读筛选。
各模块把已经发生的真实处理过程转换为只读快照，后续窗口只读取
这些快照进行展示，绝不重新执行 SQL。

公开类型集中从这里导出，调用方无需依赖 ``UI.trace_models`` 的内部辅助函数。
"""

from UI.trace_models import (
    QueryTrace,
    StageTrace,
    TraceEvent,
    TraceOwner,
    TraceStatus,
)
from UI.trace_hub import TraceHub, TraceReservation
from UI.compiler_trace import (
    CompilerTraceMode,
    CompilerTraceResult,
    trace_parse,
    trace_parse_script,
)
from UI.storage_trace import StorageTraceCollector, StorageTraceRouter
from UI.execution_trace import ExecutionTraceCollector, ExecutionTraceRouter
from UI.inspection import (
    InspectionModule,
    InspectionSnapshot,
    QueryInspector,
    format_inspection_text,
)
from UI.viewer import InspectionViewer
from UI.linkage import build_linkage

__all__ = (
    "QueryTrace",
    "StageTrace",
    "TraceEvent",
    "TraceOwner",
    "TraceStatus",
    "TraceHub",
    "TraceReservation",
    "CompilerTraceMode",
    "CompilerTraceResult",
    "trace_parse",
    "trace_parse_script",
    "StorageTraceCollector",
    "StorageTraceRouter",
    "ExecutionTraceCollector",
    "ExecutionTraceRouter",
    "InspectionModule",
    "InspectionSnapshot",
    "QueryInspector",
    "format_inspection_text",
    "InspectionViewer",
    "build_linkage",
)
