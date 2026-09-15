"""索引文件层（V3 D27/D28/D29；M2：节点页原语 + B+ 树）。

职责：把"一个索引 = 一个 .idx 文件"落地。

- 页 0：文件头（magic `HSIX` / version / 键类型 / root / height / free_head）；
- 节点页：叶页与内节点页，采用与数据页相同的 slotted 写法
  （页头 16 B，条目区自页头向后、槽目录自页尾向前）；
- B+ 树：等值、范围、插入与分裂、删除与惰性回收（D31–D35）。

不变量：

- 索引文件长度恒为 PAGE_SIZE 的整数倍，页 0 永不释放（D28）；
- 空树也有合法根：页 0 的 root 指向一个 `entry_count == 0` 的空叶页；
- 根页**永远存在**：根叶被删空时不回收，只有"内节点根 + 0 条目"才折叠；
- 叶页 offset 12 存前驱叶、offset 8 存后继叶，删除空叶时 O(1) 摘链；
- 键按 `(键, rid)` 排序；键字节沿用记录的**单值编码**（valuecodec）；
- 比较时解码成 Python 值（D31）：`-0.0 == 0.0`、TEXT 前缀序、INT 负数序
  都由 Python 比较天然给出，不做保序字节编码；
- 查找下降用"最左规则"、插入下降用"最右规则"，两者不同是刻意设计，
  重复键跨叶时靠它避免漏行（见 `leaf_for_search` / `leaf_for_insert`）；
- 任何结构损坏、键类型不符、单条目过大 → E_STORAGE。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct
import os
from typing import Sequence

from contracts.ast import ColumnDef, SqlType, Value
from contracts.errors import E_STORAGE, SqlError
from storage.cache import BufferPool
from storage.constants import (
    FREE_LIST_END,
    INDEX_CHILD_PTR_SIZE,
    INDEX_FILE_VERSION,
    INDEX_KEY_TYPE_BOOLEAN,
    INDEX_KEY_TYPE_INT,
    INDEX_KEY_TYPE_REAL,
    INDEX_KEY_TYPE_TEXT,
    INDEX_MAGIC,
    INDEX_NODE_COUNT_OFFSET,
    INDEX_NODE_FIRST_CHILD_OFFSET,
    INDEX_NODE_FREE_PTR_OFFSET,
    INDEX_NODE_HEADER_SIZE,
    INDEX_NODE_NEXT_LEAF_OFFSET,
    INDEX_NODE_PREV_LEAF_OFFSET,
    INDEX_NODE_TYPE_OFFSET,
    INDEX_PAGE0_FREE_HEAD_OFFSET,
    INDEX_PAGE0_HEADER_SIZE,
    INDEX_PAGE0_HEIGHT_OFFSET,
    INDEX_PAGE0_KEY_TYPE_OFFSET,
    INDEX_PAGE0_MAGIC_OFFSET,
    INDEX_PAGE0_ROOT_OFFSET,
    INDEX_LEAF_PREFIX_SIZE,
    INDEX_RID_SIZE,
    INDEX_SLOT_SIZE,
    INTERIOR_NODE,
    LEAF_NODE,
    MAX_INDEX_KEY_BYTES,
    PAGE_SIZE,
)
from storage.pager import INDEX_FILE_KIND, alloc_page, free_page, page_count, read_page, write_page
from storage.trace_hooks import trace_storage_operation
from storage.valuecodec import decode_value, encode_value, normalize_value


# 页 0 头部 20 B：magic(4s) + version(H) + key_type(H) + root(I) + height(I) + free_head(I)
_PAGE0_STRUCT = struct.Struct("<4sHHIII")
_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")

# 初始空树的根页号与树高（D28：空树 = 一个空叶页，height = 1）。
_EMPTY_ROOT_PAGE = 1
_EMPTY_TREE_HEIGHT = 1

_SQLTYPE_TO_KEY_TAG = {
    SqlType.INT: INDEX_KEY_TYPE_INT,
    SqlType.REAL: INDEX_KEY_TYPE_REAL,
    SqlType.TEXT: INDEX_KEY_TYPE_TEXT,
    SqlType.BOOLEAN: INDEX_KEY_TYPE_BOOLEAN,
}
_KEY_TAG_TO_SQLTYPE = {tag: kind for kind, tag in _SQLTYPE_TO_KEY_TAG.items()}


@dataclass(frozen=True)
class IndexFileHeader:
    """索引文件页 0 的对外可读字段。"""

    root_page: int
    height: int
    free_head: int
    key_type_tag: int


@dataclass(frozen=True)
class IndexTreeNode:
    """一个节点页的只读视图（供树算法与测试使用）。"""

    page_no: int
    is_leaf: bool
    entries: tuple[bytes, ...]
    next_leaf: int = 0
    prev_leaf: int = 0
    first_child: int = 0


# ---------- 页 0 ----------


def create_index_file(file_path: Path, key_type: SqlType) -> None:
    """建索引文件：页 0 头（含键类型）+ 一个空叶根页。

    与 create_table_file 一致：文件已存在时直接覆盖重建；父目录
    （indexes/）不存在时自动创建。
    """
    try:
        tag = _SQLTYPE_TO_KEY_TAG[key_type]
    except KeyError:
        raise SqlError(
            E_STORAGE, f"unsupported index key type: {key_type!r}"
        ) from None

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SqlError(
            E_STORAGE, f"cannot create index directory: {file_path.parent}"
        ) from exc

    page0 = bytearray(PAGE_SIZE)
    _PAGE0_STRUCT.pack_into(
        page0,
        0,
        INDEX_MAGIC,
        INDEX_FILE_VERSION,
        tag,
        _EMPTY_ROOT_PAGE,
        _EMPTY_TREE_HEIGHT,
        FREE_LIST_END,
    )
    leaf = new_leaf_page()
    try:
        with open(file_path, "wb") as fh:
            written = fh.write(bytes(page0)) + fh.write(bytes(leaf))
    except OSError as exc:
        raise SqlError(E_STORAGE, f"cannot create index file: {file_path}") from exc
    if written != 2 * PAGE_SIZE:
        raise SqlError(E_STORAGE, f"short write creating index file: {file_path}")


@trace_storage_operation("index", "open_index_file")
def open_index_file(
    pool: BufferPool,
    file_path: Path,
    *,
    expected_key_type: SqlType | None = None,
) -> IndexFileHeader:
    """打开索引文件并校验页 0，返回文件头。

    magic / version / 文件长度由 pager 按 INDEX_FILE_KIND 校验；这里再校验
    键类型标记、root 范围、root 节点类型与 free_head 范围。
    """
    page0 = read_page(pool, file_path, 0, kind=INDEX_FILE_KIND)
    total_pages = page_count(pool, file_path, kind=INDEX_FILE_KIND)

    tag = _U16.unpack_from(page0, INDEX_PAGE0_KEY_TYPE_OFFSET)[0]
    if tag not in _KEY_TAG_TO_SQLTYPE:
        raise SqlError(
            E_STORAGE, f"corrupt index file {file_path}: unknown key type {tag}"
        )
    if expected_key_type is not None and _SQLTYPE_TO_KEY_TAG[expected_key_type] != tag:
        raise SqlError(
            E_STORAGE,
            f"index file {file_path} key type does not match column type "
            f"{expected_key_type.value}",
        )

    root_page = _U32.unpack_from(page0, INDEX_PAGE0_ROOT_OFFSET)[0]
    height = _U32.unpack_from(page0, INDEX_PAGE0_HEIGHT_OFFSET)[0]
    free_head = _U32.unpack_from(page0, INDEX_PAGE0_FREE_HEAD_OFFSET)[0]

    if height < 1:
        raise SqlError(
            E_STORAGE, f"corrupt index file {file_path}: height {height} is invalid"
        )
    if not 0 < root_page < total_pages:
        raise SqlError(
            E_STORAGE,
            f"corrupt index file {file_path}: root {root_page} out of range "
            f"({total_pages} pages)",
        )
    if free_head != FREE_LIST_END and not 0 < free_head < total_pages:
        raise SqlError(
            E_STORAGE,
            f"corrupt index file {file_path}: free_head {free_head} out of range",
        )

    root = read_page(pool, file_path, root_page, kind=INDEX_FILE_KIND)
    kind = node_type(root)
    if kind not in (LEAF_NODE, INTERIOR_NODE):
        raise SqlError(
            E_STORAGE,
            f"corrupt index file {file_path}: unknown node type {kind} "
            f"at root page {root_page}",
        )
    if height == 1 and kind != LEAF_NODE:
        raise SqlError(
            E_STORAGE,
            f"corrupt index file {file_path}: height 1 requires a leaf root",
        )

    return IndexFileHeader(
        root_page=root_page,
        height=height,
        free_head=free_head,
        key_type_tag=tag,
    )


def key_type_of(header: IndexFileHeader) -> SqlType:
    """把文件头里的键类型标记翻译回 SqlType。"""
    return _KEY_TAG_TO_SQLTYPE[header.key_type_tag]


def index_file_version(file_path: Path | str) -> int:
    """用普通文件读取出页 0 的版本号（D49a 迁移判断专用）。

    pager 在每次页访问时都会强校验版本，因此"这是不是已知旧版本"必须先
    绕开它读这 6 个字节。magic 不符 → E_STORAGE（这不是索引文件）。
    """

    path = Path(file_path)
    try:
        with open(path, "rb") as fh:
            header = fh.read(INDEX_PAGE0_HEADER_SIZE)
    except OSError as exc:
        raise SqlError(E_STORAGE, f"cannot read index file: {path}") from exc
    if len(header) < INDEX_PAGE0_HEADER_SIZE or header[:4] != INDEX_MAGIC:
        raise SqlError(E_STORAGE, f"not a hello-sql index file: {path}")
    return int.from_bytes(header[4:6], "little")


# ---------- 节点页原语 ----------


def new_leaf_page() -> bytearray:
    """构造一个空叶页（条目区连续空闲，前后指针均为 0）。"""
    page = bytearray(PAGE_SIZE)
    page[INDEX_NODE_TYPE_OFFSET] = LEAF_NODE
    _U16.pack_into(page, INDEX_NODE_COUNT_OFFSET, 0)
    _U32.pack_into(page, INDEX_NODE_FREE_PTR_OFFSET, INDEX_NODE_HEADER_SIZE)
    _U32.pack_into(page, INDEX_NODE_NEXT_LEAF_OFFSET, 0)
    _U32.pack_into(page, INDEX_NODE_PREV_LEAF_OFFSET, 0)
    return page


def new_interior_page(first_child: int = 0) -> bytearray:
    """构造一个空内节点页（只有最左子页号，没有分隔键）。"""
    page = bytearray(PAGE_SIZE)
    page[INDEX_NODE_TYPE_OFFSET] = INTERIOR_NODE
    _U16.pack_into(page, INDEX_NODE_COUNT_OFFSET, 0)
    _U32.pack_into(page, INDEX_NODE_FREE_PTR_OFFSET, INDEX_NODE_HEADER_SIZE)
    _U32.pack_into(page, INDEX_NODE_FIRST_CHILD_OFFSET, first_child)
    return page


def node_type(page: bytes) -> int:
    return page[INDEX_NODE_TYPE_OFFSET]


def entry_count(page: bytes) -> int:
    return _U16.unpack_from(page, INDEX_NODE_COUNT_OFFSET)[0]


def free_ptr(page: bytes) -> int:
    return _U32.unpack_from(page, INDEX_NODE_FREE_PTR_OFFSET)[0]


def leaf_next(page: bytes) -> int:
    return _U32.unpack_from(page, INDEX_NODE_NEXT_LEAF_OFFSET)[0]


def leaf_prev(page: bytes) -> int:
    return _U32.unpack_from(page, INDEX_NODE_PREV_LEAF_OFFSET)[0]


def first_child(page: bytes) -> int:
    return _U32.unpack_from(page, INDEX_NODE_FIRST_CHILD_OFFSET)[0]


def free_space(page: bytes) -> int:
    """当前页还能放多少字节（条目负载 + 槽）——即"从 free_ptr 到槽目录之间"。"""
    return (PAGE_SIZE - INDEX_SLOT_SIZE * entry_count(page)) - free_ptr(page)


def slot(page: bytes, index: int) -> tuple[int, int]:
    """返回第 index 条槽的 (偏移, 长度)。槽 0 贴着页尾。"""
    base = PAGE_SIZE - INDEX_SLOT_SIZE * (index + 1)
    return _U32.unpack_from(page, base)[0], _U32.unpack_from(page, base + 4)[0]


def entry_payloads(page: bytes) -> tuple[bytes, ...]:
    """按槽序返回全部条目负载（叶 = rid + 键；内节点 = 子页号 + 键）。"""
    result = []
    for index in range(entry_count(page)):
        offset, length = slot(page, index)
        result.append(bytes(page[offset : offset + length]))
    return tuple(result)


def validate_node(page: bytes, *, context: str) -> None:
    """校验节点页结构；任何不一致 → E_STORAGE。"""
    kind = node_type(page)
    if kind not in (LEAF_NODE, INTERIOR_NODE):
        raise SqlError(E_STORAGE, f"corrupt index node {context}: bad type {kind}")
    count = entry_count(page)
    # 叶条目定长前缀是 rid + 页号；内节点是子页号 + rid（分隔键要带 rid）。
    pointer = (
        INDEX_LEAF_PREFIX_SIZE
        if kind == LEAF_NODE
        else INDEX_CHILD_PTR_SIZE + INDEX_RID_SIZE
    )
    if INDEX_NODE_HEADER_SIZE + INDEX_SLOT_SIZE * count > PAGE_SIZE:
        raise SqlError(
            E_STORAGE, f"corrupt index node {context}: slot count {count} overflows"
        )
    ptr = free_ptr(page)
    if ptr < INDEX_NODE_HEADER_SIZE or ptr > PAGE_SIZE:
        raise SqlError(
            E_STORAGE, f"corrupt index node {context}: free_ptr {ptr} out of range"
        )
    if free_space(page) < 0:
        raise SqlError(
            E_STORAGE, f"corrupt index node {context}: entries overlap slot directory"
        )
    for index in range(count):
        offset, length = slot(page, index)
        if offset < INDEX_NODE_HEADER_SIZE or length < pointer or offset + length > ptr:
            raise SqlError(
                E_STORAGE, f"corrupt index node {context}: bad slot {index}"
            )


def rebuild_node(
    page: bytearray,
    kind: int,
    entries: Sequence[bytes],
    *,
    next_leaf: int = 0,
    prev_leaf: int = 0,
    first_child: int = 0,
) -> None:
    """按给定条目重建整页（保持"条目区连续 + 槽目录自页尾向前"的不变式）。

    重建式写入与数据页的做法一致：先整页清零，再顺序写条目与槽。
    """
    page[:] = bytes(PAGE_SIZE)
    page[INDEX_NODE_TYPE_OFFSET] = kind
    _U16.pack_into(page, INDEX_NODE_COUNT_OFFSET, len(entries))
    if kind == LEAF_NODE:
        _U32.pack_into(page, INDEX_NODE_NEXT_LEAF_OFFSET, next_leaf)
        _U32.pack_into(page, INDEX_NODE_PREV_LEAF_OFFSET, prev_leaf)
    else:
        _U32.pack_into(page, INDEX_NODE_FIRST_CHILD_OFFSET, first_child)

    cursor = INDEX_NODE_HEADER_SIZE
    for index, payload in enumerate(entries):
        page[cursor : cursor + len(payload)] = payload
        base = PAGE_SIZE - INDEX_SLOT_SIZE * (index + 1)
        _U32.pack_into(page, base, cursor)
        _U32.pack_into(page, base + 4, len(payload))
        cursor += len(payload)
    _U32.pack_into(page, INDEX_NODE_FREE_PTR_OFFSET, cursor)


def node_fits(entries: Sequence[bytes]) -> bool:
    """给定条目集合能否放进一个节点页。"""
    payload = sum(len(entry) for entry in entries)
    return (
        INDEX_NODE_HEADER_SIZE + payload + INDEX_SLOT_SIZE * len(entries)
        <= PAGE_SIZE
    )


def leaf_entry_size(key_bytes: bytes) -> int:
    return INDEX_LEAF_PREFIX_SIZE + len(key_bytes)


def interior_entry_size(key_bytes: bytes) -> int:
    return INDEX_CHILD_PTR_SIZE + len(key_bytes)


# ---------- 键编解码 ----------


def normalize_key(column: ColumnDef, value: Value) -> Value:
    """按列类型归一化键值（与表级规则完全一致）。"""
    return normalize_value(column, value)


def encode_key(column: ColumnDef, value: Value) -> bytes:
    """编码一个已归一化的键值；超过单条目容量 → E_STORAGE（M2 决策 2）。"""
    raw = encode_value(column, value)
    if len(raw) > MAX_INDEX_KEY_BYTES:
        raise SqlError(
            E_STORAGE,
            f"index key too large for column {column.name!r}: "
            f"{len(raw)} bytes (max {MAX_INDEX_KEY_BYTES})",
        )
    return raw


def decode_key(column: ColumnDef, raw: bytes) -> Value:
    """解码键字节；长度不符视为损坏。"""
    value, position = decode_value(column, raw, 0)
    if position != len(raw):
        raise SqlError(E_STORAGE, "corrupt index key: trailing bytes")
    return value


def compare_key_to_value(column: ColumnDef, raw: bytes, value: Value) -> int:
    """把已存键与探针值比较：<0 键更小，0 相等，>0 键更大。"""
    stored = decode_key(column, raw)
    if stored < value:
        return -1
    if stored > value:
        return 1
    return 0


def compare_keys(column: ColumnDef, left: bytes, right: bytes) -> int:
    """比较两个已存键。"""
    a = decode_key(column, left)
    b = decode_key(column, right)
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


# ---------- B+ 树 ----------


_RID = struct.Struct("<Q")


def _leaf_payload(row_id: int, page_no: int, key_bytes: bytes) -> bytes:
    """叶条目负载：u64 rid + u32 行所在页号 + 键编码（D49a）。

    页号让回表 O(1)：拿到条目就能直接读行所在页，不必先建立/查询 rid→页 映射。
    它只是**提示**——读取侧仍会校验该页槽里确实是这个 rid，不符就退回全表定位。
    """
    return _RID.pack(row_id) + _U32.pack(page_no) + key_bytes


def _interior_payload(child_page: int, row_id: int, key_bytes: bytes) -> bytes:
    """内节点条目负载：u32 子页号 + u64 rid + 键编码。

    分隔键带 rid 是刻意的：内节点保存的是"右子树最小条目的完整键"，
    这样 (键, rid) 的全局次序无歧义，删除时能一次下降就命中目标叶，
    重复键跨叶也不会找错叶子。
    """
    return _U32.pack(child_page) + _RID.pack(row_id) + key_bytes


def _payload_bytes(entries: Sequence[bytes]) -> int:
    return sum(len(entry) for entry in entries) + INDEX_SLOT_SIZE * len(entries)


def _split_payloads(entries: Sequence[bytes]) -> tuple[list[bytes], list[bytes]]:
    """按字节预算把条目切成两半：两半都能放下，且负载尽量均衡。"""
    total = len(entries)
    best: tuple[int, int] | None = None
    for split in range(1, total):
        left = list(entries[:split])
        right = list(entries[split:])
        if not node_fits(left) or not node_fits(right):
            continue
        difference = abs(_payload_bytes(left) - _payload_bytes(right))
        if best is None or difference < best[0]:
            best = (difference, split)
    if best is None:
        raise SqlError(E_STORAGE, "cannot split index node: no balanced partition")
    split = best[1]
    return list(entries[:split]), list(entries[split:])


class IndexTree:
    """一个索引文件上的 B+ 树操作入口（M2 内部接口，门面接入在 M3）。

    每次公开操作都从页 0 重新读取 root / height：不缓存树状态，
    多句柄共享同一个文件时不会各自持有过期的根。
    """

    def __init__(
        self,
        pool: BufferPool,
        file_path: Path,
        column: ColumnDef,
    ) -> None:
        self.pool = pool
        self.path = Path(file_path)
        self.column = column
        # 打开即校验 magic/version/root/height/键类型，避免带病工作。
        open_index_file(pool, self.path, expected_key_type=column.type)

    # ---- 页读写 ----

    def _read(self, page_no: int) -> bytearray:
        raw = read_page(self.pool, self.path, page_no, kind=INDEX_FILE_KIND)
        validate_node(raw, context=f"{self.path.name}#{page_no}")
        return bytearray(raw)

    def _write(self, page_no: int, page: bytearray) -> None:
        write_page(self.pool, self.path, page_no, bytes(page), kind=INDEX_FILE_KIND)

    def _load(self) -> tuple[int, int]:
        page0 = read_page(self.pool, self.path, 0, kind=INDEX_FILE_KIND)
        return (
            _U32.unpack_from(page0, INDEX_PAGE0_ROOT_OFFSET)[0],
            _U32.unpack_from(page0, INDEX_PAGE0_HEIGHT_OFFSET)[0],
        )

    def _store(self, root: int, height: int) -> None:
        page0 = bytearray(read_page(self.pool, self.path, 0, kind=INDEX_FILE_KIND))
        _U32.pack_into(page0, INDEX_PAGE0_ROOT_OFFSET, root)
        _U32.pack_into(page0, INDEX_PAGE0_HEIGHT_OFFSET, height)
        write_page(self.pool, self.path, 0, bytes(page0), kind=INDEX_FILE_KIND)

    # ---- 条目视图 ----

    def _leaf_entries(self, page: bytes) -> list[tuple[int, int, bytes, Value]]:
        """叶条目视图：(rid, 行所在页号, 键字节, 键值)。"""

        result = []
        for payload in entry_payloads(page):
            row_id = _RID.unpack_from(payload, 0)[0]
            page_no = _U32.unpack_from(payload, INDEX_RID_SIZE)[0]
            key_bytes = payload[INDEX_LEAF_PREFIX_SIZE:]
            result.append(
                (row_id, page_no, key_bytes, decode_key(self.column, key_bytes))
            )
        return result

    def _interior_entries(
        self, page: bytes
    ) -> list[tuple[int, int, bytes, Value]]:
        result = []
        for payload in entry_payloads(page):
            child = _U32.unpack_from(payload, 0)[0]
            row_id = _RID.unpack_from(payload, INDEX_CHILD_PTR_SIZE)[0]
            key_bytes = payload[INDEX_CHILD_PTR_SIZE + INDEX_RID_SIZE :]
            result.append(
                (child, row_id, key_bytes, decode_key(self.column, key_bytes))
            )
        return result

    def _leaf_sort_key(self, payload: bytes) -> tuple[Value, int]:
        return (
            decode_key(self.column, payload[INDEX_LEAF_PREFIX_SIZE:]),
            _RID.unpack_from(payload, 0)[0],
        )

    def _interior_sort_key(self, payload: bytes) -> tuple[Value, int]:
        return (
            decode_key(self.column, payload[INDEX_CHILD_PTR_SIZE + INDEX_RID_SIZE :]),
            _RID.unpack_from(payload, INDEX_CHILD_PTR_SIZE)[0],
        )

    # ---- 下降规则 ----

    def _child_for_search(self, page: bytes, probe: Value, row_id: int) -> int:
        """最左规则：第一个 `(键, rid) >= 探针` 的分隔项处往左走。

        查找与范围必须用这条规则：重复键跨叶时，只有从最左候选叶起步、
        沿叶链向后扫，才不会漏掉左边那份副本。
        """
        left = first_child(page)
        for child, sep_rid, _key_bytes, sep_key in self._interior_entries(page):
            if (sep_key, sep_rid) >= (probe, row_id):
                return left
            left = child
        return left

    def _child_for_insert(self, page: bytes, probe: Value, row_id: int) -> int:
        """精确下降（最右规则）：最后一个 `(键, rid) <= 探针` 的分隔项处往右走。

        与查找规则不同是刻意的：插入把所有重复键放到最右侧的候选叶，
        而查找从最左侧候选叶开始扫，两者配合才能既写对又读全。

        插入与按 `(键, rid)` 精确删除都必须用它：只有 (键, rid) 的全局次序
        才能唯一定位"包含该条目的叶子"。若删除误用最左规则，当目标恰好等于
        某个分隔键时会往左走错叶子，报"条目不存在"。
        """
        left = first_child(page)
        for child, sep_rid, _key_bytes, sep_key in self._interior_entries(page):
            if (sep_key, sep_rid) <= (probe, row_id):
                left = child
        return left

    def _leaf_for(self, root: int, probe: Value, row_id: int) -> int:
        page_no = root
        while True:
            page = self._read(page_no)
            if node_type(page) == LEAF_NODE:
                return page_no
            page_no = self._child_for_search(page, probe, row_id)

    def _leftmost_leaf(self, root: int) -> int:
        page_no = root
        while True:
            page = self._read(page_no)
            if node_type(page) == LEAF_NODE:
                return page_no
            page_no = first_child(page)

    # ---- 链维护 ----

    def _set_leaf_next(self, page_no: int, value: int) -> None:
        page = self._read(page_no)
        _U32.pack_into(page, INDEX_NODE_NEXT_LEAF_OFFSET, value)
        self._write(page_no, page)

    def _set_leaf_prev(self, page_no: int, value: int) -> None:
        page = self._read(page_no)
        _U32.pack_into(page, INDEX_NODE_PREV_LEAF_OFFSET, value)
        self._write(page_no, page)

    # ---- 查询 ----

    def lookup(self, key: Value) -> list[tuple[int, int]]:
        """等值查找：返回全部匹配的 `(rid, 行所在页号)`，按 rid 升序（D49a）。"""
        probe = normalize_key(self.column, key)
        root, _height = self._load()
        page_no = self._leaf_for(root, probe, 0)
        total_pages = page_count(self.pool, self.path, kind=INDEX_FILE_KIND)
        steps = 0
        result: list[tuple[int, int]] = []
        while page_no != 0:
            steps += 1
            if steps > total_pages:
                # 叶链成环或过长：损坏必须是 E_STORAGE，绝不能挂死。
                raise SqlError(
                    E_STORAGE,
                    f"corrupt index file {self.path.name}: leaf chain cycle "
                    f"or too long",
                )
            page = self._read(page_no)
            for row_id, row_page, _key_bytes, value in self._leaf_entries(page):
                if value < probe:
                    continue
                if value > probe:
                    return result
                result.append((row_id, row_page))
            page_no = leaf_next(page)
        return result

    def range(
        self,
        lower: Value | None,
        upper: Value | None,
        *,
        lower_inclusive: bool = True,
        upper_inclusive: bool = True,
    ) -> list[tuple[int, int]]:
        """范围查找：端点可为 None（无界），默认闭区间，按键序返回 `(rid, 页号)`。"""
        low = None if lower is None else normalize_key(self.column, lower)
        high = None if upper is None else normalize_key(self.column, upper)
        if low is not None and high is not None:
            if low > high:
                return []
            if low == high and not (lower_inclusive and upper_inclusive):
                return []

        root, _height = self._load()
        page_no = (
            self._leftmost_leaf(root) if low is None else self._leaf_for(root, low, 0)
        )
        total_pages = page_count(self.pool, self.path, kind=INDEX_FILE_KIND)
        steps = 0
        result: list[tuple[int, int]] = []
        while page_no != 0:
            steps += 1
            if steps > total_pages:
                raise SqlError(
                    E_STORAGE,
                    f"corrupt index file {self.path.name}: leaf chain cycle "
                    f"or too long",
                )
            page = self._read(page_no)
            for row_id, row_page, _key_bytes, value in self._leaf_entries(page):
                if low is not None:
                    if value < low:
                        continue
                    if value == low and not lower_inclusive:
                        continue
                if high is not None:
                    if value > high:
                        return result
                    if value == high and not upper_inclusive:
                        return result
                result.append((row_id, row_page))
            page_no = leaf_next(page)
        return result

    # ---- 写入 ----

    def insert(self, key: Value, row_id: int, page_no: int) -> None:
        """插入一个 (键, rid, 行页号)；必要时分裂叶与内节点，并更新根与树高。"""
        probe = normalize_key(self.column, key)
        key_bytes = encode_key(self.column, probe)
        root, height = self._load()

        separator = self._insert_into(root, probe, row_id, page_no, key_bytes)
        if separator is None:
            return

        new_root = alloc_page(self.pool, self.path, kind=INDEX_FILE_KIND)
        page = new_interior_page(first_child=root)
        rebuild_node(page, INTERIOR_NODE, (separator,), first_child=root)
        self._write(new_root, page)
        self._store(new_root, height + 1)

    def _insert_into(
        self,
        page_no: int,
        probe: Value,
        row_id: int,
        row_page: int,
        key_bytes: bytes,
    ) -> bytes | None:
        """向以 page_no 为根的子树插入；返回上推的分隔条目（若发生分裂）。"""
        page = self._read(page_no)

        if node_type(page) == LEAF_NODE:
            payloads = list(entry_payloads(page))
            payloads.append(_leaf_payload(row_id, row_page, key_bytes))
            payloads.sort(key=self._leaf_sort_key)
            if node_fits(payloads):
                rebuild_node(
                    page,
                    LEAF_NODE,
                    payloads,
                    next_leaf=leaf_next(page),
                    prev_leaf=leaf_prev(page),
                )
                self._write(page_no, page)
                return None

            left, right = _split_payloads(payloads)
            next_page = leaf_next(page)
            right_page = alloc_page(self.pool, self.path, kind=INDEX_FILE_KIND)
            rebuild_node(
                page,
                LEAF_NODE,
                left,
                next_leaf=right_page,
                prev_leaf=leaf_prev(page),
            )
            self._write(page_no, page)
            new_page = new_leaf_page()
            rebuild_node(
                new_page,
                LEAF_NODE,
                right,
                next_leaf=next_page,
                prev_leaf=page_no,
            )
            self._write(right_page, new_page)
            if next_page != 0:
                self._set_leaf_prev(next_page, right_page)
            first_rid = _RID.unpack_from(right[0], 0)[0]
            first_key = right[0][INDEX_LEAF_PREFIX_SIZE:]
            return _interior_payload(right_page, first_rid, first_key)

        child = self._child_for_insert(page, probe, row_id)
        separator = self._insert_into(child, probe, row_id, row_page, key_bytes)
        if separator is None:
            return None

        payloads = list(entry_payloads(page))
        payloads.append(separator)
        payloads.sort(key=self._interior_sort_key)
        if node_fits(payloads):
            rebuild_node(
                page, INTERIOR_NODE, payloads, first_child=first_child(page)
            )
            self._write(page_no, page)
            return None

        left, right = _split_payloads(payloads)
        right_page = alloc_page(self.pool, self.path, kind=INDEX_FILE_KIND)
        promoted = right[0]
        promoted_child = _U32.unpack_from(promoted, 0)[0]
        promoted_rid = _RID.unpack_from(promoted, INDEX_CHILD_PTR_SIZE)[0]
        promoted_key = promoted[INDEX_CHILD_PTR_SIZE + INDEX_RID_SIZE :]
        new_page = new_interior_page(first_child=promoted_child)
        rebuild_node(
            new_page,
            INTERIOR_NODE,
            right[1:],
            first_child=promoted_child,
        )
        self._write(right_page, new_page)
        rebuild_node(page, INTERIOR_NODE, left, first_child=first_child(page))
        self._write(page_no, page)
        return _interior_payload(right_page, promoted_rid, promoted_key)

    def delete(self, key: Value, row_id: int) -> None:
        """删除一个 (键, rid)。

        惰性策略（D34）：节点欠载不处理，只有整页删空才归还空闲链表；
        根页永远保留，只有"内节点根 + 0 条目"才折叠。
        """
        probe = normalize_key(self.column, key)
        root, height = self._load()

        self._delete_from(root, probe, row_id, root)

        while height > 1:
            page = self._read(root)
            if node_type(page) != INTERIOR_NODE or entry_count(page) != 0:
                break
            child = first_child(page)
            free_page(self.pool, self.path, root, kind=INDEX_FILE_KIND)
            root, height = child, height - 1
            self._store(root, height)

    def update_page(self, key: Value, row_id: int, page_no: int) -> None:
        """刷新既有条目的行页号（D49a：整行更新把行挪到别的页时调用）。

        只改那一页号字段：键与 rid 都不变，因此条目在叶内/树内的位置不动，
        重复键之间也不会被重排。找不到条目 → E_STORAGE（索引与表不一致属损坏，
        不能静默跳过）。
        """

        probe = normalize_key(self.column, key)
        root, _height = self._load()
        leaf = self._leaf_for_exact(root, probe, row_id)
        page = self._read(leaf)
        payloads = list(entry_payloads(page))
        for index, payload in enumerate(payloads):
            entry_rid = _RID.unpack_from(payload, 0)[0]
            if entry_rid != row_id:
                continue
            entry_key = decode_key(self.column, payload[INDEX_LEAF_PREFIX_SIZE:])
            if entry_key != probe:
                continue
            payloads[index] = _leaf_payload(
                row_id, page_no, payload[INDEX_LEAF_PREFIX_SIZE:]
            )
            rebuild_node(
                page,
                LEAF_NODE,
                payloads,
                next_leaf=leaf_next(page),
                prev_leaf=leaf_prev(page),
            )
            self._write(leaf, page)
            return
        raise SqlError(
            E_STORAGE,
            f"index entry not found: ({probe!r}, {row_id}) in {self.path.name}",
        )

    def _leaf_for_exact(self, root: int, probe: Value, row_id: int) -> int:
        """按 (键, rid) 精确下降到一个叶（与删除同一条最右规则）。"""

        page_no = root
        while True:
            page = self._read(page_no)
            if node_type(page) == LEAF_NODE:
                return page_no
            page_no = self._child_for_insert(page, probe, row_id)

    def _delete_from(
        self, page_no: int, probe: Value, row_id: int, root_page: int
    ) -> int | None:
        """从子树删除；返回被回收的页号（若该子树整页删空）。"""
        page = self._read(page_no)

        if node_type(page) == LEAF_NODE:
            payloads = list(entry_payloads(page))
            target = None
            for index, payload in enumerate(payloads):
                entry_rid = _RID.unpack_from(payload, 0)[0]
                entry_key = decode_key(
                    self.column, payload[INDEX_LEAF_PREFIX_SIZE:]
                )
                if entry_key == probe and entry_rid == row_id:
                    target = index
                    break
            if target is None:
                raise SqlError(
                    E_STORAGE,
                    f"index entry not found: ({probe!r}, {row_id}) "
                    f"in {self.path.name}",
                )

            remaining = payloads[:target] + payloads[target + 1 :]
            if remaining:
                rebuild_node(
                    page,
                    LEAF_NODE,
                    remaining,
                    next_leaf=leaf_next(page),
                    prev_leaf=leaf_prev(page),
                )
                self._write(page_no, page)
                return None

            if page_no == root_page:
                # 根页必须永远存在：删空后留作空叶根。
                rebuild_node(page, LEAF_NODE, (), next_leaf=0, prev_leaf=0)
                self._write(page_no, page)
                return None

            prev_page = leaf_prev(page)
            next_page = leaf_next(page)
            if prev_page != 0:
                self._set_leaf_next(prev_page, next_page)
            if next_page != 0:
                self._set_leaf_prev(next_page, prev_page)
            free_page(self.pool, self.path, page_no, kind=INDEX_FILE_KIND)
            return page_no

        child = self._child_for_insert(page, probe, row_id)
        freed = self._delete_from(child, probe, row_id, root_page)
        if freed is None:
            return None

        payloads = list(entry_payloads(page))
        head = first_child(page)
        if head == freed:
            if not payloads:
                if page_no == root_page:
                    return None
                free_page(self.pool, self.path, page_no, kind=INDEX_FILE_KIND)
                return page_no
            head = _U32.unpack_from(payloads[0], 0)[0]
            payloads = payloads[1:]
        else:
            payloads = [
                payload
                for payload in payloads
                if _U32.unpack_from(payload, 0)[0] != freed
            ]
        rebuild_node(page, INTERIOR_NODE, payloads, first_child=head)
        self._write(page_no, page)
        return None
