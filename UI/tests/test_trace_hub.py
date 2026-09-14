"""验证 TraceHub 的查询编号、发布更新、线程安全和固定容量缓存。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from UI import QueryTrace, TraceHub, TraceReservation, TraceStatus


def _trace(
    reservation: TraceReservation,
    *,
    status: TraceStatus = TraceStatus.SUCCESS,
    sql: str = "SELECT 1;",
    database: str = "main",
) -> QueryTrace:
    """根据 Hub 预约构造一条身份一致的最小查询追踪。"""

    error_code = "E_TEST" if status is TraceStatus.FAILED else None
    error_message = "测试失败" if status is TraceStatus.FAILED else None
    return QueryTrace(
        trace_id=reservation.trace_id,
        query_number=reservation.query_number,
        sql=sql,
        database=database,
        status=status,
        stages=(),
        error_code=error_code,
        error_message=error_message,
    )


def test_reserve_generates_monotonic_human_readable_ids() -> None:
    """连续预约应按照查询开始顺序生成不重复且便于展示的编号。"""

    hub = TraceHub(start_number=40)
    first = hub.reserve()
    second = hub.reserve()

    assert first == TraceReservation(query_number=41, trace_id="trace-000041")
    assert second == TraceReservation(query_number=42, trace_id="trace-000042")
    assert hub.last_query_number == 42
    assert hub.pending_count == 2


def test_publish_makes_trace_available_to_latest_and_get() -> None:
    """首次发布应结束预约，并让 ID 查询和最近查询返回同一不可变对象。"""

    hub = TraceHub()
    reservation = hub.reserve()
    trace = _trace(reservation)

    assert hub.publish(trace) is trace
    assert hub.latest() is trace
    assert hub.get(reservation.trace_id) is trace
    assert reservation.trace_id in hub
    assert len(hub) == 1
    assert hub.pending_count == 0


def test_capacity_evicts_oldest_published_trace() -> None:
    """缓存超过容量时应淘汰最早查询，并保留从新到旧的最近记录。"""

    hub = TraceHub(capacity=2)
    reservations = [hub.reserve() for _ in range(3)]
    traces = [
        _trace(reservation, sql=f"SELECT {index};")
        for index, reservation in enumerate(reservations)
    ]
    for trace in traces:
        hub.publish(trace)

    assert hub.get(traces[0].trace_id) is None
    assert hub.recent() == (traces[2], traces[1])
    assert len(hub) == 2


def test_republish_updates_state_without_changing_query_order() -> None:
    """运行中快照更新为最终状态时，不应被错误移动到更新更晚的位置。"""

    hub = TraceHub(capacity=3)
    first_reservation = hub.reserve()
    second_reservation = hub.reserve()
    running = _trace(first_reservation, status=TraceStatus.RUNNING)
    second = _trace(second_reservation, sql="SELECT 2;")
    hub.publish(running)
    hub.publish(second)

    completed = _trace(first_reservation, status=TraceStatus.SUCCESS)
    hub.publish(completed)

    assert hub.get(first_reservation.trace_id) is completed
    assert hub.latest() is second
    assert hub.recent() == (second, completed)


def test_out_of_order_completion_still_uses_query_start_order() -> None:
    """后开始的查询先完成时，历史顺序仍必须由预约编号决定。"""

    hub = TraceHub(capacity=2)
    first_reservation = hub.reserve()
    second_reservation = hub.reserve()
    first = _trace(first_reservation)
    second = _trace(second_reservation, sql="SELECT 2;")

    hub.publish(second)
    hub.publish(first)

    assert hub.latest() is second
    assert hub.recent() == (second, first)


def test_capacity_evicts_oldest_query_even_if_it_finishes_last() -> None:
    """迟完成的旧查询不得挤掉编号更新、实际开始更晚的缓存记录。"""

    hub = TraceHub(capacity=1)
    first_reservation = hub.reserve()
    second_reservation = hub.reserve()
    first = _trace(first_reservation)
    second = _trace(second_reservation, sql="SELECT 2;")

    hub.publish(second)
    hub.publish(first)

    assert hub.get(first.trace_id) is None
    assert hub.latest() is second


def test_publish_rejects_unreserved_or_mismatched_identity() -> None:
    """Hub 必须拒绝外部伪造 ID 以及预约编号不一致的追踪。"""

    hub = TraceHub()
    foreign = TraceReservation(99, "trace-000099")
    with pytest.raises(ValueError, match="not reserved"):
        hub.publish(_trace(foreign))

    reservation = hub.reserve()
    mismatched = QueryTrace(
        trace_id=reservation.trace_id,
        query_number=reservation.query_number + 1,
        sql="SELECT 1;",
        database="main",
        status=TraceStatus.SUCCESS,
        stages=(),
    )
    with pytest.raises(ValueError, match="does not match"):
        hub.publish(mismatched)


def test_republish_rejects_changes_to_query_identity() -> None:
    """同一 ID 的状态更新不能偷换 SQL、数据库或脚本位置。"""

    hub = TraceHub()
    reservation = hub.reserve()
    hub.publish(_trace(reservation, status=TraceStatus.RUNNING))

    with pytest.raises(ValueError, match="cannot change query identity"):
        hub.publish(_trace(reservation, sql="SELECT 2;"))


def test_abandon_removes_only_matching_unpublished_reservation() -> None:
    """取消预约应清理待处理状态，但不能删除已经发布的历史。"""

    hub = TraceHub()
    abandoned = hub.reserve()
    assert hub.abandon(abandoned)
    assert not hub.abandon(abandoned)

    published = hub.reserve()
    hub.publish(_trace(published))
    assert not hub.abandon(published)
    assert hub.get(published.trace_id) is not None


def test_recent_limit_and_clear_preserve_monotonic_numbering() -> None:
    """最近记录限制和清空操作不得导致查询编号回退或重复。"""

    hub = TraceHub()
    first = hub.reserve()
    second = hub.reserve()
    hub.publish(_trace(first))
    hub.publish(_trace(second, sql="SELECT 2;"))

    assert hub.recent(0) == ()
    assert hub.recent(1) == (hub.get(second.trace_id),)
    assert hub.clear() == 2
    assert hub.latest() is None
    assert len(hub) == 0
    assert hub.reserve().query_number == 3


def test_concurrent_reservations_are_unique_and_gap_free() -> None:
    """多个执行线程同时开始查询时仍应得到连续且不重复的编号。"""

    hub = TraceHub()
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = tuple(executor.submit(hub.reserve) for _ in range(100))
        reservations = tuple(future.result() for future in futures)

    numbers = sorted(reservation.query_number for reservation in reservations)
    ids = {reservation.trace_id for reservation in reservations}
    assert numbers == list(range(1, 101))
    assert len(ids) == 100
    assert hub.pending_count == 100


@pytest.mark.parametrize("capacity", [0, -1, True])
def test_invalid_capacity_is_rejected(capacity: int) -> None:
    """零、负数和布尔值不能作为最近记录缓存容量。"""

    with pytest.raises(ValueError, match="positive integer"):
        TraceHub(capacity=capacity)
