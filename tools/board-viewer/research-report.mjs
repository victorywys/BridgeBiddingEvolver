import { impNumber } from "./evolution-view.mjs";
import { protocolReviewURL } from "./system-browser.mjs";

export function clauseHighlights(text) {
  const action = text.match(/(?:^|；)行动：([\s\S]*)$/)?.[1];
  const target = text.match(/(?:^|；)转入：([^；]+)/)?.[1];
  const points = [...text.matchAll(/大牌点（A=4、K=3、Q=2、J=1）\s*(不小于|不大于|大于|小于|等于)\s*(\d+)/g)]
    .map(match => `${match[1]}${match[2]}`);
  const states = [...text.matchAll(/根据全部公开叫牌重建的协议状态\s*等于\s*"([^"]+)"/g)].map(match => match[1]);
  const parts = [];
  if (action) parts.push("行动：" + (action.length > 200 ? action.slice(0, 200) + "…（完整公式见展开）" : action));
  if (target) parts.push("转入：" + target);
  if (points.length) parts.push("涉及本人点力边界：" + [...new Set(points)].join("、"));
  if (states.length) parts.push("涉及阶段：" + [...new Set(states)].join("、"));
  return parts.join("；") || (text.length > 200 ? text.slice(0, 200) + "…（见完整条件）" : text);
}

export function renderResearchReport(run, escape) {
  const report = run.research_summary;
  if (!report) return "";
  const versionButton = version => `<a class="button small primary"
    href="${escape(protocolReviewURL({ run: run.id, system: version.hash }))}" target="_blank" rel="noopener">查看完整体系／按进程审查</a>
    <button class="button small" data-dashboard-system="${escape(version.hash)}"
    data-system-run="${escape(run.id)}" data-system-name="${escape(version.label + " · " + version.name)}">工程定义</button>`;
  const contribution = evidence => evidence?.score_contributions ? `<p class="form-hint">
    正常计分贡献 ${impNumber(evidence.score_contributions.played_contribution)}，
    baseline弃权贡献 ${impNumber(evidence.score_contributions.forfeit_contribution)} IMP/副；
    候选／亲本遇对手弃权 ${evidence.score_contributions.candidate_forfeit_pairs}／${evidence.score_contributions.parent_forfeit_pairs}个配对。
    这是全样本贡献分解，不是删除错误牌后重算。</p>` : "";
  return `<section class="evo-draft-card" data-readable-research>
    <h3>研究结论先读</h3><p class="research-text"><strong>${escape(report.conclusion)}</strong></p>
    <p class="research-text">${escape(report.stability)}</p>
    <p>${escape(report.current_title || "当前接受版本")}：<strong>${escape(report.current.join("、") || "无")}</strong>；最新发布：<strong>${escape(report.latest || "无")}</strong>。最新不等于最好。</p>
    <p class="form-hint">${escape(report.reading_guide)}</p>
    <h3>体系是什么</h3><p class="research-text">${escape(report.objective)}</p>
    <p class="form-hint">${escape(report.baseline_fault_explanation)}</p>
    <h3>版本、修改与实际结果</h3>
    ${report.versions.map(version => `<details class="evolution-generation" data-dashboard-detail="version-${escape(version.hash)}"
      ${version.is_current || version.label === report.latest ? "open" : ""}>
      <summary>${escape(version.label)} · ${escape(version.name)} · ${escape(version.decision)}</summary>
      <p>${version.rules}条规则 · ${version.states}个公开阶段 · 兜底 ${escape(version.fallback)}</p>
      ${version.author_intent ? `<h4>作者设计意图（不当作已证结论）</h4><p class="research-text">${escape(version.author_intent)}</p>` : ""}
      <h4>实际验证与裁决</h4><p><strong>${escape(version.decision)}</strong></p>
      ${version.evidence?.tests ? `<p>真实回归 ${version.evidence.tests.passed}/${version.evidence.tests.tests}通过
        ${version.evidence.tests.protocol_witnesses != null ? ` · ${version.evidence.tests.protocol_witnesses}项自然协议见证` : ""}</p>` : ""}
      ${version.evidence?.protocol ? `<p>协议验收：${escape(version.evidence.protocol.status === "passed" ? "通过" : version.evidence.protocol.status)} · ${version.evidence.protocol.passed}/${version.evidence.protocol.probes ?? version.evidence.protocol.cases}项</p>` : ""}
      ${version.evidence?.gain != null ? `<p>${escape(version.evidence.kind)} · ${version.evidence.boards}副
        · 相对${escape(version.parent_label || "亲本")} <strong>${impNumber(version.evidence.gain)} IMP/副</strong></p>` : ""}
      ${contribution(version.evidence)}
      ${(version.evidence?.errors || []).map(error => `<p class="form-error">第${escape(error.board ?? "未知")}副：${escape(error.message)}（保留原始错误，受阻不等于判负）</p>`).join("")}
      <details data-dashboard-detail="rules-${escape(version.hash)}"><summary>怎么叫牌：开局规则与接续阶段</summary>
        <p class="form-hint">${escape(version.opening_scope)}</p>
        ${version.opening.length ? `<ol>${version.opening.map(rule => `<li class="research-text">${escape(clauseHighlights(rule.text))}
          <details><summary>完整条件与优先级</summary><p>${escape(rule.text)}</p></details></li>`).join("")}</ol>`
          : `<p>没有决策规则；全部决定使用兜底 ${escape(version.fallback)}。</p>`}
        ${version.opening_total > version.opening.length ? `<p class="form-hint">显示${version.opening.length}/${version.opening_total}项；请打开完整体系查看其余规则。</p>` : ""}
        <p class="research-text">接续：${escape(version.branches.map(branch => `${branch.state}（${branch.transitions}项公开转移→${branch.targets.join("／")}）`).join("；"))}</p>
      </details>
      ${version.changes ? `<details data-dashboard-detail="changes-${escape(version.hash)}"><summary>相对${escape(version.parent_label)}具体改了什么 · ${version.changes.total}项语义变化</summary>
        <p>${escape(Object.entries(version.changes.counts).map(([name, count]) => `${name}${count}项`).join("；"))}</p>
        ${version.changes.items.map(change => `<article class="evo-draft-card"><h4>${escape(change.operation + change.kind)}</h4>
          <p class="research-text"><strong>原来：</strong>${escape(clauseHighlights(change.before))}</p>
          <p class="research-text"><strong>现在：</strong>${escape(clauseHighlights(change.after))}</p>
          <details><summary>完整条件、优先级与公开更新</summary>
            <p class="research-text"><strong>原来：</strong>${escape(change.before)}</p>
            <p class="research-text"><strong>现在：</strong>${escape(change.after)}</p>
            <small>${escape(change.key)}</small></details></article>`).join("")}
        <p class="form-hint">点力边界与阶段仅为定位线索；不是省略了其他条件的可执行规则。完整逻辑请展开。</p>
        <p class="form-hint">${escape(version.changes.scope)}</p></details>` : "<p>这是本次起始版本，不是新发布的改进。</p>"}
      <div class="report-actions">${versionButton(version)}
        <a class="button small" href="/api/evolutions/${escape(run.id)}/systems/${escape(version.hash)}.md" download>下载同源完整说明</a></div>
    </details>`).join("")}</section>`;
}
