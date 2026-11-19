import { parseCallText, protocolReviewURL } from "./system-browser.mjs";

const seats = ["N", "E", "S", "W"];
const seatNames = { N: "北家", E: "东家", S: "南家", W: "西家" };
const statusNames = { defined: "有选牌规则", unused: "本阶段不用", missing: "说明／实现缺口", illegal: "不合法" };
const suits = { C: "♣", D: "♦", H: "♥", S: "♠", NT: "NT" };
const escapeHTML = value => String(value).replace(/[&<>"']/g, character =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
const sourceValue = source => source.kind === "draft" ? "draft:" + source.session : "frozen:" + source.hash;
const callText = call => ({ P: "Pass", X: "X", XX: "XX" })[call] || call[0] + suits[call.slice(1)];

export class ReviewPosition {
  constructor({ history = [], dealer = "N", vulnerability = "none", call = "", turn } = {}) {
    this.original = { history: [...history], dealer, vulnerability, call, turn };
    this.reset();
  }
  reset() {
    Object.assign(this, this.original, { history: [...this.original.history],
      inspection: this.original.turn == null ? null : this.original.turn - 1 });
    this.selection = this.original.call || (this.inspection == null ? "" : this.history[this.inspection]);
  }
  request() {
    return { history: this.inspection == null ? [...this.history] : this.history.slice(0, this.inspection),
      dealer: this.dealer, vulnerability: this.vulnerability, ...(this.selection ? { call: this.selection } : {}) };
  }
  inspect(index) {
    if (!Number.isInteger(index) || index < 0 || index >= this.history.length) throw new Error("历史叫牌不存在。");
    this.inspection = index;
    this.selection = this.history[index];
  }
  preview(call) {
    const calls = parseCallText(call);
    if (calls.length !== 1) throw new Error("请选择一个叫品。");
    this.selection = calls[0];
  }
  commit() {
    if (!this.selection) throw new Error("请先预览一个叫品。");
    this.history = [...this.request().history, this.selection];
    this.inspection = null;
    this.selection = "";
  }
  undo() {
    this.history = this.history.slice(0, -1);
    this.inspection = null;
    this.selection = "";
  }
  end() {
    this.inspection = null;
    this.selection = "";
  }
}

export function meaningChanges(current, previous) {
  if (!current || !previous) return [];
  const canonical = value => JSON.stringify(value);
  const changes = [];
  if (current.status !== previous.status) changes.push("叫品的定义／缺口状态不同");
  if (canonical([...new Set(current.senders.map(item => item.fingerprint))].sort()) !==
      canonical([...new Set(previous.senders.map(item => item.fingerprint))].sort())) changes.push("发送条件或优先级排除不同");
  for (const [field, label] of [["purpose", "公开用途不同"], ["new_information", "新增公开保证不同"],
    ["retained_information", "累计公开保证不同"], ["notes", "逼叫／接续责任不同"], ["warnings", "公开协议警告不同"]]) {
    if (canonical(current.explanation?.[field]) !== canonical(previous.explanation?.[field])) changes.push(label);
  }
  if (canonical(current.public_information_after?.map(row => [row.seat, row.claims])) !==
      canonical(previous.public_information_after?.map(row => [row.seat, row.claims]))) changes.push("预览后的公开牌情不同");
  if (current.public_error !== previous.public_error) changes.push("可履行性不同");
  return changes;
}

function exclusionLabel(exclusion, selectedCall, opening) {
  const calls = exclusion.calls || [];
  if (!calls.length) return "叫品未确定或未实现的优先分支";
  if (calls.length > 1) return `${calls.map(callText).join("／")} 的动态优先分支`;
  const call = calls[0];
  if (call === selectedCall) return `${callText(call)} 的更优先分支（同一叫品）`;
  return `${callText(call)}${opening && !["P", "X", "XX"].includes(call) ? " 开叫" : ""}的优先分支`;
}

function renderBranches(branches, escape, selectedCall, history) {
  if (!branches.length) return "<p>没有可展开的发送路径。请结合上方状态和阶段缺口阅读，不等于此叫品是自然叫。</p>";
  const opening = history.every(call => call === "P");
  return `<ol class="review-branches">${branches.map(branch => {
    const exclusions = branch.exclusion_details || branch.exclusions.map(condition => ({ condition, calls: [] }));
    const labels = [...new Set(exclusions.map(item => exclusionLabel(item, selectedCall, opening)))];
    return `<li>
    <p>${escape(branch.fallback ? "未满足更高优先级规则时使用的兜底路径" : branch.condition)}</p>
    ${!branch.readable ? '<p class="review-warning">有条件尚不能完整解释，不能把此路径当成完整定义。</p>' : ""}
    ${exclusions.length ? `<details><summary>本分支先排除：${labels.map(escape).join("、")}（${exclusions.length}项）</summary>
      <p class="form-hint">仅排除下面列出的具体分支，不代表排除这些叫品的全部含义，也不是对某副手牌的检测结果。</p>
      <ul>${exclusions.map(item => `<li><strong>${escape(exclusionLabel(item, selectedCall, opening))}：</strong>${escape(item.condition)}</li>`).join("")}</ul></details>` : ""}
    </li>`;
  }).join("")}</ol>`;
}

export function renderReviewDetail(result, escape = escapeHTML) {
  const selected = result.selected;
  if (!selected) return `<section class="review-empty"><span class="review-eyebrow">先选叫品，再读含义</span>
    <h2>${result.ended ? "本轮叫牌已结束" : "点选叫牌盒中的任一合法叫品"}</h2>
    <p>点选只会预览；按“加入进程”才推进。点拍卖表中的历史叫品，可回到它发生之前审查，之后可以临时分支。</p>
    <p>不需要手牌，也不会读取牌例中的暗手。缺失的约定和公开保证会明确标出。</p></section>`;
  const explanation = selected.explanation;
  return `<header class="review-meaning-heading"><div><span class="review-eyebrow">叫品含义 · 公开视角</span>
    <h2>${escape(callText(selected.call))} <small>${escape(statusNames[selected.status])}</small></h2></div>
    <div class="review-meaning-actions"><a class="review-back-box" href="#review-call-box">返回叫牌盒 ↑</a>
    <button class="button primary" type="button" data-review-commit ${selected.public_error ? "disabled" : ""}>
      ${selected.status === "defined" ? "加入进程" : "作为假设加入"}</button></div></header>
    <p class="review-meaning-summary">${escape(explanation?.summary || selected.reason)}</p>
    ${selected.status !== "defined" ? `<p class="review-warning">桥规允许不代表本体系会这样叫。此处状态为“${escape(statusNames[selected.status])}”，不能据此认定存在相应手牌。</p>` : ""}
    ${selected.public_error ? `<p class="form-error" role="alert">${escape(selected.public_error)}。进程不能在此协议错误后继续。</p>` : ""}
    <section><h3>什么手牌会这样叫</h3>
      <p class="form-hint">这是发送方的选牌条件，不是接收方已经获得的公开保证。所有路径都未代入具体手牌。</p>
      ${renderBranches(selected.senders, escape, selected.call, result.history || [])}</section>
    <section><h3>这一叫新增了什么公开信息</h3>
      ${explanation?.new_information?.length ? `<ul>${explanation.new_information.map(text => `<li>${escape(text)}</li>`).join("")}</ul>` :
        '<p class="review-warning">公开协议没有登记新的量化保证；不能把选牌条件擅自补成公开承诺。</p>'}
      ${explanation?.retained_information?.length ? `<details open><summary>此前已有、仍然保留的信息</summary>
        <ul>${explanation.retained_information.map(text => `<li>${escape(text)}</li>`).join("")}</ul></details>` : ""}</section>
    <section><h3>逼叫、停止与后续责任</h3>
      ${explanation?.notes?.length ? `<ul>${explanation.notes.map(text => `<li>${escape(text)}</li>`).join("")}</ul>` :
        `<p>${selected.public_meaning ? "未登记额外的逼叫责任；下一轮仍按公开协议和桥规处理，不根据叫品名称猜测。" : "当前行动方的公开协议未知，不能判断逼叫或停止责任。"}</p>`}
      ${selected.ends_auction ? "<p>这次叫牌会结束拍卖。</p>" : `<p>加入后轮到${escape(seatNames[seats[(seats.indexOf(result.actor) + 1) % 4]])}；不会自动替对方补Pass。</p>`}
      ${(explanation?.warnings || []).map(text => `<p class="form-error">${escape(text)}</p>`).join("")}</section>`;
}

function renderInformation(information, escape) {
  return `<div class="review-facts">${information.map(row => `<article><h4>${escape(seatNames[row.seat])}</h4>
    ${row.claims.length ? `<ul>${row.claims.map(text => `<li>${escape(text)}</li>`).join("")}</ul>` :
      `<p>${row.known_system ? "公开协议尚未限定点力／牌型；物理范围不是承诺。" : "此方体系未指定，不能重建公开保证。"}</p>`}</article>`).join("")}</div>`;
}

function shell(sources, escape) {
  return `<section class="review-settings"><div class="review-source-controls">
    <label class="review-source">审查版本／研究草稿<select data-review-source>${sources.map(source =>
      `<option value="${escape(sourceValue(source))}">${escape(source.label)}</option>`).join("")}</select></label>
    <label>此体系用于<select data-review-side><option value="NS">南北</option><option value="EW">东西</option></select></label>
    <label>另一方体系<select data-review-opponent><option value="">未指定（含义未知）</option>
      <option value="same">双方明确使用同一体系</option>${sources.filter(source => source.kind === "frozen").map(source =>
        `<option value="${escape(source.hash)}">${escape(source.name)}</option>`).join("")}</select></label>
    <label>同一进程对照<select data-review-compare><option value="">不比较</option><option value="parent">登记的亲本</option>
      ${sources.filter(source => source.kind === "frozen").map(source =>
        `<option value="${escape(source.hash)}">${escape(source.name)}</option>`).join("")}</select></label>
    </div><div class="review-source-status"><span data-review-source-status></span>
      <button class="button small" type="button" data-review-refresh hidden>读取最新草稿</button></div></section>
    <div class="review-feedback" role="status" aria-live="polite" data-review-status></div>
    <div class="review-layout"><div class="review-left">
    <section class="review-panel"><div class="review-panel-title"><h2>叫牌进程</h2><span data-review-mode></span></div>
      <div data-review-auction></div>
      <div class="review-actions"><button class="button small" type="button" data-review-undo>撤回最后一叫</button>
      <button class="button small" type="button" data-review-end>回到末尾</button>
      <button class="button small" type="button" data-review-reset>返回原进程</button></div>
      <details class="review-position"><summary>输入公开进程／设置发牌人与局况</summary><form data-review-position>
      <div class="review-position-controls"><label>发牌人<select data-review-dealer>${seats.map(seat =>
        `<option value="${seat}">${seatNames[seat]}</option>`).join("")}</select></label>
      <label>局况<select data-review-vulnerability><option value="none">双方无局</option><option value="ns">南北有局</option>
        <option value="ew">东西有局</option><option value="both">双方有局</option></select></label></div>
      <label>叫品以空格分隔，不自动补Pass<textarea data-review-history rows="3" placeholder="例如：P P 1C P"></textarea></label>
      <button class="button small" type="submit">查看此进程</button></form></details>
    </section>
    <section class="review-panel" id="review-call-box"><h2 data-review-stage>正在重建公开进程…</h2><p data-review-acting class="form-hint"></p>
      <div class="review-call-legend"><span>有选牌规则</span><span class="unused">本阶段不用</span>
      <span class="missing">说明／实现缺口</span><span class="illegal">桥规不合法</span></div>
      <div data-review-calls></div>
      <p class="form-hint">点选仅预览，不推进。合法但未使用与尚未解释／实现是不同状态。</p>
      <details data-review-partitions><summary>本阶段的手牌分区与叫品总览</summary><div data-review-partition-list></div></details>
    </section>
    <section class="review-panel" data-review-issues-panel hidden><h2>阶段缺口与未确定信息</h2><div data-review-issues></div></section>
    </div><div class="review-right">
      <article class="review-panel review-meaning" data-review-detail></article>
      <section class="review-panel" data-review-comparison-panel hidden><h2>相同公开进程的版本对照</h2><div data-review-comparison></div></section>
      <section class="review-panel"><h2 data-review-facts-title>此前累计公开牌情</h2><div data-review-information></div>
        <p class="form-hint">只展示协议已登记的保证。未限定的点力和各套张数不显示成绿色“已知信息”。</p></section>
      <details class="review-panel review-engineering"><summary>工程依据、版本身份与原始公开状态</summary>
        <div data-review-downloads></div><pre data-review-engineering></pre></details>
    </div></div>`;
}

export function createStageReview(container, { query, onPosition = () => {}, escape = escapeHTML }) {
  const $ = selector => container.querySelector(selector);
  let position, sources, source, run, snapshot, sequence = 0, lastResult;

  function drawAuction() {
    const offset = seats.indexOf(position.dealer);
    const count = Math.max(4, Math.ceil((offset + position.history.length) / 4) * 4);
    const cells = Array.from({ length: count }, (_, cell) => {
      const index = cell - offset, call = position.history[index];
      return `<td${position.inspection !== null && index > position.inspection ? ' class="review-future"' : ""}>${call ?
        `<button type="button" class="${index === position.inspection ? "is-selected" : ""}" data-review-turn="${index}"
          aria-label="查看第${index + 1}叫${escape(callText(call))}">${escape(callText(call))}</button>` : "—"}</td>`;
    });
    $("[data-review-auction]").innerHTML = `<table class="review-auction"><caption>点击历史叫品，只读取它发生之前的进程</caption>
      <thead><tr>${seats.map(seat => `<th scope="col">${seatNames[seat]}</th>`).join("")}</tr></thead>
      <tbody>${Array.from({ length: count / 4 }, (_, row) => `<tr>${cells.slice(row * 4, row * 4 + 4).join("")}</tr>`).join("")}</tbody></table>`;
    $("[data-review-mode]").textContent = position.inspection !== null ? `回看第${position.inspection + 1}叫之前` :
      JSON.stringify(position.history) === JSON.stringify(position.original.history) ? "原始公开进程" : "临时分支 · 不改写原记录";
    $("[data-review-undo]").disabled = !position.history.length;
    $("[data-review-end]").disabled = position.inspection === null;
    $("[data-review-history]").value = position.history.join(" ");
    $("[data-review-dealer]").value = position.dealer;
    $("[data-review-vulnerability]").value = position.vulnerability;
  }

  function draw(result) {
    $("[data-review-stage]").textContent = result.stage;
    $("[data-review-acting]").textContent = result.acting_system ? `当前行动方使用：${result.acting_system.name}` :
      "当前轮到另一方，但尚未指定其体系。可以选择合法叫品探索，不能套用我方含义。";
    const button = item => `<button type="button" class="review-call ${escape(item.status)} ${position.selection === item.call ? "is-selected" : ""}"
      data-review-call="${escape(item.call)}" title="${escape(item.reason)}" aria-pressed="${position.selection === item.call}"
      ${item.status === "illegal" ? "disabled" : ""}><strong class="${/^[1-7][DH]$/.test(item.call) ? "red-suit" : ""}">${escape(callText(item.call))}</strong>
      <small>${escape(statusNames[item.status])}</small></button>`;
    $("[data-review-calls]").innerHTML = `<div class="review-special-calls">${result.choices.filter(item => /^(P|X|XX)$/.test(item.call)).map(button).join("")}</div>
      <div class="review-bids">${result.choices.filter(item => !/^(P|X|XX)$/.test(item.call)).map(button).join("")}</div>`;
    $("[data-review-detail]").innerHTML = renderReviewDetail(result, escape);
    $("[data-review-information]").innerHTML = renderInformation(result.selected?.public_information_after || result.public_information, escape);
    $("[data-review-facts-title]").textContent = result.selected?.public_information_after ? "预览这一叫后的累计公开牌情（尚未加入）" : "此前累计公开牌情";
    const drafts = source.kind === "draft";
    $("[data-review-source-status]").textContent = drafts ?
      `未发布／未验证 · ${result.source.name}。本页固定草稿快照，后台继续修改不会替换它。` :
      `冻结版本 · ${result.source.name}。冻结不等于验证或晋升。`;
    $("[data-review-refresh]").hidden = !drafts;
    $("[data-review-issues-panel]").hidden = !result.issues.length && !result.unavailable_definitions.length;
    $("[data-review-issues]").innerHTML = result.issues.map(issue =>
      `<p class="review-warning"><strong>${escape(issue.message)}</strong>：${escape(issue.condition)}</p>`).join("") +
      (result.unavailable_definitions.length ? '<p class="review-warning">有条件依赖未提供的另一方体系；未把未知条件当作不成立。</p>' : "");
    $("[data-review-partition-list]").innerHTML = `<p class="form-hint">列出公开兼容分支，未穷举所有手牌证明覆盖／互斥，也不证明自然可达。更优先分区先执行。</p>` +
      (result.partitions.length ? result.partitions.map(part => `<article class="review-partition"><button type="button" class="text-button"
        data-review-call="${escape(part.call)}">${escape(callText(part.call))}</button><ul>${part.conditions.map((text, index) =>
        `<li>${escape(text)}${part.priority_exclusions[index].length ? `（须先排除${part.priority_exclusions[index].length}个更优先分区，点叫品查看）` : ""}</li>`).join("")}</ul></article>`).join("") :
        "<p>没有可展开的手牌分区；请检查体系来源和阶段缺口。</p>");
    $("[data-review-comparison-panel]").hidden = !result.comparison && !result.comparison_note && !result.comparison_error;
    if (result.comparison) {
      const parent = result.comparison;
      const changes = meaningChanges(result.selected, parent.selected);
      $("[data-review-comparison]").innerHTML = `<p>对照版本：${escape(parent.source.name)}。对方体系保持不变，不是IR行数对比。</p>` +
        (result.selected ? `<p>${escape(changes.length ? changes.join("；") : "当前叫品在本页可解释的发送条件、公开保证和责任没有差异；不是整体等价证明。")}</p>
          <div class="review-compare-grid"><section><h3>本版本</h3>${renderBranches(result.selected.senders, escape)}
          <p>${escape(result.selected.explanation?.summary || result.selected.reason)}</p></section>
          <section><h3>对照版本</h3>${renderBranches(parent.selected.senders, escape)}
          <p>${escape(parent.selected.explanation?.summary || parent.selected.reason)}</p>
          ${(parent.selected.explanation?.notes || []).map(text => `<p>${escape(text)}</p>`).join("")}
          ${parent.selected.public_error ? `<p class="form-error">${escape(parent.selected.public_error)}</p>` : ""}</section></div>` :
          "<p>选择一个叫品后，比较同一进程下的发送条件、公开信息和后续责任。</p>");
    } else {
      $("[data-review-comparison]").innerHTML = `<p class="${result.comparison_error ? "form-error" : "form-hint"}">
        ${escape(result.comparison_error || result.comparison_note || "")}</p>`;
    }
    const engineering = { source: result.source, scope: result.scope, ...result.engineering,
      selected: result.selected ? { sources: result.selected.senders.map(item => item.source_key), public_meaning: result.selected.public_meaning } : null,
      issues: result.issues };
    $("[data-review-engineering]").textContent = JSON.stringify(engineering, null, 2);
    const base = run ? `/api/evolutions/${encodeURIComponent(run)}/systems/` : "/api/systems/";
    $("[data-review-downloads]").innerHTML = drafts ? "<p>未发布快照不登记成冻结候选，也不会生成比赛成绩。</p>" :
      `<p><a href="${escape(base + result.source.hash + (run ? ".md" : ""))}" target="_blank" rel="noopener">冻结说明</a>
        ${run ? ` · <a href="${escape(base + result.source.hash + ".json")}" download>冻结IR</a>` : ""}</p>`;
  }

  async function refresh() {
    const ticket = ++sequence;
    lastResult = null;
    drawAuction();
    $("[data-review-status]").textContent = "正在按公开进程重建含义…";
    $("[data-review-status]").classList.remove("form-error");
    $("[data-review-source-status]").textContent = source.kind === "draft" ? "未发布／未验证 · 正在读取固定草稿快照…" : "正在读取已登记的冻结版本…";
    $("[data-review-refresh]").hidden = source.kind !== "draft";
    $("[data-review-detail]").innerHTML = "<p>查询中；不沿用旧位置的解释。</p>";
    $("[data-review-calls]").innerHTML = "";
    $("[data-review-stage]").textContent = "正在重建当前位置…";
    $("[data-review-acting]").textContent = "";
    $("[data-review-information]").innerHTML = "<p>正在读取当前位置的公开信息…</p>";
    $("[data-review-partition-list]").innerHTML = "<p>正在重建手牌分区…</p>";
    $("[data-review-issues-panel]").hidden = true;
    $("[data-review-comparison-panel]").hidden = true;
    $("[data-review-engineering]").textContent = "";
    $("[data-review-downloads]").innerHTML = "";
    container.setAttribute("aria-busy", "true");
    const payload = { source: { kind: source.kind, ...(source.kind === "draft" ? { session: source.session, ...(snapshot ? { snapshot } : {}) } : { hash: source.hash }) },
      request: position.request(), side: $("[data-review-side]").value, ...(run ? { evolution: run } : {}),
      ...($("[data-review-opponent]").value ? { opponent: $("[data-review-opponent]").value } : {}),
      ...($("[data-review-compare]").value ? { compare_to: $("[data-review-compare]").value } : {}) };
    try {
      const result = await query(payload);
      if (ticket !== sequence) return;
      lastResult = result;
      snapshot = result.source.snapshot;
      draw(result);
      if (result.selected && window.matchMedia("(max-width:760px)").matches) {
        $("[data-review-detail]").scrollIntoView({ block: "start" });
      }
      $("[data-review-status]").textContent = "只读公开解释；未调用模型、读取暗手、生成牌例或运行DDS。";
      onPosition({ run, source: result.source, initialRequest: position.request(),
        side: payload.side, opponent: payload.opponent, compareTo: payload.compare_to });
    } catch (error) {
      if (ticket !== sequence) return;
      $("[data-review-status]").textContent = `查询失败：${error.message}`;
      $("[data-review-status]").classList.add("form-error");
      $("[data-review-detail]").innerHTML = `<p class="form-error" role="alert">无法解释当前进程：${escape(error.message)}</p>
        <button type="button" class="button" data-review-retry>重新查询</button>`;
      $("[data-review-calls]").innerHTML = "";
      $("[data-review-information]").innerHTML = "<p>本次查询未完成，旧牌情不作为当前位置的证据。</p>";
      $("[data-review-comparison-panel]").hidden = true;
      $("[data-review-partition-list]").innerHTML = "<p>查询未完成。</p>";
    } finally {
      if (ticket === sequence) container.setAttribute("aria-busy", "false");
    }
  }

  container.addEventListener("click", event => {
    const call = event.target.closest("[data-review-call]");
    const turn = event.target.closest("[data-review-turn]");
    if (call) position.preview(call.dataset.reviewCall);
    else if (turn) position.inspect(Number(turn.dataset.reviewTurn));
    else if (event.target.closest("[data-review-commit]")) {
      if (!lastResult?.selected || lastResult.selected.call !== position.selection || lastResult.selected.public_error) return;
      position.commit();
    } else if (event.target.closest("[data-review-undo]")) position.undo();
    else if (event.target.closest("[data-review-end]")) position.end();
    else if (event.target.closest("[data-review-reset]")) position.reset();
    else if (event.target.closest("[data-review-refresh]")) snapshot = undefined;
    else if (!event.target.closest("[data-review-retry]")) return;
    void refresh();
  });
  container.addEventListener("change", event => {
    if (event.target.matches("[data-review-source]")) {
      source = sources.find(item => sourceValue(item) === event.target.value);
      snapshot = undefined;
      $("[data-review-compare] option[value=parent]").disabled = !source.parent;
      $("[data-review-compare]").value = source.parent ? "parent" : "";
    } else if (!event.target.matches("[data-review-side], [data-review-opponent], [data-review-compare]")) return;
    void refresh();
  });
  container.addEventListener("submit", event => {
    if (!event.target.matches("[data-review-position]")) return;
    event.preventDefault();
    try {
      const history = parseCallText($("[data-review-history]").value);
      position.history = history;
      position.dealer = $("[data-review-dealer]").value;
      position.vulnerability = $("[data-review-vulnerability]").value;
      position.end();
      void refresh();
    } catch (error) {
      $("[data-review-status]").textContent = error.message;
      $("[data-review-status]").classList.add("form-error");
    }
  });
  return {
    async open(options) {
      sources = options.sources;
      run = options.run;
      source = sources.find(item => sourceValue(item) === options.value) || sources[0];
      if (!source) throw new Error("没有可审查的已登记体系或研究草稿。");
      if (options.value && sourceValue(source) !== options.value) throw new Error("所选体系／草稿未登记，不能自动换成其他版本。");
      position = new ReviewPosition(options.initialRequest);
      snapshot = options.snapshot;
      container.innerHTML = shell(sources, escape);
      $("[data-review-source]").value = sourceValue(source);
      $("[data-review-side]").value = options.side || (["N", "S"].includes(seats[
        (seats.indexOf(position.dealer) + position.request().history.length) % 4]) ? "NS" : "EW");
      $("[data-review-opponent]").value = options.opponent || "";
      $("[data-review-compare]").value = options.compareTo || (source.parent ? "parent" : "");
      if (options.opponent && $("[data-review-opponent]").value !== options.opponent) throw new Error("指定的对方体系未登记，未忽略或替换该版本。");
      if (options.compareTo && $("[data-review-compare]").value !== options.compareTo) throw new Error("指定的对照版本未登记，未自动换成其他版本。");
      $("[data-review-compare] option[value=parent]").disabled = !source.parent;
      return refresh();
    },
  };
}

export function reviewLink(state) {
  const url = new URL(protocolReviewURL({ run: state.run,
    system: state.source.kind === "frozen" ? state.source.hash : undefined,
    session: state.source.session, snapshot: state.source.kind === "draft" ? state.source.snapshot : undefined,
    initialRequest: state.initialRequest, side: state.side, opponent: state.opponent }), "http://localhost");
  if (state.compareTo) url.searchParams.set("compare_to", state.compareTo);
  return url.pathname + url.search;
}
