"""缓存层 BufferPool（PRD §7；D09/D10/D16/D17/D18）。

职责：管理页帧。pager / engine 的一切页 I/O 都经过本层，本层是
页读写唯一入口（读盘、写盘都由这里做）。

不变量：
- 容量在构造时固定，默认 DEFAULT_CACHE_CAPACITY = 64（D16）；
- key = (表文件绝对路径, page_no)，跨库同名表天然隔离（D09）；
- 帧 = 4 KB bytearray + clean/dirty + pin 计数（本文件 Frame）；
- pin > 0 的帧不可淘汰；淘汰只发生在 pin == 0 的帧上（D17）；
- 淘汰脏帧前必须先写回文件；clean 帧可直接丢弃；
- drop_table / drop_database 用 discard（不写回，D11）；
- 统计：hits / misses / evictions / dirty_writes 只增不减（D18）。

实现阶段：M3 已完成（M1/M2 过渡期直读文件；现在一切页 I/O 走本层）。

追踪：有可选 sink 时，每次页访问都提交操作前后统计，UI 由差值
判断本次 hit/miss、淘汰与脏页写回；sink 失败不得改变缓存语义。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from contracts.errors import E_STORAGE, SqlError
from storage.constants import DEFAULT_CACHE_CAPACITY, PAGE_SIZE
from storage.trace_hooks import StorageTracePayload, StorageTraceSink, trace_storage_operation


@dataclass
class Frame:
    """一个页帧 = 内存里的一个“车位”（D17）。

    data  ：该页 4 KB 内容的副本；engine 改这里后标脏；
    dirty ：内存与磁盘是否不一致（标脏 = 置 True）；
    pin   ：当前正握着本帧的操作数；>0 时 LRU 不得淘汰。
    """

    data: bytearray = field(default_factory=lambda: bytearray(PAGE_SIZE))
    dirty: bool = False
    pin: int = 0


class BufferPool:
    """页缓存：LRU + pin/dirty + 写回（M3 实现）。

    计划内部结构：OrderedDict[key, Frame]（D10：只做 LRU，FIFO 预留策略位）。
    """

    def __init__(
        self,
        capacity: int = DEFAULT_CACHE_CAPACITY,
        trace_sink: StorageTraceSink | None = None,
    ) -> None:
        """初始化容量、空帧表与统计（M0 只立状态，页操作 M3 实现）。

        ``capacity`` 是 B 的固定容量。``trace_sink`` 是可选的依赖倒置
        回调；B 只向它提交普通记录，不导入或构造任何 UI 类型。
        未提供时所有装饰器走快速直通路径。

        Args:
            capacity: 最多同时驻留的页帧数。
            trace_sink: 可选同步追踪回调，通常由根装配层注入。
        """
        if capacity <= 0:
            raise ValueError("cache capacity must be positive")
        self.capacity = capacity
        self._frames: OrderedDict[tuple[Path, int], Frame] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._dirty_writes = 0
        self._trace_sink = trace_sink

    def _trace_stats_snapshot(self) -> dict[str, int | float]:
        """返回不经 ``stats`` 属性的内部统计快照。

        追踪装饰器在每个操作前后调用本方法，用差值展示本次
        调用导致的命中、缺页、淘汰和脏页写回。直接读字段可避免
        统计快照自身再产生递归追踪。
        """

        accesses = self._hits + self._misses
        return {
            "capacity": self.capacity,
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
            "dirty_writes": self._dirty_writes,
            "hit_rate": (self._hits / accesses) if accesses else 0.0,
            "resident_frames": len(self._frames),
        }

    def _emit_trace(self, payload: StorageTracePayload) -> None:
        """向可选观察者提交记录，并隔离观察者的任何异常。

        追踪属于诊断能力，不得因回调编程错误导致正常的页读写
        失败或事务语义改变。因此本方法同步调用 sink，但会吞掉
        sink 自身抛出的异常。
        """

        sink = self._trace_sink
        if sink is None:
            return
        try:
            sink(payload)
        except Exception:
            return

    # ---- 内部：key 规范化与磁盘 I/O ----

    @staticmethod
    def _key(file_path: Path, page_no: int) -> tuple[Path, int]:
        """缓存 key = (绝对路径, 页号)；绝对化保证同一文件只有一个身份（D09）。"""
        return (Path(file_path).absolute(), page_no)

    @trace_storage_operation("cache", "disk_read")
    def _read_disk(self, file_path: Path, page_no: int) -> bytearray:
        """按页偏移从磁盘读一整页；缺失/短读 → E_STORAGE。"""
        offset = page_no * PAGE_SIZE
        try:
            with open(file_path, "rb") as fh:
                fh.seek(offset)
                data = fh.read(PAGE_SIZE)
        except OSError as exc:
            raise SqlError(E_STORAGE, f"cannot read page {page_no}: {file_path}") from exc
        if len(data) != PAGE_SIZE:
            raise SqlError(
                E_STORAGE,
                f"corrupt table file {file_path}: short read on page {page_no}",
            )
        return bytearray(data)

    @trace_storage_operation("cache", "disk_write")
    def _write_disk(
        self, file_path: Path, page_no: int, data: bytes | bytearray
    ) -> None:
        """按页偏移把一整页写回磁盘；写失败/短写 → E_STORAGE。"""
        offset = page_no * PAGE_SIZE
        try:
            with open(file_path, "r+b") as fh:
                fh.seek(offset)
                written = fh.write(data)
        except OSError as exc:
            raise SqlError(
                E_STORAGE, f"cannot write page {page_no}: {file_path}"
            ) from exc
        if written != PAGE_SIZE:
            raise SqlError(
                E_STORAGE,
                f"short write on page {page_no}: {file_path}",
            )

    @trace_storage_operation("cache", "evict_lru")
    def _evict_lru(self) -> None:
        """容量已满时按 LRU 淘汰一个 pin==0 的帧；脏帧先写回（D11/D17）。"""
        victim_key = None
        for key, frame in self._frames.items():
            if frame.pin == 0:
                victim_key = key
                break
        if victim_key is None:
            raise SqlError(
                E_STORAGE,
                f"cache capacity {self.capacity} exhausted and all frames pinned",
            )
        victim = self._frames[victim_key]
        if victim.dirty:
            self._write_disk(victim_key[0], victim_key[1], victim.data)
            self._dirty_writes += 1
        del self._frames[victim_key]
        self._evictions += 1

    # ---- 帧操作（D17）----

    @trace_storage_operation("cache", "get_page")
    def get_page(self, file_path: Path, page_no: int) -> bytearray:
        """取页：命中直接返回；未命中读盘后返回。返回前已 pin（D17）。

        调用方必须成对调用 unpin_page；不允许跨公开方法持有。
        """
        key = self._key(file_path, page_no)
        frame = self._frames.get(key)
        if frame is None:
            self._misses += 1
            frame = Frame(data=self._read_disk(file_path, page_no))
            while len(self._frames) >= self.capacity:
                self._evict_lru()
            self._frames[key] = frame
        else:
            self._hits += 1
            self._frames.move_to_end(key)
        frame.pin += 1
        return frame.data

    @trace_storage_operation("cache", "unpin_page")
    def unpin_page(self, file_path: Path, page_no: int) -> None:
        """放页：pin -= 1；归零后该帧恢复可淘汰状态（D17）。"""
        key = self._key(file_path, page_no)
        frame = self._frames.get(key)
        if frame is None or frame.pin <= 0:
            raise SqlError(
                E_STORAGE, f"unpin without pin: page {page_no} in {file_path}"
            )
        frame.pin -= 1

    @trace_storage_operation("cache", "mark_dirty")
    def mark_dirty(self, file_path: Path, page_no: int) -> None:
        """标脏：记录本帧与磁盘不一致（flush 时写回）。"""
        key = self._key(file_path, page_no)
        frame = self._frames.get(key)
        if frame is None:
            raise SqlError(E_STORAGE, f"mark_dirty on missing frame: page {page_no}")
        frame.dirty = True
        self._frames.move_to_end(key)

    @trace_storage_operation("cache", "flush")
    def flush(self, file_path: Path | None = None) -> None:
        """把指定表文件（或全部）的脏帧写回磁盘；写回后置 clean（D11）。"""
        target_path = self._key(file_path, 0)[0] if file_path is not None else None
        for key, frame in list(self._frames.items()):
            if target_path is not None and key[0] != target_path:
                continue
            if frame.dirty:
                self._write_disk(key[0], key[1], frame.data)
                frame.dirty = False
                self._dirty_writes += 1

    @trace_storage_operation("cache", "discard")
    def discard(self, file_path: Path) -> None:
        """丢弃相关帧、不写回——删表/删库前调用（D11）。

        file_path 为表文件时精确丢弃该表全部帧；为目录时丢弃其下全部帧。
        """
        target = Path(file_path).absolute()
        drop_keys = [
            key
            for key in self._frames
            if key[0] == target
            or (target.is_dir() and key[0].is_relative_to(target))
        ]
        for key in drop_keys:
            del self._frames[key]

    @trace_storage_operation("cache", "reset_stats")
    def reset_stats(self) -> None:
        """清零统计（仅供测试与报告，D18）。"""
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._dirty_writes = 0

    @property
    def stats(self) -> dict[str, int | float]:
        """只读统计快照：capacity/hits/misses/evictions/dirty_writes/hit_rate。"""
        snapshot = self._trace_stats_snapshot()
        snapshot.pop("resident_frames")
        return snapshot
