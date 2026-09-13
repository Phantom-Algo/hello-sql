"use strict";

/**
 * HELLO-SQL 查询流程查看器的联动交互层。
 *
 * 页面只通过同源 GET API 读取 QueryTrace 和服务端生成的 linkage。
 * 点击 AST/计划/Executor 节点、Lexer Token、存储页或带 SourceSpan 的事件
 * 只会改变本地高亮和详情面板，不会发送 SQL 或触发任何数据库操作。
 */

const state = {
  module: "ALL",
  trace: null,
  view: "stage",
  selectedStageId: null,
  nodeStageId: null,
  selectedNodeId: null,
  selectedTokenId: null,
  selectedPageId: null,
};

/**
 * 定义 NODES 面板中三棵树的稳定顺序与界面名称。
 *
 * stage_id 与服务端追踪契约一致；short 只用于树节点的
 * 简短类型标识，不改变原节点数据。
 */
const NODE_STAGE_OPTIONS = [
  { stageId: "a.ast", label: "AST", short: "AST", owner: "A" },
  { stageId: "c.logical_plan", label: "LOGICAL PLAN", short: "PLAN", owner: "C" },
  { stageId: "c.executor", label: "EXECUTOR", short: "EXEC", owner: "C" },
];

/** 按 DOM id 取得已在 HTML 中声明的固定元素。 */
const byId = (id) => document.getElementById(id);

/** 将任意快照值格式化为带缩进的稳定 JSON 文本。 */
function pretty(value) {
  return JSON.stringify(value ?? {}, null, 2);
}

/** 根据 TraceStatus 返回 CSS 状态类，未知值使用普通样式。 */
function statusClass(status) {
  return String(status || "").toLowerCase();
}

/** 把可选 SourceSpan 转换为一基行列范围文本。 */
function spanText(span) {
  if (!span) return "SOURCE —";
  return `SOURCE ${span.start_line}:${span.start_col}–${span.end_line}:${span.end_col}`;
}

/** 建立当前快照的 Token ID 到 Token 记录的映射。 */
function tokenMap() {
  return new Map((state.trace?.linkage.tokens || []).map((item) => [item.token_id, item]));
}

/** 建立当前快照的 Node ID 到节点记录的映射。 */
function nodeMap() {
  return new Map((state.trace?.linkage.nodes || []).map((item) => [item.node_id, item]));
}

/** 建立当前快照的 Page ID 到数据页记录的映射。 */
function pageMap() {
  return new Map((state.trace?.linkage.pages || []).map((item) => [item.page_id, item]));
}

/** 根据 TokenType 选择 SQL 原文的语法颜色类。 */
function tokenStyle(tokenType) {
  if (String(tokenType).startsWith("KW_")) return "token-keyword";
  if (["STRING", "INTEGER", "REAL"].includes(tokenType)) return "token-literal";
  if (tokenType === "IDENTIFIER") return "token-identifier";
  return "";
}

/** 使用 Lexer 的真实半开偏移将完整 SQL 原文渲染为可点击 Token。 */
function renderSqlSource(linkage) {
  const source = linkage.source || state.trace.sql || "";
  const tokens = [...(linkage.tokens || [])].sort((a, b) => a.start_offset - b.start_offset);
  const fragment = document.createDocumentFragment();
  let cursor = 0;
  for (const token of tokens) {
    const start = Math.max(cursor, Number(token.start_offset));
    const end = Math.max(start, Number(token.end_offset));
    if (start > cursor) fragment.append(document.createTextNode(source.slice(cursor, start)));
    const element = document.createElement("span");
    element.className = `sql-token ${tokenStyle(token.type)}`.trim();
    element.dataset.tokenId = token.token_id;
    element.title = `${token.type}  ·  ${spanText(token.source_span)}`;
    element.textContent = source.slice(start, end) || token.lexeme;
    element.addEventListener("click", () => selectToken(token.token_id));
    fragment.append(element);
    cursor = end;
  }
  if (cursor < source.length) fragment.append(document.createTextNode(source.slice(cursor)));
  byId("sql-text").replaceChildren(fragment);
}

/** 更新查询元数据、SQL Token 原文以及联动对象计数。 */
function renderHeader(trace) {
  byId("query-number").textContent = `QUERY #${String(trace.query_number).padStart(4, "0")}  ·  ${trace.trace_id}`;
  const status = byId("query-status");
  status.textContent = trace.status;
  status.className = `status ${statusClass(trace.status)}`;
  byId("database-name").textContent = `DATABASE ${trace.database}`;
  byId("source-span").textContent = spanText(trace.source_span);
  byId("elapsed").textContent = `${Number(trace.elapsed_ms).toFixed(3)} ms`;
  const counts = trace.linkage.counts;
  byId("link-summary").textContent = `TOKENS ${counts.tokens} · NODES ${counts.nodes} · PAGES ${counts.pages}`;
  byId("stage-tab-count").textContent = String(trace.stages.length);
  byId("node-tab-count").textContent = String(counts.nodes);
  byId("token-tab-count").textContent = String(counts.tokens);
  byId("page-tab-count").textContent = String(counts.pages);
  renderSqlSource(trace.linkage);
}

/** 切换 STAGE/NODES/TOKENS/PAGES 面板并保留当前联动高亮。 */
function switchView(view) {
  state.view = view;
  document.querySelectorAll(".entity-tab").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  document.querySelectorAll(".entity-pane").forEach((item) => { item.hidden = item.id !== `${view}-pane`; });
}

/** 创建一个阶段按钮，点击只切换本地阶段详情。 */
function stageButton(stage) {
  const button = document.createElement("button");
  button.className = `stage owner-${stage.owner}`;
  button.dataset.stageId = stage.stage_id;
  const index = document.createElement("span");
  index.className = "stage-index";
  index.textContent = String(stage.sequence).padStart(2, "0");
  const names = document.createElement("span");
  const name = document.createElement("span");
  name.className = "stage-name";
  name.textContent = stage.name;
  const owner = document.createElement("span");
  owner.className = "stage-owner";
  owner.textContent = `${stage.owner}  ·  ${stage.status}`;
  names.append(name, owner);
  const dot = document.createElement("span");
  dot.className = `stage-dot stage-dot-${statusClass(stage.status)}`;
  button.append(index, names, dot);
  button.addEventListener("click", () => selectStage(stage.stage_id));
  return button;
}

/** 渲染有序阶段列表，优先保留仍然可见的选中阶段。 */
function renderStages(stages) {
  byId("stage-list").replaceChildren(...stages.map(stageButton));
  byId("stage-count").textContent = String(stages.length);
  if (!stages.some((stage) => stage.stage_id === state.selectedStageId)) {
    state.selectedStageId = stages[0]?.stage_id ?? null;
  }
  if (state.selectedStageId) selectStage(state.selectedStageId, false);
  else showEmptyStage();
}

/** 当筛选结果没有阶段时显示无操作引导占位。 */
function showEmptyStage() {
  byId("empty-detail").hidden = false;
  byId("stage-detail").hidden = true;
}

/** 刷新指定阶段的契约、快照和事件，可选切换到 STAGE 面板。 */
function selectStage(stageId, activateView = true) {
  const stage = state.trace?.stages.find((item) => item.stage_id === stageId);
  if (!stage) return showEmptyStage();
  state.selectedStageId = stageId;
  if (activateView) switchView("stage");
  document.querySelectorAll(".stage").forEach((item) => item.classList.toggle("active", item.dataset.stageId === stageId));
  byId("empty-detail").hidden = true;
  byId("stage-detail").hidden = false;
  byId("detail-owner").textContent = `MODULE ${stage.owner}  ·  STAGE ${stage.sequence}`;
  byId("detail-name").textContent = stage.name;
  const status = byId("detail-status");
  status.textContent = stage.status;
  status.className = `status ${statusClass(stage.status)}`;
  byId("detail-description").textContent = stage.description;
  byId("input-contract").textContent = stage.input_contract || "—";
  byId("output-contract").textContent = stage.output_contract || "—";
  byId("input-snapshot").textContent = pretty(stage.input_snapshot);
  byId("output-snapshot").textContent = pretty(stage.output_snapshot);
  byId("event-count").textContent = String(stage.events.length);
  byId("event-list").replaceChildren(...stage.events.map(eventCard));
}

/** 创建阶段事件卡片；有 SourceSpan 时点击可高亮对应 SQL Token。 */
function eventCard(event) {
  const card = document.createElement("button");
  card.className = "event";
  card.type = "button";
  card.dataset.eventId = event.event_id;
  const sequence = document.createElement("span");
  sequence.className = "event-seq";
  sequence.textContent = String(event.sequence).padStart(2, "0");
  const body = document.createElement("div");
  const action = document.createElement("strong");
  action.textContent = event.action;
  const description = document.createElement("p");
  description.textContent = event.description || spanText(event.source_span);
  body.append(action, description);
  const elapsed = document.createElement("span");
  elapsed.className = "event-time";
  elapsed.textContent = `${Number(event.elapsed_ms).toFixed(3)} ms`;
  card.append(sequence, body, elapsed);
  if (event.source_span) card.addEventListener("click", () => highlightSourceSpan(event.source_span));
  return card;
}

/**
 * 从节点快照中提取一行简短业务说明。
 *
 * 优先显示列限定名、表名、别名、字面量或类型等用户能
 * 直接对应 SQL 的标量。复合字段仍保留在右侧完整快照中，
 * 不在树节点上展开，避免大型计划树被文字淹没。
 */
function nodeSummary(node) {
  const fields = node.snapshot?.fields || {};
  if (typeof fields.name === "string") {
    return typeof fields.qualifier === "string" ? `${fields.qualifier}.${fields.name}` : fields.name;
  }
  if (typeof fields.table === "string") {
    return typeof fields.alias === "string" ? `${fields.table} AS ${fields.alias}` : fields.table;
  }
  if (Object.prototype.hasOwnProperty.call(fields, "value") && ["string", "number", "boolean"].includes(typeof fields.value)) {
    return String(fields.value);
  }
  if (typeof fields.type === "string") return fields.type;
  const tail = String(node.path || "root").split(".").pop();
  return tail === "root" ? "ROOT" : tail;
}

/**
 * 把同一阶段的扁平 node/parent_id 记录组装成可渲染的森林。
 *
 * 只将父节点也存在于当前可见集合的记录连为子节点。
 * 父节点被筛选掉或原数据未指定 parent_id 时，该节点成为根，
 * 从而保证界面不会因为局部快照而丢失节点。
 */
function buildNodeForest(nodes) {
  const visibleIds = new Set(nodes.map((node) => node.node_id));
  const childrenByParent = new Map(nodes.map((node) => [node.node_id, []]));
  const roots = [];
  for (const node of nodes) {
    if (node.parent_id && visibleIds.has(node.parent_id)) {
      childrenByParent.get(node.parent_id).push(node);
    } else {
      roots.push(node);
    }
  }
  return { roots, childrenByParent };
}

/**
 * 创建一个可点击的树节点卡片。
 *
 * 节点卡只展示类型、业务摘要和所属 AST/Plan/Executor；
 * 点击后复用 selectNode 显示原始快照，并联动 SQL Token 与数据页。
 */
function nodeTreeButton(node) {
  const option = NODE_STAGE_OPTIONS.find((item) => item.stageId === node.stage_id);
  const button = document.createElement("button");
  button.type = "button";
  button.className = `node-item tree-node tree-owner-${option?.owner || "C"}`;
  button.dataset.nodeId = node.node_id;
  button.setAttribute("aria-label", `${node.kind}: ${nodeSummary(node)}`);
  const type = document.createElement("span");
  type.className = "tree-node-type";
  type.textContent = option?.short || "NODE";
  const kind = document.createElement("strong");
  kind.textContent = node.kind;
  const summary = document.createElement("small");
  summary.textContent = nodeSummary(node);
  button.append(type, kind, summary);
  button.addEventListener("click", () => selectNode(node.node_id));
  return button;
}

/**
 * 递归生成一个 ``li`` 树分支，子 ``ul`` 交由 CSS 绘制连线。
 *
 * ancestry 记录当前递归路径，即使追踪数据意外出现环，
 * 也会在第一次重复前停止，防止浏览器无限创建 DOM。
 */
function nodeTreeBranch(node, childrenByParent, ancestry = new Set()) {
  const branch = document.createElement("li");
  branch.className = "tree-branch";
  branch.setAttribute("role", "treeitem");
  branch.append(nodeTreeButton(node));
  const nextAncestry = new Set(ancestry);
  nextAncestry.add(node.node_id);
  const children = (childrenByParent.get(node.node_id) || []).filter((child) => !nextAncestry.has(child.node_id));
  if (children.length) {
    branch.setAttribute("aria-expanded", "true");
    const group = document.createElement("ul");
    group.className = "tree-children";
    group.setAttribute("role", "group");
    group.append(...children.map((child) => nodeTreeBranch(child, childrenByParent, nextAncestry)));
    branch.append(group);
  }
  return branch;
}

/**
 * 渲染当前树阶段的根节点、子节点和连线结构。
 *
 * 绘制只使用 linkage 中已有的 parent_id，不重建 AST 或计划。
 * 当 A/B/C 筛选导致当前类型没有节点时，显示明确空状态。
 */
function renderNodeTree(nodes) {
  const visible = nodes.filter((node) => node.stage_id === state.nodeStageId);
  byId("tree-node-count").textContent = String(visible.length);
  const viewport = byId("node-tree");
  if (!visible.length) {
    const empty = document.createElement("div");
    empty.className = "tree-empty";
    empty.textContent = "当前模块没有可视化树节点";
    viewport.replaceChildren(empty);
    return;
  }
  const { roots, childrenByParent } = buildNodeForest(visible);
  const forest = document.createElement("ul");
  forest.className = "tree-roots";
  forest.setAttribute("role", "group");
  forest.append(...roots.map((root) => nodeTreeBranch(root, childrenByParent)));
  viewport.replaceChildren(forest);
}

/**
 * 将 AST、Logical Plan 和 Executor 切换器与可视树同步到最新快照。
 *
 * 三个入口始终保持固定顺序，没有数据的入口保留但禁用，
 * 让用户能区分“尚未实现”和“当前 A/B/C 筛选不可见”。
 */
function renderNodes(nodes) {
  const counts = new Map(NODE_STAGE_OPTIONS.map((option) => [
    option.stageId,
    nodes.filter((node) => node.stage_id === option.stageId).length,
  ]));
  const available = NODE_STAGE_OPTIONS.filter((option) => counts.get(option.stageId) > 0);
  if (!available.some((option) => option.stageId === state.nodeStageId)) {
    state.nodeStageId = available[0]?.stageId ?? null;
  }
  const controls = NODE_STAGE_OPTIONS.map((option) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `tree-stage-button owner-${option.owner}`;
    button.dataset.nodeStageId = option.stageId;
    button.disabled = counts.get(option.stageId) === 0;
    button.classList.toggle("active", option.stageId === state.nodeStageId);
    button.setAttribute("aria-pressed", String(option.stageId === state.nodeStageId));
    const label = document.createElement("span");
    label.textContent = option.label;
    const count = document.createElement("small");
    count.textContent = String(counts.get(option.stageId));
    button.append(label, count);
    button.addEventListener("click", () => {
      state.nodeStageId = option.stageId;
      state.selectedNodeId = null;
      renderNodes(nodes);
      resetNodeDetail("点击树中节点查看详情");
      applyLinkHighlights([], [], []);
    });
    return button;
  });
  byId("node-stage-tabs").replaceChildren(...controls);
  renderNodeTree(nodes);
  const selected = nodes.find((node) => node.node_id === state.selectedNodeId);
  if (!selected || selected.stage_id !== state.nodeStageId) {
    state.selectedNodeId = null;
    resetNodeDetail(available.length ? "点击树中节点查看详情" : "当前筛选没有节点数据");
  }
}

/** 清空节点详情和反向关联，但不修改当前树类型。 */
function resetNodeDetail(message) {
  byId("selected-node-label").textContent = "未选择";
  byId("node-snapshot").textContent = pretty({ "提示": message });
  renderRelationLinks("node-token-links", [], "token");
  renderRelationLinks("node-page-links", [], "page");
}

/** 选中树节点，展示快照并反向高亮相关 Token 和物理页。 */
function selectNode(nodeId, activateView = true) {
  const node = nodeMap().get(nodeId);
  if (!node) return;
  if (state.nodeStageId !== node.stage_id) {
    state.nodeStageId = node.stage_id;
    renderNodes(state.trace?.linkage.nodes || []);
  }
  state.selectedNodeId = nodeId;
  state.selectedTokenId = null;
  state.selectedPageId = null;
  if (activateView) switchView("nodes");
  byId("selected-node-label").textContent = `${node.kind}  ·  ${node.source_link}`;
  byId("node-snapshot").textContent = pretty({ node_id: node.node_id, stage_id: node.stage_id, path: node.path, source_span: node.source_span, snapshot: node.snapshot });
  renderRelationLinks("node-token-links", node.token_ids, "token");
  renderRelationLinks("node-page-links", node.page_ids, "page");
  applyLinkHighlights(node.token_ids, [nodeId], node.page_ids);
}

/** 渲染 Lexer Token 芯片，保留类型、原文和源码顺序。 */
function renderTokens(tokens) {
  const items = tokens.map((token) => {
    const button = document.createElement("button");
    button.className = "token-chip";
    button.dataset.tokenId = token.token_id;
    const lexeme = document.createElement("strong");
    lexeme.textContent = token.lexeme;
    const type = document.createElement("small");
    type.textContent = `${token.type}  ·  ${token.start_offset}:${token.end_offset}`;
    button.append(lexeme, type);
    button.addEventListener("click", () => selectToken(token.token_id));
    return button;
  });
  byId("token-list").replaceChildren(...items);
}

/** 选中 Token，展示偏移和 SourceSpan，并联动所属节点与相关数据页。 */
function selectToken(tokenId, activateView = true) {
  const token = tokenMap().get(tokenId);
  if (!token) return;
  state.selectedTokenId = tokenId;
  state.selectedNodeId = null;
  state.selectedPageId = null;
  if (activateView) switchView("tokens");
  byId("selected-token-label").textContent = `${token.type}  ·  ${spanText(token.source_span)}`;
  byId("token-snapshot").textContent = pretty(token);
  renderRelationLinks("token-node-links", token.node_ids, "node");
  renderRelationLinks("token-page-links", token.page_ids, "page");
  applyLinkHighlights([tokenId], token.node_ids, token.page_ids, tokenId);
}

/** 渲染去重后的物理页卡片，展示表页文件、页号和访问次数。 */
function renderPages(pages) {
  const cards = pages.map((page) => {
    const button = document.createElement("button");
    button.className = "page-card";
    button.dataset.pageId = page.page_id;
    const number = document.createElement("span");
    number.className = "page-number";
    number.textContent = `PAGE ${page.page_number}`;
    const file = document.createElement("span");
    file.className = "page-file";
    file.textContent = page.file_name;
    const operations = document.createElement("span");
    operations.className = "page-ops";
    operations.textContent = `${page.events.length} operations  ·  ${page.stage_ids.join(" / ")}`;
    button.append(number, file, operations);
    button.addEventListener("click", () => selectPage(page.page_id));
    return button;
  });
  byId("page-list").replaceChildren(...cards);
}

/** 选中物理页，展示 B 操作并反向高亮表名 Token 和扫描节点。 */
function selectPage(pageId, activateView = true) {
  const page = pageMap().get(pageId);
  if (!page) return;
  state.selectedPageId = pageId;
  state.selectedNodeId = null;
  state.selectedTokenId = null;
  if (activateView) switchView("pages");
  byId("selected-page-label").textContent = `${page.file_name}  ·  PAGE ${page.page_number}`;
  byId("page-snapshot").textContent = pretty({ page_id: page.page_id, file: page.file, table: page.table, page_number: page.page_number, stage_ids: page.stage_ids });
  renderRelationLinks("page-token-links", page.token_ids, "token");
  renderRelationLinks("page-node-links", page.node_ids, "node");
  byId("page-event-list").replaceChildren(...page.events.map(pageEventCard));
  applyLinkHighlights(page.token_ids, page.node_ids, [pageId]);
}

/** 把一个页访问引用渲染为带组件、操作、命中状态和耗时的事件卡。 */
function pageEventCard(event) {
  const card = document.createElement("div");
  card.className = "event linked";
  const component = document.createElement("span");
  component.className = "event-seq";
  component.textContent = String(event.stage_id || "B").replace("b.", "").toUpperCase();
  const body = document.createElement("div");
  const action = document.createElement("strong");
  action.textContent = event.action;
  const description = document.createElement("p");
  description.textContent = event.cache_outcome ? `${event.description}  ·  cache ${event.cache_outcome}` : event.description;
  body.append(action, description);
  const elapsed = document.createElement("span");
  elapsed.className = "event-time";
  elapsed.textContent = `${Number(event.elapsed_ms).toFixed(3)} ms`;
  card.append(component, body, elapsed);
  return card;
}

/** 用可点击芯片渲染 Token/Node/Page 反向 ID 列表。 */
function renderRelationLinks(containerId, ids, kind) {
  const container = byId(containerId);
  if (!container) return;
  if (!ids?.length) {
    const empty = document.createElement("span");
    empty.className = "relation-empty";
    empty.textContent = "无可确定关联";
    container.replaceChildren(empty);
    return;
  }
  const maps = { token: tokenMap(), node: nodeMap(), page: pageMap() };
  const actions = { token: selectToken, node: selectNode, page: selectPage };
  const buttons = ids.map((id) => {
    const item = maps[kind].get(id);
    const button = document.createElement("button");
    button.className = "relation-link";
    button.textContent = relationLabel(kind, item, id);
    button.addEventListener("click", () => actions[kind](id));
    return button;
  });
  container.replaceChildren(...buttons);
}

/** 为关联芯片生成人类可读且稳定的简短标签。 */
function relationLabel(kind, item, fallback) {
  if (!item) return fallback;
  if (kind === "token") return `${item.lexeme} · ${item.type}`;
  if (kind === "node") return `${item.kind} · ${item.stage_id}`;
  return `${item.file_name} · page ${item.page_number}`;
}

/** 同时刷新 SQL、Node、Token 和 Page 控件的联动高亮。 */
function applyLinkHighlights(tokenIds = [], nodeIds = [], pageIds = [], selectedTokenId = null) {
  const tokenSet = new Set(tokenIds);
  const nodeSet = new Set(nodeIds);
  const pageSet = new Set(pageIds);
  document.querySelectorAll(".sql-token").forEach((item) => {
    item.classList.toggle("linked", tokenSet.has(item.dataset.tokenId));
    item.classList.toggle("selected", item.dataset.tokenId === selectedTokenId);
  });
  document.querySelectorAll(".token-chip").forEach((item) => {
    item.classList.toggle("related", tokenSet.has(item.dataset.tokenId));
    item.classList.toggle("active", item.dataset.tokenId === selectedTokenId);
  });
  document.querySelectorAll(".node-item").forEach((item) => {
    item.classList.toggle("related", nodeSet.has(item.dataset.nodeId));
    item.classList.toggle("active", item.dataset.nodeId === state.selectedNodeId);
  });
  document.querySelectorAll(".page-card").forEach((item) => {
    item.classList.toggle("related", pageSet.has(item.dataset.pageId));
    item.classList.toggle("active", item.dataset.pageId === state.selectedPageId);
  });
}

/** 将事件 SourceSpan 转换为相交 Token 集合并在 SQL 原文中高亮。 */
function highlightSourceSpan(span) {
  const ids = (state.trace?.linkage.tokens || []).filter((token) => spansOverlap(token.source_span, span)).map((token) => token.token_id);
  applyLinkHighlights(ids, [], []);
}

/** 判断两个一基闭区间 SourceSpan 在行列序上是否相交。 */
function spansOverlap(left, right) {
  const lStart = [left.start_line, left.start_col];
  const lEnd = [left.end_line, left.end_col];
  const rStart = [right.start_line, right.start_col];
  const rEnd = [right.end_line, right.end_col];
  const compare = (a, b) => a[0] === b[0] ? a[1] - b[1] : a[0] - b[0];
  return compare(lStart, rEnd) <= 0 && compare(rStart, lEnd) <= 0;
}

/** 清除选中身份和四个视图的联动样式，不更改追踪数据。 */
function clearLinkSelection() {
  state.selectedNodeId = null;
  state.selectedTokenId = null;
  state.selectedPageId = null;
  byId("selected-node-label").textContent = "未选择";
  byId("selected-token-label").textContent = "未选择";
  byId("selected-page-label").textContent = "未选择";
  applyLinkHighlights([], [], []);
}

/** 用服务端返回的完整快照一次性刷新所有视图。 */
function renderTrace(trace) {
  state.trace = trace;
  state.selectedStageId = null;
  state.selectedNodeId = null;
  state.selectedTokenId = null;
  state.selectedPageId = null;
  renderHeader(trace);
  renderStages(trace.stages);
  renderNodes(trace.linkage.nodes);
  renderTokens(trace.linkage.tokens);
  renderPages(trace.linkage.pages);
  switchView(state.view);
}

/** 在不泄露服务端内部错误的前提下显示可恢复的错误卡片。 */
function renderError(message) {
  state.trace = null;
  byId("stage-list").replaceChildren();
  byId("stage-count").textContent = "0";
  const title = document.createElement("h2");
  title.textContent = message;
  const hint = document.createElement("p");
  hint.textContent = "请返回 HELLO-SQL 终端执行一条 SQL，然后再次输入 /inspect。";
  byId("empty-detail").replaceChildren(title, hint);
  switchView("stage");
  showEmptyStage();
}

/** 向同源 API 请求当前模块快照，并处理无历史或服务结束。 */
async function loadTrace() {
  try {
    const response = await fetch(`/api/trace?module=${encodeURIComponent(state.module)}`, { cache: "no-store" });
    if (!response.ok) throw new Error(response.status === 404 ? "暂无可查看的 SQL 追踪" : "无法读取查询追踪");
    renderTrace(await response.json());
  } catch (error) {
    renderError(error.message || "无法读取查询追踪");
  }
}

/** 切换 A/B/C/ALL，同步 URL 且重新读取最近快照，不触发 SQL。 */
function chooseModule(module) {
  state.module = module;
  state.selectedStageId = null;
  state.view = "stage";
  document.querySelectorAll(".filter").forEach((item) => item.classList.toggle("active", item.dataset.module === module));
  const url = new URL(window.location.href);
  url.searchParams.set("module", module);
  window.history.replaceState({}, "", url);
  loadTrace();
}

/** 读取首屏参数，注册模块/对象/清理事件，然后加载第一份快照。 */
function bootstrap() {
  const requested = new URL(window.location.href).searchParams.get("module")?.toUpperCase();
  state.module = ["ALL", "A", "B", "C"].includes(requested) ? requested : "ALL";
  document.querySelectorAll(".filter").forEach((button) => button.addEventListener("click", () => chooseModule(button.dataset.module)));
  document.querySelectorAll(".entity-tab").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
  byId("clear-link").addEventListener("click", clearLinkSelection);
  chooseModule(state.module);
}

bootstrap();
