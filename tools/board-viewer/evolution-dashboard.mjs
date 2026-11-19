import { activityText, comparisonRows, creditNumber, currentDrafts, durationLabel, elapsedSeconds, impNumber,
  liveStatuses, milestoneNames, milestoneSummary, scoredRounds } from "./evolution-view.mjs";
import { createSystemBrowser, protocolReviewURL } from "./system-browser.mjs";
import { renderResearchReport } from "./research-report.mjs";
import { renderResearchWorkflow } from "./research-workflow.mjs";

const $ = selector => document.querySelector(selector);
const timeText = value => {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }) : "时间未记录";
};
const tone = value => typeof value === "number" ? value > 0 ? "positive" : value < 0 ? "negative" : "" : "";
const numberText = value => Number.isFinite(value) ? new Intl.NumberFormat("zh-CN").format(Math.round(value)) : "—";

export function evaluationScope(run) {
  if (run.selection_policy === "parent-improvement") return "从零开发选择：只需优于上一亲本，不要求成熟体系达标";
  return run.development_sampling ? `局部条件开发评价：${run.development_sampling.name}（不是全局成绩）`
    : "均匀全局开发评价";
}

export function createEvolutionDashboard({ escapeHTML: escape, fetchJSON, phaseName, statusName, shortSystem,
  openResearch, openLegacy, prepareContinuation, importFile, refreshHome, toast }) {
  const view = { id: null, run: null, tab: "overview", round: null, timer: null, timelineTimer: null, systemLoad: 0 };
  const systemBrowser = createSystemBrowser($("#evo-system-browser"), { escapeHTML: escape, fetchJSON, toast });

  function metric(label, used, maximum, note) {
    const bounded = Number.isFinite(maximum) && maximum > 0;
    return `<div class="evo-metric"><span>${escape(label)}</span><strong>${numberText(used)}<small> / ${numberText(maximum)}</small></strong>
      ${bounded ? `<progress value="${Math.min(maximum, Math.max(0, used || 0))}" max="${maximum}" aria-label="${escape(label)}"></progress>` : ""}
      <small>${escape(note || "")}</small></div>`;
  }

  function milestones(events, runId) {
    return `<ol class="evo-timeline">${events.map(event => {
      const summary = milestoneSummary(event);
      const category = ["promotion", "release", "candidate_invalid", "tool_issue", "comparison_blocked"].includes(event.kind) ? event.kind : "normal";
      return `<li class="evo-milestone ${category}" data-milestone="${escape(event.id)}">
        <div class="evo-milestone-heading"><strong>${escape(event.kind === "promotion" && event.selection_policy === "parent-improvement" ? "开发亲本升级" : milestoneNames[event.kind] || event.kind)}${event.lineage ? ` · ${escape(event.lineage)}` : ""}${event.generation ? ` · 第${event.generation}代` : ""}</strong><time>${escape(timeText(event.timestamp))}</time></div>
        ${summary.length > 260 ? `<details data-dashboard-detail="milestone-${escape(event.id)}"><summary>${escape(summary.slice(0, 180))}…</summary><p class="research-text">${escape(summary)}</p></details>` : `<p class="research-text">${escape(summary)}</p>`}
        <div class="report-actions">${event.session ? `<button class="text-button" data-dashboard-research="/api/evolutions/${runId}/research/${escape(event.session)}">查看对应研究</button>` : ""}
        ${event.kind === "release" && event.candidate ? systemButton(runId, { hash: event.candidate, name: "已发布候选 " + event.candidate.slice(0, 12) }, "查看发布候选") : ""}</div></li>`;
    }).join("")}</ol>`;
  }

  function drafts(run) {
    const sessions = currentDrafts(run);
    if (!sessions.length) return '<p class="form-hint">正在准备新牌诊断；研究启动后会显示工作版本与实际测试。</p>';
    return `<div class="evo-draft-grid">${sessions.map(session => {
      const draft = session.working;
      return `<article class="evo-draft-card"><div class="evo-card-heading"><strong>${escape(session.id)}</strong><span class="evo-status">${escape(statusName(session.status))}</span></div>
        <h4>${escape(draft?.name || "研究工作区")}</h4><p class="form-hint">工作草稿，不是当前冠军 · ${session.turns}/${run.budgets?.max_research_turns || "—"}轮模型 · ${session.tool_calls}次工具</p>
        ${draft ? `<p>${draft.rules ?? "—"}条规则 · ${draft.states ?? "—"}个状态 · ${draft.cases}副诊断牌</p>
          ${draft.tests ? `<p class="${draft.tests.failed ? "form-error" : ""}">最近测试 ${draft.tests.passed}/${draft.tests.tests}通过${draft.tests.failed ? `，${draft.tests.failed}失败` : ""}${draft.tests_current ? "" : "（体系已修改，待重新测试）"}</p>` : '<p class="form-hint">尚无已执行测试结果。</p>'}
          ${draft.note ? `<p class="evo-note">${escape(draft.note)}</p>` : ""}
          ${draft.trial ? `<p class="form-hint">上次探索：${draft.trial.boards}副${typeof draft.trial.delta === "number" ? ` · 相对父本 ${impNumber(draft.trial.delta)} IMP/副` : ` · ${escape(statusName(draft.trial.status))}`}，不是晋级证据。</p>` : ""}` : ""}
        <button class="button small" data-dashboard-research="/api/evolutions/${run.id}/research/${session.id}">研究、调试与讨论</button></article>`;
    }).join("")}</div>`;
  }

  function systemButton(runId, system, label = "体系审查") {
    return `<a class="button small primary" href="${escape(protocolReviewURL({ run: runId, system: system.hash }))}" target="_blank" rel="noopener">${escape(label)}</a>
      <button class="button small" data-dashboard-system="${system.hash}" data-system-run="${runId}" data-system-name="${escape(system.name || system.lineage + ' 当前体系')}">工程定义</button>`;
  }

  function systems(run) {
    const versions = run.current_systems || Object.entries(run.champions).map(([lineage, hash]) => ({ lineage, hash }));
    return `<div class="evo-system-grid">${versions.map(system => {
      const result = system.latest_result;
      return `<article class="evo-system-card" data-current-system="${system.hash}">
        <div class="evo-card-heading"><strong>${escape(system.lineage)} · ${run.selection_policy === "parent-improvement" ? "当前开发亲本" : "当前冠军"}</strong><span class="evo-status">${system.promotion ? run.selection_policy === "parent-improvement" ? "本次开发接受" : "本次已晋级" : system.source?.kind === "champion" ? "继承已发布版本" : "本次起点"}</span></div>
        <h4>${escape(system.name || (system.lineage === "Z" ? "原创体系" : run.baselines.find(base => base.lineage === system.lineage)?.name || system.hash.slice(0, 12)))}</h4>
        <code class="evo-hash">${escape(system.hash.slice(0, 16))}</code><p class="form-hint">${system.rules ?? "—"}条规则 · ${system.states ?? "—"}个状态 · ${system.features ?? "—"}个特征</p>
        ${result ? `<dl class="evo-system-scores">${run.baselines.map(base => `<div><dt>对${escape(shortSystem(base.name))}</dt><dd class="${tone(result.means[base.hash])}">${impNumber(result.means[base.hash])}</dd></div>`).join("")}</dl><p class="form-hint">最近${result.boards}副${result.scope === "confirmation" ? "新牌确认" : "开发评测"} · IMP/副${Object.keys(run.comparison_issues || {}).length ? "（存在已知比较缺陷，不作正式排名）" : ""}</p>` : '<p class="form-hint">本次实验尚无该版本的完整评测。</p>'}
        ${system.promotion?.statistics ? `<p>相对父版本 ${impNumber(system.promotion.statistics.mean)}，下界 ${impNumber(system.promotion.statistics.lower)} IMP/副。</p>` : ""}
        <div class="report-actions">${systemButton(run.id, system)}<a class="button small" href="/api/evolutions/${run.id}/systems/${system.hash}.md" download>下载说明</a><a class="button small" href="/api/evolutions/${run.id}/systems/${system.hash}.json" download>IR</a></div>
        ${system.source ? `<p class="form-hint">来源 <button class="text-button" data-track-evolution="${escape(system.source.source_run)}">${escape(system.source.source_run.slice(0, 16))}</button></p>` : ""}</article>`;
    }).join("")}</div>`;
  }

  function scoreChart(rows, run) {
    const numeric = rows.flatMap(row => row.means).filter(value => typeof value === "number");
    if (!numeric.length) return "";
    const maximum = Math.max(.25, ...numeric.map(Math.abs)) * 1.15;
    const width = 800, left = 185, plotWidth = 540, height = 60 + rows.length * 52;
    const x = value => left + (value + maximum) / (maximum * 2) * plotWidth;
    return `<div class="evo-score-chart"><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="同轮各版本对两个固定对手的平均配对IMP；横轴零为持平">
      ${[-maximum, 0, maximum].map(value => `<line x1="${x(value)}" x2="${x(value)}" y1="28" y2="${height - 20}" class="${value === 0 ? "zero" : "grid"}"/><text x="${x(value)}" y="16" text-anchor="middle" class="axis-label">${impNumber(value)}</text>`).join("")}
      ${rows.map((row, index) => {
        const y = 52 + index * 52;
        return `<text x="8" y="${y + 4}" class="version-label">${escape(row.label)}</text><line x1="${left}" x2="${left + plotWidth}" y1="${y}" y2="${y}" class="grid"/>
          ${row.means.map((value, opponent) => typeof value === "number" ? `<circle cx="${x(value)}" cy="${y + (opponent === 0 ? -6 : 6)}" r="5" class="opponent-${opponent}"><title>${escape(row.label)} 对${escape(shortSystem(run.baselines[opponent].name))}：${impNumber(value)} IMP/副</title></circle>` : "").join("")}`;
      }).join("")}</svg></div>`;
  }

  function comparisons(run) {
    const rounds = scoredRounds(run);
    if (!rounds.length) return '<p class="form-hint">尚无完整成绩；运行中或失败的配对不会被当成有效最终成绩。</p>';
    if (!rounds.some(round => round.id === view.round)) view.round = [...rounds].reverse().find(round => round.scope !== "exploration")?.id || rounds.at(-1).id;
    const selected = rounds.find(round => round.id === view.round);
    const rows = comparisonRows(run, view.round);
    return `<div class="evo-comparison-controls"><label for="evo-score-round">比较轮次</label><select id="evo-score-round">${[...rounds].reverse().map(round => `<option value="${escape(round.id)}" ${round.id === view.round ? "selected" : ""}>${escape(phaseName(round.id))} · ${round.boards}副${round.scope === "exploration" ? " · 研究内探索" : ""}</option>`).join("")}</select></div>
      <p class="form-hint">${selected.scope === "exploration" ? "研究内探索，不能作为晋级结论。" : "每一行使用本轮相同牌例；不同轮次不直接混排。"} 数值为IMP/副。</p>
      <div class="evo-chart-legend">${run.baselines.map((base, index) => `<span><i class="opponent-${index}"></i>对${escape(shortSystem(base.name))}</span>`).join("")}</div>${scoreChart(rows, run)}
      <div class="evo-table-wrap"><table class="evo-score-table"><thead><tr><th>体系版本</th>${run.baselines.map(base => `<th>对${escape(shortSystem(base.name))}</th>`).join("")}<th>等权均值</th><th>状态</th></tr></thead><tbody>${rows.map(row => `<tr><th><a class="text-button" href="${escape(protocolReviewURL({ run: run.id, system: row.hash }))}" target="_blank" rel="noopener">${escape(row.label)}</a><code>${escape(row.hash.slice(0, 10))}</code></th>${row.means.map(mean => `<td class="${tone(mean)}">${impNumber(mean)}</td>`).join("")}<td class="${tone(row.objective)}">${impNumber(row.objective)}</td><td>${escape(statusName(row.status))}</td></tr>`).join("")}</tbody></table></div>`;
  }

  function generations(run) {
    return (run.generations || []).map(generation => `<details class="evolution-generation" data-dashboard-detail="generation-${generation.number}"><summary>第 ${generation.number} 代 · ${generation.candidates.length} 个候选 · ${escape(statusName(generation.status))}</summary>
      <div class="report-actions">${Object.keys(generation.analysis || {}).map(lineage => `<a class="button small" href="/api/evolutions/${run.id}/proposals/g${String(generation.number).padStart(3, "0")}-${lineage}" target="_blank" rel="noopener">${escape(lineage)}研究提交</a>`).join("")}</div>
      ${generation.candidates.map(candidate => `<article class="history-item"><div><strong>${escape(statusName(candidate.status))} · ${escape(candidate.lineage)}</strong><code>${escape(candidate.hash?.slice(0, 12) || "")}</code></div><p>${escape(candidate.hypothesis)}</p>
        ${candidate.error ? `<p class="form-error">${escape(candidate.error)}</p>` : ""}
        ${candidate.errors?.length ? `<details data-dashboard-detail="errors-${candidate.hash}"><summary>${candidate.error_count}项检查问题（展示${candidate.errors.length}项）</summary><ul>${candidate.errors.map(error => `<li>第${error.board}副 · ${escape(error.message)}</li>`).join("")}</ul></details>` : ""}
        ${candidate.full_delta != null ? `<p>复测配对提升 ${impNumber(candidate.full_delta)} IMP/副${candidate.changes ? ` · ${candidate.changes.changed_families}副改变叫牌` : ""}</p>` : ""}
        ${candidate.tests ? `<p>发布前实际回归 ${candidate.tests.passed}/${candidate.tests.tests}通过</p>` : ""}
        ${candidate.hash ? `<div class="report-actions">${systemButton(run.id, { ...candidate, name: candidate.lineage + " 候选体系" }, "查看完整候选")}<a class="button small" href="/api/evolutions/${run.id}/systems/${candidate.hash}.md" download>下载说明</a></div>` : ""}</article>`).join("")}
      ${Object.entries(generation.promotions).map(([lineage, value]) => `<p>${escape(value.lineage || lineage)} · ${escape(statusName(value.status))}${value.statistics ? ` · 相对父本 ${impNumber(value.statistics.mean)}，下界 ${impNumber(value.statistics.lower)} IMP/副（${value.statistics.boards}副新牌）` : ""}</p>`).join("")}</details>`).join("") || '<p class="form-hint">研究启动后显示每代候选、复测和晋级结果。</p>';
  }

  function recordLinks(run) {
    return `<h3>研究会话</h3>${(run.research_sessions || []).map(session => `<article class="history-item"><strong>${escape(session.id)} · ${escape(statusName(session.status))}</strong><p>${session.turns}轮模型 · ${session.tool_calls}次工具 · ${session.releases}个完整候选</p><button class="button small" data-dashboard-research="/api/evolutions/${run.id}/research/${session.id}">查看研究、工具与讨论</button></article>`).join("") || '<p class="form-hint">暂无自主研究会话。</p>'}
      <h3>审计牌例</h3>${(run.rounds || []).filter(round => round.pairs?.length).map(round => `<details class="evolution-generation" data-dashboard-detail="round-${round.id}"><summary>${escape(phaseName(round.id))} · ${round.boards}副${round.status === "blocked" ? " · 存在失败配对" : ""}</summary><div class="report-actions">${round.pairs.flatMap(pair => Array.from({ length: Math.ceil(round.boards / 1000) }, (_, index) => `<button class="button small" data-dashboard-pair="/api/evolutions/${run.id}/rounds/${round.id}/pairs/${pair.candidate}/${pair.opponent}?offset=${index * 1000}&amp;limit=1000">${escape(pair.candidate.slice(0, 6))} 对 ${escape(pair.opponent.slice(0, 6))}${round.boards > 1000 ? ` · 第${index * 1000 + 1}–${Math.min(round.boards, (index + 1) * 1000)}副` : ""}</button>`)).join("")}</div></details>`).join("")}`;
  }

  function comparisonWarning(run) {
    const issues = Object.values(run.comparison_issues || {});
    if (!issues.length) return "";
    return `<aside class="evo-comparison-warning" role="status"><strong>${liveStatuses.has(run.status) ? "历史比较问题，研究仍在运行" : "本轮已停止或交接，原比较仍有未决问题"}</strong><p>这些工程故障不表示候选已判负。非法叫品、公开保证失真等必须修复并在新版本重新评测，不能一律转成对手弃权；原牌例、错误与分数保留。</p>
      <details data-dashboard-detail="comparison-issues"><summary>查看${issues.length}组原始检查问题</summary>${issues.map(issue => `<article><p>${escape(issue.round)} · ${issue.error_count}项${issue.source_run ? ` · 来源${escape(issue.source_run.slice(0, 16))}` : ""}</p><ul>${issue.errors.slice(0, 4).map(error => `<li>第${error.board}副 · ${escape(error.message)}</li>`).join("")}</ul>${issue.source_run ? `<button class="text-button" data-track-evolution="${escape(issue.source_run)}">查看原始实验</button>` : ""}</article>`).join("")}</details></aside>`;
  }

  function referenceRevision(run) {
    const revision = run.benchmark_revision;
    if (!revision) return "";
    return `<aside class="evo-reference-revision" role="note"><strong>本次采用明确修订后的参考体系</strong><p>${escape(revision.reason)}</p>
      <details data-dashboard-detail="reference-revision"><summary>查看参考版本变更</summary>
      <ul>${(revision.previous || []).map((old, index) => `<li>${escape(shortSystem(old.name))} → ${escape(shortSystem(revision.current[index].name))}<br><code>${escape(old.hash.slice(0, 12))} → ${escape(revision.current[index].hash.slice(0, 12))}</code></li>`).join("")}</ul>
      ${revision.source_reference_unknown ? "<p>来源归档未登记参考身份；本次使用明确声明的新参考，不复用旧成绩。</p>" : ""}
      <p>旧牌例、错误、成绩和模型用量仍在来源实验保存，不与本次新牌成绩混合。</p><button class="text-button" data-track-evolution="${escape(revision.source_run)}">查看来源实验</button></details></aside>`;
  }

  function evaluationQueue(run) {
    const jobs = run.evaluation_queue || [];
    if (!jobs.length) return "";
    const names = { queued: "等待评价", allocating: "预留资源", running: "正在评价", development_complete: "开发评价完成",
      no_full_gain: "本轮未提升", insufficient_coverage: "样本未触及改动", invalid: "候选未通过校验",
      comparison_blocked: "比较受阻", budget_blocked: "资源未获预留", budget_exhausted: "评价额度用尽" };
    return `<section><h3>独立候选评价</h3><p class="form-hint">${run.evaluation_queue_unique_candidates ?? jobs.length}个不同候选 · ${run.evaluation_queue_total ?? jobs.length}个评价任务；同哈希重发不算新设计。以下为开发结果，不代表晋级。</p>
      <div class="evo-table-wrap"><table class="evo-score-table"><thead><tr><th>候选</th><th>状态</th><th>相对父本 IMP/副</th><th>回归</th><th>剩余预留配对</th></tr></thead><tbody>${jobs.map(job =>
        `<tr><td>${escape(job.lineage)} · ${escape(job.candidate.slice(0, 12))}</td><td>${escape(names[job.status] || job.status)}</td><td>${impNumber(job.outcome?.full_delta)}${job.outcome?.statistics ? `<small>95%区间 [${job.outcome.statistics.ci95.map(impNumber).join(", ")}]</small>` : ""}</td>
        <td>${job.tests ? `${job.tests.passed}/${job.tests.tests}` : "—"}</td><td>${numberText(job.remaining_pairs)} / ${numberText(job.reserved_pairs)}</td></tr>`).join("")}</tbody></table></div></section>`;
  }

  function resources(run) {
    const summary = run.resources;
    if (!summary) return "";
    const models = summary.models;
    return `<details data-dashboard-detail="resources"><summary>研究资源 · ${creditNumber(models.ai_credits)} AI Credits · ${summary.evaluated_candidates}/${summary.unique_candidates}候选已评价</summary>
      <p class="form-hint">${models.missing_credit_receipts ? `${models.missing_credit_receipts}次请求缺少已登记的计费收据；${models.credit_receipts ? `已有收据合计${creditNumber(models.known_ai_credits)}，` : ""}完整总额未知。` : "取实际上游收据，不用总token推算费用。"} 本轮单位已评价候选${creditNumber(summary.ai_credits_per_evaluated_candidate)} AI Credits。</p>
      <div class="evo-table-wrap"><table class="evo-score-table"><thead><tr><th>研究会话</th><th>请求</th><th>输入 / 输出token</th><th>缓存读取token</th><th>AI Credits</th><th>发布</th></tr></thead><tbody>
      ${Object.entries(summary.sessions).map(([id, unit]) => `<tr><td>${escape(id)}</td><td>${unit.models.requests}</td><td>${numberText(unit.models.input_tokens)} / ${numberText(unit.models.output_tokens)}</td><td>${numberText(unit.models.cache_read_tokens)}</td><td>${creditNumber(unit.models.ai_credits)}</td><td>${unit.published}</td></tr>`).join("")}</tbody></table></div>
      <a class="button small" href="/api/evolutions/${run.id}/resources">完整资源记录</a></details>`;
  }

  function growthChecks(run) {
    const checks = run.growth_checks || [];
    if (!checks.length) return "";
    return `<section><h3>共同新牌成长验证</h3><p class="form-hint">全Pass与各代版本在同一批新牌比较；下降也保留，不将多轮正收益简单相加。</p>
      ${checks.map(check => `<details data-dashboard-detail="growth-${check.generation}"><summary>单元${check.generation} · ${escape(check.status)}${check.boards ? ` · ${check.boards}副` : ""}</summary>
      ${check.error ? `<p class="form-error">${escape(check.error)}</p>` : ""}
      ${check.curve ? `<table class="evo-score-table"><thead><tr><th>版本</th><th>对固定参考 IMP/副</th><th>相对全Pass</th><th>同批相对前版</th></tr></thead><tbody>${check.curve.map((row, index) =>
        `<tr><td>${index} · ${escape(row.hash.slice(0, 10))}</td><td>${impNumber(row.objective)}</td><td>${impNumber(row.delta_to_seed)}</td><td>${impNumber(row.against_previous?.mean)}</td></tr>`).join("")}</tbody></table>` : "<p>成长检查资源已预留，单元完成后显示结果。</p>"}</details>`).join("")}</section>`;
  }

  function render(run) {
    const opened = new Set([...$("#evolution-dashboard-content").querySelectorAll("details[open]")].map(item => item.dataset.dashboardDetail));
    const scrollTop = $("#evolution-dashboard-dialog").scrollTop;
    const focused = document.activeElement?.id;
    const active = liveStatuses.has(run.status);
    const maintenance = active && (run.activity?.kind === "repair"
      || run.activity?.label?.startsWith("repair:") || run.activity?.session?.startsWith("repair-"));
    const generation = active ? run.active_generation || run.generation : run.generation;
    const progress = run.evaluation_progress;
    const waiting = active && run.activity?.started_at ? durationLabel(elapsedSeconds(run.activity.started_at)) : "";
    const researchPool = run.budgets?.maintenance_budget ? run.resources?.budget_pools?.research : null;
    $("#evolution-dashboard-title").textContent = run.name;
    $("#evolution-dashboard-content").innerHTML = `<section class="evo-live-summary ${active ? "is-live" : ""}">
      <div class="evo-card-heading"><span class="evo-status">${active ? '<i class="live-dot"></i>' : ""}${escape(statusName(run.status))}</span><span id="evolution-dashboard-sync" class="form-hint">${active ? "每2秒同步" : "已保存"} · 检查点 ${escape(timeText(run.updated_at))}</span></div>
      <h3>${escape(!active && run.research_summary ? "研究结论与版本结果" : maintenance ? "系统维护进行中" : phaseName(run.phase))}</h3><p class="evo-activity">${escape(!active && run.research_summary ? run.research_summary.conclusion : activityText(run))}${waiting ? ` <span>· 当前步骤 ${escape(waiting)}</span>` : ""}</p>
      <p class="form-hint">${escape(evaluationScope(run))}</p>
      ${progress?.status === "running" ? `<progress value="${progress.completed_pairs}" max="${progress.total_pairs}" aria-label="当前轮次配对进度"></progress><p class="form-hint">本轮 ${progress.completed_pairs}/${progress.total_pairs}个配对完成 · ${progress.boards}副共同牌</p>` : ""}
      ${run.execution_mode === "evaluation-only" ? '<p class="form-hint">仅评价冻结候选 · 不调用研究模型 · 不申请晋级</p>' : `<p class="form-hint">研究单元进度（完成单元不等于体系升级）</p><div class="evo-generation-track" aria-label="研究单元进度">${Array.from({ length: run.max_generations }, (_, index) => `<span class="${index < run.generation ? "done" : index + 1 === generation && active ? "active" : ""}"><i></i>单元${index + 1}${index < run.generation ? "完成" : index + 1 === generation && active ? "进行中" : ""}</span>`).join("")}</div>`}
      <div class="report-actions">${active ? `<button class="button small" data-dashboard-cancel="${run.id}" ${run.cancel_requested ? "disabled" : ""}>${run.cancel_requested ? "已请求安全停止" : "安全停止"}</button>` : ["ready", "failed", "interrupted", "cancelled", "blocked"].includes(run.status) ? `<button class="button small" data-dashboard-resume="${run.id}">继续原实验</button>` : ""}
        ${!active && run.source === "research" ? `<button class="button small" data-dashboard-continue="${run.id}">从当前${run.selection_policy === "parent-improvement" ? "开发亲本" : "冠军"}继续新实验</button>` : ""}
        ${!active && (run.evaluation_queue || []).some(job => ["queued", "running", "allocating"].includes(job.status)) ? `<button class="button small" data-dashboard-evaluate="${run.id}">仅继续候选评价（不调用模型）</button>` : ""}
        <a class="button small" href="/?evolution=${run.id}">固定追踪链接</a><button class="button small" data-dashboard-legacy="${run.id}">原始详情</button>
        ${run.report_available ? `<a class="button small" href="/api/evolutions/${run.id}/report" download>下载可读报告</a><a class="text-button" href="/api/evolutions/${run.id}/report?raw=1" download>原技术报告</a>` : ""}</div>
      ${run.error ? `<details data-dashboard-detail="technical-stop"><summary>停止原因原文（技术记录）</summary><p class="form-error">${escape(run.error)}</p></details>` : ""}</section>
      ${run.module_contract ? `<p class="form-hint">受控自然1NT模块 · 固定外围，内部协议可重设计 · <a href="/api/evolutions/${run.id}/module-contract" download>模块合同与探针</a></p>` : ""}
      ${referenceRevision(run)}${comparisonWarning(run)}
      <nav class="evo-tabs" role="tablist" aria-label="进化实验视图">${[["overview", "实时总览"], ["results", "迭代与成绩"], ["systems", "当前体系"], ["records", "完整记录"]].map(([value, name]) => `<button type="button" id="evo-tab-${value}" role="tab" data-dashboard-tab="${value}" aria-selected="${view.tab === value}" aria-controls="evo-panel-${value}" tabindex="${view.tab === value ? 0 : -1}">${name}</button>`).join("")}</nav>
      <section id="evo-panel-overview" role="tabpanel" aria-labelledby="evo-tab-overview" ${view.tab === "overview" ? "" : "hidden"}>
        ${renderResearchWorkflow(run, escape)}${renderResearchReport(run, escape)}
        <div class="evo-metrics">${metric("研究模型请求", researchPool?.requests ?? run.agent_requests, run.budgets?.max_agent_requests, `${run.model} / ${run.reasoning_effort || ""} · 全角色实际请求 ${run.agent_requests}`)}${metric("研究token已结算／预留", researchPool?.charged_tokens ?? run.model_usage?.charged_tokens ?? 0, run.budgets?.max_agent_tokens, run.model_usage?.unreported_requests ? `${run.model_usage.unreported_requests}次用量待确认` : researchPool ? "维护单独计账，不隐去总费用" : "研究与独立讨论共享预算")}${metric("比赛配对", run.evaluated_pairs, run.budgets?.max_pairs, `${run.promotions}次晋级`)}${metric("研究活动时间（分钟）", Math.round((run.spent_seconds - (run.resources?.maintenance_seconds || 0)) / 60), Math.round((run.budgets?.max_seconds || 0) / 60), "维护时间单列；关闭页面不停止后台任务")}</div>
        ${evaluationQueue(run)}${growthChecks(run)}${resources(run)}<h3>正在研究的工作版本</h3>${run.execution_mode === "evaluation-only" ? '<p class="form-hint">本次没有研究会话；冻结候选按队列独立评价。</p>' : drafts(run)}<div class="section-heading"><h3>重要迭代进度</h3><button class="text-button" data-dashboard-timeline="${run.id}">全部里程碑（${run.milestone_count || 0}）</button></div>
        ${(run.milestones || []).length ? milestones(run.milestones.slice(-16).reverse(), run.id) : '<p class="form-hint">新实验持续记录假设、修改、实际测试、探索比赛、发布和晋级。旧实验可从完整研究日志查看。</p>'}
      </section>
      <section id="evo-panel-results" role="tabpanel" aria-labelledby="evo-tab-results" ${view.tab === "results" ? "" : "hidden"}><h3>同轮成绩对照</h3>${comparisons(run)}<h3>候选与晋级</h3>${generations(run)}</section>
      <section id="evo-panel-systems" role="tabpanel" aria-labelledby="evo-tab-systems" ${view.tab === "systems" ? "" : "hidden"}><h3>${run.selection_policy === "parent-improvement" ? "已接受的开发亲本（不是全局确认）" : "已冻结的当前冠军"}</h3><p class="form-hint">已接受版本、最新发布候选和未发布草稿分别展示，不把它们混为强度结论。</p>${systems(run)}</section>
      <section id="evo-panel-records" role="tabpanel" aria-labelledby="evo-tab-records" ${view.tab === "records" ? "" : "hidden"}>${recordLinks(run)}<p class="form-hint">${run.goal.status === "not_checked" ? "10000副目标尚未检测。" : `10000副目标：${escape(run.goal.status)}`}</p></section>
      <p id="evolution-dashboard-error" class="form-error" role="alert" hidden></p>`;
    for (const item of $("#evolution-dashboard-content").querySelectorAll("details")) if (opened.has(item.dataset.dashboardDetail)) item.open = true;
    $("#evolution-dashboard-dialog").scrollTop = scrollTop;
    if (focused && document.getElementById(focused) && $("#evolution-dashboard-content").contains(document.getElementById(focused))) document.getElementById(focused).focus({ preventScroll: true });
  }

  async function poll() {
    const id = view.id;
    try {
      const run = await fetchJSON(`/api/evolutions/${id}`);
      if (view.id !== id || !$("#evolution-dashboard-dialog").open) return;
      view.run = run;
      render(run);
      clearTimeout(view.timer);
      if (liveStatuses.has(run.status)) view.timer = setTimeout(poll, 2000);
    } catch (error) {
      if (view.id !== id || !$("#evolution-dashboard-dialog").open) return;
      const sync = $("#evolution-dashboard-sync");
      if (sync) { sync.textContent = `同步中断，显示上次记录：${error.message}；3秒后重试`; sync.classList.add("form-error"); }
      else $("#evolution-dashboard-content").innerHTML = `<p class="form-error">${escape(error.message)}；3秒后重试。</p>`;
      clearTimeout(view.timer);
      view.timer = setTimeout(poll, 3000);
    }
  }

  async function open(id) {
    if (!/^evo-[0-9a-f]{32}$/.test(id)) throw new Error("无效的进化实验编号");
    if (view.id !== id) { view.tab = "overview"; view.round = null; view.run = null; }
    view.id = id;
    clearTimeout(view.timer);
    $("#evolution-dashboard-content").textContent = "正在读取实时进度…";
    if (!$("#evolution-dashboard-dialog").open) $("#evolution-dashboard-dialog").showModal();
    const url = new URL(location.href);
    url.searchParams.set("evolution", id);
    history.replaceState(null, "", url);
    await poll();
  }

  async function loadSystem(format) {
    const current = view.system;
    const sequence = ++view.systemLoad;
    $("#evo-system-document").textContent = "正在读取冻结体系…";
    $("#evo-system-document").setAttribute("aria-busy", "true");
    try {
      const response = await fetch(`/api/evolutions/${current.runId}/systems/${current.hash}.${format}`);
      if (!response.ok) throw new Error((await response.text()).slice(0, 300));
      const text = format === "json" ? JSON.stringify(await response.json(), null, 2) : await response.text();
      if (sequence !== view.systemLoad || !$("#evo-system-dialog").open) return;
      $("#evo-system-document").textContent = text;
      $("#evo-system-document").setAttribute("aria-busy", "false");
    } catch (error) {
      if (sequence !== view.systemLoad) return;
      $("#evo-system-document").textContent = `体系读取失败：${error.message}`;
      $("#evo-system-document").setAttribute("aria-busy", "false");
    }
  }

  async function showSystem(button) {
    view.system = { runId: button.dataset.systemRun, hash: button.dataset.dashboardSystem };
    $("#evo-system-title").textContent = button.dataset.systemName;
    $("#evo-system-hash").textContent = view.system.hash;
    for (const extension of ["json", "md"]) $("#evo-system-download-" + extension).href = `/api/evolutions/${view.system.runId}/systems/${view.system.hash}.${extension}`;
    view.systemLoad++;
    $("#evo-system-format").value = "navigation";
    $("#evo-system-document").hidden = true;
    $("#evo-system-document").textContent = "";
    $("#evo-system-browser").hidden = false;
    if (!$("#evo-system-dialog").open) $("#evo-system-dialog").showModal();
    await systemBrowser.open({ endpoint: `/api/evolutions/${view.system.runId}/systems/${view.system.hash}/query`,
      systems: view.run?.id === view.system.runId ? view.run.current_systems || [] : [] });
  }

  async function pollTimeline() {
    const id = view.timelineRun;
    try {
      const query = view.timelineOffset == null ? "" : `?offset=${view.timelineOffset}`;
      const page = await fetchJSON(`/api/evolutions/${id}/timeline${query}`);
      if (id !== view.timelineRun || !$("#evo-timeline-dialog").open) return;
      const scrollTop = $("#evo-timeline-dialog").scrollTop;
      const opened = new Set([...$("#evo-timeline-content").querySelectorAll("details[open]")].map(item => item.dataset.dashboardDetail));
      $("#evo-timeline-content").innerHTML = `<p class="form-hint">第${page.total ? page.offset + 1 : 0}–${Math.min(page.total, page.offset + page.limit)}条／共${page.total}条；最新记录在上方。</p>
        ${page.events.length ? milestones([...page.events].reverse(), id) : '<p class="form-hint">暂无里程碑；旧版实验可从完整研究日志查看。</p>'}
        <div class="report-actions">${page.offset > 0 ? `<button class="button small" data-dashboard-timeline-offset="${Math.max(0, page.offset - page.limit)}">更早记录</button>` : ""}${page.offset + page.limit < page.total ? `<button class="button small" data-dashboard-timeline-offset="${page.offset + page.limit}">较新记录</button>` : ""}<button class="button small" data-dashboard-timeline-latest>跟踪最新</button></div>`;
      for (const item of $("#evo-timeline-content").querySelectorAll("details")) if (opened.has(item.dataset.dashboardDetail)) item.open = true;
      $("#evo-timeline-dialog").scrollTop = scrollTop;
      clearTimeout(view.timelineTimer);
      if (view.timelineOffset == null && (view.run?.id !== id || liveStatuses.has(view.run.status))) view.timelineTimer = setTimeout(pollTimeline, 2500);
    } catch (error) {
      if (id !== view.timelineRun || !$("#evo-timeline-dialog").open) return;
      $("#evo-timeline-content").innerHTML = `<p class="form-error">${escape(error.message)}</p><button class="button small" data-dashboard-timeline-latest>重试</button>`;
    }
  }

  document.addEventListener("click", async event => {
    const track = event.target.closest("[data-track-evolution]");
    const tab = event.target.closest("[data-dashboard-tab]");
    const system = event.target.closest("[data-dashboard-system]");
    const research = event.target.closest("[data-dashboard-research]");
    const legacy = event.target.closest("[data-dashboard-legacy]");
    const stop = event.target.closest("[data-dashboard-cancel]");
    const resume = event.target.closest("[data-dashboard-resume]");
    const evaluate = event.target.closest("[data-dashboard-evaluate]");
    const repair = event.target.closest("[data-dashboard-repair]");
    const continuation = event.target.closest("[data-dashboard-continue]");
    const pair = event.target.closest("[data-dashboard-pair]");
    const timeline = event.target.closest("[data-dashboard-timeline]");
    const timelinePage = event.target.closest("[data-dashboard-timeline-offset]");
    const latest = event.target.closest("[data-dashboard-timeline-latest]");
    try {
      if (track) await open(track.dataset.trackEvolution);
      if (tab && view.run) { view.tab = tab.dataset.dashboardTab; render(view.run); document.getElementById("evo-tab-" + view.tab).focus({ preventScroll: true }); }
      if (system) await showSystem(system);
      if (research) await openResearch(research.dataset.dashboardResearch);
      if (legacy) await openLegacy(legacy.dataset.dashboardLegacy);
      if (stop || resume || evaluate || repair) {
        const button = stop || resume || evaluate || repair;
        button.disabled = true;
        try {
          const result = await fetchJSON(`/api/evolutions/${view.id}/${stop ? "cancel" : repair ? "repair" : evaluate ? "evaluate" : "resume"}`, {
            method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
          });
          clearTimeout(view.timer);
          if (repair && result.id !== view.id) await open(result.id);
          else await poll();
          await refreshHome();
        } finally { if (button.isConnected) button.disabled = false; }
      }
      if (continuation) {
        const run = view.run?.id === continuation.dataset.dashboardContinue ? view.run : await fetchJSON(`/api/evolutions/${continuation.dataset.dashboardContinue}`);
        $("#evolution-dashboard-dialog").close();
        await prepareContinuation(run, "champion");
      }
      if (pair) {
        if (view.pairLoading) return;
        view.pairLoading = true;
        pair.disabled = true;
        clearTimeout(view.timer);
        const label = pair.textContent;
        pair.textContent = "正在读取牌例…";
        try {
          const response = await fetch(pair.dataset.dashboardPair);
          if (!response.ok) throw new Error((await response.text()).slice(0, 300));
          const blob = await response.blob();
          $("#evolution-dashboard-dialog").close();
          await importFile(new File([blob], "evolution-pair.zip", { type: "application/zip" }));
        } finally {
          view.pairLoading = false;
          if (pair.isConnected) { pair.disabled = false; pair.textContent = label; }
          if ($("#evolution-dashboard-dialog").open) view.timer = setTimeout(poll, 2000);
        }
      }
      if (timeline) {
        view.timelineRun = timeline.dataset.dashboardTimeline;
        view.timelineOffset = null;
        $("#evo-timeline-content").textContent = "正在读取重要迭代记录…";
        if (!$("#evo-timeline-dialog").open) $("#evo-timeline-dialog").showModal();
        await pollTimeline();
      }
      if (timelinePage || latest) {
        clearTimeout(view.timelineTimer);
        view.timelineOffset = timelinePage ? Number(timelinePage.dataset.dashboardTimelineOffset) : null;
        await pollTimeline();
      }
    } catch (error) {
      toast(error.message, true);
      const target = $("#evolution-dashboard-error");
      if (target) { target.textContent = error.message; target.hidden = false; }
    }
  });
  $("#evolution-dashboard-content").addEventListener("change", event => {
    if (event.target.id === "evo-score-round") { view.round = event.target.value; render(view.run); }
  });
  $("#evolution-dashboard-content").addEventListener("keydown", event => {
    if (!event.target.matches("[data-dashboard-tab]") || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...$("#evolution-dashboard-content").querySelectorAll("[data-dashboard-tab]")];
    tabs[(tabs.indexOf(event.target) + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length].click();
  });
  $("#evo-system-format").addEventListener("change", event => {
    const navigation = event.target.value === "navigation";
    $("#evo-system-browser").hidden = !navigation;
    $("#evo-system-document").hidden = navigation;
    if (navigation) view.systemLoad++;
    else loadSystem(event.target.value);
  });
  $("#evo-system-dialog").addEventListener("close", () => { view.systemLoad++; systemBrowser.cancel(); });
  $("#evolution-dashboard-dialog").addEventListener("close", () => {
    clearTimeout(view.timer);
    const url = new URL(location.href);
    url.searchParams.delete("evolution");
    history.replaceState(null, "", url);
  });
  $("#evo-timeline-dialog").addEventListener("close", () => { clearTimeout(view.timelineTimer); view.timelineRun = null; });
  return { open };
}
