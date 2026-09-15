"""索引结构审计（测试专用，对标表侧的 audit_util.py）。

单页校验（`validate_node`）只能发现"页内坏"；本模块检查"树级坏"：

- 所有叶都在同一深度，且深度与页 0 的 height 一致；
- 内节点分隔键 == 右子树最小条目的完整 (键, rid)（D46）；
- 叶内条目按 (键, rid) 严格有序；
- 叶条目里的**行页号提示**确实指向装着该行的页（D49a；需调用方给出表侧回调）；
- 叶链自最左叶起可覆盖全部可达叶，长度正确、prev/next 互指、无环；
- 每个非页 0 的页要么从根可达、要么在空闲页链表里（无游离页）。

任何违反都抛 E_STORAGE，供损坏矩阵与 soak 断言使用。

注意：**必须传入与写入方相同的 BufferPool**。用第二个池去读同一个文件会
读到该池缓存的陈旧页（这也是"同一数据目录只允许一个写入方"的既有约定）。
"""

from __future__ import annotations

from pathlib import Path

from contracts.ast import ColumnDef
from contracts.errors import E_STORAGE, SqlError
from storage.cache import BufferPool
from storage.index import (
    first_child,
    leaf_next,
    leaf_prev,
    node_type,
    open_index_file,
    entry_payloads,
    validate_node,
)
from storage.constants import (
    INDEX_LEAF_PREFIX_SIZE,
    INDEX_RID_SIZE,
    INTERIOR_NODE,
    LEAF_NODE,
)
from storage.pager import INDEX_FILE_KIND, free_pages, page_count, read_page
from storage.valuecodec import decode_value


def _decode_leaf_entry(column: ColumnDef, payload: bytes):
    """叶条目 = u64 rid + u32 行页号 + 键编码（D49a）。"""

    row_id = int.from_bytes(payload[:INDEX_RID_SIZE], "little")
    page_no = int.from_bytes(
        payload[INDEX_RID_SIZE:INDEX_LEAF_PREFIX_SIZE], "little"
    )
    value, position = decode_value(column, payload, INDEX_LEAF_PREFIX_SIZE)
    if position != len(payload):
        raise SqlError(E_STORAGE, "audit: leaf entry trailing bytes")
    return row_id, page_no, value


def _decode_interior_entry(column: ColumnDef, payload: bytes):
    child = int.from_bytes(payload[:4], "little")
    row_id = int.from_bytes(payload[4 : 4 + INDEX_RID_SIZE], "little")
    value, position = decode_value(column, payload, 4 + INDEX_RID_SIZE)
    if position != len(payload):
        raise SqlError(E_STORAGE, "audit: interior entry trailing bytes")
    return child, row_id, value


def _subtree_min(pool, path, column, page_no: int):
    """沿最左孩子下降，返回该子树最小条目的 (键, rid)。"""
    seen: set[int] = set()
    while True:
        if page_no in seen:
            raise SqlError(E_STORAGE, "audit: cycle while descending leftmost")
        seen.add(page_no)
        page = read_page(pool, path, page_no, kind=INDEX_FILE_KIND)
        payloads = entry_payloads(page)
        if node_type(page) == LEAF_NODE:
            if not payloads:
                raise SqlError(E_STORAGE, "audit: empty leaf in subtree")
            row_id, _page_no, value = _decode_leaf_entry(column, payloads[0])
            return (value, row_id)
        if not payloads and first_child(page) == 0:
            raise SqlError(E_STORAGE, "audit: childless interior node")
        page_no = first_child(page)


def _subtree_max(pool, path, column, page_no: int):
    """沿最右孩子下降，返回该子树最大条目的 (键, rid)。"""
    seen: set[int] = set()
    while True:
        if page_no in seen:
            raise SqlError(E_STORAGE, "audit: cycle while descending rightmost")
        seen.add(page_no)
        page = read_page(pool, path, page_no, kind=INDEX_FILE_KIND)
        payloads = entry_payloads(page)
        if node_type(page) == LEAF_NODE:
            if not payloads:
                raise SqlError(E_STORAGE, "audit: empty leaf in subtree")
            row_id, _page_no, value = _decode_leaf_entry(column, payloads[-1])
            return (value, row_id)
        if not payloads:
            raise SqlError(E_STORAGE, "audit: childless interior node")
        page_no = _decode_interior_entry(column, payloads[-1])[0]


def audit_index(
    pool: BufferPool,
    path: Path,
    column: ColumnDef,
    *,
    expect_page_of=None,
) -> dict:
    """审计一个索引文件；返回 {pages, leaves, entries}，违反则抛 E_STORAGE。

    `expect_page_of` 是可选回调 `rid -> 行真实所在页号`（D49a）：给了就逐条
    校验叶条目的页号提示。提示错了不会让查询出错，只会退化成全表探测——正因
    如此必须在测试里逐条锁死，否则性能退化会静默发生。
    """
    header = open_index_file(pool, path, expected_key_type=column.type)
    total_pages = page_count(pool, path, kind=INDEX_FILE_KIND)

    reachable: set[int] = set()
    leaf_depths: dict[int, int] = {}
    entries = 0
    stack: list[tuple[int, int]] = [(header.root_page, 0)]
    while stack:
        page_no, depth = stack.pop()
        if page_no in reachable:
            raise SqlError(E_STORAGE, f"audit: page {page_no} reached twice")
        reachable.add(page_no)
        page = read_page(pool, path, page_no, kind=INDEX_FILE_KIND)
        validate_node(page, context=f"{path.name}#{page_no}")
        payloads = entry_payloads(page)
        if node_type(page) == LEAF_NODE:
            leaf_depths[page_no] = depth
            entries += len(payloads)
            keys = [_decode_leaf_entry(column, payload) for payload in payloads]
            ordered = [(value, rid) for rid, _page, value in keys]
            if ordered != sorted(ordered):
                raise SqlError(
                    E_STORAGE, f"audit: leaf {page_no} entries out of order"
                )
            if expect_page_of is not None:
                for row_id, hint, _value in keys:
                    actual = expect_page_of(row_id)
                    if hint != actual:
                        raise SqlError(
                            E_STORAGE,
                            f"audit: leaf {page_no} entry {row_id} claims page "
                            f"{hint}, row actually lives in page {actual}",
                        )
            continue
        children = [first_child(page)]
        if not children[0]:
            raise SqlError(E_STORAGE, f"audit: interior {page_no} has no child")
        separators = []
        for payload in payloads:
            child, row_id, value = _decode_interior_entry(column, payload)
            separators.append((child, (value, row_id)))
            children.append(child)
        # 惰性删除下分隔键只要求"左子树最大 < 分隔键 <= 右子树最小"，
        # 不要求恒等于最小键（删掉最小条目会让子树最小值变大）。
        previous_max = _subtree_max(pool, path, column, children[0])
        for child, separator in separators:
            actual_min = _subtree_min(pool, path, column, child)
            if not (previous_max < separator <= actual_min):
                raise SqlError(
                    E_STORAGE,
                    f"audit: interior {page_no} separator {separator} violates "
                    f"left_max {previous_max} < sep <= right_min {actual_min}",
                )
            previous_max = _subtree_max(pool, path, column, child)
        for child in children:
            if not 0 < child < total_pages:
                raise SqlError(
                    E_STORAGE, f"audit: interior {page_no} child {child} invalid"
                )
            stack.append((child, depth + 1))

    # 所有叶必须同深度，且与页 0 的 height 一致。
    for page_no, depth in leaf_depths.items():
        if depth != header.height - 1:
            raise SqlError(
                E_STORAGE,
                f"audit: leaf {page_no} at depth {depth}, height is {header.height}",
            )

    # 叶链：从左端开始必须恰好覆盖全部可达叶，且 prev/next 互指。
    leftmost = None
    for page_no in leaf_depths:
        if leaf_prev(read_page(pool, path, page_no, kind=INDEX_FILE_KIND)) == 0:
            if leftmost is not None:
                raise SqlError(E_STORAGE, "audit: multiple leaves with no prev")
            leftmost = page_no
    if leaf_depths and leftmost is None:
        raise SqlError(E_STORAGE, "audit: leaf chain has no left end")

    walked: list[int] = []
    current = leftmost or 0
    steps = 0
    while current != 0:
        steps += 1
        if steps > total_pages:
            raise SqlError(E_STORAGE, "audit: leaf chain cycle")
        walked.append(current)
        page = read_page(pool, path, current, kind=INDEX_FILE_KIND)
        nxt = leaf_next(page)
        if nxt != 0:
            next_page = read_page(pool, path, nxt, kind=INDEX_FILE_KIND)
            if leaf_prev(next_page) != current:
                raise SqlError(E_STORAGE, "audit: leaf prev does not match next")
        current = nxt
    if set(walked) != set(leaf_depths) or len(walked) != len(leaf_depths):
        raise SqlError(E_STORAGE, "audit: leaf chain does not cover all leaves")

    free = set(free_pages(pool, path, kind=INDEX_FILE_KIND))
    expected = set(range(1, total_pages)) - free
    if reachable != expected:
        raise SqlError(
            E_STORAGE,
            f"audit: pages not accounted for: reachable={sorted(reachable)} "
            f"expected={sorted(expected)}",
        )

    return {"pages": total_pages, "leaves": len(leaf_depths), "entries": entries}
