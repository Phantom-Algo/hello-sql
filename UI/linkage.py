"""QueryTrace 的节点、Token、数据页与 SQL 原文联动索引。

追踪契约已经保存了 Token 的字符偏移、AST/计划/Executor 树快照、
存储事件的文件与页号以及语句级 SourceSpan。本模块只把这些已有
事实整理为前端可直接使用的交叉引用，不重新分词、不重建 AST、
不读取数据页、也不执行 SQL。

联动索引包含三类可选对象：

* ``tokens`` 来自 A Lexer 的真实输出，保留零基半开偏移；
* ``nodes`` 来自 A AST、C Logical Plan 和 C Executor 快照；
* ``pages`` 来自 B Cache/Pager/Engine 的真实页操作事件。

节点与 Token 通过结构中已保存的表名、列名、限定符、字面量和
操作符建立展示关联；扫描节点与页通过表名和 ``*.table`` 文件名关联。
这些是显式的“展示链接”而不是新语义契约；不能精确匹配时保持空集，
绝不伪造源码位置或物理页访问。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from hashlib import sha1
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from UI.inspection import InspectionSnapshot


_NODE_STAGE_IDS = frozenset({"a.ast", "c.logical_plan", "c.executor"})
_PAGE_STAGE_IDS = frozenset({"b.cache", "b.pager", "b.engine"})
_POSITIONAL_PAGE_OPERATIONS = frozenset(
    {
        "get_page",
        "unpin_page",
        "read_page",
        "write_page",
        "free_page",
        "mark_dirty",
    }
)
_PAGE_FIELD_NAMES = frozenset(
    {"page", "page_id", "page_no", "page_num", "page_number", "page_index"}
)


def build_linkage(snapshot: InspectionSnapshot) -> dict[str, object]:
    """从一份只读查看快照构建前端联动索引。

    Args:
        snapshot: 包含完整 QueryTrace 和当前 A/B/C/ALL 阶段子集的快照。

    Returns:
        可 JSON 序列化的 source/tokens/nodes/pages 字典。

    Token 始终从完整追踪中提取，因为 B 页视图和 C 计划视图也需要
    高亮 SQL 原文。节点和页则只从当前筛选可见阶段中提取，保持
    ``/inspect A|B|C`` 的责任边界。
    """

    trace_data = snapshot.trace.to_dict()
    all_stages = tuple(trace_data["stages"])
    visible_stage_ids = {stage.stage_id for stage in snapshot.stages}
    source = _source_text(trace_data, all_stages)
    tokens = _extract_tokens(all_stages, _token_filter_span(trace_data))
    nodes = _extract_nodes(
        all_stages,
        visible_stage_ids,
        tokens,
        int(trace_data["statement_index"]),
    )
    pages = _extract_pages(all_stages, visible_stage_ids)
    _connect_pages_nodes_and_tokens(pages, nodes, tokens)
    return {
        "source": source,
        "active_statement_span": trace_data.get("source_span"),
        "tokens": tokens,
        "nodes": nodes,
        "pages": pages,
        "counts": {
            "tokens": len(tokens),
            "nodes": len(nodes),
            "pages": len(pages),
        },
    }


def _token_filter_span(trace_data: Mapping[str, object]) -> object:
    """返回用于限定 Lexer Token 的语句范围。

    正常单语句和多语句执行使用 QueryTrace.source_span，以便脚本
    视图只展示当前语句的 Token。编译失败时尚未产生完整的
    ParsedStatement，QueryTrace.source_span 因而表示“错误点”而非“语句范围”。
    此时返回 ``None`` 保留 Lexer 已识别的全部 Token，同时上层仍将
    原 source_span 作为 ``active_statement_span`` 交给界面高亮精确错误位置。

    Args:
        trace_data: QueryTrace.to_dict() 产生的 JSON 兼容字典。

    Returns:
        普通查询返回 SourceSpan 字典；编译失败返回 ``None``。
    """

    summary = trace_data.get("result_summary")
    if isinstance(summary, Mapping) and summary.get("phase") == "compile":
        return None
    return trace_data.get("source_span")


def _source_text(
    trace_data: Mapping[str, object],
    stages: tuple[dict[str, object], ...],
) -> str:
    """取得 Token 偏移所针对的完整 SQL 原文。

    脚本查询的 Lexer 偏移相对整个输入缓冲区，所以优先读取
    ``c.repl.input_snapshot.sql``。老追踪没有 REPL 阶段时回退到查询 SQL。
    """

    repl = next((stage for stage in stages if stage["stage_id"] == "c.repl"), None)
    if repl is not None:
        candidate = repl.get("input_snapshot", {}).get("sql")
        if isinstance(candidate, str):
            return candidate
    sql = trace_data.get("sql")
    return sql if isinstance(sql, str) else ""


def _extract_tokens(
    stages: tuple[dict[str, object], ...],
    active_span: object,
) -> list[dict[str, object]]:
    """提取当前语句范围内的非 EOF Lexer Token。

    每个 Token 保留原始半开字符偏移，并增加稳定 ``token_id`` 与
    反向 node/page ID 数组。多语句脚本中只保留与当前 QueryTrace
    SourceSpan 相交的 Token，避免查看第三条语句时高亮前两条。
    ``active_span=None`` 表示编译失败：此时保留 Lexer 已识别的
    全部非 EOF Token，便于在错误点前后完整回放分词结果。
    """

    lexer = next((stage for stage in stages if stage["stage_id"] == "a.lexer"), None)
    if lexer is None:
        return []
    raw_tokens = lexer.get("output_snapshot", {}).get("tokens", [])
    result: list[dict[str, object]] = []
    for index, token in enumerate(raw_tokens, start=1):
        if not isinstance(token, Mapping) or token.get("type") == "EOF":
            continue
        span = _token_span(token)
        if isinstance(active_span, Mapping) and not _spans_overlap(span, active_span):
            continue
        result.append(
            {
                "token_id": f"token-{index:04d}",
                "type": token.get("type", "UNKNOWN"),
                "lexeme": token.get("lexeme", ""),
                "start_offset": token.get("start_offset", 0),
                "end_offset": token.get("end_offset", 0),
                "source_span": span,
                "stage_id": "a.lexer",
                "event_id": f"lexer.token.{index:04d}",
                "node_ids": [],
                "page_ids": [],
            }
        )
    return result


def _token_span(token: Mapping[str, object]) -> dict[str, int]:
    """将 Lexer Token 的起点和原文长度转为一基闭区间。"""

    line = int(token.get("line", 1))
    column = int(token.get("column", 1))
    lexeme = str(token.get("lexeme", ""))
    lines = lexeme.splitlines() or [""]
    if len(lines) == 1:
        return {
            "start_line": line,
            "start_col": column,
            "end_line": line,
            "end_col": column + max(len(lexeme) - 1, 0),
        }
    return {
        "start_line": line,
        "start_col": column,
        "end_line": line + len(lines) - 1,
        "end_col": max(len(lines[-1]), 1),
    }


def _spans_overlap(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    """判断两个一基闭区间 SourceSpan 是否相交。"""

    left_start = (int(left["start_line"]), int(left["start_col"]))
    left_end = (int(left["end_line"]), int(left["end_col"]))
    right_start = (int(right["start_line"]), int(right["start_col"]))
    right_end = (int(right["end_line"]), int(right["end_col"]))
    return left_start <= right_end and right_start <= left_end


def _extract_nodes(
    stages: tuple[dict[str, object], ...],
    visible_stage_ids: set[str],
    tokens: list[dict[str, object]],
    statement_index: int,
) -> list[dict[str, object]]:
    """从可见 AST、Logical Plan 和 Executor 阶段提取扁平节点树。

    AST 使用 A 已记录的真实节点事件和路径，并严格限制到当前
    statement_index。Logical Plan/Executor 使用 C 输出快照的 ``fields``
    嵌套关系递归建立 parent_id。每个节点都保留原快照，不重建业务对象。
    """

    nodes: list[dict[str, object]] = []
    for stage in stages:
        stage_id = str(stage["stage_id"])
        if stage_id not in visible_stage_ids or stage_id not in _NODE_STAGE_IDS:
            continue
        if stage_id == "a.ast":
            _append_ast_nodes(stage, tokens, statement_index, nodes)
        else:
            root = stage.get("output_snapshot", {}).get("last_result")
            _walk_runtime_nodes(root, stage_id, "root", None, 0, tokens, nodes)
    return nodes


def _append_ast_nodes(
    stage: Mapping[str, object],
    tokens: list[dict[str, object]],
    statement_index: int,
    nodes: list[dict[str, object]],
) -> None:
    """将 A AST 阶段中当前语句的节点事件转为联动节点。

    父节点由 A 事件中的结构路径前缀确定。只有 A 真实提供的
    SourceSpan 才会直接采用；子节点则使用快照值匹配 Token，并在数据中
    标记 ``source_link=token_inference``，不将展示关联伪装成编译契约。
    """

    path_to_id: dict[str, str] = {}
    for event in stage.get("events", []):
        input_snapshot = event.get("input_snapshot", {})
        if input_snapshot.get("statement_index") != statement_index:
            continue
        path = str(input_snapshot.get("path", "root"))
        raw_node = event.get("output_snapshot", {}).get("node", {})
        kind = _node_kind(raw_node)
        node_id = f"a.ast:{event['event_id']}"
        parent_path = _nearest_parent_path(path, path_to_id)
        token_ids = _infer_token_ids(raw_node, tokens)
        explicit_span = event.get("source_span")
        nodes.append(
            _node_record(
                node_id=node_id,
                stage_id="a.ast",
                kind=kind,
                path=path,
                parent_id=path_to_id.get(parent_path),
                depth=_path_depth(path),
                snapshot=raw_node,
                token_ids=token_ids,
                source_span=explicit_span or _span_for_token_ids(token_ids, tokens),
                source_link="explicit" if explicit_span else "token_inference",
                event_id=str(event["event_id"]),
            )
        )
        path_to_id[path] = node_id


def _walk_runtime_nodes(
    value: object,
    stage_id: str,
    path: str,
    parent_id: str | None,
    depth: int,
    tokens: list[dict[str, object]],
    nodes: list[dict[str, object]],
) -> None:
    """递归遍历 C 快照，为每个 ``value_type + fields`` 对象生成节点。

    映射中没有类型与 fields 的普通快照不会被伪装成树节点，但仍会
    继续向下遍历，以支持节点嵌套在列表或其他输出容器中的情况。
    """

    current_parent = parent_id
    next_depth = depth
    if isinstance(value, Mapping) and _node_kind(value) != "Unknown":
        node_id = f"{stage_id}:node-{len(nodes) + 1:04d}"
        token_ids = _infer_token_ids(value, tokens)
        nodes.append(
            _node_record(
                node_id=node_id,
                stage_id=stage_id,
                kind=_node_kind(value),
                path=path,
                parent_id=parent_id,
                depth=depth,
                snapshot=value,
                token_ids=token_ids,
                source_span=_span_for_token_ids(token_ids, tokens),
                source_link="token_inference" if token_ids else "none",
                event_id=None,
            )
        )
        current_parent = node_id
        next_depth = depth + 1
        value = value.get("fields", {})
    if isinstance(value, Mapping):
        for key, child in value.items():
            _walk_runtime_nodes(
                child,
                stage_id,
                f"{path}.{key}",
                current_parent,
                next_depth,
                tokens,
                nodes,
            )
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _walk_runtime_nodes(
                child,
                stage_id,
                f"{path}[{index}]",
                current_parent,
                next_depth,
                tokens,
                nodes,
            )


def _node_record(
    *,
    node_id: str,
    stage_id: str,
    kind: str,
    path: str,
    parent_id: str | None,
    depth: int,
    snapshot: object,
    token_ids: list[str],
    source_span: object,
    source_link: str,
    event_id: str | None,
) -> dict[str, object]:
    """统一构造前端节点记录的稳定字段。"""

    return {
        "node_id": node_id,
        "stage_id": stage_id,
        "event_id": event_id,
        "kind": kind,
        "path": path,
        "parent_id": parent_id,
        "depth": depth,
        "snapshot": snapshot,
        "source_span": source_span,
        "source_link": source_link,
        "token_ids": token_ids,
        "page_ids": [],
    }


def _node_kind(value: object) -> str:
    """从 A ``node_type`` 或 C ``value_type`` 取得节点类型名。"""

    if not isinstance(value, Mapping):
        return "Unknown"
    fields = value.get("fields")
    candidate = value.get("node_type", value.get("value_type"))
    return str(candidate) if isinstance(candidate, str) and isinstance(fields, Mapping) else "Unknown"


def _nearest_parent_path(path: str, known: Mapping[str, str]) -> str | None:
    """返回已记录 AST 路径中最长的真前缀父路径。"""

    candidates = [
        candidate
        for candidate in known
        if path.startswith(f"{candidate}.") or path.startswith(f"{candidate}[")
    ]
    return max(candidates, key=len) if candidates else None


def _path_depth(path: str) -> int:
    """使用字段点和数组下标数计算节点视觉缩进深度。"""

    return path.count(".") + path.count("[")


def _infer_token_ids(
    snapshot: object,
    tokens: list[dict[str, object]],
) -> list[str]:
    """使用节点快照中的语义标量匹配真实 Token ID。

    匹配不依赖字段名本身，只收集 fields 中已保存的字符串、布尔值和
    数值。字符串 Token 比较时会去掉一层 SQL 引号。一个名称在 SQL 中
    出现多次时保留所有匹配，不猜测 Parser 未提供的精确子节点范围。
    """

    values = {_normalize_lexeme(item) for item in _scalar_values(snapshot)}
    values.discard("")
    return [
        str(token["token_id"])
        for token in tokens
        if _normalize_lexeme(token.get("lexeme")) in values
    ]


def _scalar_values(value: object) -> Iterable[object]:
    """深度遍历节点 fields，产生可用于展示匹配的标量值。"""

    if isinstance(value, Mapping):
        fields = value.get("fields", value)
        if isinstance(fields, Mapping):
            for key, child in fields.items():
                if key in {"index", "type"}:
                    continue
                yield from _scalar_values(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _scalar_values(child)
    elif isinstance(value, (str, bool, int, float)):
        yield value


def _normalize_lexeme(value: object) -> str:
    """把节点标量与 SQL Token 统一为小写可比较文本。"""

    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    text = str(value).strip().lower()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1]
    return text


def _span_for_token_ids(
    token_ids: list[str],
    tokens: list[dict[str, object]],
) -> dict[str, int] | None:
    """将一组 Token ID 合并为最小一基闭区间，空集返回 None。"""

    selected = [token for token in tokens if token["token_id"] in token_ids]
    if not selected:
        return None
    first = min(selected, key=lambda item: int(item["start_offset"]))
    last = max(selected, key=lambda item: int(item["end_offset"]))
    return {
        "start_line": first["source_span"]["start_line"],
        "start_col": first["source_span"]["start_col"],
        "end_line": last["source_span"]["end_line"],
        "end_col": last["source_span"]["end_col"],
    }


def _extract_pages(
    stages: tuple[dict[str, object], ...],
    visible_stage_ids: set[str],
) -> list[dict[str, object]]:
    """从可见 B 阶段事件中提取并合并物理页引用。

    一个页用“文件规范字符串 + 页号”区分，多个 Cache/Pager/Engine 事件
    指向同一页时会追加到同一 ``events`` 列表。page_id 使用文件路径
    SHA-1 的短摘要与页号，避免 DOM ID 包含本地分隔符或超长路径。
    """

    pages: dict[tuple[str, int], dict[str, object]] = {}
    for stage in stages:
        stage_id = str(stage["stage_id"])
        if stage_id not in visible_stage_ids or stage_id not in _PAGE_STAGE_IDS:
            continue
        for event in stage.get("events", []):
            for file_path, page_number in _page_references(event):
                key = (file_path, page_number)
                if key not in pages:
                    digest = sha1(file_path.encode("utf-8")).hexdigest()[:10]
                    pages[key] = {
                        "page_id": f"page-{digest}-{page_number}",
                        "file": file_path,
                        "file_name": Path(file_path).name,
                        "table": _table_from_page_file(file_path),
                        "page_number": page_number,
                        "events": [],
                        "stage_ids": [],
                        "node_ids": [],
                        "token_ids": [],
                    }
                page = pages[key]
                reference = {
                    "stage_id": stage_id,
                    "event_id": event.get("event_id"),
                    "action": event.get("action"),
                    "description": event.get("description"),
                    "elapsed_ms": event.get("elapsed_ms", 0.0),
                    "cache_outcome": event.get("output_snapshot", {}).get("cache_outcome"),
                }
                if reference not in page["events"]:
                    page["events"].append(reference)
                if stage_id not in page["stage_ids"]:
                    page["stage_ids"].append(stage_id)
    return sorted(
        pages.values(),
        key=lambda item: (str(item["file_name"]), int(item["page_number"])),
    )


def _page_references(event: Mapping[str, object]) -> set[tuple[str, int]]:
    """从一个 B 事件的位置参数和命名字段中读取页引用。

    对 get/read/write/unpin 等契约明确的调用，首个参数是文件且第二个
    参数是页号。其他快照只接受 page_id/page_number 等显式键，不会把
    影响行数、列下标或缓存统计误判为页号。
    """

    references: set[tuple[str, int]] = set()
    action = str(event.get("action", ""))
    operation = action.rsplit(".", maxsplit=1)[-1]
    input_snapshot = event.get("input_snapshot", {})
    arguments = input_snapshot.get("arguments", []) if isinstance(input_snapshot, Mapping) else []
    file_path = _first_page_file(arguments)
    if (
        operation in _POSITIONAL_PAGE_OPERATIONS
        and file_path is not None
        and isinstance(arguments, (list, tuple))
        and len(arguments) >= 2
        and _is_page_number(arguments[1])
    ):
        references.add((file_path, int(arguments[1])))
    if file_path is not None:
        for page_number in _named_page_numbers(event):
            references.add((file_path, page_number))
    return references


def _first_page_file(arguments: object) -> str | None:
    """返回位置参数中第一个表页文件路径。"""

    if not isinstance(arguments, (list, tuple)):
        return None
    return next(
        (
            value
            for value in arguments
            if isinstance(value, str) and value.lower().endswith((".table", ".pages"))
        ),
        None,
    )


def _named_page_numbers(value: object, key: str | None = None) -> set[int]:
    """递归收集只位于显式页号字段下的非负整数。"""

    result: set[int] = set()
    if key in _PAGE_FIELD_NAMES and _is_page_number(value):
        result.add(int(value))
    elif isinstance(value, Mapping):
        for child_key, child in value.items():
            result.update(_named_page_numbers(child, str(child_key).lower()))
    elif isinstance(value, (list, tuple)) and key in _PAGE_FIELD_NAMES:
        result.update(int(child) for child in value if _is_page_number(child))
    return result


def _is_page_number(value: object) -> bool:
    """判断值是否为非布尔、非负整数页号。"""

    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _table_from_page_file(file_path: str) -> str | None:
    """从 ``users.table`` 类文件名提取用于展示关联的表名。"""

    name = Path(file_path).name
    for suffix in (".table", ".pages"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)].lower()
    return None


def _connect_pages_nodes_and_tokens(
    pages: list[dict[str, object]],
    nodes: list[dict[str, object]],
    tokens: list[dict[str, object]],
) -> None:
    """在节点、Token 和页之间补齐对称反向 ID 引用。

    节点—Token 关系来自节点匹配结果；页—Token 关系使用表页文件名与
    SQL 表名 Token；页—节点关系使用节点快照中的表名。所有列表去重
    并保持首次发现顺序，便于前端进行稳定单步导航。
    """

    token_by_id = {str(token["token_id"]): token for token in tokens}
    node_by_id = {str(node["node_id"]): node for node in nodes}
    for node in nodes:
        for token_id in node["token_ids"]:
            _append_unique(token_by_id[token_id]["node_ids"], node["node_id"])
    for page in pages:
        table = page.get("table")
        if isinstance(table, str):
            for token in tokens:
                if _normalize_lexeme(token.get("lexeme")) == table:
                    _append_unique(page["token_ids"], token["token_id"])
                    _append_unique(token["page_ids"], page["page_id"])
            for node in nodes:
                values = {_normalize_lexeme(item) for item in _scalar_values(node["snapshot"])}
                if table in values:
                    _append_unique(page["node_ids"], node["node_id"])
                    _append_unique(node["page_ids"], page["page_id"])
    # 保留显式变量可让未来增加反向校验时无需重建映射；当前读取
    # 确保所有 page.node_ids 都指向本次可见节点。
    assert all(node_id in node_by_id for page in pages for node_id in page["node_ids"])


def _append_unique(values: list[object], value: object) -> None:
    """在保持发现顺序的前提下向 ID 列表追加非重复值。"""

    if value not in values:
        values.append(value)
