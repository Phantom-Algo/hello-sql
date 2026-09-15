"""Executor 树统一构建入口。"""

from __future__ import annotations

from collections.abc import Callable

from contracts.errors import E_BAD_ARG, SqlError
from contracts.storage import TableInfo
from runner.executor.base import StatementExecutor
from runner.executor.ddl import build_ddl_executor
from runner.executor.dml import build_dml_executor
from runner.executor.dql import build_select_executor
from runner.logical_plan.base import LogicalPlan
from runner.logical_plan.builder import DescribeTable
from runner.logical_plan.plans import (
    LogicalCreateDatabase,
    LogicalCreateIndex,
    LogicalCreateTable,
    LogicalDelete,
    LogicalDropDatabase,
    LogicalDropIndex,
    LogicalDropTable,
    LogicalInsert,
    LogicalProjection,
    LogicalUpdate,
    LogicalUseDatabase,
)
from runner.physical.planner import (
    AccessPath,
    BuildContext,
    ListIndexesOfTable,
    PhysicalMode,
    PhysicalPlanner,
    StatisticsOfTable,
)
from runner.trace_hooks import RunnerTraceSink, trace_runner_operation

CurrentDatabase = Callable[[], str]
"""读取当前数据库名的回调；库名进缓存键，必须每次现取而非构造期固定。"""

# 可能改写目录的语句：构建这类语句时先失效结构缓存，后续语句才会重新 describe。
# USE 不在其中——切换后的库名不同，命中的是另一组键，无需清空。
_CATALOG_WRITING_PLANS = (
    LogicalCreateDatabase,
    LogicalDropDatabase,
    LogicalCreateTable,
    LogicalDropTable,
)


class ExecutorTreeBuilder:
    """把语句级 LogicalPlan 根节点转换为 StatementExecutor。

    本类只负责识别**根节点**所属的语句类别，具体 Executor 的构造由各执行模块负责；
    describe_table 供 Scan 执行器在构建期反查表完整列序（裁剪计划的执行侧配套），
    statistics / list_indexes 供选路器估算代价（同一个 Scan 的物理路径决策）。

    表结构按 (当前数据库, 表名) 缓存：构建期只需要「表完整列序」这一静态信息，
    而同一会话里同名表可能存在于多个库且结构不同，因此库名必须进键。缓存跨语句
    存活，使同一张表在反复执行时只 describe 一次。

    统计与索引清单不在这里缓存：写入会让统计失效，跨语句缓存是错的。它们由每次
    build() 新建的 PhysicalPlanner 持有，缓存范围因此天然限定在单条语句内。

    trace_sink: 接收构建输入、最终 Executor 树和失败的字典回调。
    """

    def __init__(
        self,
        describe_table: DescribeTable,
        current_database: CurrentDatabase,
        *,
        statistics: StatisticsOfTable,
        list_indexes: ListIndexesOfTable,
        trace_sink: RunnerTraceSink | None = None,
    ) -> None:
        self._describe_table = describe_table
        self._current_database = current_database
        self._statistics = statistics
        self._list_indexes = list_indexes
        self._schema_cache: dict[tuple[str, str], TableInfo] = {}
        self._trace_sink = trace_sink
        self._last_paths: tuple[AccessPath, ...] = ()

    @property
    def last_access_paths(self) -> tuple[AccessPath, ...]:
        """最近一次 build() 产生的选路记录；尚未构建时为空。

        记录在语句级判定之前落定，因此强制索引因"整条语句都没有候选"而失败时，
        仍能读到每个扫描位置的决策，便于定位是哪个位置没有键值可用。
        """
        return self._last_paths

    @trace_runner_operation("executor", "build_executor_tree")
    def build(
        self,
        plan: LogicalPlan,
        *,
        physical: PhysicalMode = "auto",
    ) -> StatementExecutor:
        """构建一棵可执行的 Executor 树。

        physical 决定每个 Scan 的访问路径。构建完成后做一次语句级判定：
        强制索引模式下，只要语句确实含扫描却一处都构造不出索引请求，
        就抛 E_BAD_ARG；不含扫描的语句（DDL、INSERT、USE）正常执行。
        """
        if isinstance(plan, _CATALOG_WRITING_PLANS):
            # 先失效再构建：本语句执行完缓存必为空，不会把过期结构带进后续语句
            self._schema_cache.clear()
        self._last_paths = ()
        # 选路器每次构建新建，元数据缓存因此不会跨语句存活
        planner = PhysicalPlanner(
            statistics=self._statistics,
            list_indexes=self._list_indexes,
            physical=physical,
            trace_sink=self._trace_sink,
        )
        context = BuildContext(describe=self._describe, planner=planner)
        executor = self._build_statement(plan, context)
        self._last_paths = planner.paths
        if (
            planner.physical == "index"
            and self._last_paths
            and all(not path.requests for path in self._last_paths)
        ):
            raise SqlError(
                E_BAD_ARG,
                'physical="index" requires at least one index-eligible predicate',
            )
        return executor

    def _build_statement(
        self,
        plan: LogicalPlan,
        context: BuildContext,
    ) -> StatementExecutor:
        """按根节点所属的语句类别分派到各执行模块的构建入口。"""
        match plan:
            case LogicalProjection():
                return build_select_executor(plan, context)
            case LogicalInsert() | LogicalUpdate() | LogicalDelete():
                return build_dml_executor(plan, context)
            case (
                LogicalCreateDatabase()
                | LogicalDropDatabase()
                | LogicalUseDatabase()
                | LogicalCreateTable()
                | LogicalDropTable()
                | LogicalCreateIndex()
                | LogicalDropIndex()
            ):
                return build_ddl_executor(plan)
            case _:
                raise TypeError(
                    "unsupported statement plan: "
                    f"{type(plan).__name__}"
                )

    def _describe(self, table: str) -> TableInfo:
        """按 (当前数据库, 表名) 取表结构；命中缓存时不访问 Storage。"""
        key = (self._current_database(), table)
        schema = self._schema_cache.get(key)
        if schema is None:
            schema = self._describe_table(table)
            self._schema_cache[key] = schema
        return schema
