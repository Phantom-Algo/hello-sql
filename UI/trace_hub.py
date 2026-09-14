"""C 模块使用的查询编号中心与最近追踪缓存。

``TraceHub`` 是后续可视化链路的汇总入口。C 在开始处理 SQL 时先调用
``reserve``，得到不会重复的 ``query_number`` 和 ``trace_id``；随后把执行中或
执行结束后的不可变 ``QueryTrace`` 交给 ``publish``。本模块只保存追踪快照，
不调用 Parser、Storage 或 Executor，因此查看历史不会重新执行 SQL。

缓存采用固定容量、按查询开始顺序排列的内存结构，默认保留最近 20 条。终端
执行线程可以发布新记录，本地查看窗口可以同时读取记录，所以全部共享状态都
由可重入锁保护。编号在 ``clear`` 后也不会回退，避免同一进程内出现重复 ID。
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from UI.trace_models import QueryTrace


def _require_positive_integer(value: int, field_name: str) -> None:
    """验证容量等配置值是大于零的整数。

    Python 的 ``bool`` 是 ``int`` 的子类，但布尔值不能作为缓存容量或查询编号，
    因此需要显式排除，防止 ``True`` 被误解释为容量 1。
    """

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")


def _require_non_negative_integer(value: int, field_name: str) -> None:
    """验证起始编号和查询数量限制是非负整数。

    起始编号允许为零，第一次预约将得到编号 1；历史读取限制允许为零，用于
    明确请求空结果。其余类型和负数会破坏编号或切片语义，因此统一拒绝。
    """

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


def _require_identifier(value: str, field_name: str) -> None:
    """验证 TraceHub 使用的 ID 与前缀包含可展示文本。

    查看器使用这些字符串定位历史记录，空白 ID 无法作为可靠键；该校验不限制
    字符集，从而允许团队以后使用中文前缀或其他可读命名方案。
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


@dataclass(frozen=True, slots=True)
class TraceReservation:
    """一次查询开始时由 ``TraceHub`` 分配的稳定身份。

    C 应立即保存该对象，并使用其中两个字段构造后续 ``QueryTrace``。编号在
    ``reserve`` 时确定而不是完成时确定，因此并发查询即使以不同顺序结束，
    界面仍能按照真实开始顺序排列。
    """

    query_number: int
    trace_id: str

    def __post_init__(self) -> None:
        """保证预约身份包含正查询编号和非空追踪 ID。"""

        _require_positive_integer(self.query_number, "TraceReservation.query_number")
        _require_identifier(self.trace_id, "TraceReservation.trace_id")


class TraceHub:
    """线程安全的查询编号分配器和固定容量追踪缓存。

    Hub 只管理已经结构化的 ``QueryTrace``，不会理解各阶段快照内容。新记录按
    查询编号进入有序缓存；同一追踪 ID 可以从 RUNNING 更新为 SUCCESS/FAILED，
    但 SQL、数据库和脚本位置等身份字段不能在更新过程中改变。
    """

    def __init__(
        self,
        capacity: int = 20,
        *,
        id_prefix: str = "trace",
        start_number: int = 0,
    ) -> None:
        """创建一个进程内 TraceHub。

        ``capacity`` 控制最终快照的最大保留数量；``id_prefix`` 决定类似
        ``trace-000001`` 的可读 ID；``start_number`` 主要用于测试或未来从外部
        恢复编号。构造函数不会创建磁盘文件，也不会启动后台线程。
        """

        _require_positive_integer(capacity, "TraceHub.capacity")
        _require_identifier(id_prefix, "TraceHub.id_prefix")
        _require_non_negative_integer(start_number, "TraceHub.start_number")
        self._capacity = capacity
        self._id_prefix = id_prefix.strip()
        self._last_query_number = start_number
        self._records: dict[str, QueryTrace] = {}
        self._reservations: dict[str, TraceReservation] = {}
        self._lock = RLock()

    @property
    def capacity(self) -> int:
        """返回缓存最多能够保留的查询追踪数量。"""

        return self._capacity

    @property
    def last_query_number(self) -> int:
        """返回已经分配的最大查询编号。

        读取操作加锁是为了与其他线程中的 ``reserve`` 保持一致；该值包含仍在
        执行但尚未发布的预约，也不会因为旧记录被淘汰或清空而减小。
        """

        with self._lock:
            return self._last_query_number

    @property
    def pending_count(self) -> int:
        """返回已经预约但尚未首次发布的查询数量。"""

        with self._lock:
            return len(self._reservations)

    def reserve(self) -> TraceReservation:
        """按照查询开始顺序分配下一个编号和追踪 ID。

        该方法是编号的唯一生产入口，并在一把锁内完成递增、格式化和预约登记，
        因此多个执行线程不会得到重复编号。返回对象本身不可变。
        """

        with self._lock:
            self._last_query_number += 1
            query_number = self._last_query_number
            trace_id = f"{self._id_prefix}-{query_number:06d}"
            reservation = TraceReservation(query_number, trace_id)
            self._reservations[trace_id] = reservation
            return reservation

    def publish(self, trace: QueryTrace) -> QueryTrace:
        """首次发布或更新一条已经预约的查询追踪。

        首次发布必须匹配 ``reserve`` 返回的编号；后续可以用同一 ID 更新状态和
        阶段内容，例如从 RUNNING 更新为 SUCCESS。更新不会改变原有排序。插入
        新记录后若超过容量，会自动淘汰最早开始的记录。
        """

        if not isinstance(trace, QueryTrace):
            raise TypeError("TraceHub.publish requires a QueryTrace")
        with self._lock:
            existing = self._records.get(trace.trace_id)
            reservation = self._reservations.get(trace.trace_id)
            if existing is None and reservation is None:
                raise ValueError(
                    "trace_id was not reserved by this TraceHub; call reserve() first"
                )
            if existing is not None:
                expected_number = existing.query_number
            else:
                # 前面的守卫已排除 existing 与 reservation 同时为空；显式断言
                # 既记录该不变式，也让静态类型检查器理解此分支的数据形状。
                assert reservation is not None
                expected_number = reservation.query_number
            if trace.query_number != expected_number:
                raise ValueError("QueryTrace.query_number does not match its reservation")
            if existing is not None and not self._same_query_identity(existing, trace):
                raise ValueError("an existing trace update cannot change query identity")

            self._records[trace.trace_id] = trace
            self._reservations.pop(trace.trace_id, None)
            if existing is None and len(self._records) > self._capacity:
                oldest_id = min(
                    self._records,
                    key=lambda trace_id: self._records[trace_id].query_number,
                )
                del self._records[oldest_id]
            return trace

    def abandon(self, reservation: TraceReservation) -> bool:
        """取消一个尚未首次发布的预约。

        正常 SQL 即使失败也应该发布 FAILED 追踪；本方法只处理终端在追踪创建前
        被中断或出现装配异常等极端情况。返回 ``True`` 表示确实移除了待处理
        预约；已发布或不存在的预约返回 ``False``，查询编号不会被重复使用。
        """

        if not isinstance(reservation, TraceReservation):
            raise TypeError("TraceHub.abandon requires a TraceReservation")
        with self._lock:
            current = self._reservations.get(reservation.trace_id)
            if current != reservation:
                return False
            del self._reservations[reservation.trace_id]
            return True

    def get(self, trace_id: str) -> QueryTrace | None:
        """按稳定追踪 ID 返回记录，不存在时返回 ``None``。

        返回的 ``QueryTrace`` 是不可变对象，可以安全交给本地 HTTP 查看线程，
        不需要在锁外再复制整个 AST、计划或存储指标。
        """

        _require_identifier(trace_id, "trace_id")
        with self._lock:
            return self._records.get(trace_id)

    def latest(self) -> QueryTrace | None:
        """返回最近开始且已经发布的查询追踪。

        若缓存为空则返回 ``None``；仍处于预约状态但从未发布的查询不属于可查看
        历史，因此不会被该方法返回。
        """

        with self._lock:
            if not self._records:
                return None
            return max(self._records.values(), key=lambda trace: trace.query_number)

    def recent(self, limit: int | None = None) -> tuple[QueryTrace, ...]:
        """按从新到旧顺序返回最近追踪的不可变元组。

        ``limit`` 为 ``None`` 时返回缓存中的全部记录，为零时返回空元组。该方法
        在锁内完成快照复制，浏览器遍历结果时不会受到随后发布操作的影响。
        """

        if limit is not None:
            _require_non_negative_integer(limit, "limit")
        with self._lock:
            records = tuple(
                sorted(
                    self._records.values(),
                    key=lambda trace: trace.query_number,
                    reverse=True,
                )
            )
        return records if limit is None else records[:limit]

    def clear(self) -> int:
        """清空已发布记录和未完成预约，并返回移除项目总数。

        清空通常发生在 REPL 关闭或测试清理阶段。最大查询编号故意保留，保证
        同一进程之后创建的新查询仍使用更大的 ID，不会与旧浏览器页面混淆。
        """

        with self._lock:
            removed = len(self._records) + len(self._reservations)
            self._records.clear()
            self._reservations.clear()
            return removed

    def __len__(self) -> int:
        """返回当前缓存中已经发布的记录数量，不包含待发布预约。"""

        with self._lock:
            return len(self._records)

    def __contains__(self, trace_id: object) -> bool:
        """支持使用 ``trace_id in hub`` 判断一条记录是否已经发布。"""

        if not isinstance(trace_id, str):
            return False
        with self._lock:
            return trace_id in self._records

    @staticmethod
    def _same_query_identity(existing: QueryTrace, replacement: QueryTrace) -> bool:
        """判断一次状态更新是否仍表示同一条脚本语句。

        状态、阶段、耗时和结果允许变化；编号、SQL、数据库、脚本下标、脚本总数
        以及源码范围属于查询身份，发布后不得替换。
        """

        return (
            existing.trace_id == replacement.trace_id
            and existing.query_number == replacement.query_number
            and existing.sql == replacement.sql
            and existing.database == replacement.database
            and existing.statement_index == replacement.statement_index
            and existing.statement_count == replacement.statement_count
            and existing.source_span == replacement.source_span
        )
