const seatOrder = ["N", "E", "S", "W"];
const kindNames = { rule: "决策规则", transition: "公开转移", announcement: "公开声明",
  feature: "共享条件", parameter: "协议参数", state: "协议阶段", fallback: "兜底" };

export function parseCallText(text) {
  const calls = String(text).trim() ? String(text).trim().split(/\s+/) : [];
  return calls.map(value => {
    const call = value.toUpperCase().replaceAll("♠", "S").replaceAll("♥", "H").replaceAll("♦", "D").replaceAll("♣", "C");
    const canonical = call === "PASS" ? "P" : call;
    if (!/^(P|X|XX|[1-7](C|D|H|S|NT))$/.test(canonical)) throw new Error(`无效叫品：${value}；叫品之间请用空格分隔。`);
    return canonical;
  });
}

export function positionRequest({ history, dealer = "N", vulnerability = "none", call = "", hand = "" }, decision = false) {
  const calls = parseCallText(history);
  if (!seatOrder.includes(dealer)) throw new Error("请选择发牌人。");
  if (decision) {
    if (!hand.trim()) throw new Error("请填写本人13张牌，格式为S.H.D.C。");
    const seat = seatOrder[(seatOrder.indexOf(dealer) + calls.length) % 4];
    return { view: "hand", observation: { history: calls, dealer, vulnerability, seat, hand: hand.trim() } };
  }
  const selected = parseCallText(call);
  if (selected.length > 1) throw new Error("查看叫品只能填写一个叫品。");
  return { view: "auction", history: calls, dealer, vulnerability, ...(selected.length ? { call: selected[0] } : {}) };
}

export function navigationPage(result) {
  return result.modules || result.matches || result.definitions || result.users || result.changes || result.compatible_rules;
}

export function renderNavigationResult(result, escape) {
  const keyButton = (key, label = key) => `<button type="button" class="text-button" data-nav-key="${escape(key)}">${escape(label)}</button>`;
  const node = item => `<article class="system-node"><div class="section-heading"><strong>${escape(kindNames[item.kind] || item.kind)}</strong>
    ${keyButton(item.key, item.id)}</div>${item.action ? `<p>行动：${escape(item.action)}</p>` : ""}
    ${item.description ? `<p class="system-condition">${escape(item.description)}</p>` : ""}
    ${item.data !== undefined ? `<details><summary>定义与引用</summary><pre>${escape(JSON.stringify(item.data, null, 2))}</pre>
    ${(item.dependencies || []).map(key => keyButton(key)).join(" · ")}</details>` : ""}
    ${item.definition_omitted ? `<p class="form-error">该节点较大，请按源路径读取具体字段：${escape(item.pointer)}</p>` : ""}</article>`;
  if (result.view === "catalog") {
    return `<p class="form-hint">${escape(result.states_total)}个阶段 · ${escape(result.counts.rule || 0)}条决策规则</p>
      <div class="system-module-list">${result.modules.items.map(item => `<button type="button" class="button small"
      data-nav-module="${escape(item.id)}">${escape(item.id)} <small>${escape(Object.values(item.counts).reduce((a, b) => a + b, 0))}</small></button>`).join("")}</div>`;
  }
  if (["auction", "hand"].includes(result.view)) {
    const explanation = result.explanation;
    return `${result.decision ? `<h3>建议 ${escape(result.decision.call)}</h3><p>${keyButton(result.decision.rule_key, "查看命中的决策规则")}</p>` : ""}
      ${explanation ? `<h3>${escape(explanation.purpose)}</h3><p class="public-meaning-summary">${escape(explanation.summary)}</p>
      <ul>${(explanation.new_information || []).map(value => `<li>${escape(value)}</li>`).join("")}</ul>
      ${(explanation.notes || []).map(value => `<p class="form-hint">${escape(value)}</p>`).join("")}
      ${(explanation.warnings || []).map(value => `<p class="form-error">${escape(value)}</p>`).join("")}` : `<p>当前阶段：${escape(result.state)}</p>`}
      <div class="report-actions">${(result.suggested_keys || []).map(key => keyButton(key)).join("")}</div>
      ${result.unavailable_definitions?.length ? `<p class="form-error">部分条件需要${escape(result.unavailable_definitions.join("/"))}的公开体系，请补充对方体系。</p>` : ""}
      <details><summary>相关发送规则（${escape(result.compatible_rules.total)}条，是否命中仍需手牌判断）</summary>
      ${result.compatible_rules.items.map(node).join("") || "<p>没有匹配的固定发送规则。</p>"}</details>`;
  }
  if (result.view === "diff") {
    return result.changes.items.map(item => `<article class="system-node"><strong>${escape(item.change)}</strong>
      ${item.after ? keyButton(item.key) : escape(item.key)}</article>`).join("") || "<p>此范围内没有语义变化。</p>";
  }
  const page = navigationPage(result);
  const definitions = page?.items.map(node).join("") || "<p>没有找到符合条件的内容。</p>";
  return definitions + (result.dependencies && !result.dependencies.complete_in_response
    ? `<p class="form-hint">依赖尚未全部展开；可继续下一页，或点击具体引用。</p>` : "");
}

export function createSystemBrowser(container, { escapeHTML: escape, fetchJSON, toast }) {
  let context, sequence = 0, lastRequest, lastResult;
  const $ = selector => container.querySelector(selector);

  function shell(systems) {
    container.innerHTML = `<form class="system-search" data-nav-search>
      <label>关键词<input data-nav-query placeholder="规则名、条件或叫品"></label>
      <label>阶段<select data-nav-state><option value="">全部阶段</option></select></label>
      <label>叫品<input data-nav-call placeholder="例如4NT"></label>
      <button class="button" type="submit">查找</button><button class="button" type="button" data-nav-catalog>目录</button>
      </form><details class="system-position" data-nav-position><summary>按叫牌序列或手牌查询</summary>
      <div class="system-position-grid">
      <label>发牌人<select data-nav-dealer>${seatOrder.map(seat => `<option>${seat}</option>`).join("")}</select></label>
      <label>局况<select data-nav-vulnerability><option value="none">双方无局</option><option value="ns">南北有局</option><option value="ew">东西有局</option><option value="both">双方有局</option></select></label>
      <label class="system-wide">已有叫牌<input data-nav-history placeholder="例如1NT P"></label>
      <label>查看叫品<input data-nav-meaning-call placeholder="例如4NT"></label>
      <label>对方体系<select data-nav-opponent><option value="">未指定</option>${systems.map(item =>
        `<option value="${escape(item.hash || item.id)}">${escape(item.name || String(item.hash || item.id).slice(0, 12))}</option>`).join("")}</select></label>
      <label class="system-wide">本人手牌<input data-nav-hand placeholder="S.H.D.C，例如AKQ.JT9.KQ32.876"></label>
      </div><div class="report-actions"><button class="button" type="button" data-nav-meaning>查看叫品含义</button>
      <button class="button" type="button" data-nav-decision>用此手牌判断下一叫</button></div></details>
      <p class="form-hint" data-nav-status role="status"></p><div data-nav-results></div>
      <button class="button small" type="button" data-nav-more hidden>下一页</button>`;
  }

  async function send(request) {
    const token = ++sequence;
    lastRequest = request;
    $("[data-nav-status]").textContent = "正在查询…";
    $("[data-nav-status]").classList.remove("form-error");
    $("[data-nav-more]").hidden = true;
    container.setAttribute("aria-busy", "true");
    const publicSystems = { ...context.publicSystems };
    if (["auction", "hand"].includes(request.view)) {
      const position = request.observation || request;
      const actor = seatOrder[(seatOrder.indexOf(position.dealer || "N") + (position.history || []).length) % 4];
      const own = ["N", "S"].includes(actor) ? "NS" : "EW";
      delete publicSystems[own];
      const opponent = $("[data-nav-opponent]").value;
      const other = own === "NS" ? "EW" : "NS";
      if (opponent) publicSystems[other] = opponent;
      else delete publicSystems[other];
    }
    try {
      const result = await fetchJSON(context.endpoint, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request, public_systems: publicSystems }) });
      if (token !== sequence) return null;
      lastResult = result;
      $("[data-nav-results]").innerHTML = renderNavigationResult(result, escape);
      const page = navigationPage(result);
      $("[data-nav-status]").textContent = page ? `${page.total ? page.offset + 1 : 0}–${page.offset + page.items.length} / ${page.total}` : "";
      $("[data-nav-more]").hidden = page?.next_offset == null;
      if (result.view === "catalog") {
        const selected = $("[data-nav-state]").value;
        $("[data-nav-state]").innerHTML = '<option value="">全部阶段</option>' +
          result.states.map(state => `<option value="${escape(state)}">${escape(state)}</option>`).join("");
        $("[data-nav-state]").value = selected;
      }
      return result;
    } catch (error) {
      if (token !== sequence) return null;
      $("[data-nav-status]").textContent = `查询失败：${error.message}`;
      $("[data-nav-status]").classList.add("form-error");
      $("[data-nav-results]").replaceChildren();
      toast(error.message, true);
      return null;
    } finally {
      if (token === sequence) container.setAttribute("aria-busy", "false");
    }
  }

  function formPosition(decision) {
    return positionRequest({ history: $("[data-nav-history]").value, dealer: $("[data-nav-dealer]").value,
      vulnerability: $("[data-nav-vulnerability]").value, call: $("[data-nav-meaning-call]").value,
      hand: $("[data-nav-hand]").value }, decision);
  }

  container.addEventListener("submit", event => {
    if (!event.target.matches("[data-nav-search]")) return;
    event.preventDefault();
    const query = $("[data-nav-query]").value.trim(), state = $("[data-nav-state]").value;
    const call = $("[data-nav-call]").value.trim().toUpperCase();
    send({ view: "search", ...(query ? { query } : {}), ...(state ? { state } : {}), ...(call ? { call } : {}) });
  });
  container.addEventListener("click", event => {
    const button = event.target.closest("button");
    if (!button || !container.contains(button)) return;
    if (button.dataset.navKey) send({ view: "read", keys: [button.dataset.navKey] });
    else if (button.dataset.navModule) send({ view: "search", module: button.dataset.navModule });
    else if (button.hasAttribute("data-nav-catalog")) send({ view: "catalog", limit: 100 });
    else if (button.hasAttribute("data-nav-more")) send({ ...lastRequest, offset: navigationPage(lastResult).next_offset });
    else if (button.hasAttribute("data-nav-meaning") || button.hasAttribute("data-nav-decision")) {
      try { send(formPosition(button.hasAttribute("data-nav-decision"))); }
      catch (error) { $("[data-nav-status]").textContent = error.message; $("[data-nav-status]").classList.add("form-error"); toast(error.message, true); }
    }
  });

  return {
    async open(options) {
      sequence++;
      context = options;
      shell(options.systems || []);
      if (options.initialRequest) {
        const initial = options.initialRequest;
        const position = initial.observation || initial;
        $("[data-nav-position]").open = true;
        $("[data-nav-history]").value = (position.history || []).join(" ");
        $("[data-nav-dealer]").value = position.dealer || "N";
        $("[data-nav-vulnerability]").value = position.vulnerability || "none";
        $("[data-nav-meaning-call]").value = initial.call || "";
        $("[data-nav-hand]").value = position.hand || options.hand || "";
        const actor = seatOrder[(seatOrder.indexOf(position.dealer || "N") + (position.history || []).length) % 4];
        const other = ["N", "S"].includes(actor) ? "EW" : "NS";
        $("[data-nav-opponent]").value = options.publicSystems?.[other] || "";
      }
      const catalog = await send({ view: "catalog", limit: 100 });
      if (catalog && options === context && options.initialRequest) await send(options.initialRequest);
      return catalog;
    },
    cancel() { sequence++; container.setAttribute("aria-busy", "false"); },
  };
}
