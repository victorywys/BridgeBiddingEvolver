// Source entry; npm run build produces the app.mjs served by the viewer.
const $ = (selector) => document.querySelector(selector);
const symbols = { S: "♠", H: "♥", D: "♦", C: "♣", NT: "NT" };
const seats = ["N", "E", "S", "W"];
const seatNames = { N: "北", E: "东", S: "南", W: "西" };
const vulNames = { none: "双方无局", ns: "南北有局", ew: "东西有局", both: "双方有局" };
const state = { catalog: null, bundle: null, filtered: [], activeId: null, limit: 80, trace: [0, 0], comparison: null, interactive: false, annotations: {}, loadId: 0, loadController: null, loadingRun: null, lastRunLoad: null, auditPoll: null, auditJob: null };
state.humanCache = new Map();
state.humanLoading = new Set();
state.humanRequests = new Map();
state.homeJobs = [];
state.homeEvolutions = [];
state.ddsCache = new Map();
state.ddsPending = new Set();
state.ddsErrors = new Map();
state.boardDetails = new Map();
state.boardErrors = new Map();
state.library = { entries: [], categories: [] };
state.librarySelected = new Set();
let toastTimer;
try { state.annotations = JSON.parse(localStorage.getItem("bridge-review.annotations.v1") || "{}"); } catch { state.annotations = {}; }

function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}
function icons() { globalThis.lucide?.createIcons({ attrs: { "aria-hidden": "true" } }); }
function sign(value) { return value == null ? "—" : value > 0 ? `+${value}` : String(value); }
function tone(value) { return value > 0 ? "positive" : value < 0 ? "negative" : "neutral"; }
function recordSize(value) { return value >= 1e6 ? `${(value / 1e6).toFixed(2)} MB` : `${(value / 1e3).toFixed(1)} KB`; }
function shortSystem(name) {
  const version = String(name || "").match(/v\d+(?:\.\d+)*/)?.[0] || "";
  if (String(name).includes("CCBA")) return `CCBA ${version}`.trim();
  if (String(name).includes("精确")) return `精确 ${version}`.trim();
  return name || "未指定体系";
}
function callMarkup(call) {
  if (call === "P") return "P";
  const match = /^(\d)(C|D|H|S|NT)$/.exec(call || "");
  return match ? `${match[1]}<span class="bid-suit ${["H", "D"].includes(match[2]) ? "red-suit" : "black-suit"}">${symbols[match[2]]}</span>` : escapeHTML(call);
}
function contractText(table, result = false) {
  if (table?.error) return "执行失败";
  const contract = table?.contract;
  if (!contract) return "未计分";
  if (contract.passed_out) return "全 Pass";
  const doubling = { 1: "", 2: "X", 4: "XX" }[contract.doubling] || "";
  let outcome = "";
  if (result && table.tricks != null) {
    const delta = table.tricks - contract.level - 6;
    outcome = delta === 0 ? " =" : ` ${sign(delta)}`;
  }
  return `${contract.level}${symbols[contract.strain]}${doubling} · ${contract.declarer}${outcome}`;
}
function currentBase() { return state.boardDetails.get(state.activeId) || state.bundle?.boards.find((board) => board.id === state.activeId); }
function current() { return state.interactive && state.comparison?.sourceId === state.activeId ? state.comparison.record : currentBase(); }
function annotation(board) { return state.annotations[board.id] || board.review || { status: "unreviewed", note: "" }; }
function purposeName(purpose) {
  return { "development-smoke": "测试审计", "development-audit": "批量审计", "single-board-audit": "单副审计", "custom-deals": "自定义", "batch-audit": "批量审计", evolution: "体系自进化" }[purpose] || "历史审计";
}
function findingName(finding) {
  return {
    "stale-export": "常规导出仍待同步。",
    "known-old-defect": "旧导出的已知反例仍可复现。",
    "release-validation-gap": "全量开发审计、完整回归与正式测试尚未完成。",
    "artificial-splinter-terminal-3nt": "Splinter 人工3NT被错误停叫",
    "precision-shortness-terminal-suit": "精确高花短套答叫被当作终局",
  }[finding.id] || finding.description;
}
function boardAuditFindings(board) {
  if (board.audit_result?.findings) return board.audit_result.findings;
  return (state.bundle?.evaluation?.audit?.findings || []).filter((finding) =>
    finding.cases?.some((item) => item.index === board.index && item.family_id === board.family_id));
}
function boardAuditErrors(board) { return board.audit_result?.errors || []; }
function auditErrorMarkup(error) {
  const stages = { auction: "叫牌", dds: "DDS / Par", score: "计分", scoring: "计分", checks: "审计检查", execution: "执行" };
  return `<strong>${escapeHTML(error.table || "整副")} · ${escapeHTML(stages[error.stage] || error.stage)}</strong>：${escapeHTML(error.message)}`;
}
function toast(message, error = false) {
  clearTimeout(toastTimer);
  const target = $("#toast");
  target.textContent = message;
  target.classList.toggle("error", error);
  target.hidden = false;
  toastTimer = setTimeout(() => { target.hidden = true; }, error ? 9000 : 3500);
}
async function fetchJSON(url, options, onResponse) {
  const response = await fetch(url, options);
  onResponse?.(response);
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `请求失败：${response.status}`);
  return value;
}
function busy(value) {
  $("#main").setAttribute("aria-busy", String(value));
  $("#compare-button").disabled = value || !currentBase();
  $("#audit-button").disabled = value || !currentBase();
  $("#run-status").textContent = value ? "载入中" : "已加载";
  $("#run-status").dataset.status = value ? "loading" : "completed";
  if (value) $("#run-summary").hidden = true;
}
function renderRunSummary(bundle) {
  const evaluation = bundle.evaluation;
  const summary = evaluation?.summary;
  const audit = evaluation?.audit;
  const contracts = audit?.metrics?.strict_semantic_contracts;
  const strictCount = typeof contracts === "number" ? contracts : contracts?.consistent;
  if (summary && audit && (!Number.isInteger(strictCount) || strictCount < 0
      || !Number.isInteger(audit.metrics?.bound_checked_decisions) || audit.metrics.bound_checked_decisions < 0)) {
    throw new Error("审计检查数无效，未展示通过结论。");
  }
  $("#run-audit-findings").hidden = true;
  $("#run-audit-findings").replaceChildren();
  $("#run-summary").hidden = !summary;
  $("#run-slam-summary").hidden = true;
  if (!summary) return;
  const fixedImp = (value) => `${value > 0 ? "+" : ""}${value.toFixed(3)}`;
  $("#run-purpose").textContent = `${bundle.statistics.boards}副`;
  $("#run-score-perspective").textContent = `${shortSystem(bundle.systems.A.name)} 相对 ${shortSystem(bundle.systems.B.name)} · ${summary.audited_boards != null ? `有效配对 ${summary.scored_boards}/${summary.audited_boards} 副` : "每副两桌配对"}`;
  $("#run-total-imp").textContent = sign(summary.total_imp);
  $("#run-mean-imp").textContent = fixedImp(summary.mean_imp);
  $("#run-ci95").textContent = `[${summary.ci95.map(fixedImp).join(", ")}]`;
  $("#run-outcomes").textContent = `${summary.a_wins} / ${summary.b_wins} / ${summary.ties}`;
  $("#run-audit-summary").hidden = true;
  $("#run-technical").textContent = JSON.stringify({ summary, audit: audit ? {
    status: audit.status, metrics: audit.metrics, release_acceptance: audit.release_acceptance,
  } : null }, null, 2);
  if (audit) {
    if (audit.slam) {
      const slam = audit.slam;
      $("#run-slam-summary").hidden = false;
      $("#run-slam-summary").innerHTML = `<h3>满贯 · ${slam.tables}桌</h3><div class="slam-metrics"><span>小满贯：叫 ${slam.called.small} / 成 ${slam.made.small} / 宕 ${slam.failed.small}</span><span>大满贯：叫 ${slam.called.grand} / 成 ${slam.made.grand} / 宕 ${slam.failed.grand}</span></div><p>DDS可成：小满贯 ${slam.unique_families_with_small_slam} 副，大满贯 ${slam.unique_families_with_grand_slam} 副。漏叫机会：小满贯 ${slam.opportunities.small_not_bid}、大满贯 ${slam.opportunities.grand_not_bid}（按桌次和定约方统计）。</p><div class="report-actions"><button class="button small" data-slam-filter="called">只看叫贯</button><button class="button small" data-slam-filter="missed-small">只看漏叫小满贯</button><button class="button small" data-slam-filter="missed-grand">只看漏叫大满贯</button></div>`;
      if (audit.games) {
        const games = audit.games;
        const paragraph = document.createElement("p");
        paragraph.textContent = `进局：叫到 ${games.called} 桌，打成 ${games.made}、宕约 ${games.failed}；漏叫机会 ${games.opportunities.not_bid}。邀局 ${games.signals.game_invitations || 0} 次，满贯探索 ${games.signals.slam_explorations || 0} 次。`;
        $("#run-slam-summary").append(paragraph);
      }
    }
    const cases = audit.findings.filter((finding) => finding.cases?.length);
    if (cases.length) {
      $("#run-audit-findings").hidden = false;
      $("#run-audit-findings").innerHTML = cases.map((finding) => {
        const numbers = [...new Set(finding.cases.map((item) => item.board_number))].sort((a, b) => a - b);
        return `<div class="audit-finding"><p class="audit-finding-heading"><strong>${escapeHTML(findingName(finding))}</strong><span>${finding.affected_tables} 桌 / ${numbers.length} 副</span></p><div class="audit-board-links">${numbers.map((number) => `<button type="button" class="button small" data-audit-board="${number}">第 ${number} 副</button>`).join("")}</div></div>`;
      }).join("");
    }
  }
  $("#run-summary-note").textContent = summary.invalid_boards ? `${summary.invalid_boards}副失败，未计入成绩。` : "";
}
function renderBatchSummary(bundle) {
  const report = bundle.batch_audit;
  const target = $("#batch-audit-summary");
  target.hidden = !report;
  if (!report) { target.replaceChildren(); return; }
  const failures = report.board_failures || [];
  const failed = report.failed_boards ?? 0;
  const valid = report.valid_boards ?? report.completed_boards;
  target.classList.toggle("has-failures", failed > 0);
  target.innerHTML = `<div class="section-heading"><h2>已完成 ${report.completed_boards}/${report.sampling.count} 副</h2><span>有成绩 ${valid} 副${failed ? ` · 失败 ${failed} 副` : ""}</span></div>
    ${failures.length ? `<div class="report-actions"><button class="button small" data-failed-only>只看失败牌</button></div><ul class="audit-error-list">${failures.map(failure =>
      `<li><button class="button small" data-error-board="${failure.board_number}">第 ${failure.board_number} 副</button><ul>${failure.errors.map(error => `<li>${auditErrorMarkup(error)}</li>`).join("")}</ul></li>`).join("")}</ul>` : ""}
    <details class="technical-details"><summary>任务详情与下载</summary><p>种子 ${report.sampling.seed}${report.execution ? ` · ${report.execution.workers}进程` : ""}</p>${report.records ? `<p>记录 ${recordSize(report.records.stored_bytes)} · 完整展开 ${recordSize(report.records.expanded_bytes)}</p>` : ""}<div class="report-actions">${reportLinks(report.id)}</div></details>`;
}
function updateURL() {
  if (state.isHome) return;
  const url = new URL(location.href);
  const board = currentBase();
  url.searchParams.set("run", $("#run-select").value);
  if (board) url.searchParams.set("board", board.number); else url.searchParams.delete("board");
  url.searchParams.set("filter", $("#difference-filter").value);
  const query = $("#search").value.trim();
  if (query) url.searchParams.set("q", query); else url.searchParams.delete("q");
  url.searchParams.set("sort", $("#sort").value);
  for (const [id, defaultValue] of Object.entries(filterDefaults)) {
    const value = $("#" + id).value.trim();
    if (value !== defaultValue) url.searchParams.set(id, value); else url.searchParams.delete(id);
  }
  if (board?.tables[state.trace[0]]?.auction[state.trace[1]]) {
    url.searchParams.set("table", state.trace[0] + 1);
    url.searchParams.set("turn", state.trace[1] + 1);
  } else {
    url.searchParams.delete("table");
    url.searchParams.delete("turn");
  }
  history.replaceState(null, "", url);
}
function setBundle(bundle, requestedNumber, loadId) {
  if (loadId === undefined) {
    cancelRunLoad();
    state.boardDetails.clear();
    state.boardErrors.clear();
  }
  state.bundle = bundle;
  state.frequencies = buildAuctionStatistics(bundle.boards);
  showReview();
  state.comparison = null;
  state.interactive = false;
  state.trace = state.requestedTrace || [0, 0];
  state.requestedTrace = null;
  state.limit = 80;
  const stats = bundle.statistics;
  $("#metric-boards").textContent = stats.boards;
  $("#metric-auctions").textContent = stats.different_auctions;
  $("#metric-contracts").textContent = stats.different_contracts;
  $("#metric-imp").textContent = stats.nonzero_imp;
  $("#source-caption").textContent = "已保存牌例";
  renderRunSummary(bundle);
  renderSingleAudit(bundle.single_audit);
  renderBatchSummary(bundle);
  if (bundle.boards.every((board) => !board.tables.length)) $("#difference-filter").value = "all";
  state.activeId = bundle.boards.find((board) => board.number === requestedNumber)?.id || null;
  applyFilters(Boolean(state.activeId));
  $("#run-status").textContent = bundle.imported ? "已导入" : "已加载";
}
function cancelRunLoad() {
  state.loadController?.abort();
  state.boardController?.abort();
  state.boardRequest = null;
  state.loadController = null;
  state.loadingRun = null;
  state.loadId += 1;
  $("#run-loading").hidden = true;
  busy(false);
}
function clearRunView() {
  state.bundle = null;
  state.frequencies = null;
  state.boardDetails.clear();
  state.boardErrors.clear();
  $("#board-loading").hidden = true;
  state.activeId = null;
  state.filtered = [];
  state.comparison = null;
  state.interactive = false;
  for (const id of ["#run-summary", "#single-audit-report", "#batch-audit-summary", "#board-content", "#empty-state"]) $(id).hidden = true;
  $("#board-list").replaceChildren();
  $("#filtered-count").textContent = "—";
  for (const id of ["#metric-boards", "#metric-auctions", "#metric-contracts", "#metric-imp"]) $(id).textContent = "—";
}
async function loadRun(identifier, requestedNumber, { refreshCatalog = false } = {}) {
  if (state.loadingRun === identifier && state.lastRunLoad?.requestedNumber === requestedNumber) return;
  cancelRunLoad();
  const loadId = state.loadId;
  const controller = new AbortController();
  state.loadController = controller;
  state.loadingRun = identifier;
  state.lastRunLoad = { identifier, requestedNumber, refreshCatalog };
  clearRunView();
  showReview();
  busy(true);
  const entry = state.catalog?.runs.find(run => run.id === identifier);
  $("#run-loading-title").textContent = `正在打开 · ${entry?.name || identifier}`;
  $("#run-loading").hidden = false;
  $("#run-loading").classList.remove("load-failed");
  $("#run-loading-progress").hidden = false;
  $("#run-loading-error").hidden = true;
  $("#retry-run-load").hidden = true;
  $("#run-loading-elapsed").textContent = "已用时 0 秒";
  $("#run-loading-stage").textContent = "正在打开审计…";
  const started = Date.now();
  const timer = setInterval(() => {
    if (loadId === state.loadId) $("#run-loading-elapsed").textContent = `已用时 ${Math.floor((Date.now() - started) / 1000)} 秒`;
  }, 1000);
  const url = new URL(location.href);
  url.searchParams.set("run", identifier);
  if (requestedNumber != null) url.searchParams.set("board", requestedNumber); else url.searchParams.delete("board");
  if (!state.requestedTrace) { url.searchParams.delete("table"); url.searchParams.delete("turn"); }
  history.replaceState(null, "", url);
  try {
    if (refreshCatalog) {
      const catalog = await fetchJSON("/api/catalog", { signal: controller.signal });
      if (loadId !== state.loadId) return;
      state.catalog = catalog;
      syncRunOptions();
      if (!catalog.runs.some(run => run.id === identifier)) throw new Error("该运行尚未完整保存或未登记，请查看任务详情。");
    }
    $("#run-select").value = identifier;
    const count = state.catalog?.runs.find(run => run.id === identifier)?.boards;
    $("#run-loading-stage").textContent = `正在读取${count ? ` ${count} 副` : ""}牌例…`;
    const bundle = await fetchJSON(`/api/runs/${encodeURIComponent(identifier)}/index`, { signal: controller.signal }, () => {
      if (loadId === state.loadId) $("#run-loading-stage").textContent = "正在接收牌例…";
    });
    if (loadId !== state.loadId) return;
    $("#run-loading-stage").textContent = "正在准备列表…";
    await new Promise(resolve => setTimeout(resolve, 0));
    if (loadId !== state.loadId) return;
    setBundle(bundle, requestedNumber, loadId);
    $("#run-loading").hidden = true;
    busy(false);
  } catch (error) {
    if (loadId !== state.loadId || controller.signal.aborted) return;
    clearRunView();
    busy(false);
    $("#run-status").textContent = "载入失败";
    $("#run-loading").hidden = false;
    $("#run-loading").classList.add("load-failed");
    $("#run-loading-progress").hidden = true;
    $("#run-loading-stage").textContent = "未能打开牌例，请重试或返回首页。";
    $("#run-loading-error").textContent = error.message;
    $("#run-loading-error").hidden = false;
    $("#retry-run-load").hidden = false;
    toast(error.message, true);
  } finally {
    clearInterval(timer);
    if (state.loadController === controller) {
      state.loadController = null;
      state.loadingRun = null;
    }
  }
}
function applyFilters(preserveSelection = true) {
  if (!state.bundle) return;
  const filter = $("#difference-filter").value;
  const query = $("#search").value.trim().toLowerCase();
  let advanced;
  try {
    advanced = compileFilters(Object.fromEntries(Object.keys(filterDefaults).map((id) => [id, $("#" + id).value])));
    $("#filter-error").hidden = true;
  } catch (error) {
    $("#filter-error").textContent = error.message;
    $("#filter-error").hidden = false;
    state.filtered = [];
    state.activeId = null;
    renderList();
    renderBoard();
    updateURL();
    return;
  }
  const count = Object.entries(filterDefaults).filter(([id, value]) => $("#" + id).value !== value).length;
  $("#advanced-count").textContent = count ? `${count} 项` : "";
  $("#filter-caption").textContent = count || query ? "筛选已生效 · 可复制链接" : "可组合 · / 搜索 · ← → 翻牌";
  state.filtered = state.bundle.boards.filter((board) => {
    const allowed = filter === "all" || filter === "auction" && board.difference.auction
      || filter === "contract" && board.difference.contract || filter === "imp" && board.imp != null && board.imp !== 0
      || filter === "same-score" && board.difference.auction && board.point_difference === 0
      || filter === "audit" && (boardAuditFindings(board).length > 0 || boardAuditErrors(board).length > 0)
      || filter === "audit-failed" && boardAuditErrors(board).length > 0
      || filter === "flagged" && annotation(board).status === "flagged";
    if (!allowed) return false;
    if (!matchesAdvancedFilters({ ...board, dds: board.dds || state.ddsCache.get(ddsKey(board)), review: annotation(board) }, advanced)) return false;
    if (!query) return true;
    const haystack = [String(board.number), `#${board.number}`, `index:${board.index}`, board.label || "", board.family_id || "", board.search_text || "", ...Object.values(board.hands),
      ...boardAuditErrors(board).map(error => error.message),
      ...board.tables.flatMap((table) => [contractText(table), ...table.auction.flatMap((step) => [step.call, step.private_trace?.rule_id || "", step.public_explanation?.summary || ""])])].join(" ").toLowerCase();
    return haystack.includes(query);
  });
  state.filtered.sort($("#sort").value === "number" ? (left, right) => left.number - right.number
    : (left, right) => Math.abs(right.imp || 0) - Math.abs(left.imp || 0) || left.number - right.number);
  if (!preserveSelection || !state.filtered.some((board) => board.id === state.activeId)) state.activeId = state.filtered[0]?.id || null;
  state.limit = Math.max(80, Math.ceil((state.filtered.findIndex((board) => board.id === state.activeId) + 1) / 80) * 80);
  renderList();
  renderBoard();
  updateURL();
}
function resetFilters(apply = true) {
  $("#search").value = "";
  $("#difference-filter").value = "all";
  for (const [id, value] of Object.entries(filterDefaults)) $("#" + id).value = value;
  if (apply) applyFilters(false);
}
function renderList() {
  $("#filtered-count").textContent = `${state.filtered.length} / ${state.bundle.boards.length}`;
  $("#board-list").innerHTML = state.filtered.slice(0, state.limit).map((board) => {
    const flagged = annotation(board).status === "flagged";
    return `<button class="board-item ${board.id === state.activeId ? "active" : ""}" data-board="${escapeHTML(board.id)}" role="listitem" aria-current="${board.id === state.activeId}"><span class="board-item-top"><strong><small>第</small>${board.number}<small> 副</small></strong><span class="imp-mini ${tone(board.imp)}">${sign(board.imp)} IMP</span></span>${board.label ? `<span class="board-item-label">${escapeHTML(board.label)}</span>` : ""}<span class="board-item-contracts">${escapeHTML(contractText(board.tables[0]))}<span class="arrow">↔</span>${escapeHTML(board.tables[1] ? contractText(board.tables[1]) : "—")}</span><span class="board-item-bottom"><span>${board.dealer} 发牌 · ${vulNames[board.vulnerability]}</span>${boardAuditErrors(board).length ? '<span class="audit-badge failed-badge">检查失败</span>' : boardAuditFindings(board).length ? '<span class="audit-badge">审计问题</span>' : ""}${flagged ? '<i data-lucide="flag" class="flag-indicator"></i>' : ""}</span></button>`;
  }).join("");
  $("#load-more").hidden = state.limit >= state.filtered.length;
  icons();
}
function setActive(identifier) {
  state.activeId = identifier;
  state.interactive = false;
  state.comparison = null;
  state.trace = [0, 0];
  const position = state.filtered.findIndex((board) => board.id === identifier);
  if (position >= state.limit) state.limit = Math.ceil((position + 1) / 80) * 80;
  renderList();
  renderBoard();
  if (innerWidth <= 720) $(".sidebar").classList.remove("expanded");
  $(".board-item.active")?.scrollIntoView({ block: "nearest" });
}
async function ensureBoardDetails(identifier) {
  const bundle = state.bundle;
  if (!bundle?.details_on_demand || state.boardDetails.has(identifier)) return;
  if (state.boardRequest?.id === identifier) return state.boardRequest.promise;
  state.boardController?.abort();
  const controller = new AbortController();
  state.boardController = controller;
  state.boardErrors.delete(identifier);
  const request = { id: identifier };
  state.boardRequest = request;
  request.promise = (async () => {
    try {
      const result = await fetchJSON(`/api/runs/${encodeURIComponent(bundle.source)}/boards/${encodeURIComponent(identifier)}?version=${encodeURIComponent(bundle.version)}`,
        { signal: controller.signal });
      if (state.bundle !== bundle || controller.signal.aborted) return;
      if (result.record?.id !== identifier || result.version !== bundle.version) throw new Error("牌例已变化，请刷新后查看。");
      state.boardDetails.delete(identifier);
      state.boardDetails.set(identifier, result.record);
      while (state.boardDetails.size > 32) state.boardDetails.delete(state.boardDetails.keys().next().value);
      if (state.activeId === identifier) renderBoard();
    } catch (error) {
      if (controller.signal.aborted || state.bundle !== bundle) return;
      state.boardErrors.set(identifier, error.message);
      if (state.activeId === identifier) renderBoard();
    } finally {
      if (state.boardRequest === request) state.boardRequest = null;
    }
  })();
  return request.promise;
}
function navigate(direction) {
  const index = state.filtered.findIndex((board) => board.id === state.activeId);
  const next = state.filtered[index + direction];
  if (next) setActive(next.id);
}
function renderHands(board) {
  for (const seat of seats) {
    const hand = board.hands[seat].split(".");
    const vulnerable = board.vulnerability === "both" || board.vulnerability === ("NS".includes(seat) ? "ns" : "ew");
    const hcp = board.hand_summary?.[seat]?.hcp ?? "—";
    $("#hand-" + seat).innerHTML = `<div class="hand-heading"><span class="hand-seat ${vulnerable ? "vulnerable" : ""}">${seat} ${seatNames[seat]}</span>${seat === board.dealer ? '<span class="dealer-tag">D</span>' : ""}<span class="hand-hcp">${hcp} HCP</span></div>${["S", "H", "D", "C"].map((suit, index) => `<div class="suit-row"><span class="suit-symbol ${["H", "D"].includes(suit) ? "red-suit" : "black-suit"}">${symbols[suit]}</span><span class="ranks">${hand[index] === "-" || !hand[index] ? '<span class="void">—</span>' : [...hand[index]].map((rank) => `<span class="rank">${escapeHTML(rank === "T" ? "10" : rank)}</span>`).join("")}</span></div>`).join("")}`;
  }
  $("#dealer-caption").textContent = `发牌 ${board.dealer} · ${seatNames[board.dealer]}家`;
  $("#vul-caption").textContent = vulNames[board.vulnerability];
}
function renderDDS(board) {
  const key = ddsKey(board);
  const data = board.dds || state.ddsCache.get(key);
  const error = state.ddsErrors.get(key);
  const pending = state.ddsPending.has(key);
  const target = $("#dds-panel");
  if (!data) {
    if (board.details_pending) {
      target.innerHTML = '<p class="form-hint">正在读取DDS / Par…</p>';
      return;
    }
    target.innerHTML = `<div class="section-heading"><h2>DDS / Par · 牌后参考</h2><button id="solve-dds" class="button small" ${pending ? "disabled" : ""}>${pending ? "求解中…" : "求解DDS / Par"}</button></div><p class="form-hint">${error ? escapeHTML(error) : "本记录未附20项DDS表；可独立求解，不重新叫牌、不改写冻结成绩。"}</p>`;
    return;
  }
  const strains = ["NT", "S", "H", "D", "C"];
  const par = data.par;
  const contracts = par.passed_out ? "全 Pass" : par.contracts.map((item) => {
    const delta = item.tricks - item.level - 6;
    return `${item.level}${symbols[item.strain]}${item.doubling === 2 ? "X" : ""} · ${item.declarers.join("/")} ${delta === 0 ? "=" : sign(delta)}`;
  }).join("；");
  target.innerHTML = `<div class="section-heading"><h2>DDS可成阶数</h2><span>不是墩数 · —表示不足一阶</span></div>
    <table class="makeable-table"><thead><tr><th>庄位</th>${strains.map((suit) => `<th class="${["H", "D"].includes(suit) ? "red-suit" : ""}">${symbols[suit]}</th>`).join("")}</tr></thead><tbody>${seats.map((seat) => `<tr><th>${seat}</th>${strains.map((suit) => {
      const level = data.makeable_levels[seat][suit];
      return `<td class="${level >= 6 ? "makeable-slam" : ""}" title="${seat}庄 ${symbols[suit]}：${data.table[seat][suit]}墩，${level ? `可成${level}阶` : "不足一阶"}">${level || "—"}</td>`;
    }).join("")}</tr>`).join("")}</tbody></table>
    <div class="par-result"><strong>Par · NS ${sign(par.score_ns)}</strong><span>${escapeHTML(contracts)}</span></div>
    <p class="form-hint">${data.dealer}发牌 · ${vulNames[data.vulnerability]}</p>`;
}
function ddsKey(board) {
  return JSON.stringify([board.dealer, board.vulnerability, ...seats.map((seat) => board.hands[seat])]);
}
async function solveDDS() {
  const board = current();
  if (!board) return;
  const key = ddsKey(board);
  if (state.ddsPending.has(key)) return;
  state.ddsPending.add(key);
  state.ddsErrors.delete(key);
  renderDDS(board);
  try {
    const result = await fetchJSON("/api/dds", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ board }) });
    state.ddsCache.set(key, result.dds);
    if (state.bundle?.boards.some((item) => item.id === board.id)) applyFilters();
  } catch (error) {
    state.ddsErrors.set(key, `DDS / Par未完成：${error.message}`);
    toast(error.message, true);
  } finally {
    state.ddsPending.delete(key);
    if (current() && ddsKey(current()) === key) renderDDS(current());
  }
}
function renderAuctions(board) {
  const firstTurn = board.difference.first_turn;
  $("#first-difference").textContent = board.tables.some(table => table.error) ? "执行不完整 · 不作完整配对比较" : firstTurn == null ? "叫牌序列相同" : `第 ${firstTurn + 1} 叫开始分歧`;
  $("#auction-tables").innerHTML = [0, 1].map((tableIndex) => {
    const table = board.tables[tableIndex];
    if (!table) return `<section class="auction-panel"><div class="auction-header"><div class="auction-name">桌${tableIndex + 1}<span>未运行</span></div></div><div class="bidding-grid"></div><div class="auction-result"><div class="contract-line">—</div><div class="score-line">NS 分数 <strong>—</strong></div></div></section>`;
    const offset = ["W", "N", "E", "S"].indexOf(board.dealer);
    const cells = Math.ceil((table.auction.length + offset) / 4) * 4;
    const other = board.tables[1 - tableIndex];
    const systemLinks = ["NS", "EW"].map((side) => {
      const system = table.systems?.[side] || {};
      return `<button class="system-link" data-system="${escapeHTML(system.hash || "")}" title="${escapeHTML(system.name || "未指定体系")}">${side} · ${escapeHTML(shortSystem(system.name))}</button>`;
    }).join("");
    const bids = Array.from({ length: cells }, (_, cellIndex) => {
      const turn = cellIndex - offset;
      const step = table.auction[turn];
      if (!step) return '<div class="bid-cell blank">·</div>';
      const different = other && step.call !== other.auction[turn]?.call;
      const selected = state.trace[0] === tableIndex && state.trace[1] === turn;
      const meaning = step.public_explanation;
      const tooltip = meaning ? [meaning.summary, meaning.notes[0], meaning.warnings[0], "点击查看本次新增、此前承诺及完整公开说明。"].filter(Boolean).join("\n") : "未附公开解释，点击查看已保存的信息。";
      return `<div class="bid-cell"><button class="bid ${step.call === "P" ? "pass" : ""} ${["X", "XX"].includes(step.call) ? "double" : ""} ${different ? "different" : ""} ${selected ? "selected" : ""}" data-table="${tableIndex}" data-turn="${turn}" aria-label="桌${tableIndex + 1} 第${turn + 1}叫 ${step.seat} ${step.call}；${escapeHTML(meaning?.purpose || "公开含义未附")}" title="${escapeHTML(tooltip)}">${callMarkup(step.call)}</button></div>`;
    }).join("");
    return `<section class="auction-panel"><div class="auction-header"><div class="auction-name">桌${tableIndex + 1}<span>${state.interactive ? "交互重叫" : "原始记录"}</span></div><div class="system-links">${systemLinks}</div></div>${table.error ? `<p class="table-execution-error">${escapeHTML(table.error.message)}${!table.auction.length ? "（执行器未返回叫牌轨迹，未补造Pass。）" : ""}</p>` : ""}<div class="bidding-grid">${["W", "N", "E", "S"].map((seat) => `<div class="bidding-seat">${seat}</div>`).join("")}${bids}</div><div class="auction-result"><div class="contract-line">${escapeHTML(contractText(table, true))}</div><div class="score-line"><span>DDS ${table.tricks == null ? "—" : table.tricks + " 墩"}</span><span>NS <strong class="${tone(table.score_ns)}">${sign(table.score_ns)}</strong></span></div></div></section>`;
  }).join("");
}
function expressionText(expression) {
  if (expression.op === "hcp") return "HCP";
  if (expression.op === "length") return `${symbols[expression.suit] || expression.suit} 长度`;
  if (expression.op === "honors") return `${symbols[expression.suit] || expression.suit} ${expression.ranks} 数量`;
  if (expression.op === "balanced") return "均型";
  if (expression.op === "shape") return "牌型";
  if (expression.op === "context") return expression.name;
  if (expression.op === "vulnerable") return "己方有局";
  return JSON.stringify(expression);
}
function hcpInterval(lower, upper) {
  if (upper == null) return `${lower ?? 0}+ HCP`;
  return `${lower ?? 0}–${upper} HCP`;
}
function suitInterval(lower, upper) {
  const minimum = lower ?? 0;
  return upper == null ? `${minimum}+` : minimum === upper ? String(minimum) : `${minimum}–${upper}`;
}
function suitRanges(context, prefix) {
  return ["S", "H", "D", "C"].map(suit => `${symbols[suit]} ${suitInterval(context[`${prefix}${suit}_min`], context[`${prefix}${suit}_max`])}`).join(" · ");
}
function publicSuitRoles(context) {
  const captain = context["var.captain"];
  if (!captain) return "尚未确定角色";
  if (!seats.includes(captain)) throw new Error("公开协议中的开叫人座位无效。");
  const responder = seats[(seats.indexOf(captain) + 2) % 4];
  return `${captain} 开叫/首介：${suitRanges(context, "var.captain_")}；${responder} 搭档：${suitRanges(context, "var.responder_")}`;
}
function publicSuitRelations(context) {
  const captain = context["var.captain"];
  if (!captain) return "尚无公开套间关系";
  if (!seats.includes(captain)) throw new Error("公开协议中的开叫人座位无效。");
  const result = [];
  for (const [role, seat] of [["captain", captain], ["responder", seats[(seats.indexOf(captain) + 2) % 4]]]) {
    const relations = [];
    for (let left = 0; left < 4; left++) for (let right = left + 1; right < 4; right++) {
      const a = ["S", "H", "D", "C"][left], b = ["S", "H", "D", "C"][right];
      const forward = context[`var.${role}_${a}_ge_${b}`] === true;
      const reverse = context[`var.${role}_${b}_ge_${a}`] === true;
      if (forward && reverse) relations.push(`${symbols[a]} = ${symbols[b]}`);
      else if (forward) relations.push(`${symbols[a]} ≥ ${symbols[b]}`);
      else if (reverse) relations.push(`${symbols[b]} ≥ ${symbols[a]}`);
    }
    if (relations.length) result.push(`${seat}：${relations.join("；")}`);
    for (const suit of ["S", "H", "D", "C"]) {
      if (context[`var.${role}_${suit}_support_quality`]) result.push(`${seat} ${symbols[suit]}：Qxx（或更好）/xxxx支持`);
      if (context[`var.${role}_${suit}_support_denied`]) result.push(`${seat} ${symbols[suit]}：未达Qxx/xxxx支持`);
      if (context[`var.${role}_${suit}_control`]) result.push(`${seat} ${symbols[suit]}：首轮或次轮控制`);
    }
  }
  return result.join(" · ") || "尚无公开套间关系";
}
function renderTrace(board) {
  const [tableIndex, turn] = state.trace;
  const step = board.tables[tableIndex]?.auction[turn];
  renderFrequency(board);
  $("#trace-selection").textContent = step ? `桌${tableIndex + 1} · 第${turn + 1}叫 · ${step.seat} ${seatNames[step.seat]} · ${step.call}` : "无叫牌记录";
  if (board.details_pending) {
    for (const id of ["#public-meaning", "#protocol-state", "#protocol-after"]) $(id).hidden = true;
    $("#trace-rule").textContent = "";
    $("#trace-features").replaceChildren();
    $("#protocol-timing-note").textContent = "";
    return;
  }
  $("#trace-rule").textContent = step?.private_trace?.rule_id || (step ? "未附私有trace" : "—");
  const meaning = step?.public_explanation;
  $("#public-meaning").hidden = !step;
  $("#public-meaning-summary").textContent = meaning?.summary || "暂无叫品解释。";
  $("#public-meaning-basis").textContent = "";
  $("#public-meaning-new").innerHTML = (meaning?.new_information?.length ? meaning.new_information
    : ["无新增点力或套长限制。"]).map(text => `<li>${escapeHTML(text)}</li>`).join("");
  $("#public-meaning-retained").innerHTML = (meaning?.retained_information || []).map(text => `<li>${escapeHTML(text)}</li>`).join("");
  $("#public-meaning-retained-section").hidden = !meaning?.retained_information?.length;
  $("#public-meaning-notes").textContent = (meaning?.notes || []).join(" ");
  $("#meaning-technical").hidden = !meaning?.warnings?.length;
  $("#public-meaning-warning").textContent = (meaning?.warnings || []).join(" ");
  const context = step?.public_context;
  $("#protocol-state").hidden = !context || context.partnership_hcp_min == null;
  if (context) {
    const goals = { none: "不逼局", one_round: "逼叫一轮", invite_game: "邀请至局", game: "逼局", slam_try: "满贯探查", small_slam: "小满贯", invite_grand: "邀请大满贯", grand_slam: "大满贯" };
    const kinds = { takeout: "技术性", negative: "否定性", penalty: "惩罚性", strength: "实力再加倍", escape: "逃叫", two_suited: "双套" };
    $("#protocol-goal").textContent = `${goals[context.goal] || context.goal || "—"}${context.goal_met && context.goal !== "none" ? " · 已达目标" : ""}`;
    $("#protocol-forcing").textContent = context.forcing === "none" ? "无待履行义务" : `${goals[context.forcing] || context.forcing}${context.must_bid ? " · 本次须继续" : ""}`;
    $("#protocol-trump").textContent = context.agreed_trump ? `${symbols[context.agreed_trump]} · ${suitInterval(context.agreed_trump_min, context.agreed_trump_max)} 张` : "未定";
    $("#protocol-hcp").textContent = hcpInterval(context.partnership_hcp_min, context.partnership_hcp_max);
    const captain = context["var.captain"];
    const responder = captain ? seats[(seats.indexOf(captain) + 2) % 4] : null;
    function roleRange(role, seat) {
      const uppers = [context[`var.${role}_hcp_max`], context[`var.seat_${seat}_hcp_max`]].filter(Number.isInteger);
      return hcpInterval(context[`var.${role}_hcp_min`], uppers.length ? Math.min(...uppers) : null);
    }
    $("#protocol-roles").textContent = captain ? `${captain} 开叫/首介 ${roleRange("captain", captain)}；${responder} 搭档 ${roleRange("responder", responder)}` : "尚未确定角色";
    const passed = seats.filter(seat => Number.isInteger(context[`var.seat_${seat}_hcp_max`]) && context[`var.seat_${seat}_hcp_max`] < 40);
    $("#protocol-passes").textContent = passed.length ? passed.map(seat => `${seat} ≤${context[`var.seat_${seat}_hcp_max`]} HCP`).join("；") : "无已记录限制";
    $("#protocol-suits").className = "suits";
    $("#protocol-suits").innerHTML = ["S", "H", "D", "C"].map(suit => `<span class="bound ${["H", "D"].includes(suit) ? "red-suit" : "black-suit"}">${symbols[suit]} ${escapeHTML(suitInterval(context[`partnership_${suit}_min`], context[`partnership_${suit}_max`]))}</span>`).join("");
    $("#protocol-shapes").textContent = publicSuitRoles(context);
    $("#protocol-relations").textContent = publicSuitRelations(context);
    const inference = step?.private_trace?.inference;
    const privateSuits = ["S", "H", "D", "C"].filter(suit => Number.isInteger(inference?.suit_min?.[suit]))
      .map(suit => `${symbols[suit]} ${suitInterval(inference.suit_min[suit], inference.suit_max?.[suit])}`);
    $("#protocol-private").textContent = inference ? `${hcpInterval(inference.partnership_hcp_min, inference.partnership_hcp_max)} · ${privateSuits.join(" · ") || "未记录私有套长估计"}` : "—";
    $("#protocol-double").textContent = `己方：${kinds[context.double_kind] || "未声明"}；敌方：${kinds[context.opponent_double_kind] || "未声明"}`;
  }
  const after = step?.public_context_after;
  $("#protocol-after").hidden = !after;
  $("#protocol-after").innerHTML = after ? `<div class="section-heading"><h2>本次叫品的公开更新</h2><span>叫牌后</span></div><dl class="protocol-ledger"><div><dt>联手公开点力</dt><dd>${escapeHTML(hcpInterval(after.partnership_hcp_min, after.partnership_hcp_max))}</dd></div><div class="wide"><dt>双方套长区间</dt><dd>${escapeHTML(publicSuitRoles(after))}</dd></div><div class="wide"><dt>联手公开张数区间</dt><dd>${escapeHTML(suitRanges(after, "partnership_"))}</dd></div><div class="wide"><dt>已知套长关系</dt><dd>${escapeHTML(publicSuitRelations(after))}</dd></div></dl>` : "";
  $("#protocol-timing-note").textContent = "";
  $("#trace-features").innerHTML = (step?.private_trace?.features || []).map((feature) => `<div class="feature-row"><span>${escapeHTML(expressionText(feature.expression))}</span><span>${escapeHTML(typeof feature.value === "string" ? feature.value : JSON.stringify(feature.value))}</span></div>`).join("");
}
function renderBoard() {
  const board = current();
  $("#empty-state").hidden = Boolean(board);
  $("#board-content").hidden = !board;
  $("#compare-button").disabled = !board;
  $("#audit-button").disabled = !board;
  if (!board) { $("#board-loading").hidden = true; return; }
  const pending = Boolean(board.details_pending);
  const loadingError = state.boardErrors.get(board.id);
  $("#board-loading").hidden = !pending;
  $("#board-loading-text").textContent = loadingError ? `第${board.number}副详情读取失败：${loadingError}` : `正在读取第${board.number}副详情…`;
  $("#retry-board-load").hidden = !loadingError;
  const findings = state.interactive ? [] : boardAuditFindings(board);
  const errors = boardAuditErrors(board);
  $("#board-audit").hidden = !findings.length && !errors.length;
  $("#board-audit").innerHTML = `${errors.length ? `<strong>本副审计失败 · 未计入成绩</strong><ul>${errors.map(error => `<li>${auditErrorMarkup(error)}</li>`).join("")}</ul>` : ""}${findings.length ? `<strong>叫牌问题</strong>${findings.map((finding) => {
    const tables = finding.cases.filter((item) => item.index === board.index).map((item) => item.table === "table1" ? "第一桌" : "第二桌");
    return `<p>${escapeHTML(findingName(finding))} · ${tables.join("、")}</p>`;
  }).join("")}` : ""}`;
  const position = state.filtered.findIndex((item) => item.id === state.activeId);
  $("#board-title").textContent = `第 ${board.number} 副`;
  $("#board-imp").innerHTML = `${sign(board.imp)}<small>IMP</small>`;
  $("#board-imp").className = `imp-display ${tone(board.imp)}`;
  $("#board-origin").textContent = state.interactive ? "重新叫牌" : state.bundle.imported ? "导入牌例" : "牌例";
  $("#board-meta").innerHTML = `<span>${board.dealer} 发牌</span><span>${vulNames[board.vulnerability]}</span>${board.label ? `<span>${escapeHTML(board.label)}</span>` : ""}`;
  $("#board-position").textContent = `${position + 1} / ${state.filtered.length}`;
  $("#previous-board").disabled = position <= 0;
  $("#next-board").disabled = position < 0 || position >= state.filtered.length - 1;
  const note = annotation(board);
  $("#flag-button").setAttribute("aria-pressed", String(note.status === "flagged"));
  $("#review-status").value = note.status || "unreviewed";
  $("#review-note").value = note.note || "";
  $("#annotation-status").textContent = note.updatedAt ? `已保存 ${new Date(note.updatedAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}` : "本机记录";
  const hasComparison = state.comparison?.sourceId === state.activeId;
  $("#comparison-bar").hidden = !hasComparison;
  $("#show-frozen").setAttribute("aria-pressed", String(!state.interactive));
  $("#show-interactive").setAttribute("aria-pressed", String(state.interactive));
  if (hasComparison) $("#comparison-oracle").textContent = state.comparison.oracle.oracle_kind === "dds" ? "真实 DDS · 非正式单副重叫" : "DDS不可用 · 仅叫牌";
  if (!board.tables[state.trace[0]]?.auction[state.trace[1]]) {
    const table = Math.max(0, board.tables.findIndex(item => item.auction.length));
    state.trace = [table, board.tables[table]?.auction[board.difference.first_turn] ? board.difference.first_turn : 0];
  }
  renderHands(board);
  renderDDS(board);
  renderHumanSummary(board);
  renderAuctions(board);
  renderTrace(board);
  $("#raw-json").textContent = $("#raw-record").open && !pending ? JSON.stringify({ ...board, review: note }, null, 2) : "";
  $("#record-identity").textContent = state.bundle.name;
  $("#score-perspective").textContent = errors.length ? "本副失败，未计入IMP。" : `IMP：桌1_NS − 桌2_NS${board.point_difference == null ? "" : ` · 分差 ${sign(board.point_difference)}`}`;
  updateURL();
  icons();
  if (pending && !loadingError) ensureBoardDetails(board.id);
}
function saveAnnotation(override) {
  const board = current();
  if (!board) return;
  const value = { status: override || $("#review-status").value, note: $("#review-note").value, updatedAt: new Date().toISOString() };
  state.annotations[board.id] = value;
  try { localStorage.setItem("bridge-review.annotations.v1", JSON.stringify(state.annotations)); }
  catch { toast("本机存储不可用，审查备注仍可随JSON导出。", true); }
  applyFilters();
  toast("审查记录已保存");
}
function download(blob, name) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.hidden = true;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 15000);
}
async function exportBoards(mode) {
  $("#export-menu").open = false;
  const active = current();
  if (!active) return;
  const records = (mode.startsWith("filtered") ? state.filtered : [active]).map((board) => ({
    ...board, ...(state.ddsCache.has(ddsKey(board)) ? { dds: state.ddsCache.get(ddsKey(board)) } : {}), review: annotation(board) }));
  const format = mode.endsWith("pbn") ? "pbn" : mode.endsWith("compact") ? "compact" : mode.endsWith("agent") ? "agent" : "json";
  const extension = { pbn: "pbn", compact: "compact.jsonl", agent: "agent.jsonl", json: "bridge.json" }[format];
  try {
    if (state.bundle.details_on_demand && mode !== "board-json" && !(state.interactive && !mode.startsWith("filtered"))) {
      toast(`正在导出 ${records.length} 副…`);
      const response = await fetch(`/api/runs/${encodeURIComponent(state.bundle.source)}/export`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
          ids: records.map(board => board.id), version: state.bundle.version,
          reviews: Object.fromEntries(records.map(board => [board.id, annotation(board)])),
          dds: Object.fromEntries(records.filter(board => state.ddsCache.has(ddsKey(board)))
            .map(board => [board.id, state.ddsCache.get(ddsKey(board))])),
          format,
        }),
      });
      if (!response.ok) throw new Error((await response.json()).error);
      download(await response.blob(), records.length === 1 ? `board-${active.number}.${extension}` : `boards-${records.length}.${extension}`);
      toast(`已导出 ${records.length} 副`);
      return;
    }
    if (mode === "board-json") {
      const raw = Object.fromEntries(["dealer", "vulnerability", "hands"].map((key) => [key, active[key]]));
      download(new Blob([JSON.stringify(raw, null, 2)], { type: "application/json" }), `board-${active.number}.json`);
    } else if (format !== "json") {
      const response = await fetch("/api/export", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ format, boards: records }) });
      if (!response.ok) throw new Error((await response.json()).error);
      download(await response.blob(), records.length === 1 ? `board-${active.number}.${extension}` : `boards-${records.length}.${extension}`);
    } else {
      const bundle = { schema: "bridge-review/v1", name: state.bundle.name, source: state.interactive ? "interactive-comparison" : state.bundle.source,
        systems: state.bundle.systems || {}, score_perspective: "table1_NS - table2_NS", boards: records };
      download(new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" }), records.length === 1 ? `board-${active.number}.bridge.json` : `boards-${records.length}.bridge.json`);
    }
    toast(`已导出 ${records.length} 副`);
  } catch (error) { toast(error.message, true); }
}
async function importFile(file) {
  if (!file) return;
  if (file.size > 32 * 1024 * 1024) return toast("文件超过32MiB限制。", true);
  busy(true);
  try {
    const bundle = await fetchJSON(`/api/import?filename=${encodeURIComponent(file.name)}`, { method: "POST", body: file });
    const id = `import-${Date.now()}`;
    const option = new Option(`导入 · ${file.name}`, id);
    $("#run-select").add(option);
    $("#run-select").value = id;
    state.imports ||= new Map();
    state.imports.set(id, bundle);
    resetFilters(false);
    setBundle({ ...bundle, imported: true });
    busy(false);
    $("#run-status").textContent = "已导入";
    toast(`已导入 ${bundle.boards.length} 副`);
  } catch (error) { busy(false); toast(error.message, true); }
  finally { $("#import-file").value = ""; }
}
function openCompare() {
  const board = currentBase();
  if (!board) return;
  selectSystems("#system-a", "#system-b");
  $("#compare-error").hidden = true;
  $("#compare-dialog").showModal();
}
async function compare(event) {
  event.preventDefault();
  const base = currentBase();
  if (!base) return;
  const button = $("#run-comparison");
  button.disabled = true;
  button.classList.add("is-running");
  button.querySelector("span").textContent = "叫牌中";
  $("#compare-error").hidden = true;
  try {
    const result = await fetchJSON("/api/compare", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ board: base, system_a: $("#system-a").value, system_b: $("#system-b").value }) });
    if (state.activeId === base.id) {
      state.comparison = { sourceId: base.id, ...result };
      state.interactive = true;
      state.trace = [0, result.record.difference.first_turn || 0];
      renderBoard();
    }
    $("#compare-dialog").close();
    toast(result.oracle.oracle_kind === "dds" ? "两桌重新叫牌完成 · 真实DDS已计分" : "两桌叫牌完成 · DDS不可用，未计分");
  } catch (error) { $("#compare-error").textContent = error.message; $("#compare-error").hidden = false; }
  finally { button.disabled = false; button.classList.remove("is-running"); button.querySelector("span").textContent = "重新叫牌"; }
}

function selectSystems(aSelector, bSelector) {
  const table = current()?.tables[0];
  const ids = new Set(state.catalog.systems.map((system) => system.id));
  const latest = (name) => state.catalog.systems.filter((system) => system.name.includes(name))
    .sort((a, b) => (b.name.match(/v(\d+(?:\.\d+)*)/)?.[1] || "0")
      .localeCompare(a.name.match(/v(\d+(?:\.\d+)*)/)?.[1] || "0", "en", { numeric: true }))[0]?.id;
  const a = table?.systems?.NS?.hash || state.bundle?.systems?.A?.hash || latest("CCBA");
  const b = table?.systems?.EW?.hash || state.bundle?.systems?.B?.hash || latest("精确");
  if (ids.has(a)) $(aSelector).value = a;
  if (ids.has(b)) $(bSelector).value = b;
}

function syncRunOptions() {
  const selected = $("#run-select").value;
  const visible = new Set(state.catalog.runs.map(run => run.id));
  for (const option of [...$("#run-select").options]) {
    if (!visible.has(option.value) && !state.imports?.has(option.value)) option.remove();
  }
  for (const run of state.catalog.runs) {
    let option = [...$("#run-select").options].find((item) => item.value === run.id);
    if (!option) { option = new Option("", run.id); $("#run-select").add(option); }
    option.textContent = `${purposeName(run.purpose)} · ${run.boards}副 · ${run.name}`;
  }
  if ([...$("#run-select").options].some((option) => option.value === selected)) $("#run-select").value = selected;
}

async function openSavedRun(identifier, number) {
  resetFilters(false);
  await loadRun(identifier, number, { refreshCatalog: true });
}

function customPayload() {
  return { format: $("#custom-format").value, dealer: $("#custom-dealer").value, vulnerability: $("#custom-vul").value,
    label: $("#custom-label").value.trim(), hands: Object.fromEntries(seats.map((seat) => [seat, $("#custom-" + seat).value])),
    text: $("#custom-text").value };
}
function customMessage(message, error = false) {
  $("#custom-error").textContent = message;
  $("#custom-error").classList.toggle("success-message", !error);
  $("#custom-error").hidden = false;
}
function customCounts() {
  for (const seat of seats) {
    const raw = $("#custom-" + seat).value.toUpperCase().replace(/10/g, "T").replace(/\s/g, "");
    const parts = raw.split(".");
    const cards = raw.replace(/[.-]/g, "");
    const valid = parts.length === 4 && /^[AKQJT98765432.-]*$/.test(raw);
    const hcp = [...cards].reduce((sum, rank) => sum + ({ A: 4, K: 3, Q: 2, J: 1 }[rank] || 0), 0);
    const target = $("#custom-count-" + seat);
    target.textContent = raw ? `${cards.length}张 · ${hcp} HCP${valid ? ` · ${parts.map((part) => part.replace(/-/g, "").length).join("-")}` : " · 请使用S.H.D.C格式"}` : "尚未填写";
    target.classList.toggle("invalid-count", Boolean(raw) && (!valid || cards.length !== 13));
  }
}
function customFormat() {
  const hands = $("#custom-format").value === "hands";
  $("#custom-hands").hidden = !hands;
  $("#custom-text-field").hidden = hands;
}
function fillCustom(board) {
  for (const seat of seats) $("#custom-" + seat).value = board.hands[seat];
  $("#custom-dealer").value = board.dealer;
  $("#custom-vul").value = board.vulnerability;
  $("#custom-format").value = "hands";
  customFormat();
  customCounts();
}
async function randomCustom() {
  $("#random-deal").disabled = true;
  try {
    const seed = Number($("#random-seed").value);
    if ($("#random-seed").value === "" || !Number.isSafeInteger(seed)) throw new Error("请填写有效的整数随机种子。");
    const result = await fetchJSON("/api/deals/preview", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ format: "random", seed, dealer: $("#custom-dealer").value, vulnerability: $("#custom-vul").value }) });
    fillCustom(result.record);
    if (!$("#custom-label").value.trim()) $("#custom-label").value = `随机牌 · 种子${seed}`;
    customMessage(`已按种子${seed}填入合法完整牌例；尚未保存。`);
  } catch (error) { customMessage(error.message, true); }
  finally { $("#random-deal").disabled = false; }
}
async function submitCustom(event, preview = false) {
  event?.preventDefault();
  const button = preview ? $("#validate-custom") : $("#save-custom");
  button.disabled = true;
  try {
    const result = await fetchJSON(preview ? "/api/deals/preview" : "/api/deals", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(customPayload()) });
    if (preview) {
      $("#custom-dealer").value = result.record.dealer;
      $("#custom-vul").value = result.record.vulnerability;
      customMessage("校验通过：每家13张、全副52张无重复。完整PBN / JSON中的发牌人和局况已同步。");
    } else {
      $("#custom-dialog").close();
      await openSavedRun(result.run, result.record.number);
      toast("自定义牌例已持久保存，可点击“单副审计”生成报告。");
    }
  } catch (error) { customMessage(error.message, true); }
  finally { button.disabled = false; }
}

const jobStatus = (status) => ({ queued: "排队中", running: "执行中", completed: "已完成", failed: "失败", interrupted: "已中断", cancelled: "已取消",
  checked: "检查完成", "completed-with-findings": "完成 · 有发现或提示", passed: "通过", warning: "提示",
  "not-applicable": "不适用", "not-requested": "未执行" }[status] || status);
function reportLinks(identifier) {
  return [["md", "中文报告"], ["json", "机器报告"], ["zip", "完整证据ZIP"]].map(([format, label]) =>
    `<a class="button small" href="/api/audits/${encodeURIComponent(identifier)}/report?format=${format}" download>${label}</a>`).join("");
}
function auditActions(job) {
  return `${job.status === "completed" ? `<button class="button small primary" data-open-audit="${job.id}">打开牌例与检查详情</button>` : ""}${job.report_sha256 ? reportLinks(job.id) : ""}
    ${["failed", "interrupted", "completed", "cancelled"].includes(job.status) ? `<button class="button small" data-retry-audit="${job.id}">按原输入再跑一次</button>` : ""}
    ${job.kind === "batch" && ["queued", "running"].includes(job.status) ? `<button class="button small" data-cancel-audit="${job.id}">请求取消</button>` : ""}`;
}
function renderTaskDetails(job) {
  const focusedCancel = document.activeElement?.dataset.cancelAudit;
  const report = job.report;
  const seed = job.seed ?? report?.sampling?.seed;
  const valid = job.valid_boards ?? report?.valid_boards;
  const failed = job.failed_boards ?? report?.failed_boards;
  const workers = job.workers ?? report?.execution?.workers;
  $("#task-dialog-content").innerHTML = `<h3>${escapeHTML(job.label)}</h3><p class="task-identity" hidden>${escapeHTML(job.id)}</p>
    <p><strong>${escapeHTML(jobStatus(job.status))}</strong> · ${escapeHTML(job.stage)}</p>
    <progress max="100" value="${job.progress || 0}" aria-label="任务进度"></progress>
    ${job.kind === "batch" ? `<dl class="task-metrics"><div><dt>已审计</dt><dd>${job.completed_boards || 0}/${job.count} 副</dd></div><div><dt>已接受 / 采样次数</dt><dd>${job.accepted_boards || 0} / ${job.attempts || 0}</dd></div><div><dt>有效配对 / 失败</dt><dd>${valid ?? "—"} / ${failed ?? "未记录"}</dd></div><div><dt>实际随机种子</dt><dd>${seed ?? "旧任务请查原报告"}</dd></div><div><dt>并行执行</dt><dd>${workers == null ? "旧任务未记录" : `${workers}进程＋各自DDS线程`}</dd></div></dl>` : ""}
    ${job.error ? `<p class="form-error">${escapeHTML(job.error)}</p>` : ""}
    ${report?.board_failures?.length ? `<details><summary>逐副失败原因（${report.board_failures.length}副）</summary><ul class="audit-error-list">${report.board_failures.map(failure =>
      `<li>第 ${failure.board_number} 副<ul>${failure.errors.map(error => `<li>${auditErrorMarkup(error)}</li>`).join("")}</ul></li>`).join("")}</ul></details>` : ""}
    <p id="task-poll-error" class="form-error" role="alert" hidden></p>
    <div class="report-actions">${auditActions(job)}</div>`;
  if (focusedCancel === job.id) $("#task-dialog-content [data-cancel-audit]")?.focus({ preventScroll: true });
}
async function openTask(identifier, job = null) {
  state.taskDialogId = identifier;
  state.hiddenAudit = null;
  if (job) renderTaskDetails(job);
  else $("#task-dialog-content").innerHTML = `<p>正在读取任务 ${escapeHTML(identifier)}…</p><p id="task-poll-error" class="form-error" role="alert" hidden></p>`;
  if (!$("#task-dialog").open) $("#task-dialog").showModal();
  await watchAudit(identifier);
}
function renderSingleAudit(report) {
  const target = $("#single-audit-report");
  target.hidden = !report;
  if (!report) { target.replaceChildren(); return; }
  const source = report.source;
  const comparison = report.source_comparison;
  target.innerHTML = `<div class="section-heading"><h2>单副审计报告</h2><span>${escapeHTML(jobStatus(report.status))}</span></div>
    <p class="form-hint">${escapeHTML(new Date(report.finished_at).toLocaleString("zh-CN"))}</p>
    ${report.error ? `<p class="form-error">${escapeHTML(report.error)}</p>` : ""}
    <details class="technical-details"><summary>检查明细</summary><ul class="audit-checks">${(report.checks || []).map((check) => `<li class="check-${escapeHTML(check.status)}"><strong>${escapeHTML(jobStatus(check.status))}</strong> ${escapeHTML(check.detail)}</li>`).join("")}</ul></details>
    ${(report.findings || []).map((finding) => `<p class="audit-report-finding"><button class="text-button" data-finding-table="${finding.table === "table1" ? 0 : 1}" data-finding-turn="${finding.turn}">${escapeHTML(finding.table)} · 第${finding.turn + 1}叫</button>：${escapeHTML(finding.description)}</p>`).join("")}
    ${comparison?.tables?.length ? `<p class="form-hint">${comparison.same_systems ? "与原记录使用相同体系" : "体系配置与原记录不同，不作为同版本复现"}：${comparison.tables.map((table) => `${table.table} · 叫牌${table.auction_same ? "一致" : "变化"} / 定约${table.contract_same ? "一致" : "变化"} / 分数${table.score_ns_same ? "一致" : "变化"}`).join("；")}</p>` : ""}
    <div class="report-actions">${reportLinks(report.id)}${source?.run ? `<button class="button small" data-source-run="${escapeHTML(source.run)}" data-source-number="${source.number}">返回原牌例</button>` : ""}</div>
    ${report.dds_table ? `<details class="dds-reference"><summary>查看20项双明手墩数（仅牌后参考）</summary><table><thead><tr><th>庄位</th>${["C", "D", "H", "S", "NT"].map((suit) => `<th>${symbols[suit]}</th>`).join("")}</tr></thead><tbody>${seats.map((seat) => `<tr><th>${seat}</th>${["C", "D", "H", "S", "NT"].map((suit) => `<td>${report.dds_table[seat][suit]}</td>`).join("")}</tr>`).join("")}</tbody></table></details>` : ""}`;
}
function renderAuditTask(job) {
  state.auditJob = job;
  $("#active-audit").hidden = state.hiddenAudit === job.id;
  $("#audit-task-label").textContent = `${job.kind === "batch" ? "批量审计" : "单副审计"} · ${job.label}`;
  $("#audit-task-stage").textContent = `${jobStatus(job.status)} · ${job.stage}${job.error ? ` · ${job.error}` : ""}`;
  $("#audit-progress").value = job.progress;
  $("#audit-task-actions").innerHTML = `<button class="button small" data-watch-audit="${job.id}">${["queued", "running"].includes(job.status) ? "查看进度" : "任务详情"}</button>${auditActions(job)}`;
  if (state.taskDialogId === job.id && $("#task-dialog").open) renderTaskDetails(job);
  state.homeJobs = [job, ...state.homeJobs.filter(item => item.id !== job.id)].sort((a, b) => b.created_at.localeCompare(a.created_at));
  if (state.isHome) renderHomeJobs();
}
async function watchAudit(identifier) {
  clearTimeout(state.auditPoll);
  state.watchedAudit = identifier;
  try {
    const job = await fetchJSON(`/api/audits/${encodeURIComponent(identifier)}`);
    if (state.watchedAudit !== identifier) return;
    renderAuditTask(job);
    if (["queued", "running"].includes(job.status)) {
      state.auditPoll = setTimeout(() => watchAudit(identifier), 1000);
    } else {
      const catalog = await fetchJSON("/api/catalog");
      if (state.watchedAudit !== identifier) return;
      state.catalog = catalog;
      syncRunOptions();
      if (state.announcedAudit !== identifier) {
        state.announcedAudit = identifier;
        toast(job.status === "completed" ? `${job.kind === "batch" ? "批量" : "单副"}审计已保存 · ${jobStatus(job.report_status)}` : `审计${jobStatus(job.status)}：${job.error || job.stage}`, job.status !== "completed");
      }
    }
  } catch (error) {
    if (state.watchedAudit !== identifier) return;
    $("#audit-task-stage").textContent = `任务状态连接失败，将重试：${error.message}`;
    $("#active-audit").hidden = false;
    if (state.taskDialogId === identifier && $("#task-dialog").open) {
      $("#task-poll-error").textContent = `任务状态连接失败，将重试：${error.message}`;
      $("#task-poll-error").hidden = false;
    }
    state.auditPoll = setTimeout(() => watchAudit(identifier), 3000);
  }
}
function openAudit() {
  const board = currentBase();
  if (!board) return;
  state.auditInput = { board, source_run: state.bundle.imported ? null : $("#run-select").value };
  selectSystems("#audit-system-a", "#audit-system-b");
  $("#audit-source-label").textContent = `第${board.number}副${board.label ? ` · ${board.label}` : ""} · ${board.dealer}发牌 · ${vulNames[board.vulnerability]}`;
  $("#audit-error").hidden = true;
  $("#audit-dialog").showModal();
}
async function startAudit(event) {
  event.preventDefault();
  $("#start-audit").disabled = true;
  $("#audit-error").hidden = true;
  try {
    const input = state.auditInput;
    const payload = { system_a: $("#audit-system-a").value, system_b: $("#audit-system-b").value, fresh_dds: $("#audit-fresh-dds").checked };
    if (input.source_run) Object.assign(payload, { source_run: input.source_run, board_id: input.board.id });
    else payload.board = input.board;
    const job = await fetchJSON("/api/audits", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    $("#audit-dialog").close();
    renderAuditTask(job);
    await openTask(job.id, job);
  } catch (error) { $("#audit-error").textContent = error.message; $("#audit-error").hidden = false; }
  finally { $("#start-audit").disabled = false; }
}
async function showHistory() {
  $("#audit-history-list").textContent = "载入中…";
  $("#history-dialog").showModal();
  try {
    const { jobs } = await fetchJSON("/api/audits");
    $("#audit-history-list").innerHTML = jobs.length ? jobs.map(jobCard).join("") : '<p class="form-hint">尚无任务，可从首页开启批量审计，或从牌例内开启单副审计。</p>';
  } catch (error) { $("#audit-history-list").textContent = `载入失败：${error.message}`; }
}
async function auditAction(event) {
  const open = event.target.closest("[data-open-audit]");
  const openLabel = open?.textContent;
  const watch = event.target.closest("[data-watch-audit]");
  const retry = event.target.closest("[data-retry-audit]");
  const cancel = event.target.closest("[data-cancel-audit]");
  const source = event.target.closest("[data-source-run]");
  const finding = event.target.closest("[data-finding-turn]");
  try {
    if (open || watch || retry) $("#history-dialog").close();
    if (open) {
      open.disabled = true;
      open.textContent = "正在打开…";
      $("#task-dialog").close();
      await openSavedRun(open.dataset.openAudit);
    }
    if (watch) await openTask(watch.dataset.watchAudit);
    if (retry) {
      retry.disabled = true;
      const job = await fetchJSON(`/api/audits/${encodeURIComponent(retry.dataset.retryAudit)}/retry`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      renderAuditTask(job);
      await openTask(job.id, job);
    }
    if (cancel) {
      const job = await fetchJSON(`/api/audits/${encodeURIComponent(cancel.dataset.cancelAudit)}/cancel`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      renderAuditTask(job);
      watchAudit(job.id);
    }
    if (source) { $("#task-dialog").close(); await openSavedRun(source.dataset.sourceRun, Number(source.dataset.sourceNumber)); }
    if (finding) {
      state.trace = [Number(finding.dataset.findingTable), Number(finding.dataset.findingTurn)];
      renderBoard();
      $("#public-meaning").scrollIntoView({ block: "center" });
    }
  } catch (error) {
    toast(error.message, true);
    if ($("#task-dialog").open) {
      $("#task-poll-error").textContent = error.message;
      $("#task-poll-error").hidden = false;
    }
  }
  finally {
    if (retry?.isConnected) retry.disabled = false;
    if (open?.isConnected) { open.disabled = false; open.textContent = openLabel; }
  }
}

function showReview() {
  state.isHome = false;
  clearTimeout(state.homeTimer);
  $("#home").hidden = true;
  $("#review-runbar").hidden = false;
  $("#review-workspace").hidden = false;
}
async function showHome() {
  cancelRunLoad();
  state.isHome = true;
  $("#home").hidden = false;
  $("#review-runbar").hidden = true;
  $("#review-workspace").hidden = true;
  $("#compare-button").disabled = true;
  $("#audit-button").disabled = true;
  history.replaceState(null, "", "/");
  await refreshHome();
}
function jobCard(job) {
  return `<article class="history-item"><div><strong>${escapeHTML(job.label)}</strong><span>${job.kind === "batch" ? `${job.completed_boards || 0}/${job.count}副${job.failed_boards != null ? ` · 失败${job.failed_boards}` : ""} · ` : ""}${escapeHTML(jobStatus(job.status))}</span></div><p>${escapeHTML(job.error || job.stage)}</p>${["queued", "running"].includes(job.status) ? `<progress max="100" value="${job.progress || 0}"></progress>` : ""}<div class="report-actions"><button class="button small" data-watch-audit="${job.id}">${["queued", "running"].includes(job.status) ? "查看进度" : "任务详情"}</button>${auditActions(job)}</div></article>`;
}
function renderHomeJobs() {
  $("#home-jobs").innerHTML = state.homeJobs.length ? state.homeJobs.slice(0, 8).map(jobCard).join("")
    : '<p class="form-hint">尚无任务。左侧可以开启新批次，历史冻结审计仍保留在下方。</p>';
}
function renderHomeRuns() {
  const query = $("#history-search").value.trim().toLowerCase();
  const category = $("#library-category").value;
  const view = $("#library-view").value;
  const status = $("#library-status").value;
  const runs = state.library.entries.filter(run =>
    Boolean(run.trashed_at) === (view === "trash")
    && (category === "all" || run.category === category)
    && (status === "all" || status === "completed" && ["completed", "target_met"].includes(run.status)
      || status === "failed" && ["failed", "cancelled", "interrupted", "blocked", "budget_exhausted"].includes(run.status)
      || status === "running" && ["queued", "running"].includes(run.status))
    && `${run.name} ${run.original_name} ${run.category} ${purposeName(run.purpose)}`.toLowerCase().includes(query))
    .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || "") || a.name.localeCompare(b.name, "zh-CN", { numeric: true }));
  state.libraryVisible = runs;
  const visible = new Set(runs.map(run => run.id));
  for (const id of state.librarySelected) if (!visible.has(id)) state.librarySelected.delete(id);
  $("#home-run-count").textContent = `${runs.length} 条`;
  $("#home-runs").innerHTML = runs.length ? runs.map(run => `<article class="run-card">
    <label class="library-choice"><input type="checkbox" data-library-select="${escapeHTML(run.id)}" ${state.librarySelected.has(run.id) ? "checked" : ""} aria-label="选择 ${escapeHTML(run.name)}"></label>
    <div><strong>${escapeHTML(run.name)}</strong><span class="form-hint">${run.purpose === "evolution" ? `${run.generation}/${run.max_generations}代` : `${run.boards}副`} · ${escapeHTML(evolutionStatus(run.status))}${run.category ? ` · ${escapeHTML(run.category)}` : ""}</span>
    <div class="report-actions">${view === "trash" ? `<button class="button small" data-library-restore="${escapeHTML(run.id)}">恢复</button>` : `${run.purpose === "evolution" ? `<button class="button small" data-track-evolution="${escapeHTML(run.id)}">迭代追踪</button>` : run.can_open ? `<button class="button small" data-home-run="${escapeHTML(run.id)}">打开</button>` : `<button class="button small" data-watch-audit="${escapeHTML(run.id)}">任务详情</button>`}<button class="button small" data-library-edit="${escapeHTML(run.id)}">整理</button><button class="button small danger" data-library-trash="${escapeHTML(run.id)}" ${["queued", "running"].includes(run.status) ? 'disabled title="请先取消运行中的任务"' : ""}>删除</button>`}</div></div></article>`).join("")
    : '<p class="form-hint">没有匹配的历史记录。</p>';
  renderLibrarySelection();
}
function renderLibraryCategories() {
  const selected = $("#library-category").value;
  $("#library-category").replaceChildren(new Option("全部分类", "all"), new Option("未分类", ""),
    ...state.library.categories.map(name => new Option(name, name)));
  if ([...$("#library-category").options].some(option => option.value === selected)) $("#library-category").value = selected;
  $("#library-categories").replaceChildren(...state.library.categories.map(name => new Option(name, name)));
}
function renderLibrarySelection() {
  const selected = state.librarySelected.size;
  const trash = $("#library-view").value === "trash";
  $("#library-selected-count").textContent = selected ? `已选${selected}条` : "未选择";
  $("#library-select-all").checked = selected > 0 && selected === state.libraryVisible.length;
  $("#library-select-all").indeterminate = selected > 0 && selected < state.libraryVisible.length;
  $("#library-move").disabled = !selected;
  $("#library-move").hidden = trash;
  $("#library-trash").hidden = trash;
  $("#library-trash").disabled = !selected;
  $("#library-restore").hidden = !trash;
  $("#library-restore").disabled = !selected;
}
function openLibraryEdit(identifiers) {
  const rows = state.library.entries.filter(row => identifiers.includes(row.id));
  if (!rows.length) return;
  state.libraryEditing = identifiers;
  $("#library-edit-title").textContent = rows.length === 1 ? "整理审计" : `分类 ${rows.length} 条审计`;
  $("#library-name-field").hidden = rows.length !== 1;
  $("#library-name").required = rows.length === 1;
  $("#library-name").value = rows[0].name;
  $("#library-category-input").value = rows.every(row => row.category === rows[0].category) ? rows[0].category : "";
  $("#library-edit-error").hidden = true;
  $("#library-edit-dialog").showModal();
}
function confirmLibraryTrash(identifiers) {
  const rows = state.library.entries.filter(row => identifiers.includes(row.id));
  if (!rows.length) return;
  if (rows.some(row => ["queued", "running"].includes(row.status))) return toast("请先取消正在运行或排队的任务。", true);
  state.libraryDeleting = identifiers;
  $("#library-delete-caption").textContent = `将这 ${rows.length} 条记录移入回收站？`;
  $("#library-delete-list").innerHTML = rows.slice(0, 6).map(row => `<li>${escapeHTML(row.name)}</li>`).join("")
    + (rows.length > 6 ? `<li>另 ${rows.length - 6} 条</li>` : "");
  $("#library-delete-error").hidden = true;
  $("#library-delete-dialog").showModal();
}
async function changeLibrary(payload) {
  state.library = await fetchJSON("/api/library", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload) });
  state.librarySelected.clear();
  await refreshHome();
}
async function restoreLibrary(identifiers) {
  $("#library-error").hidden = true;
  try { await changeLibrary({ ids: identifiers, action: "restore" }); toast("已恢复"); }
  catch (error) { $("#library-error").textContent = error.message; $("#library-error").hidden = false; }
}
async function saveLibraryEdit(event) {
  event.preventDefault();
  $("#save-library-edit").disabled = true;
  $("#library-edit-error").hidden = true;
  try {
    const ids = state.libraryEditing;
    await changeLibrary({ ids, action: "update", category: $("#library-category-input").value,
      ...(ids.length === 1 ? { name: $("#library-name").value } : {}) });
    $("#library-edit-dialog").close();
    toast("已保存");
  } catch (error) { $("#library-edit-error").textContent = error.message; $("#library-edit-error").hidden = false; }
  finally { $("#save-library-edit").disabled = false; }
}
async function deleteLibrary(event) {
  event.preventDefault();
  $("#confirm-library-delete").disabled = true;
  $("#library-delete-error").hidden = true;
  try {
    await changeLibrary({ ids: state.libraryDeleting, action: "trash", confirm: true });
    $("#library-delete-dialog").close();
    toast("已移入回收站");
  } catch (error) { $("#library-delete-error").textContent = error.message; $("#library-delete-error").hidden = false; }
  finally { $("#confirm-library-delete").disabled = false; }
}
function evolutionStatus(value) {
  return { ready: "待启动", target_met: "目标达成", blocked: "需处理", budget_exhausted: "预算用尽", eligible: "候选有效",
    rejected: "提案拒绝", duplicate: "重复提案", invalid: "候选无效", screen_rejected: "初筛未入围",
    checked: "完整有效", comparison_blocked: "正式比较受阻",
    no_full_gain: "复测未见提升", insufficient_coverage: "尚未测到行为差异", portfolio_retained: "保留研究分支",
    needs_more_research: "已保存，待继续研究", promoted: "已晋级", not_confirmed: "新牌未确认", allocated: "确认中" }[value] || jobStatus(value);
}
function evolutionPhase(value) {
  const phases = { diagnosis: "随机新牌诊断", validity: "候选协议验证", screen: "共同新牌初筛", full: "新牌复测", promotion: "新牌晋级确认", agent: "Agent分析与提案", research: "Agent自主研究" };
  if (/^g\d+-[ABZ]-research$/.test(value || "")) return `第${Number(value.slice(1, 4))}代 · ${value.split("-")[1]}自主研究`;
  if (value?.startsWith("r-g")) return `研究内探索 · ${value.slice(2, 8)}`;
  const match = /^g0*(\d+)[-:](.*)$/.exec(value || "");
  if (!match) return value === "queued" ? "排队中" : value?.startsWith("goal") ? "10000副目标检测" : value || "";
  const step = match[2].split(/[-:]/)[0];
  return `第${match[1]}代 · ${phases[step] || step}`;
}
function evolutionSource(value) { return { research: "自主研究", agent: "旧版局部提案", numbers: "确定性对照", replay: "录制提案" }[value] || value; }
function budgetNumber(value) { return Number.isFinite(value) ? new Intl.NumberFormat("zh-CN").format(Math.round(value)) : "未记录"; }
async function refreshEvolutionModels() {
  $("#refresh-evolution-models").disabled = true;
  $("#evolution-model-status").textContent = "正在读取…";
  try {
    const catalog = await fetchJSON("/api/evolution-models");
    $("#evolution-models").replaceChildren(...catalog.models.map(model => new Option(model.name, model.id)));
    $("#evolution-model-status").textContent = `${catalog.models.length}个模型可用 · 默认 ${catalog.default}`;
  } catch (error) {
    $("#evolution-model-status").textContent = error.message;
    toast(error.message, true);
  } finally { $("#refresh-evolution-models").disabled = false; }
}
function renderEvolutions() {
  $("#evolution-jobs").innerHTML = state.homeEvolutions.length ? state.homeEvolutions.slice(0, 3).map(run =>
    `<article class="history-item evolution-run-card ${liveStatuses.has(run.status) ? "is-live" : ""}"><div><strong>${escapeHTML(run.name)}</strong><span>${liveStatuses.has(run.status) ? '<i class="live-dot" aria-hidden="true"></i>' : ""}${run.generation}/${run.max_generations}代完成 · ${escapeHTML(evolutionStatus(run.status))}</span></div><p>${escapeHTML(evolutionPhase(run.phase))} · ${escapeHTML(activityText(run))}</p>
      ${run.evaluation_progress?.status === "running" ? `<progress value="${run.evaluation_progress.completed_pairs}" max="${run.evaluation_progress.total_pairs}" aria-label="本轮配对进度"></progress><p class="form-hint">本轮 ${run.evaluation_progress.completed_pairs}/${run.evaluation_progress.total_pairs}个配对完成</p>` : ""}
      <p class="form-hint">${escapeHTML(run.model)} · ${run.agent_requests}次请求 · ${run.promotions}次晋级</p>
      ${Object.keys(run.comparison_issues || {}).length ? '<p class="form-hint">存在固定对手检查问题；研究继续，正式比较受阻。</p>' : ""}
      <div class="report-actions"><button class="button small ${liveStatuses.has(run.status) ? "primary" : ""}" data-track-evolution="${run.id}">${liveStatuses.has(run.status) ? "实时追踪" : "迭代结果与当前体系"}</button><button class="button small" data-watch-evolution="${run.id}">原始详情</button>${run.report_available ? `<a class="button small" href="/api/evolutions/${run.id}/report" download>下载报告</a>` : ""}</div></article>`).join("")
    : '<p class="form-hint">尚无进化实验。</p>';
}
async function submitEvolution(event) {
  event.preventDefault();
  $("#start-evolution").disabled = true;
  $("#start-evolution").textContent = "正在创建…";
  $("#evolution-error").hidden = true;
  try {
    const payload = { system_a: $("#evolution-system-a").value, system_b: $("#evolution-system-b").value,
      profile: $("#evolution-profile").value, source: $("#evolution-source").value,
      generations: Number($("#evolution-generations").value), model: $("#evolution-model").value.trim(),
      reasoning_effort: $("#evolution-effort").value,
      origins: $("#evolution-source").value === "research" ? ($("#evolution-origins").value === "both" ? ["existing", "blank"] : [$("#evolution-origins").value]) : ["existing"],
      max_agent_requests: Number($("#evolution-max-requests").value), max_agent_tokens: Number($("#evolution-max-tokens").value),
      max_output_tokens: Number($("#evolution-max-output").value), max_research_turns: Number($("#evolution-max-turns").value),
      max_tool_calls: Number($("#evolution-max-tools").value), max_pairs: Number($("#evolution-max-pairs").value),
      max_seconds: Number($("#evolution-max-seconds").value), research_seconds: Number($("#evolution-research-seconds").value),
      max_lab_boards: Number($("#evolution-lab-boards").value), max_releases: Number($("#evolution-max-releases").value) };
    if ($("#evolution-task").value.trim()) payload.research_task = $("#evolution-task").value.trim();
    if (state.researchSeedRun) payload.research_seed_run = state.researchSeedRun;
    if (state.championSeedRun) payload.champion_seed_run = state.championSeedRun;
    if ($("#evolution-name").value.trim()) payload.name = $("#evolution-name").value.trim();
    const result = await fetchJSON("/api/evolutions", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload) });
    await refreshHome();
    await openEvolution(result.id);
  } catch (error) { $("#evolution-error").textContent = error.message; $("#evolution-error").hidden = false; }
  finally { $("#start-evolution").disabled = false; $("#start-evolution").textContent = "开始自进化"; }
}
async function openEvolution(identifier) {
  state.watchedEvolution = identifier;
  clearTimeout(state.evolutionTimer);
  $("#evolution-details").textContent = "正在读取进化实验…";
  if (!$("#evolution-dialog").open) $("#evolution-dialog").showModal();
  await watchEvolution(identifier);
}
async function watchEvolution(identifier) {
  try {
    const run = await fetchJSON(`/api/evolutions/${encodeURIComponent(identifier)}`);
    if (state.watchedEvolution !== identifier || !$("#evolution-dialog").open) return;
    if (state.evolutionPairLoading) return;
    const opened = new Set([...$("#evolution-details").querySelectorAll("details[open]")].map(item => item.dataset.evoDetail));
    const scrollTop = $("#evolution-dialog").scrollTop;
    $("#evolution-dialog-title").textContent = run.name;
    const goal = { not_checked: "未运行10000副检测", allocated: "10000副检测中", target_met: "目标达成",
      statistically_better_only: "显著优于起点，未达强目标", not_established: "未确认目标提升",
      invalid: "候选未通过检测", blocked: "检测受阻" }[run.goal.status] || run.goal.status;
    const goalReason = { "No evolved champion; no 10000-board check was consumed.": "尚无晋级的新版本。",
      "Fresh selection evidence is below the target; no 10000-board check was consumed.": "新牌比较尚未显示足够提升。" }[run.goal.reason] || run.goal.reason;
    $("#evolution-details").innerHTML = `<p><strong>${escapeHTML(evolutionStatus(run.status))}</strong> · ${run.generation}/${run.max_generations}代 · 晋级 ${run.promotions} 次</p>
      <p class="form-hint">${escapeHTML(evolutionPhase(run.phase))} · ${escapeHTML(evolutionSource(run.source))} · ${escapeHTML(run.model)}${run.reasoning_effort ? ` / ${escapeHTML(run.reasoning_effort)}` : ""}</p>
      <p class="form-hint">模型请求 ${budgetNumber(run.agent_requests)} / ${budgetNumber(run.budgets?.max_agent_requests)} · 比赛配对 ${budgetNumber(run.evaluated_pairs)} / ${budgetNumber(run.budgets?.max_pairs)} · 已用 ${Math.round(run.spent_seconds / 60)} 分钟</p>
      ${run.model_usage ? `<p class="form-hint">token已结算／预留 ${budgetNumber(run.model_usage.charged_tokens)} / ${budgetNumber(run.budgets?.max_agent_tokens)} · 实际输入 ${budgetNumber(run.model_usage.input_tokens)}，输出 ${budgetNumber(run.model_usage.output_tokens)}${run.model_usage.unreported_requests ? ` · ${run.model_usage.unreported_requests}次请求用量待确认` : ""}</p>` : ""}
      <p>${escapeHTML(goal)}${goalReason ? ` · ${escapeHTML(goalReason)}` : ""}</p>${run.error ? `<p class="form-error">${escapeHTML(run.error)}</p>` : ""}
      <div class="report-actions">${["queued", "running"].includes(run.status) ? `<button class="button small" data-cancel-evolution="${run.id}" ${run.cancel_requested ? "disabled" : ""}>${run.cancel_requested ? "已请求取消" : "取消运行"}</button>` : ["ready", "failed", "interrupted", "cancelled", "blocked"].includes(run.status) ? `<button class="button small" data-resume-evolution="${run.id}">继续实验</button>` : ""}
      ${run.report_available ? `<a class="button small" href="/api/evolutions/${run.id}/report" download>下载报告</a>` : ""}</div>
      ${run.source === "research" && !["queued", "running"].includes(run.status) && run.research_sessions?.some(session => session.releases === 0) ? `<p><button class="button small" data-continue-research="${run.id}">承接未发布工作，创建新实验</button></p>` : ""}
      ${Object.values(run.research_seeds || {}).map(seed => `<p class="form-hint">承接 ${escapeHTML(seed.source_session)} 的已保存工作，当前实验预算单独记录。</p>`).join("")}
      <h3>当前版本</h3>${Object.entries(run.champions).map(([lineage, hash]) => `<p class="form-hint">${escapeHTML(lineage)} · ${lineage === "Z" ? "空白起点原创" : escapeHTML(shortSystem(run.baselines.find(base => base.lineage === lineage)?.name))} · ${escapeHTML(hash.slice(0, 12))}</p>`).join("")}
      ${(run.research_sessions || []).length ? `<h3>自主研究</h3>${run.research_sessions.map(session => `<article class="history-item"><strong>${escapeHTML(session.id)} · ${escapeHTML(evolutionStatus(session.status))}</strong><p>${session.turns}轮模型 · ${session.tool_calls}次工具 · ${session.releases}个完整候选</p><button class="button small" data-research-session="/api/evolutions/${run.id}/research/${session.id}">查看研究、调试与讨论</button></article>`).join("")}` : ""}
      <h3>代际结果</h3>${(run.generations || []).map(generation => `<details class="evolution-generation" data-evo-detail="generation-${generation.number}"><summary>第 ${generation.number} 代 · ${generation.candidates.length} 个候选</summary><div class="report-actions">${Object.keys(generation.analysis || {}).map(lineage => `<a class="button small" href="/api/evolutions/${run.id}/proposals/g${String(generation.number).padStart(3, "0")}-${lineage}" target="_blank" rel="noopener">${escapeHTML(lineage)}模型回答</a>`).join("")}</div>${generation.candidates.map(candidate => `<article class="history-item"><strong>${escapeHTML(evolutionStatus(candidate.status))} · ${candidate.lineage}</strong><p>${escapeHTML(candidate.hypothesis)}</p>${candidate.error ? `<p class="form-error">${escapeHTML(candidate.error)}</p>` : ""}${candidate.errors?.length ? `<details data-evo-detail="errors-${candidate.hash}"><summary>${candidate.error_count} 项检查问题${candidate.error_count > candidate.errors.length ? `（展示 ${candidate.errors.length} 项）` : ""}</summary><ul>${candidate.errors.map(error => `<li>第 ${error.board} 副 · ${auditErrorMarkup(error)}</li>`).join("")}</ul></details>` : ""}${candidate.full_delta != null ? `<p>复测配对提升 ${sign(Number(candidate.full_delta.toFixed(3)))} IMP/副${candidate.changes ? ` · ${candidate.changes.changed_families}副改变叫牌` : ""}</p>` : ""}${candidate.tests ? `<p>发布前实际回归 ${candidate.tests.passed}/${candidate.tests.tests} 通过</p>` : ""}${candidate.hash ? `<div class="report-actions"><a class="button small" href="/api/evolutions/${run.id}/systems/${candidate.hash}.json" download>完整体系</a><a class="button small" href="/api/evolutions/${run.id}/systems/${candidate.hash}.md" download>完整说明</a></div>` : ""}</article>`).join("")}${Object.entries(generation.promotions).map(([lineage, value]) => `<p>${lineage} · ${escapeHTML(evolutionStatus(value.status))}${value.statistics ? ` · 新牌提升 ${Number(value.statistics.mean.toFixed(3))}，下界 ${Number(value.statistics.lower.toFixed(3))}` : ""}</p>`).join("")}</details>`).join("")}
      <h3>查看牌例</h3>${(run.rounds || []).filter(round => round.pairs?.length).map(round => `<details class="evolution-generation" data-evo-detail="round-${round.id}"><summary>${escapeHTML(evolutionPhase(round.id))} · ${round.boards} 副</summary><div class="report-actions">${round.pairs.flatMap(pair => Array.from({ length: Math.ceil(round.boards / 1000) }, (_, index) => `<button class="button small" data-evolution-pair="/api/evolutions/${run.id}/rounds/${round.id}/pairs/${pair.candidate}/${pair.opponent}?offset=${index * 1000}&amp;limit=1000">${escapeHTML(pair.candidate.slice(0, 6))} 对 ${escapeHTML(pair.opponent.slice(0, 6))}${round.boards > 1000 ? ` · 第${index * 1000 + 1}–${Math.min(round.boards, (index + 1) * 1000)}副` : ""}</button>`)).join("")}</div></details>`).join("")}
      <p id="evolution-action-error" class="form-error" role="alert" hidden></p>`;
    if (["queued", "running"].includes(run.status)) state.evolutionTimer = setTimeout(() => watchEvolution(identifier), 2000);
    for (const item of $("#evolution-details").querySelectorAll("details")) if (opened.has(item.dataset.evoDetail)) item.open = true;
    $("#evolution-dialog").scrollTop = scrollTop;
    icons();
  } catch (error) {
    if (state.watchedEvolution !== identifier || !$("#evolution-dialog").open) return;
    $("#evolution-details").innerHTML = `<p class="form-error">${escapeHTML(error.message)}</p><button class="button small" data-watch-evolution="${escapeHTML(identifier)}">重试</button>`;
  }
}
async function prepareEvolutionContinuation(run, kind) {
  state.researchSeedRun = kind === "workspace" ? run.id : null;
  state.championSeedRun = kind === "champion" ? run.id : null;
  if ($("#evolution-dialog").open) $("#evolution-dialog").close();
  await showHome();
  $("#evolution-controls").open = true;
  $("#evolution-source").value = "research";
  $("#evolution-model").value = run.model;
  $("#evolution-system-a").value = run.baselines[0].hash;
  $("#evolution-system-b").value = run.baselines[1].hash;
  $("#evolution-origins").disabled = false;
  $("#evolution-origins").value = run.champions.Z ? (run.champions.A ? "both" : "blank") : "existing";
  $("#evolution-name").value = `${run.name} · ${kind === "champion" ? "冠军续跑" : "草稿续接"}`;
  $("#evolution-generations").value = kind === "champion" ? "3" : "1";
  $("#evolution-profile").value = run.profile;
  $("#evolution-max-turns").value = String(run.budgets.max_research_turns || 48);
  $("#evolution-task").value = "继续已保存的体系设计，不要从头重建。先检查现有测试、研究记录和试验结果，必要时继续修复，实际复测通过后发布完整候选；未解决的问题如实记录。";
  $("#evolution-continuation-caption").textContent = `承接「${run.name}」的${kind === "champion" ? "已冻结冠军" : "未发布工作区"}、案例与测试；不复制成绩或清零旧用量。请确认新实验模型和预算后开始。`;
  $("#evolution-continuation").hidden = false;
  $("#evolution-controls").scrollIntoView({ block: "start" });
}

async function evolutionAction(event) {
  const watch = event.target.closest("[data-watch-evolution]");
  const cancel = event.target.closest("[data-cancel-evolution]");
  const resume = event.target.closest("[data-resume-evolution]");
  const pair = event.target.closest("[data-evolution-pair]");
  const research = event.target.closest("[data-research-session]");
  const continuation = event.target.closest("[data-continue-research]");
  const pairLabel = pair?.textContent;
  if (pair && state.evolutionPairLoading) return;
  try {
    if (watch) await openEvolution(watch.dataset.watchEvolution);
    if (research) await openResearch(research.dataset.researchSession);
    if (continuation) {
      const run = await fetchJSON(`/api/evolutions/${continuation.dataset.continueResearch}`);
      await prepareEvolutionContinuation(run, "workspace");
    }
    if (cancel || resume) {
      const id = cancel?.dataset.cancelEvolution || resume.dataset.resumeEvolution;
      (cancel || resume).disabled = true;
      await fetchJSON(`/api/evolutions/${id}/${cancel ? "cancel" : "resume"}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      await watchEvolution(id);
    }
    if (pair) {
      state.evolutionPairLoading = true;
      clearTimeout(state.evolutionTimer);
      pair.disabled = true;
      pair.textContent = "正在读取牌例…";
      const response = await fetch(pair.dataset.evolutionPair);
      if (!response.ok) throw new Error((await response.json()).error);
      $("#evolution-dialog").close();
      await importFile(new File([await response.blob()], "evolution-pair.zip", { type: "application/zip" }));
    }
  } catch (error) {
    toast(error.message, true);
    if ($("#evolution-action-error")) {
      $("#evolution-action-error").textContent = error.message;
      $("#evolution-action-error").hidden = false;
    }
  } finally {
    if (cancel?.isConnected) cancel.disabled = false;
    if (resume?.isConnected) resume.disabled = false;
    if (pair) {
      state.evolutionPairLoading = false;
      if (pair.isConnected) { pair.disabled = false; pair.textContent = pairLabel; }
      if ($("#evolution-dialog").open && state.watchedEvolution) state.evolutionTimer = setTimeout(() => watchEvolution(state.watchedEvolution), 2000);
    }
  }
}
async function openResearch(url) {
  state.researchURL = url;
  state.researchOffset = null;
  state.researchToolView = null;
  clearTimeout(state.researchTimer);
  $("#research-details").textContent = "正在读取研究日志…";
  if (!$("#research-dialog").open) $("#research-dialog").showModal();
  await watchResearch();
}

async function watchResearch() {
  const url = state.researchURL;
  try {
    const run = await fetchJSON(url + (state.researchOffset == null ? "" : `?offset=${state.researchOffset}`));
    if (state.researchURL !== url || !$("#research-dialog").open) return;
    const opened = new Set([...$("#research-details").querySelectorAll("details[open]")].map(item => item.dataset.researchDetail));
    const scrollTop = $("#research-dialog").scrollTop;
    const savedTool = state.researchToolView?.url === url ? state.researchToolView.text : null;
    $("#research-dialog-title").textContent = `${run.id} · 自主研究`;
    $("#research-details").innerHTML = `<p><strong>${escapeHTML(evolutionStatus(run.status))}</strong> · ${escapeHTML(run.model)} · ${run.turns}轮 · ${run.tool_calls}次工具</p>
      ${run.summary ? `<p>${escapeHTML(run.summary)}</p>` : ""}
      <details class="technical-details" data-research-detail="prompt"><summary>实际系统prompt</summary><pre>${escapeHTML(run.prompt)}</pre></details>
      <h3>研究记录与讨论</h3>${run.notes.length ? run.notes.map(note => `<article class="history-item"><strong>${note.kind === "review" ? "独立讨论" : note.kind === "handoff" ? "上下文交接" : "研究记录"}</strong>${note.question ? `<p>${escapeHTML(note.question)}</p>` : ""}<p class="research-text">${escapeHTML(note.text)}</p></article>`).join("") : '<p class="form-hint">尚无研究记录。</p>'}
      ${run.tests ? `<h3>实际测试</h3><p>${run.tests.passed}/${run.tests.tests}通过 · ${run.tests.failed}失败 · ${run.tests.change_witnesses}项行为变化验证</p>` : ""}
      <h3>工具与调试历史</h3><p class="form-hint">第${run.total_events ? run.offset + 1 : 0}–${Math.min(run.total_events, run.offset + run.limit)}条／共${run.total_events}条</p>
      ${run.events.map((item, index) => item.type === "tool_result" ? `<details class="technical-details" data-research-detail="event-${run.offset + index}"><summary>${escapeHTML(item.name)} · ${item.ok ? "完成" : "返回问题"}</summary><pre>${escapeHTML(item.summary)}</pre><button class="button small" data-research-tool="${escapeHTML(item.call_id)}">查看完整输入与输出</button></details>` : `<article class="history-item"><p class="research-text">${escapeHTML(item.text || item.summary || item.type)}</p></article>`).join("")}
      <div class="report-actions">${run.offset > 0 ? `<button class="button small" data-research-offset="${Math.max(0, run.offset - run.limit)}">上一页</button>` : ""}${run.offset + run.limit < run.total_events ? `<button class="button small" data-research-offset="${run.offset + run.limit}">下一页</button>` : ""}<button class="button small" data-research-latest>最新记录</button></div>
      <pre id="research-tool-output" class="research-tool-output" ${savedTool ? "" : "hidden"}>${savedTool ? escapeHTML(savedTool) : ""}</pre>`;
    for (const item of $("#research-details").querySelectorAll("details")) if (opened.has(item.dataset.researchDetail)) item.open = true;
    $("#research-dialog").scrollTop = scrollTop;
    if (["ready", "running"].includes(run.status)) state.researchTimer = setTimeout(watchResearch, 2500);
  } catch (error) {
    if (state.researchURL === url && $("#research-dialog").open) $("#research-details").innerHTML = `<p class="form-error">${escapeHTML(error.message)}</p><button class="button small" data-research-latest>重试</button>`;
  }
}

async function refreshHome() {
  $("#refresh-home").disabled = true;
  try {
    const [catalog, history, library, evolutions] = await Promise.all([fetchJSON("/api/catalog"), fetchJSON("/api/audits"),
      fetchJSON("/api/library"), fetchJSON("/api/evolutions")]);
    state.catalog = catalog;
    state.homeJobs = history.jobs;
    state.library = library;
    state.homeEvolutions = evolutions.experiments;
    syncRunOptions();
    renderHomeJobs();
    renderLibraryCategories();
    renderHomeRuns();
    renderEvolutions();
  } catch (error) {
    $("#home-jobs").textContent = `历史记录载入失败：${error.message}`;
    toast(error.message, true);
  } finally {
    $("#refresh-home").disabled = false;
    clearTimeout(state.homeTimer);
    if (state.isHome && (state.homeJobs.some(job => ["queued", "running"].includes(job.status))
        || state.homeEvolutions.some(run => ["queued", "running"].includes(run.status)))) {
      state.homeTimer = setTimeout(refreshHome, 3000);
    }
  }
}
function samplingFields() {
  $("#sampling-fields").innerHTML = seats.map(seat => `<fieldset><legend>${seat} ${seatNames[seat]}家</legend>${["hcp", "S", "H", "D", "C"].map(field =>
    `<label>${field === "hcp" ? "HCP" : symbols[field] + "张数"}<span><input id="sample-${seat}-${field}-min" type="number" min="0" max="${field === "hcp" ? 40 : 13}" placeholder="下界" aria-label="${seat}${field}下界"><span>–</span><input id="sample-${seat}-${field}-max" type="number" min="0" max="${field === "hcp" ? 40 : 13}" placeholder="上界" aria-label="${seat}${field}上界"></span></label>`).join("")}</fieldset>`).join("");
}
function batchPayload() {
  const constraints = {};
  for (const seat of seats) for (const field of ["hcp", "S", "H", "D", "C"]) {
    const low = $(`#sample-${seat}-${field}-min`).value;
    const high = $(`#sample-${seat}-${field}-max`).value;
    if (low === "" && high === "") continue;
    constraints[seat] ||= {};
    constraints[seat][field] = [low === "" ? 0 : Number(low), high === "" ? field === "hcp" ? 40 : 13 : Number(high)];
  }
  const payload = { count: Number($("#batch-count").value),
    dealer: $("#batch-dealer").value, vulnerability: $("#batch-vul").value, constraints,
    system_a: $("#batch-system-a").value, system_b: $("#batch-system-b").value };
  if ($("#batch-seed").value !== "") payload.seed = Number($("#batch-seed").value);
  if ($("#batch-workers").value !== "") payload.workers = Number($("#batch-workers").value);
  if ($("#batch-label").value.trim()) payload.label = $("#batch-label").value.trim();
  if ($("#batch-attempt-limit").value !== "") payload.attempt_limit = Number($("#batch-attempt-limit").value);
  return payload;
}
async function submitBatch(event, preview = false) {
  event?.preventDefault();
  const button = preview ? $("#validate-sampling") : $("#start-batch");
  button.disabled = true;
  $("#batch-error").hidden = true;
  try {
    if (!$("#batch-form").reportValidity()) return;
    const result = await fetchJSON(preview ? "/api/batches/validate" : "/api/batches", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(batchPayload()) });
    if (preview) {
      $("#batch-error").textContent = `条件校验通过，可开始审计。`;
      $("#batch-error").classList.add("success-message");
      $("#batch-error").hidden = false;
    } else {
      renderAuditTask(result);
      await openTask(result.id, result);
    }
  } catch (error) {
    $("#batch-error").textContent = error.message;
    $("#batch-error").classList.remove("success-message");
    $("#batch-error").hidden = false;
  } finally { button.disabled = false; }
}
function renderFrequency(board) {
  const [table, turn] = state.trace;
  const step = board.tables[table]?.auction[turn];
  $("#call-frequency").hidden = !step;
  if (!step || !state.frequencies) return;
  const data = state.frequencies.get(board, table, turn, $("#frequency-scope").value);
  const percentage = value => value == null ? "—" : `${value.toFixed(2)}%`;
  $("#frequency-content").innerHTML = `<p class="frequency-prefix">${escapeHTML(data.prefix.join(" ") || "开局")} → <strong>${escapeHTML(data.call)}</strong></p><dl class="frequency-metrics"><div><dt>相同序列出现</dt><dd>${data.matches}桌 / ${data.matchedBoards}副</dd></div><div><dt>当前局面选择率</dt><dd>${data.matches} / ${data.opportunities} = ${percentage(data.conditionalPercent)}</dd></div><div><dt>占全批牌例</dt><dd>${data.matchedBoards} / ${data.totalBoards} = ${percentage(data.allBoardsPercent)}</dd></div></dl><p class="form-hint">其他选择：${data.alternatives.filter(item => item.call !== data.call).map(item => `${escapeHTML(item.call)} ${item.matches}次（${percentage(item.percent)}）`).join("；") || "无"}</p><details class="technical-details"><summary>统计口径</summary><p class="form-hint">全批${data.totalBoards}副、${data.totalTables}桌，不随筛选改变。${data.scope === "strict" ? "匹配完整叫牌前缀、体系、座位和局况。" : "匹配完整前缀和相对发牌人的体系，忽略绝对座位及局况。"}未记录的下一叫不计入选择次数。${!data.identifiedSystems ? "导入记录缺少体系版本。" : ""}</p></details>`;
}
function humanRequest(board, run) {
  if (run) return { source_run: run, board_id: board.id };
  return { board: { ...Object.fromEntries(["dealer", "vulnerability", "hands", "number", "index"].map(key => [key, board[key]])),
    tables: board.tables.map(table => ({ systems: table.systems })) } };
}
async function fetchHuman(board, force = false) {
  const key = ddsKey(board);
  if (state.humanLoading.has(key) && !force) return;
  const requestId = (state.humanRequests.get(key) || 0) + 1;
  state.humanRequests.set(key, requestId);
  state.humanLoading.add(key);
  try {
    const result = await fetchJSON("/api/human-auctions/query", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ board: Object.fromEntries(["dealer", "vulnerability", "hands"].map(name => [name, board[name]])) }) });
    if (state.humanRequests.get(key) !== requestId) return;
    state.humanCache.set(key, result.references);
    if (current() && ddsKey(current()) === key) renderHumanSummary(current());
    if (state.humanInput && ddsKey(state.humanInput.board) === key && $("#human-dialog").open) renderHumanHistory();
  } catch (error) {
    if (current() && ddsKey(current()) === key) $("#human-summary").textContent = `参考载入失败：${error.message}`;
    if ($("#human-dialog").open) humanError(error.message);
  } finally {
    if (state.humanRequests.get(key) === requestId) state.humanLoading.delete(key);
  }
}
function renderHumanSummary(board) {
  const key = ddsKey(board);
  if (!state.humanCache.has(key)) {
    $("#human-summary").textContent = "正在读取本副的人类参考…";
    fetchHuman(board);
    return;
  }
  const references = state.humanCache.get(key);
  $("#human-summary").textContent = references.length
    ? `已保存${references.length}个版本；最近：${references[0].label} · ${references[0].table === "table1" ? "第一桌" : "第二桌"} · ${references[0].complete ? "完整序列" : "部分前缀"}`
    : "尚无参考，可填写你建议的叫牌。";
}
function humanError(message, success = false) {
  $("#human-error").textContent = message;
  $("#human-error").classList.toggle("success-message", success);
  $("#human-error").hidden = false;
}
function humanSystems() {
  const table = state.humanInput?.board.tables[Number($("#human-table").value.slice(-1)) - 1];
  $("#human-systems").textContent = table
    ? ["NS", "EW"].map(side => `${side}：${shortSystem(table.systems?.[side]?.name)}`).join("；")
    : "该桌尚未绑定体系，参考按独立手工序列保存。";
}
function openHuman() {
  const board = current();
  if (!board) return;
  const same = state.humanInput && ddsKey(state.humanInput.board) === ddsKey(board);
  state.humanInput = { board, run: state.interactive || state.bundle.imported ? null : $("#run-select").value };
  if (!same) {
    $("#human-table").value = `table${state.trace[0] + 1}`;
    $("#human-label").value = "人类参考";
    $("#human-calls").value = "";
    $("#human-note").value = "";
  }
  $("#human-board-label").textContent = `第${board.number}副 · ${board.dealer}发牌 · ${vulNames[board.vulnerability]}。按相同手牌、发牌人和局况关联，可跨重跑查看。`;
  $("#human-error").hidden = true;
  $("#human-preview").hidden = true;
  humanSystems();
  renderHumanHistory();
  $("#human-dialog").showModal();
  fetchHuman(board);
}
function renderHumanHistory() {
  const records = state.humanCache.get(ddsKey(state.humanInput.board)) || [];
  $("#human-history").innerHTML = records.length ? records.map(record => `<article class="history-item"><div><strong>${escapeHTML(record.label)} · 第${record.revision}版</strong><span>${record.table === "table1" ? "第一桌" : "第二桌"} · ${record.complete ? "完整" : "前缀"}</span></div><p>${escapeHTML(Object.entries(record.systems).map(([side, system]) => `${side} ${shortSystem(system.name)} ${system.hash?.slice(0, 8) || "无哈希"}`).join("；") || "未绑定体系")}</p><p>${escapeHTML(record.auction.map(step => `${step.seat} ${step.call}`).join(" · "))}</p><p>${escapeHTML(record.note)}</p><div class="report-actions"><button class="button small" data-load-human="${record.id}">载入草稿</button></div></article>`).join("") : '<p class="form-hint">暂无保存版本。</p>';
}
async function submitHuman(event, preview = false) {
  event?.preventDefault();
  const input = state.humanInput;
  const button = preview ? $("#preview-human") : $("#save-human");
  button.disabled = true;
  $("#human-error").hidden = true;
  try {
    const payload = preview ? { board: input.board } : humanRequest(input.board, input.run);
    Object.assign(payload, { calls: $("#human-calls").value, table: $("#human-table").value,
      label: $("#human-label").value, note: $("#human-note").value });
    const result = await fetchJSON(preview ? "/api/human-auctions/preview" : "/api/human-auctions", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    $("#human-preview").hidden = false;
    $("#human-preview").textContent = `${result.complete ? "合法完整叫牌" : `合法部分前缀，下一家${result.next_seat}`}：${result.auction.map(step => `${step.seat} ${step.call}`).join(" · ")}${result.contract ? `；终局${contractText({ contract: result.contract })}` : ""}`;
    if (!preview) {
      humanError(`已保存第${result.revision}版。旧版本保留，系统叫牌和成绩未改写。`, true);
      await fetchHuman(input.board, true);
      toast("人类参考已持久保存");
    }
  } catch (error) { humanError(error.message); }
  finally { button.disabled = false; }
}

async function shareBoard() {
  if (state.bundle.imported) return toast("文件导入记录只在当前页面；请先导出审查JSON，或另存到自定义牌例库后分享。", true);
  updateURL();
  $("#share-url").value = location.href;
  try {
    if (!navigator.clipboard) throw new Error("此浏览器未开放剪贴板");
    await navigator.clipboard.writeText(location.href);
    toast("链接已复制，包含筛选条件和选中的叫品。");
  } catch (error) {
    $("#share-dialog").showModal();
    $("#share-url").select();
    toast(`无法自动复制（${error.message}），请复制弹窗中的链接。`);
  }
}
const systemBrowser = createSystemBrowser($("#system-browser"), { escapeHTML, fetchJSON, toast });
async function showSystem(identifier, options = {}) {
  if (!identifier) return toast("该导入记录未附体系标识。", true);
  const loadId = state.systemLoad = (state.systemLoad || 0) + 1;
  $("#system-title").textContent = "体系内容";
  $("#system-hash").textContent = identifier;
  $("#system-specification").textContent = "";
  $("#system-full-specification").open = false;
  state.systemSpec = null;
  state.systemHash = identifier;
  state.systemPromise = null;
  state.systemPage = 0;
  $("#system-page").textContent = "0 / 0";
  $("#download-system").disabled = false;
  $("#system-previous").disabled = true;
  $("#system-next").disabled = true;
  if (!$("#system-dialog").open) $("#system-dialog").showModal();
  const catalog = await systemBrowser.open({ endpoint: `/api/systems/${encodeURIComponent(identifier)}/query`,
    systems: state.catalog?.systems || [], ...options });
  if (catalog && loadId === state.systemLoad) $("#system-title").textContent = catalog.name;
}
async function loadFullSystem() {
  if (state.systemSpec !== null) return state.systemSpec;
  if (state.systemPromise) return state.systemPromise;
  const loadId = state.systemLoad, identifier = state.systemHash;
  $("#system-specification").textContent = "正在读取完整规范…";
  const promise = fetchJSON(`/api/systems/${encodeURIComponent(identifier)}`).then(system => {
    if (loadId !== state.systemLoad || !$("#system-dialog").open) return null;
    state.systemSpec = system.specification;
    renderSystemPage();
    return state.systemSpec;
  }).catch(error => {
    if (loadId !== state.systemLoad || !$("#system-dialog").open) return null;
    throw error;
  });
  state.systemPromise = promise;
  try { return await promise; }
  finally { if (state.systemPromise === promise) state.systemPromise = null; }
}
function renderSystemPage() {
  const size = 16000;
  const count = Math.max(1, Math.ceil(state.systemSpec.length / size));
  state.systemPage = Math.max(0, Math.min(state.systemPage, count - 1));
  $("#system-specification").textContent = state.systemSpec.slice(state.systemPage * size, (state.systemPage + 1) * size);
  $("#system-page").textContent = `${state.systemPage + 1} / ${count}`;
  $("#system-previous").disabled = state.systemPage === 0;
  $("#system-next").disabled = state.systemPage === count - 1;
  $("#system-dialog").scrollTop = 0;
}

$("#board-list").addEventListener("click", (event) => { const item = event.target.closest("[data-board]"); if (item) setActive(item.dataset.board); });
$("#run-audit-findings").addEventListener("click", (event) => {
  const button = event.target.closest("[data-audit-board]");
  if (!button) return;
  const board = state.bundle.boards.find((item) => item.number === Number(button.dataset.auditBoard));
  if (!board) { toast("审计问题牌不在当前冻结记录中。", true); return; }
  resetFilters(false);
  $("#difference-filter").value = "audit";
  applyFilters(false);
  setActive(board.id);
});
$("#batch-audit-summary").addEventListener("click", (event) => {
  const only = event.target.closest("[data-failed-only]");
  const button = event.target.closest("[data-error-board]");
  if (!only && !button) return;
  resetFilters(false);
  $("#difference-filter").value = "audit-failed";
  applyFilters(false);
  if (button) {
    const board = state.filtered.find(item => item.number === Number(button.dataset.errorBoard));
    if (!board) return toast("失败牌不在当前审计记录中。", true);
    setActive(board.id);
    $("#board-audit").scrollIntoView({ block: "center" });
  }
});
$("#search").addEventListener("input", () => applyFilters());
$("#home-button").addEventListener("click", showHome);
$("#cancel-run-load").addEventListener("click", showHome);
$("#retry-run-load").addEventListener("click", () => {
  const attempt = state.lastRunLoad;
  if (attempt) loadRun(attempt.identifier, attempt.requestedNumber, { refreshCatalog: attempt.refreshCatalog });
});
$(".brand").addEventListener("click", (event) => { event.preventDefault(); showHome(); });
$("#refresh-home").addEventListener("click", refreshHome);
$("#history-search").addEventListener("input", renderHomeRuns);
$("#home-runs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-home-run]");
  if (button) openSavedRun(button.dataset.homeRun).catch(error => toast(error.message, true));
  const edit = event.target.closest("[data-library-edit]");
  const remove = event.target.closest("[data-library-trash]");
  const restore = event.target.closest("[data-library-restore]");
  if (edit) openLibraryEdit([edit.dataset.libraryEdit]);
  if (remove) confirmLibraryTrash([remove.dataset.libraryTrash]);
  if (restore) restoreLibrary([restore.dataset.libraryRestore]);
});
$("#home-runs").addEventListener("click", auditAction);
$("#home-runs").addEventListener("click", evolutionAction);
$("#evolution-jobs").addEventListener("click", evolutionAction);
$("#evolution-details").addEventListener("click", evolutionAction);
$("#evolution-form").addEventListener("submit", submitEvolution);
$("#refresh-evolution-models").addEventListener("click", refreshEvolutionModels);
$("#evolution-profile").addEventListener("change", () => { $("#evolution-max-turns").value = $("#evolution-profile").value === "smoke" ? "16" : "48"; });
$("#evolution-source").addEventListener("change", () => {
  $("#evolution-origins").disabled = $("#evolution-source").value !== "research";
  if ($("#evolution-source").value !== "research") { state.researchSeedRun = null; state.championSeedRun = null; $("#evolution-continuation").hidden = true; }
});
$("#clear-evolution-continuation").addEventListener("click", () => { state.researchSeedRun = null; state.championSeedRun = null; $("#evolution-continuation").hidden = true; });
$("#research-dialog").addEventListener("close", () => { clearTimeout(state.researchTimer); state.researchURL = null; });
$("#research-details").addEventListener("click", async event => {
  const page = event.target.closest("[data-research-offset]");
  const latest = event.target.closest("[data-research-latest]");
  const tool = event.target.closest("[data-research-tool]");
  if (page || latest) { clearTimeout(state.researchTimer); state.researchOffset = page ? Number(page.dataset.researchOffset) : null; await watchResearch(); }
  if (tool) {
    tool.disabled = true;
    const url = state.researchURL;
    try {
      const result = await fetchJSON(url + `?tool_call=${encodeURIComponent(tool.dataset.researchTool)}`);
      if (state.researchURL !== url || !$("#research-dialog").open) return;
      state.researchToolView = { url, text: JSON.stringify(result, null, 2) };
      $("#research-tool-output").textContent = state.researchToolView.text;
      $("#research-tool-output").hidden = false;
      $("#research-tool-output").scrollIntoView({ block: "nearest" });
    } catch (error) { toast(error.message, true); }
    finally { if (tool.isConnected) tool.disabled = false; }
  }
});
$("#home-runs").addEventListener("change", (event) => {
  const input = event.target.closest("[data-library-select]");
  if (!input) return;
  if (input.checked) state.librarySelected.add(input.dataset.librarySelect);
  else state.librarySelected.delete(input.dataset.librarySelect);
  renderLibrarySelection();
});
for (const id of ["#library-category", "#library-view", "#library-status"]) $(id).addEventListener("change", () => {
  state.librarySelected.clear();
  renderHomeRuns();
});
$("#library-select-all").addEventListener("change", (event) => {
  state.librarySelected = new Set(event.target.checked ? state.libraryVisible.map(row => row.id) : []);
  renderHomeRuns();
});
$("#library-move").addEventListener("click", () => openLibraryEdit([...state.librarySelected]));
$("#library-trash").addEventListener("click", () => confirmLibraryTrash([...state.librarySelected]));
$("#library-restore").addEventListener("click", () => restoreLibrary([...state.librarySelected]));
$("#library-edit-form").addEventListener("submit", saveLibraryEdit);
$("#library-delete-form").addEventListener("submit", deleteLibrary);
$("#retry-board-load").addEventListener("click", () => {
  if (state.activeId) { state.boardErrors.delete(state.activeId); renderBoard(); }
});
$("#raw-record").addEventListener("toggle", () => {
  const board = current();
  $("#raw-json").textContent = $("#raw-record").open && board && !board.details_pending
    ? JSON.stringify({ ...board, review: annotation(board) }, null, 2) : "";
});
$("#home-jobs").addEventListener("click", auditAction);
$("#all-audit-history").addEventListener("click", showHistory);
$("#batch-form").addEventListener("submit", (event) => submitBatch(event));
$("#validate-sampling").addEventListener("click", () => submitBatch(null, true));
$("#reset-sampling").addEventListener("click", () => { for (const input of $("#sampling-fields").querySelectorAll("input")) input.value = ""; });
$("#frequency-scope").addEventListener("change", () => { if (current()) renderFrequency(current()); });
$("#edit-human").addEventListener("click", openHuman);
$("#human-table").addEventListener("change", humanSystems);
$("#human-form").addEventListener("submit", (event) => submitHuman(event));
$("#preview-human").addEventListener("click", () => submitHuman(null, true));
$("#copy-human-auction").addEventListener("click", () => {
  const table = state.humanInput.board.tables[Number($("#human-table").value.slice(-1)) - 1];
  if (!table?.auction.length) return humanError("所选桌没有系统序列，请手工输入。");
  $("#human-calls").value = table.auction.map(step => step.call).join(" ");
  $("#human-preview").hidden = true;
  humanError("已复制为草稿；可以修改，保存时不会覆盖系统记录。", true);
});
$("#clear-human-auction").addEventListener("click", () => {
  $("#human-calls").value = "";
  $("#human-preview").hidden = true;
});
$("#human-history").addEventListener("click", (event) => {
  const button = event.target.closest("[data-load-human]");
  const record = (state.humanCache.get(ddsKey(state.humanInput.board)) || []).find(item => item.id === button?.dataset.loadHuman);
  if (!record) return;
  $("#human-table").value = record.table;
  $("#human-label").value = record.label;
  $("#human-calls").value = record.calls.join(" ");
  $("#human-note").value = record.note;
  $("#human-preview").hidden = true;
  humanSystems();
  humanError("已载入历史草稿；再保存会创建新版本。", true);
});
$("#difference-filter").addEventListener("change", () => applyFilters(false));
$("#sort").addEventListener("change", () => applyFilters());
$("#dds-panel").addEventListener("click", (event) => { if (event.target.closest("#solve-dds")) solveDDS(); });
$("#run-slam-summary").addEventListener("click", (event) => {
  const button = event.target.closest("[data-slam-filter]");
  if (!button) return;
  resetFilters(false);
  $("#slam-filter").value = button.dataset.slamFilter;
  $("#advanced-filters").open = true;
  applyFilters(false);
});
for (const id of Object.keys(filterDefaults)) {
  $("#" + id).addEventListener($("#" + id).tagName === "SELECT" ? "change" : "input", () => applyFilters());
}
$("#reset-filters").addEventListener("click", () => resetFilters());
$("#load-more").addEventListener("click", () => { state.limit += 80; renderList(); });
$("#previous-board").addEventListener("click", () => navigate(-1));
$("#next-board").addEventListener("click", () => navigate(1));
$("#mobile-list-toggle").addEventListener("click", () => $(".sidebar").classList.toggle("expanded"));
$("#clear-filter").addEventListener("click", () => resetFilters());
$("#auction-tables").addEventListener("click", (event) => {
  const bid = event.target.closest("[data-turn]");
  if (bid) { state.trace = [Number(bid.dataset.table), Number(bid.dataset.turn)]; renderAuctions(current()); renderTrace(current()); updateURL(); }
  const system = event.target.closest("[data-system]");
  if (system) showSystem(system.dataset.system);
});
$("#flag-button").addEventListener("click", () => saveAnnotation(annotation(current()).status === "flagged" ? "unreviewed" : "flagged"));
$("#save-note").addEventListener("click", () => saveAnnotation());
$("#import-button").addEventListener("click", () => $("#import-file").click());
$("#import-file").addEventListener("change", (event) => importFile(event.target.files[0]));
$("#export-menu").addEventListener("click", (event) => { const button = event.target.closest("[data-export]"); if (button) exportBoards(button.dataset.export); });
$("#run-select").addEventListener("change", (event) => {
  const imported = state.imports?.get(event.target.value);
  if (imported) setBundle({ ...imported, imported: true }); else loadRun(event.target.value);
});
$("#compare-button").addEventListener("click", openCompare);
$("#compare-form").addEventListener("submit", compare);
$("#custom-button").addEventListener("click", () => {
  $("#copy-current-deal").disabled = !currentBase();
  $("#custom-error").hidden = true;
  $("#custom-dialog").showModal();
  customCounts();
});
$("#copy-current-deal").addEventListener("click", () => {
  const board = currentBase();
  if (board) { fillCustom(board); if (!$("#custom-label").value) $("#custom-label").value = `第${board.number}副的编辑副本`; customMessage("已复制手牌到草稿；修改或保存不会覆盖原记录。"); }
});
$("#custom-format").addEventListener("change", customFormat);
for (const seat of seats) $("#custom-" + seat).addEventListener("input", customCounts);
$("#random-deal").addEventListener("click", randomCustom);
$("#custom-form").addEventListener("submit", (event) => submitCustom(event));
$("#validate-custom").addEventListener("click", () => submitCustom(null, true));
$("#audit-button").addEventListener("click", openAudit);
$("#audit-form").addEventListener("submit", startAudit);
$("#audit-history-button").addEventListener("click", showHistory);
for (const id of ["#active-audit", "#audit-history-list", "#single-audit-report", "#task-dialog-content"]) $(id).addEventListener("click", auditAction);
$("#hide-audit-task").addEventListener("click", () => { state.hiddenAudit = state.auditJob?.id; $("#active-audit").hidden = true; });
$("#share-board").addEventListener("click", shareBoard);
$("#jump-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const number = Number($("#jump-number").value);
  const board = state.bundle?.boards.find((item) => item.number === number);
  if (!Number.isSafeInteger(number) || !board) return toast("当前运行中没有这个副号。", true);
  if (!state.filtered.some((item) => item.id === board.id)) {
    resetFilters();
    toast("该牌不符合原筛选，已清除筛选并定位。");
  }
  setActive(board.id);
  $("#jump-number").value = "";
});
$("#jump-difference").addEventListener("click", () => {
  const board = current();
  const turn = board?.difference.first_turn;
  if (turn == null) return toast("当前两桌叫牌相同，没有分歧。");
  state.trace = [board.tables[0]?.auction[turn] ? 0 : 1, turn];
  renderBoard();
  $("#public-meaning").scrollIntoView({ block: "center" });
});
$("#system-previous").addEventListener("click", () => { state.systemPage -= 1; renderSystemPage(); });
$("#system-next").addEventListener("click", () => { state.systemPage += 1; renderSystemPage(); });
$("#browse-selected-call").addEventListener("click", () => {
  const board = current(), [tableIndex, turn] = state.trace;
  const table = board?.tables[tableIndex], step = table?.auction[turn];
  if (!step) return;
  const side = ["N", "S"].includes(step.seat) ? "NS" : "EW";
  showSystem(table.systems?.[side]?.hash, {
    initialRequest: { view: "auction", dealer: board.dealer, vulnerability: board.vulnerability,
      history: table.auction.slice(0, turn).map(item => item.call), call: step.call },
    publicSystems: Object.fromEntries(Object.entries(table.systems).map(([owner, system]) => [owner, system.hash])),
    hand: board.hands?.[step.seat],
  });
});
$("#system-full-specification").addEventListener("toggle", async event => {
  if (!event.target.open) return;
  try { await loadFullSystem(); }
  catch (error) { $("#system-specification").textContent = `规范读取失败：${error.message}`; toast(error.message, true); }
});
$("#download-system").addEventListener("click", async () => {
  try {
    const specification = await loadFullSystem();
    if (specification !== null) download(new Blob([specification], { type: "text/markdown" }), `system-${state.systemHash.slice(0, 12)}.md`);
  } catch (error) { toast(error.message, true); }
});
$("#system-dialog").addEventListener("close", () => { state.systemLoad++; systemBrowser.cancel(); });
for (const button of document.querySelectorAll("[data-close-dialog]")) button.addEventListener("click", () => document.getElementById(button.dataset.closeDialog).close());
$("#show-frozen").addEventListener("click", () => { state.interactive = false; state.trace = [0, 0]; renderBoard(); });
$("#show-interactive").addEventListener("click", () => { state.interactive = true; state.trace = [0, 0]; renderBoard(); });
$("#close-comparison").addEventListener("click", () => { state.interactive = false; state.comparison = null; renderBoard(); });
document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && event.target.id === "review-note") {
    event.preventDefault(); saveAnnotation(); return;
  }
  if (["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName) || document.querySelector("dialog[open]")) return;
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  if (event.key === "/") { event.preventDefault(); $("#search").focus(); }
  if (event.key.toLowerCase() === "f" && current()) { event.preventDefault(); saveAnnotation(annotation(current()).status === "flagged" ? "unreviewed" : "flagged"); }
  if (event.key === "ArrowLeft") { event.preventDefault(); navigate(-1); }
  if (event.key === "ArrowRight") { event.preventDefault(); navigate(1); }
});
document.addEventListener("click", (event) => { if (!event.target.closest("#export-menu")) $("#export-menu").open = false; });
document.addEventListener("dragover", (event) => { if (event.dataTransfer.types.includes("Files")) event.preventDefault(); });
document.addEventListener("drop", (event) => { if (event.dataTransfer.files.length) { event.preventDefault(); importFile(event.dataTransfer.files[0]); } });

async function initialize() {
  icons();
  samplingFields();
  try {
    state.catalog = await fetchJSON("/api/catalog");
    $("#run-select").replaceChildren();
    syncRunOptions();
    for (const id of ["#system-a", "#system-b", "#audit-system-a", "#audit-system-b", "#batch-system-a", "#batch-system-b", "#evolution-system-a", "#evolution-system-b"]) $(id).replaceChildren(...state.catalog.systems.map((system) => new Option(`${shortSystem(system.name)} · ${system.path}`, system.id)));
    selectSystems("#batch-system-a", "#batch-system-b");
    selectSystems("#evolution-system-a", "#evolution-system-b");
    const params = new URLSearchParams(location.search);
    const requested = params.get("run");
    const run = state.catalog.runs.some((item) => item.id === requested) ? requested : null;
    if (params.get("filter") && [...$("#difference-filter").options].some((option) => option.value === params.get("filter"))) $("#difference-filter").value = params.get("filter");
    $("#search").value = params.get("q") || "";
    if (["impact", "number"].includes(params.get("sort"))) $("#sort").value = params.get("sort");
    for (const id of Object.keys(filterDefaults)) {
      if (!params.has(id)) continue;
      const input = $("#" + id);
      if (input.tagName === "SELECT" && ![...input.options].some((option) => option.value === params.get(id))) {
        toast(`已忽略链接中的无效筛选项：${id}`, true); continue;
      }
      input.value = params.get(id);
      if (input.value !== params.get(id)) toast(`链接中的筛选值无效：${id}`, true);
    }
    if (Object.entries(filterDefaults).some(([id, value]) => $("#" + id).value !== value)) $("#advanced-filters").open = true;
    const table = Number(params.get("table")), turn = Number(params.get("turn"));
    if ([1, 2].includes(table) && Number.isSafeInteger(turn) && turn > 0) state.requestedTrace = [table - 1, turn - 1];
    if (run) { $("#run-select").value = run; await loadRun(run, Number(params.get("board")) || null); }
    else {
      busy(false);
      if (requested) toast("链接中的运行未登记，已返回首页。", true);
      await showHome();
    }
    const history = await fetchJSON("/api/audits");
    const pending = history.jobs.find((job) => ["queued", "running"].includes(job.status));
    if (pending) watchAudit(pending.id);
    if (params.get("evolution")) await evolutionDashboard.open(params.get("evolution"));
  } catch (error) { busy(false); toast(error.message, true); }
}
const evolutionDashboard = createEvolutionDashboard({
  escapeHTML, fetchJSON, phaseName: evolutionPhase, statusName: evolutionStatus, shortSystem,
  openResearch, openLegacy: openEvolution, prepareContinuation: prepareEvolutionContinuation, importFile, refreshHome, toast,
});
initialize();
import { filterDefaults, compileFilters, matchesAdvancedFilters } from "./review-filters.mjs";
import { buildAuctionStatistics } from "./auction-stats.mjs";
import { createEvolutionDashboard } from "./evolution-dashboard.mjs";
import { createSystemBrowser } from "./system-browser.mjs";
import { activityText, liveStatuses } from "./evolution-view.mjs";
