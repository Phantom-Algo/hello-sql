"use strict";

/**
 * HELLO-SQL 查询流程查看器的联动交互层。
 *
 * 页面只通过同源 GET API 读取 QueryTrace 和服务端生成的 linkage。
 * 点击 AST、Logical Plan、Executor Tree 节点、Token、存储页或带源码范围的事件
 * 只会改变本地高亮和详情面板，不会发送 SQL 或触发任何数据库操作。
 */

const state = {
  module: "ALL",
  trace: null,
  view: "nodes",
  selectedStageId: null,
  nodeStageId: null,
  selectedNodeId: null,
  selectedTokenId: null,
  selectedPageId: null,
  showDebug: false,
  showTechnicalEvents: false,
};

/**
 * 定义节点面板中三棵树的稳定顺序与界面名称。
 *
 * stage_id 与服务端追踪契约一致；short 只用于树节点的
 * 简短类型标识，不改变原节点数据。
 */
const NODE_STAGE_OPTIONS = [
  { stageId: "a.ast", label: "AST", short: "AST", owner: "A" },
  { stageId: "c.logical_plan", label: "Logical Plan", short: "Plan", owner: "C" },
  { stageId: "c.executor", label: "Executor Tree", short: "Executor", owner: "C" },
];

/**
 * 阶段稳定 ID 到界面技术名称的映射。
 *
 * Lexer、Parser、AST 等名称与代码模块、课堂术语和答辩材料直接对应，
 * 因此保留英文；阶段职责、操作说明和状态仍使用中文，不修改服务端契约。
 */
const STAGE_DISPLAY_NAMES = {
  "c.repl": "REPL",
  "a.lexer": "Lexer",
  "a.parser": "Parser",
  "a.ast": "AST",
  "a.source_span": "SourceSpan",
  "b.catalog": "Catalog",
  "b.cache": "Buffer Cache",
  "b.pager": "Pager",
  "b.engine": "Storage Engine",
  "c.binding": "Binder",
  "c.logical_plan": "Logical Plan",
  "c.optimizer": "Optimizer",
  "c.executor": "Executor Tree",
  "c.runtime": "Runtime",
};

/** 为各阶段提供中文职责说明，不改写追踪快照中的原始字段。 */
const STAGE_DISPLAY_DESCRIPTIONS = {
  "c.repl": "接收用户 SQL 缓冲区并建立查询追踪身份。",
  "a.lexer": "从左到右扫描 SQL，生成带位置和偏移的词法单元流。",
  "a.parser": "通过递归下降规则消费词法单元，并按优先级构建语句结构。",
  "a.ast": "展示语法分析实际构建的语句、索引、表、列和表达式节点树。",
  "a.source_span": "将每条抽象语法树映射回完整 SQL 脚本中的原文与全局行列。",
  "b.catalog": "维护用户表结构，并通过页式系统表持久化目录。",
  "b.cache": "展示页帧命中、缺页读盘、固定、脏页、淘汰与写回。",
  "b.pager": "管理页号、首页、空闲页链表以及固定大小数据页读写。",
  "b.engine": "执行数据行插入、扫描、定位、更新、删除与溢出页链操作。",
  "c.binding": "解析表、列、限定符、类型、谓词与投影顺序。",
  "c.logical_plan": "根据绑定后的语句构建不可变逻辑计划树。",
  "c.optimizer": "当前版本尚未实现逻辑优化规则，该阶段为后续扩展保留。",
  "c.executor": "将逻辑计划转换为查询、数据修改或定义语句执行树。",
  "c.runtime": "记录拉取式算子产出行、样例、耗时与最终结果。",
};

/** 为阶段输入输出契约提供中文显示文本。 */
const STAGE_DISPLAY_CONTRACTS = {
  "c.repl": ["终端 SQL 缓冲区", "查询身份与源码位置"],
  "a.lexer": ["SQL 原始文本", "以输入结束哨兵收尾的词法单元流"],
  "a.parser": ["词法分析生成的完整词法单元流", "单条语句或带原文范围的脚本语法树"],
  "a.ast": ["语法分析生成的语句或脚本", "可视化抽象语法树"],
  "a.source_span": ["带原文的已解析语句列表", "每条语句的 SQL 原文与全局源码范围"],
  "b.catalog": ["系统目录调用参数", "表结构或系统表结果"],
  "b.cache": ["文件与数据页键", "缓存页帧"],
  "b.pager": ["数据页操作", "页号或页字节"],
  "b.engine": ["数据行值或行编号", "行结果或修改结果"],
  "c.binding": ["抽象语法树语句与系统目录表结构", "已绑定的表结构与表达式"],
  "c.logical_plan": ["已绑定语句", "逻辑计划树"],
  "c.optimizer": ["逻辑计划树", "优化后的逻辑计划树"],
  "c.executor": ["逻辑计划树", "语句执行树"],
  "c.runtime": ["执行树与执行上下文", "查询结果与算子统计"],
};

/** 把追踪契约中的状态值转换为中文，CSS 状态类仍使用原值。 */
function statusText(status) {
  return {
    SUCCESS: "成功",
    FAILED: "失败",
    SKIPPED: "已跳过",
    DISABLED: "未启用",
    RUNNING: "运行中",
  }[String(status || "").toUpperCase()] || String(status || "未知");
}

/** 返回阶段的中文显示名，未知阶段回退到服务端名称。 */
function stageDisplayName(stage) {
  return STAGE_DISPLAY_NAMES[stage.stage_id] || stage.name;
}

/** 返回阶段的中文职责，并对未运行阶段补充跳过说明。 */
function stageDescriptionText(stage) {
  const description = STAGE_DISPLAY_DESCRIPTIONS[stage.stage_id] || stage.description || `模块 ${stage.owner}`;
  return String(stage.status).toUpperCase() === "SKIPPED"
    ? `${description}本次语句未运行该阶段。`
    : description;
}

/** 按输入或输出方向读取中文契约，未配置时保留原契约。 */
function stageContractText(stage, direction) {
  const contracts = STAGE_DISPLAY_CONTRACTS[stage.stage_id];
  if (contracts) return contracts[direction === "input" ? 0 : 1];
  return direction === "input" ? stage.input_contract || "—" : stage.output_contract || "—";
}

/** 将联动来源的内部枚举值转换为用户能直接理解的中文。 */
function sourceLinkText(sourceLink) {
  return {
    explicit: "精确源码定位",
    token_inference: "词法单元推导",
    none: "无源码关联",
  }[sourceLink] || sourceLink || "未知";
}

/** 追踪内部操作名到中文动作的映射，原始操作名仍保留在 JSON 中。 */
const OPERATION_DISPLAY_NAMES = {
  "binding.bind_source": "绑定数据源",
  "binding.bind_source_range": "绑定数据源范围",
  "binding.bind_filter": "绑定过滤条件",
  "binding.bind_projection": "绑定投影列",
  "binding.bind_assignments": "绑定更新赋值",
  "binding.bind_insert": "绑定插入语句",
  "binding.bind_select": "绑定查询语句",
  "binding.bind_update": "绑定更新语句",
  "binding.bind_delete": "绑定删除语句",
  "logical_plan.build_plan": "构建逻辑计划",
  "executor.build_executor_tree": "构建执行树",
  "runtime.seq_scan.rows": "顺序扫描数据行",
  "runtime.filter.rows": "过滤数据行",
  "runtime.nested_loop_join.rows": "执行嵌套循环连接",
  "runtime.projection.rows": "生成投影结果行",
  "runtime.select.execute": "执行查询",
  "runtime.insert.execute": "执行插入",
  "runtime.update.execute": "执行更新",
  "runtime.delete.execute": "执行删除",
  "runtime.create_table.execute": "执行创建表",
  "runtime.drop_table.execute": "执行删除表",
  "runtime.create_database.execute": "执行创建数据库",
  "runtime.drop_database.execute": "执行删除数据库",
  "runtime.use_database.execute": "执行切换数据库",
  "catalog.load": "加载系统目录",
  "catalog.register": "注册表结构",
  "catalog.unregister": "删除表结构",
  "catalog.get": "查询表结构",
  "catalog.names": "读取对象名称",
  "catalog.flush": "刷新系统目录",
  "catalog.insert_system_rows": "写入系统表记录",
  "catalog.delete_system_rows": "删除系统表记录",
  "catalog.restore_table": "恢复表结构",
  "catalog.cleanup_rows_by_name": "按名称清理系统表记录",
  "catalog.rollback_system_rows": "回滚系统表记录",
  "cache.disk_read": "从磁盘读取数据页",
  "cache.disk_write": "将数据页写回磁盘",
  "cache.evict_lru": "淘汰最近最少使用页",
  "cache.get_page": "获取缓存数据页",
  "cache.unpin_page": "解除数据页固定",
  "cache.mark_dirty": "标记数据页已修改",
  "cache.flush": "刷新缓存",
  "cache.discard": "丢弃缓存页",
  "cache.reset_stats": "重置缓存统计",
  "pager.page_count": "统计数据页",
  "pager.free_pages": "读取空闲页链表",
  "pager.alloc_page": "分配数据页",
  "pager.free_page": "释放数据页",
  "pager.read_page": "读取数据页",
  "pager.write_page": "写入数据页",
  "engine.take_next_row_id": "分配下一个行编号",
  "engine.find_page_for_record": "查找记录可用页",
  "engine.active_page_numbers": "读取活动数据页",
  "engine.write_or_free_page": "写入或释放数据页",
  "engine.locate_row": "定位数据行",
  "engine.alloc_overflow_chain": "分配溢出页链",
  "engine.collect_overflow_chain": "收集溢出页链",
  "engine.read_overflow_record": "读取溢出记录",
  "engine.free_overflow_chain": "释放溢出页链",
  "engine.place_record": "写入一条记录",
  "engine.insert": "插入数据行",
  "engine.scan": "扫描数据表",
  "engine.update": "更新数据行",
  "engine.delete": "删除数据行",
};

/** 将事件的内部操作名转换为中文，A 已是中文的动作原样返回。 */
function eventActionText(action) {
  return OPERATION_DISPLAY_NAMES[action] || action;
}

/** 翻译 C 追踪的通用结果说明，业务错误原文不做改写。 */
function eventDescriptionText(description) {
  return {
    "The call completed successfully.": "该操作已成功完成。",
    "The call failed and preserved its original error.": "该操作执行失败，已保留原始错误。",
    "The row stream closed before full consumption.": "数据行流在完全消费前提前关闭。",
  }[description] || description;
}

/** 按 DOM id 取得已在 HTML 中声明的固定元素。 */
const byId = (id) => document.getElementById(id);

/** 将任意快照值格式化为带缩进的稳定 JSON 文本。 */
function pretty(value) {
  return JSON.stringify(value ?? {}, null, 2);
}

/**
 * 将原始 JSON 区域统一切换为显示或隐藏状态。
 *
 * 页面默认只展示阶段产物和结构化摘要；开启后通过 body 状态类显示所有
 * ``debug-only`` 区域。按钮的文字、标题和 aria-pressed 会同步更新，
 * 因此鼠标、键盘和辅助技术得到相同的开关状态。
 */
function setDebugVisibility(enabled) {
  state.showDebug = Boolean(enabled);
  document.body.classList.toggle("show-debug", state.showDebug);
  const button = byId("debug-toggle");
  button.classList.toggle("active", state.showDebug);
  button.setAttribute("aria-pressed", String(state.showDebug));
  button.querySelector("b").textContent = state.showDebug ? "开启" : "关闭";
  button.title = state.showDebug ? "隐藏原始 JSON 数据" : "显示原始 JSON 数据";
}

/**
 * 响应顶部“原始数据”按钮，反转本次页面会话的调试数据可见状态。
 *
 * 该操作只改变浏览器中的样式，不请求新追踪、不重新执行 SQL，也不会
 * 修改 QueryTrace；切换 A/B/C 模块后仍保持用户当前选择。
 */
function toggleDebugVisibility() {
  setDebugVisibility(!state.showDebug);
}

/**
 * 将对象的关键标量字段渲染为精简信息卡，而不是要求用户阅读 JSON。
 *
 * ``facts`` 使用 ``[标签, 值]`` 二元组；空值会被过滤，数字 0 和布尔值
 * 会正常显示。所有内容均通过 textContent 写入，避免追踪内容被当作 HTML。
 */
function renderFacts(containerId, facts) {
  const cards = facts
    .filter(([, value]) => value !== undefined && value !== null && value !== "")
    .map(([label, value]) => {
      const card = document.createElement("div");
      card.className = "fact-item";
      const name = document.createElement("span");
      name.textContent = label;
      const content = document.createElement("strong");
      content.textContent = String(value);
      card.append(name, content);
      return card;
    });
  byId(containerId).replaceChildren(...cards);
}

/** 根据 TraceStatus 返回 CSS 状态类，未知值使用普通样式。 */
function statusClass(status) {
  return String(status || "").toLowerCase();
}

/** 把可选源码范围转换为一基行列范围文本。 */
function spanText(span) {
  if (!span) return "源码范围 —";
  return `源码范围 ${span.start_line}:${span.start_col}–${span.end_line}:${span.end_col}`;
}

/** 建立当前快照的词法单元 ID 到词法单元记录的映射。 */
function tokenMap() {
  return new Map((state.trace?.linkage.tokens || []).map((item) => [item.token_id, item]));
}

/** 建立当前快照的节点 ID 到节点记录的映射。 */
function nodeMap() {
  return new Map((state.trace?.linkage.nodes || []).map((item) => [item.node_id, item]));
}

/** 建立当前快照的数据页 ID 到数据页记录的映射。 */
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

/** 使用词法分析器的真实半开偏移将完整 SQL 原文渲染为可点击词法单元。 */
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

/** 更新查询元数据、SQL 词法单元原文以及联动对象计数。 */
function renderHeader(trace) {
  byId("query-number").textContent = `查询 #${String(trace.query_number).padStart(4, "0")}`;
  byId("query-trace").textContent = `追踪 ${trace.trace_id}`;
  const status = byId("query-status");
  status.textContent = statusText(trace.status);
  status.className = `status ${statusClass(trace.status)}`;
  byId("database-name").textContent = `数据库 ${trace.database}`;
  byId("source-span").textContent = spanText(trace.source_span);
  byId("elapsed").textContent = `${Number(trace.elapsed_ms).toFixed(3)} ms`;
  const counts = trace.linkage.counts;
  byId("link-summary").textContent = `词法单元 ${counts.tokens} · 节点 ${counts.nodes} · 数据页 ${counts.pages}`;
  byId("stage-tab-count").textContent = String(trace.stages.length);
  byId("node-tab-count").textContent = String(counts.nodes);
  byId("token-tab-count").textContent = String(counts.tokens);
  byId("page-tab-count").textContent = String(counts.pages);
  renderSqlSource(trace.linkage);
}

/** 切换阶段、节点、词法单元和数据页面板，并保留当前联动高亮。 */
function switchView(view) {
  state.view = view;
  document.querySelectorAll(".entity-tab").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  document.querySelectorAll(".entity-pane").forEach((item) => { item.hidden = item.id !== `${view}-pane`; });
}

/**
 * 创建一个精简阶段卡片，展示序号、职责、状态与耗时。
 *
 * 首屏不再重复显示负责人和原始快照；阶段说明帮助用户快速理解流程，
 * 完整契约与事件仍可通过点击卡片进入阶段面板查看。
 */
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
  name.textContent = stageDisplayName(stage);
  const description = document.createElement("span");
  description.className = "stage-description";
  description.textContent = stageDescriptionText(stage);
  names.append(name, description);
  const result = document.createElement("span");
  result.className = "stage-result";
  const resultLine = document.createElement("span");
  resultLine.className = "stage-result-line";
  const dot = document.createElement("span");
  dot.className = `stage-dot stage-dot-${statusClass(stage.status)}`;
  const status = document.createElement("span");
  status.textContent = statusText(stage.status);
  resultLine.append(dot, status);
  const elapsed = document.createElement("span");
  elapsed.className = "stage-time";
  elapsed.textContent = `${Number(stage.elapsed_ms).toFixed(2)} ms`;
  result.append(resultLine, elapsed);
  button.append(index, names, result);
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

/**
 * 读取语法分析器追踪事件的展示层级。
 *
 * 新版 A 追踪会在 input_snapshot 中标注 semantic/technical；
 * 历史追踪没有该字段时默认按主要语法步骤展示，
 * 保证页面与旧缓存记录兼容。
 */
function eventDetailLevel(event) {
  return event.input_snapshot?.detail_level === "technical" ? "technical" : "semantic";
}

/**
 * 渲染当前阶段的教学事件，并为语法分析器折叠底层游标操作。
 *
 * 语法分析器默认仅显示可回答“这一步构建了什么”的语义步骤；
 * advance、expect 和标识符规范化仍保留真实顺序，由用户按需展开。
 * 其他阶段不进行过滤，避免改变 B/C 的展示逻辑。
 */
function renderStageEvents(stage) {
  const isParser = stage.stage_id === "a.parser";
  const technical = isParser
    ? stage.events.filter((event) => eventDetailLevel(event) === "technical")
    : [];
  const semantic = isParser
    ? stage.events.filter((event) => eventDetailLevel(event) === "semantic")
    : stage.events;
  const visible = isParser && !state.showTechnicalEvents
    ? semantic
    : stage.events;
  const toolbar = byId("event-toolbar");
  toolbar.hidden = !isParser || technical.length === 0;
  byId("event-summary").textContent = `主要步骤 ${semantic.length} · 底层步骤 ${technical.length}`;
  byId("technical-event-count").textContent = String(technical.length);
  const toggle = byId("technical-events-toggle");
  toggle.setAttribute("aria-expanded", String(state.showTechnicalEvents));
  toggle.firstChild.textContent = state.showTechnicalEvents ? "收起底层步骤 " : "展开底层步骤 ";
  byId("event-count").textContent = String(visible.length);
  byId("event-list").replaceChildren(...visible.map(eventCard));
}

/**
 * 切换语法分析器底层步骤的可见状态，不重新请求或执行 SQL。
 *
 * 展开后事件仍按服务端记录的真实调用顺序混合渲染，
 * 而不是将 technical 事件错误地追加到列表末尾。
 */
function toggleTechnicalEvents() {
  const stage = state.trace?.stages.find((item) => item.stage_id === state.selectedStageId);
  if (!stage || stage.stage_id !== "a.parser") return;
  state.showTechnicalEvents = !state.showTechnicalEvents;
  renderStageEvents(stage);
}

/** 刷新指定阶段的契约、快照和事件，可选切换到 STAGE 面板。 */
function selectStage(stageId, activateView = true) {
  const stage = state.trace?.stages.find((item) => item.stage_id === stageId);
  if (!stage) return showEmptyStage();
  if (state.selectedStageId !== stageId) state.showTechnicalEvents = false;
  state.selectedStageId = stageId;
  if (activateView) switchView("stage");
  document.querySelectorAll(".stage").forEach((item) => item.classList.toggle("active", item.dataset.stageId === stageId));
  byId("empty-detail").hidden = true;
  byId("stage-detail").hidden = false;
  byId("detail-owner").textContent = `模块 ${stage.owner}  ·  阶段 ${stage.sequence}`;
  byId("detail-name").textContent = stageDisplayName(stage);
  const status = byId("detail-status");
  status.textContent = statusText(stage.status);
  status.className = `status ${statusClass(stage.status)}`;
  byId("detail-description").textContent = stageDescriptionText(stage);
  byId("input-contract").textContent = stageContractText(stage, "input");
  byId("output-contract").textContent = stageContractText(stage, "output");
  byId("input-snapshot").textContent = pretty(stage.input_snapshot);
  byId("output-snapshot").textContent = pretty(stage.output_snapshot);
  renderStageEvents(stage);
}

/** 创建阶段事件卡片；有 SourceSpan 时点击可高亮对应 SQL Token。 */
function eventCard(event) {
  const card = document.createElement("button");
  const detailLevel = eventDetailLevel(event);
  card.className = `event event-${detailLevel}`;
  card.type = "button";
  card.dataset.eventId = event.event_id;
  const depth = event.metrics?.depth;
  const consumed = event.metrics?.consumed_token_count;
  if (depth !== undefined || consumed !== undefined) {
    card.title = `递归深度 ${depth ?? "—"} · 消费词法单元 ${consumed ?? "—"}`;
  }
  const sequence = document.createElement("span");
  sequence.className = "event-seq";
  sequence.textContent = String(event.sequence).padStart(2, "0");
  const body = document.createElement("div");
  const action = document.createElement("strong");
  action.textContent = eventActionText(event.action);
  const description = document.createElement("p");
  description.textContent = eventDescriptionText(event.description) || spanText(event.source_span);
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
 * 优先显示索引定义、列限定名、表名、别名、字面量或类型等用户能
 * 直接对应 SQL 的标量。复合字段仍保留在右侧完整快照中，
 * 不在树节点上展开，避免大型计划树被文字淹没。
 */
function nodeSummary(node) {
  const fields = node.snapshot?.fields || {};
  if (node.kind === "CreateIndexStmt" && typeof fields.index_name === "string") {
    const target = typeof fields.table === "string" && typeof fields.column === "string"
      ? `${fields.table}.${fields.column}`
      : "未知目标";
    return `${fields.index_name} → ${target}`;
  }
  if (node.kind === "DropIndexStmt" && typeof fields.index_name === "string") {
    return fields.index_name;
  }
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
  return tail === "root" ? "根节点" : tail;
}

/**
 * 从节点追踪记录中选取适合常驻展示的字段。
 *
 * 类型、摘要、树路径与源码位置始终优先；AST/计划快照中的简单标量最多
 * 补充四项，数组和嵌套对象仍只在用户开启“原始数据”后以 JSON 展示。
 */
function nodeFacts(node) {
  const facts = [
    ["节点类型", node.kind],
    ["摘要", nodeSummary(node)],
    ["树路径", node.path || "根节点"],
    ["源码范围", spanText(node.source_span).replace(/^源码范围\s*/, "")],
  ];
  const fields = node.snapshot?.fields || {};
  let appended = 0;
  for (const [key, value] of Object.entries(fields)) {
    if (!["string", "number", "boolean"].includes(typeof value)) continue;
    const label = {
      index_name: "索引名",
      table: "表名",
      column: "列名",
      name: "名称",
      qualifier: "限定符",
      alias: "别名",
      value: "值",
      type: "类型",
      op: "运算符",
    }[key] || key;
    if (facts.some(([existing]) => existing === label)) continue;
    facts.push([label, value]);
    appended += 1;
    if (appended === 4) break;
  }
  return facts;
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
 * 节点卡只展示类型、业务摘要和所属语法树、计划或执行树；
 * 点击后复用节点选中逻辑显示原始快照，并联动 SQL 词法单元与数据页。
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
  type.textContent = option?.short || "节点";
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
 * 将抽象语法树、逻辑计划和执行树切换器与可视树同步到最新快照。
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
  renderFacts("node-facts", [["提示", message]]);
  byId("node-snapshot").textContent = pretty({ "提示": message });
  byId("node-inspector").open = false;
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
  byId("selected-node-label").textContent = `${node.kind}  ·  ${sourceLinkText(node.source_link)}`;
  byId("node-inspector").open = true;
  renderFacts("node-facts", nodeFacts(node));
  byId("node-snapshot").textContent = pretty({ node_id: node.node_id, stage_id: node.stage_id, path: node.path, source_span: node.source_span, snapshot: node.snapshot });
  renderRelationLinks("node-token-links", node.token_ids, "token");
  renderRelationLinks("node-page-links", node.page_ids, "page");
  applyLinkHighlights(node.token_ids, [nodeId], node.page_ids);
}

/** 渲染词法单元芯片，保留类型、原文和源码顺序。 */
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

/** 选中词法单元，展示字符偏移和源码范围，并联动所属节点与相关数据页。 */
function selectToken(tokenId, activateView = true) {
  const token = tokenMap().get(tokenId);
  if (!token) return;
  state.selectedTokenId = tokenId;
  state.selectedNodeId = null;
  state.selectedPageId = null;
  if (activateView) switchView("tokens");
  byId("selected-token-label").textContent = `${token.type}  ·  ${spanText(token.source_span)}`;
  byId("token-inspector").open = true;
  renderFacts("token-facts", [
    ["原始文本", token.lexeme],
    ["词法单元类型", token.type],
    ["源码范围", spanText(token.source_span).replace(/^源码范围\s*/, "")],
    ["字符偏移", `${token.start_offset}–${token.end_offset}`],
  ]);
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
    number.textContent = `数据页 ${page.page_number}`;
    const file = document.createElement("span");
    file.className = "page-file";
    file.textContent = page.file_name;
    const operations = document.createElement("span");
    operations.className = "page-ops";
    operations.textContent = `${page.events.length} 次操作  ·  ${page.stage_ids.map((id) => STAGE_DISPLAY_NAMES[id] || id).join(" / ")}`;
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
  byId("selected-page-label").textContent = `${page.file_name}  ·  数据页 ${page.page_number}`;
  byId("page-inspector").open = true;
  renderFacts("page-facts", [
    ["数据页号", page.page_number],
    ["所属表", page.table || "—"],
    ["文件", page.file_name],
    ["操作次数", page.events.length],
  ]);
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
  component.textContent = STAGE_DISPLAY_NAMES[event.stage_id] || "存储";
  const body = document.createElement("div");
  const action = document.createElement("strong");
  action.textContent = eventActionText(event.action);
  const description = document.createElement("p");
  const cacheOutcome = { hit: "缓存命中", miss: "缓存未命中" }[event.cache_outcome];
  description.textContent = cacheOutcome
    ? `${eventDescriptionText(event.description)}  ·  ${cacheOutcome}`
    : eventDescriptionText(event.description);
  body.append(action, description);
  const elapsed = document.createElement("span");
  elapsed.className = "event-time";
  elapsed.textContent = `${Number(event.elapsed_ms).toFixed(3)} ms`;
  card.append(component, body, elapsed);
  return card;
}

/** 用可点击芯片渲染词法单元、节点和数据页的反向 ID 列表。 */
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
  if (kind === "node") return `${item.kind} · ${STAGE_DISPLAY_NAMES[item.stage_id] || item.stage_id}`;
  return `${item.file_name} · 数据页 ${item.page_number}`;
}

/** 同时刷新 SQL、节点、词法单元和数据页控件的联动高亮。 */
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

/** 将事件源码范围转换为相交词法单元集合，并在 SQL 原文中高亮。 */
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
  byId("node-inspector").open = false;
  byId("token-inspector").open = false;
  byId("page-inspector").open = false;
  applyLinkHighlights([], [], []);
}

/** 用服务端返回的完整快照一次性刷新所有视图。 */
function renderTrace(trace) {
  state.trace = trace;
  state.selectedStageId = null;
  state.selectedNodeId = null;
  state.selectedTokenId = null;
  state.selectedPageId = null;
  state.showTechnicalEvents = false;
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
  state.view = "nodes";
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
  byId("debug-toggle").addEventListener("click", toggleDebugVisibility);
  byId("technical-events-toggle").addEventListener("click", toggleTechnicalEvents);
  setDebugVisibility(false);
  chooseModule(state.module);
}

bootstrap();
