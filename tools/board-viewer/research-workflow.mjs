import { creditNumber, impNumber } from "./evolution-view.mjs";
import { protocolReviewURL } from "./system-browser.mjs";

const statuses = {
  planning: "负责人制定研究方向", pending: "等待任务", implementing: "执行者实现／验证", evaluating: "可信评测",
  reported: "已汇报负责人", completed: "已闭环", cancelled: "负责人终止", budget_exhausted: "额度不足，工作已保留",
  proposing: "维护者查询／修改／复现", validating: "程序重放与差分回归", reviewing: "独立审查", requires_review: "旧工单待恢复维修",
  accepted_for_next_run: "修复已验收，交接新实验", rejected: "修复未获通过", deferred: "维修已保存，可继续",
  waiting_dependency: "等待关联故障修复后复测", engineering: "隔离工程副本内修复／测试",
  accepted_engine_revision: "工程修订已实测验收",
  resolved_by_revision: "已由关联修复覆盖，原反例复放通过",
  requires_engine_revision: "框架级工单，需隔离引擎修订",
  engine_revision_proposed: "框架修复方案已提交，待隔离工程验收",
  promoted: "已通过本批新牌升级门槛", development_only: "仅开发评价，不申请晋升",
  no_full_gain: "本轮没有优于亲本", development_complete: "开发评价已完成", not_confirmed: "新牌未支持升级",
  invalid: "未通过有效性验收", comparison_blocked: "比较受阻，未判负", insufficient_coverage: "样本未触及修改",
  blocked_harness: "接口／框架受阻，草稿与原错误已保留", queued: "等待挑战当前冠军", allocated: "正在挑战",
  reigning: "当前冠军，无需与自己比较", retained: "保留为可续接／融合分支", budget_blocked: "额度不足，候选仍保留",
  duplicate: "已比较此候选与该冠军，复用原裁决", reported_without_director_feedback: "实际结果已保留，负责人反馈暂受阻",
  awaiting_management: "到期交还全局管理者", managed_pause: "管理者暂停，草稿保留", managed_stop: "管理者结束此任务",
  active: "按交付合同执行", assigned: "已派发", paused: "暂停，保留草稿", stopped: "已结束此方向",
  delivered: "宿主证据满足本段交付", needs_followup: "已返回但尚未交付，需要补缺",
  needs_assignment: "交付责任保留，等待接手", blocked_delivery: "交付受阻，需改变方法后接手",
  blocked_engineering: "工程阻塞，等待实测修复落地", blocked_budget: "研究额度不足，责任与草稿保留",
  waiting_evidence: "等待前置验证证据", falsified: "已交付可复放负结果",
  solution_returned: "本周期方案已返回，尚未通过修复验收", delivery_failed: "本周期方案返回失败，工作保留",
};
const deliveries = { draft: "可检验未测试草稿", tests: "当前稿新增回归证据", experiment: "真实机制试验",
  counterexample: "可复放真实反例", candidate: "通过门禁发布候选" };
const decisions = { continue: "继续", redirect: "调整／拆分", pause: "暂停此分支", stop: "结束此方向" };

function renderSolutionReturns(run, management, escape) {
  return `<section data-solution-returns><h4>按周期返回的当前最佳方案</h4>
    <p>方案交付与实现、测试、修复验收、比赛晋升分别计数；不完善方案允许返回，但不能用进度或阻塞代替具体选择。</p>
    ${(management.delivery_windows || []).filter(window => window.status !== "returned").map(window => `<p class="${window.status === "failed" ? "form-error" : "research-text"}">
      ${escape(window.session || window.repair || window.label)}：
      ${window.status === "failed" ? `方案返回失败：${escape(window.error)}` : `已用 ${window.requests}/${window.turns} 轮；到期还剩 ${Math.max(0, Math.ceil((Date.parse(window.deadline_at) - Date.now()) / 1000))} 秒，未来请求槽位 ${Math.max(0, window.turns - window.requests)} 个。`}
      ${window.last_countdown ? `最近发给模型：还剩 ${window.last_countdown.seconds_remaining} 秒／${window.last_countdown.turns_remaining} 轮。` : ""}</p>`).join("")}
    ${(management.solution_returns || []).map(item => `<article class="evo-draft-card" data-solution-return>
      <h4>${escape(item.session || item.repair || item.label)} · 方案已返回（验证另计）</h4>
      <p>本周期 ${item.turns_used}/${item.turns_allowed} 轮，时间上限 ${item.seconds_allowed} 秒；
        ${escape(item.returned_at)}${item.late_seconds > 0 ? `；落盘晚于截止 ${Math.ceil(item.late_seconds)} 秒（不冒称准时）` : ""}。</p>
      <p class="research-text"><strong>具体方案：</strong>${escape(item.solution.proposal || item.solution.summary || item.solution.diagnosis)}</p>
      ${item.solution.basis ? `<p class="research-text"><strong>依据：</strong>${escape(item.solution.basis)}</p>` : ""}
      <p class="research-text"><strong>局限／待验证：</strong>${escape(item.solution.limitations || "正式提交未另列局限；实际验证与独立验收请查看下载证据及对应工单。")}</p>
      ${item.solution.next_action ? `<p class="research-text"><strong>下一步：</strong>${escape(item.solution.next_action)}</p>` : ""}
      ${item.provisional_application ? `<p data-provisional-application class="${item.provisional_application.status === "applied_provisional" ? "research-text" : "form-error"}">
        ${item.provisional_application.status === "applied_provisional" ? "临时提案已实际写入草稿；写入时未验证／未验收，不是发布或晋升。" : "临时提案未应用，原草稿未改动。"}
        ${escape(item.provisional_application.error || "")}
        涉及节点：${escape((item.provisional_application.paths || []).join("、"))}</p>` : ""}
      <a class="button small" href="/api/evolutions/${escape(run.id)}/solutions/${escape(item.id)}" download>下载方案、倒计时历史及实际工作证据</a>
      </article>`).join("") || "<p>尚无模型方案返回；保存草稿或暂停分支不算方案交付。</p>"}
    </section>`;
}

function renderManagement(run, escape) {
  const management = run.workflow.management;
  if (!management) return "";
  const limits = management.limits;
  return `<section data-campaign-manager><h3>全局管理者：交付、期限与补缺派单</h3>
    ${limits.solution_return ? `<p>每周期最多 ${limits.turns} 轮／${limits.seconds} 秒。每次模型交互告知剩余量；
      最后一轮或预留收口时间只允许返回当前最佳方案，不再继续查资料。网络请求与工程测试按剩余时间设置超时，支持取消的工具在收口边界交还。</p>
      ${renderSolutionReturns(run, management, escape)}` : `<p>每 ${limits.turns} 轮或 ${limits.seconds} 秒，先到者在模型／工具结果完整保存后的安全边界强制接管；
      单次长调用不会被中途杀死。维修与评价在交接时获得调度，管理耗时不计实现者活动时间。</p>
    <p>连续 ${limits.max_missed_deliveries} 次未交付必须暂停或结束执行者；可编译草稿两次纯草拟后必须给验证／试验／反例／发布证据。
      未测试快照不是已验证候选，管理判断不改变成绩。</p>`}
    <h4>${limits.solution_return ? "研究目标与实际验证进度（独立于每周期方案交付）" : "持续交付责任：停止执行者，不撤销目标"}</h4>
    ${(management.deliveries || []).map(goal => `<article class="evo-draft-card" data-delivery-commitment>
      <h4>${escape(goal.title)} · ${escape(goal.status === "delivered" ? "目标已验收（不等于晋升）" : statuses[goal.status] || goal.status)}</h4>
      <p>负责人：${escape(goal.owner === "research_team" ? "研究团队" : goal.owner)}；
        接手次数 ${(goal.attempts || []).length}；当前工作区 ${escape(goal.session || "尚未启动")}。</p>
      ${goal.next_action ? `<p class="research-text"><strong>下一验收点／解除阻塞：</strong>${escape(goal.next_action)}</p>` : ""}
      ${goal.blocked_by?.length ? `<p>工程依赖：${goal.blocked_by.map(id => escape(id)).join("、")}；修订验收但未落地，仍不算解除。</p>` : ""}
      ${goal.last_receipt ? `<p>最新保存版本 ${escape(goal.last_receipt.document_hash)}；
        ${goal.last_receipt.compilation?.status === "typed" ? "可编译" : "编译受阻"}；
        ${goal.last_receipt.tests_current ? `当前回归 ${goal.last_receipt.tests?.passed}/${goal.last_receipt.tests?.tests}` : "当前稿尚无有效回归结果"}。</p>` : ""}
      ${goal.outcome ? `<p class="research-text">验收结论：${escape(goal.outcome.reason)}</p>` : ""}
      </article>`).join("") || "<p>尚未启动新的交付目标；不会为历史运行伪造验收。</p>"}
    ${(management.branches || []).map(branch => `<article class="evo-draft-card"><h4>${escape(branch.session)} · ${escape(statuses[branch.status] || branch.status)}</h4>
      <p>连续未交付 ${branch.missed_deliveries ?? 0} 次${branch.last_receipt ? `；最近宿主验收：${branch.last_receipt.met ? "满足当段交付" : "未满足／初始盘点"}` : ""}。</p>
      ${branch.contract ? `<p class="research-text"><strong>本段目标：</strong>${escape(branch.contract.instruction)}</p>
        <p>交付物：${escape(deliveries[branch.contract.deliverable] || branch.contract.deliverable)}；
          从第 ${branch.contract.start_turn} 轮起限 ${branch.contract.turns} 轮；截止 ${escape(branch.contract.deadline_at)}。</p>` : ""}
      </article>`).join("")}
    <details data-dashboard-detail="management-reviews" open><summary>最近管理决策与不可变阶段快照</summary>
      ${(management.reviews || []).map(review => `<article class="evo-draft-card"><h4>${escape(review.session)} · ${escape(review.created_at)}</h4>
        <p class="research-text">${escape(review.decision?.summary || (review.interruption ? "审查已中断，交付责任与证据保留" : "管理者正在查证／决策"))}</p>
        ${review.interruption ? `<p class="research-text">停机原因：${escape(review.interruption.reason)}</p>` : ""}
        ${review.error ? `<p class="form-error">管理检查未成功（宿主已暂停，不冒充模型交付）：${escape(review.error)}</p>` : ""}
        ${review.decision ? `<p>${escape(decisions[review.decision.decision] || review.decision.decision)} ·
          下一交付：${escape(deliveries[review.decision.deliverable] || review.decision.deliverable)}</p>` : ""}
        ${review.receipt ? `<p>实际 ${review.receipt.turns_used} 轮／${review.receipt.new_tool_calls} 次工具；
          当前稿回归${review.receipt.tests_current ? ` ${review.receipt.tests?.passed}/${review.receipt.tests?.tests}` : "未验证或已过期"}；
          ${review.receipt.met ? "交付达标" : "尚未达标／初始盘点"}${review.receipt.late_seconds > 0 ? `；安全边界接管超时 ${Math.ceil(review.receipt.late_seconds)} 秒` : ""}。</p>` : ""}
        ${review.checkpoint ? `<a class="button small" href="/api/evolutions/${escape(run.id)}/research/${escape(review.session)}/evidence?pointer=${encodeURIComponent(`/management/checkpoints/${review.id}`)}" download>下载阶段快照与验收证据（未发布）</a>` : ""}
        </article>`).join("")}
    </details>
    ${(management.checkpoints || []).filter(item => item.source === "host_run_stop").map(item => `<p>
      <a class="button small" href="/api/evolutions/${escape(run.id)}/research/${escape(item.session)}/evidence?pointer=${encodeURIComponent(`/management/checkpoints/${item.id}`)}" download>下载停机交接快照（宿主保存，非模型验收）</a></p>`).join("")}
    <h4>全局待办与已派任务</h4>
    ${(management.tasks || []).map(task => `<p class="research-text">优先级 ${task.priority} · ${escape(task.idea.title)} ·
      ${escape(statuses[task.status] || task.status)}${task.session ? ` · ${escape(task.session)}` : ""}
      ${task.last_management_action ? `<br>最近调度：${escape(task.last_management_action.reason)}` : ""}</p>`).join("") || "<p>当前没有额外派单。</p>"}
    <h4>尚未完成／不完善的部分</h4>
    <ul>${(management.remaining_gaps || []).map(gap => `<li class="research-text">${escape(gap)}</li>`).join("")}</ul>
  </section>`;
}

export function renderResearchWorkflow(run, escape) {
  const workflow = run.workflow;
  if (!workflow) return "";
  const directed = workflow.architecture === "director";
  const campaign = workflow.budget_mode === "campaign";
  const pools = run.resources?.budget_pools;
  return `<section data-research-workflow><h3>研究组织与任务闭环</h3>
    <p>${directed ? "研究负责人 → 独立实现任务 → 可信评测 → 汇报／决策；单协调器写入，候选工作区相互隔离。"
      : "本历史实验采用旧单Agent模式；不会回填负责人决策或改变旧结果。新建实验可选择负责人模式。"}</p>
    ${run.continuation ? `<aside role="status"><strong>维修已交付，已创建${run.continuation.mode === "evaluation" ? "重新评测" : "评测与研究"}续接</strong>
      <p>${escape(run.continuation.resource_policy)}原冠军、候选、未完成草稿与回归均保留。</p>
      <button class="button small primary" data-track-evolution="${escape(run.continuation.id)}">查看修复后的新实验</button></aside>` : ""}
    ${run.continuation_error ? `<p class="form-error">续接尚未成功：${escape(run.continuation_error)}</p>` : ""}
    ${workflow.recovery ? `<aside data-engine-recovery role="status"><strong>已切换到验收后的独立引擎</strong>
      <p>${workflow.recovery.status === "research_resumed" ? "已实际执行研究工具，恢复凭证已保存。" : "正在等待首次真实研究工具成功，不把进程启动当成恢复。"}
      来源 ${escape(workflow.recovery.source_run)}；故障 ${escape(workflow.recovery.incident)}。</p></aside>` : ""}
    ${workflow.recovery_blocked ? `<p class="form-error">自动恢复已限次停止：${escape(workflow.recovery_blocked.reason)}</p>` : ""}
    ${workflow.provisional_proposals?.length ? `<section data-provisional-proposals><h4>最近临时体系提案</h4>
      <p>临时写入不等于验证、发布或晋升；当前稿的实际回归另计，完整来源与局限保留在方案证据中。</p>
      ${workflow.provisional_proposals.map(item => `<p>${escape(item.session)} · ${escape(item.id)}：
        ${item.status === "applied_provisional" ? "已写入，写入时未验证／未验收" : "未应用"}
        ${escape((item.paths || []).join("、"))}</p>`).join("")}</section>` : ""}
    ${run.resources?.roles && directed ? `<p class="form-hint">实际角色费用：${run.resources.roles.manager ? `全局管理 ${creditNumber(run.resources.roles.manager.ai_credits)}，` : ""}负责人 ${creditNumber(run.resources.roles.director.ai_credits)}，
      执行 ${creditNumber(run.resources.roles.implementation.ai_credits)}，修复 ${creditNumber(run.resources.roles.repair.ai_credits)} AI Credits；
      ${workflow.maintenance_budget ? "系统维护单独记账，不占研究额度；实际总费用仍全部保留。" : "共享总预算。"}</p>` : ""}
    ${campaign ? `<p class="form-hint">充足资源研究模式：任务不再均分回合／时间；负责人可继续非冠军分支或融合方案。条件试验自动补足覆盖，正式挑战仍使用另一批共同随机牌。</p>` : ""}
    ${workflow.maintenance_budget && pools ? `<details data-dashboard-detail="maintenance-ledger" open><summary>研究／系统维护独立账本</summary>
      <p>研究：${pools.research.requests}/${run.budgets.max_agent_requests}次请求，${pools.research.charged_tokens}/${run.budgets.max_agent_tokens} token。</p>
      <p>维护：${pools.maintenance.requests}/${workflow.maintenance_budget.max_requests}次请求，${pools.maintenance.charged_tokens}/${workflow.maintenance_budget.max_tokens} token；
      已知实际费用 ${creditNumber(pools.maintenance.known_ai_credits)} AI Credits${pools.maintenance.ai_credits == null ? "（尚有请求未结算）" : ""}。</p>
      <p>接口错误、格式纠正和baseline修复使用维护额度；体系设计反例、正常测试与研究讨论仍计研究。实际总费用仍全部保留。</p>
      ${(workflow.maintenance_events || []).map(event => `<p class="form-hint">${escape(event.kind)} · ${escape(event.tool || event.id)}：${escape(event.error)}</p>`).join("")}</details>` : ""}
    ${renderManagement(run, escape)}
    ${workflow.units.map(unit => `<details class="evolution-generation" data-dashboard-detail="workflow-${escape(unit.id)}" open>
      <summary>${escape(unit.id)} · ${escape(statuses[unit.status] || unit.status)} · 执行者已用${unit.worker_turns}回合</summary>
      <p class="research-text"><strong>研究负责人：</strong>${escape(unit.plan || "尚未提交计划")}</p>
      ${unit.error ? `<p class="form-error">${escape(unit.error)}</p>` : ""}
      ${(unit.jobs || []).map(job => `<article class="evo-draft-card"><h4>${escape(job.idea.title)} · ${escape(statuses[job.status] || job.status)}</h4>
        <p class="research-text"><strong>假设：</strong>${escape(job.idea.hypothesis)}</p>
        <p class="research-text"><strong>机制：</strong>${escape(job.idea.mechanism)}</p>
        <p class="research-text"><strong>怎样证伪：</strong>${escape(job.idea.falsification)}</p>
        ${job.report ? `<p class="research-text"><strong>执行者汇报：</strong>${escape(job.report.summary || job.report.worker_status)}</p>
          ${job.report.actual_tests ? `<p>实际回归 ${job.report.actual_tests.passed}/${job.report.actual_tests.tests}</p>` : ""}
          ${job.report.actual_trial ? `<p>机制试验：${job.report.actual_trial.boards}副，${job.report.actual_trial.rounds?.length || 1}批；
            ${job.report.actual_trial.effect_coverage ? `变化牌族 ${job.report.actual_trial.effect_coverage.changed_families}/${job.report.actual_trial.effect_coverage.required}；
            ${job.report.actual_trial.effect_coverage.status === "sufficient" ? "达到覆盖目标" : "覆盖仍不足，不能据此判定机制"}。` : ""}
            ${job.report.actual_trial.sampling_draw ? `程序抽取 ${job.report.actual_trial.sampling_draw.attempts}副，命中 ${job.report.actual_trial.sampling_draw.accepted}副。` : ""}
            ${job.report.actual_trial.evaluation_scope === "conditional" ? "定向条件样本，不等于总体强度。" : "探索样本，不是晋升证据。"}</p>` : ""}
          ${(job.report.releases || []).map(release => `<p>可信裁决 ${escape(statuses[release.decision] || release.decision)} · 开发收益 ${impNumber(release.development?.full_delta)} IMP/副</p>
            ${release.upgrade?.statistics ? `<p>另批新牌升级比较 ${release.upgrade.statistics.boards}副 · ${impNumber(release.upgrade.statistics.mean)} IMP/副（开发选择与统计确认不同）</p>` : ""}`).join("")}` : ""}
        ${job.review ? `<p class="research-text"><strong>负责人反馈：</strong>${escape(job.review.learning)} · ${job.review.decision === "stop" ? "停止此方向" : "继续下一假设"}</p>` : ""}
        ${job.review_error ? `<p class="form-error">负责人反馈受阻：${escape(job.review_error)}</p>` : ""}
        ${job.session ? `<div class="report-actions"><a class="button small primary" href="${escape(protocolReviewURL({ run: run.id, session: job.session }))}" target="_blank" rel="noopener">审查研究草稿（未发布）</a>
          <button class="button small" data-dashboard-research="/api/evolutions/${escape(run.id)}/research/${escape(job.session)}">查看执行细节与证据</button></div>` : ""}
        </article>`).join("")}</details>`).join("")}
    ${campaign ? `<h3>候选挑战队列与可融合分支</h3><p class="form-hint">先成为冠军不占位：后续候选挑战届时的实际冠军；未获胜候选保留，融合后必须重新测试和评测。</p>
      ${(workflow.challenge_queue || []).map(job => `<article class="evo-draft-card"><h4>${escape(job.candidate.slice(0, 12))} → 当前冠军 ${escape(job.parent.slice(0, 12))}</h4>
        <p>${escape(statuses[job.status] || job.status)}${job.statistics ? ` · 另批${job.boards}副 · ${impNumber(job.statistics.mean)} IMP/副` : ""}</p>
        ${job.error ? `<p class="form-error">${escape(job.error)}</p>` : ""}</article>`).join("") || "<p>尚无候选进入挑战队列。</p>"}
      <details data-dashboard-detail="candidate-portfolio"><summary>保留候选 ${(workflow.portfolio || []).length}个／导入研究分支 ${(workflow.branches || []).length}个</summary>
        ${(workflow.portfolio || []).map(row => `<p>${escape(row.hash?.slice(0, 12))} · ${row.champion ? "当前冠军" : "可继续研究／融合"} · ${escape(row.hypothesis || row.status || "")}</p>`).join("")}
        ${(workflow.branches || []).map(row => `<p>${escape(row.title)} · ${escape(statuses[row.status] || row.status)} ·
          <a href="${escape(protocolReviewURL({ run: run.id, session: row.id }))}" target="_blank" rel="noopener">审查保留草稿</a> ·
          <a href="/?evolution=${escape(row.source_run)}">查看原研究</a></p>`).join("")}
      </details>` : ""}
    <h3>待命系统修复</h3><p class="form-hint">没有错误不调用修复模型；只按异常唤醒。${escape(workflow.deployment_policy)}</p>
    ${workflow.repairs.length ? workflow.repairs.map(job => `<details class="evolution-generation" data-dashboard-detail="repair-${escape(job.id)}">
      <summary>${escape(job.kind === "baseline-completion" ? "基线缺口修复" : job.kind === "baseline-policy" ? "基线规则／公开协议修复" : job.kind === "framework" ? "框架修复" : job.kind)} · ${escape(statuses[job.status] || job.status)} · ${job.occurrences}次异常</summary>
      <p class="research-text">${escape(job.error?.message || "")}</p>
      <p>权限：${escape(job.permissions === "append-refusal-completion-only" ? "仅补未实现出口，不能修改旧规则或公开协议"
        : job.permissions === "diagnose-and-request-reviewed-engine-revision" ? "旧版诊断岗位，尚无已验收源码"
        : job.permissions === "repair-protocol-with-regression-review" ? "可修旧规则、参数、公开公告与接续；完整证据可查询，失败继续迭代"
        : job.permissions === "isolated-engine-repair-and-tests" ? "可修改隔离源码与测试，交付实测的新引擎版本" : job.permissions)}</p>
      ${job.iteration || job.tool_calls ? `<p>维修迭代 ${job.iteration || 1}轮 · 实际工具 ${job.tool_calls || 0}次。</p>` : ""}
      ${job.next_action ? `<p class="research-text"><strong>当前下一步：</strong>${escape(job.next_action)}</p>` : ""}
      ${job.status === "validating" && job.validation_progress ? `<p>正在运行${job.validation_progress.phase === "incidents" ? "原反例自然重放" : "随机差分回归"}：
        ${job.validation_progress.completed_tables}/${job.validation_progress.total_tables}个双向桌次，
        ${job.validation_progress.phase === "incidents" ? "全部保存的原牌与双方版本组合" : "原版与修订版各执行一次"} · ${job.validation_progress.workers}个CPU工作进程。</p>` : ""}
      ${job.blocked_by?.length ? `<p>关联维修：${job.blocked_by.map(id => escape(id)).join("、")}</p>` : ""}
      ${job.blocked_deliveries?.length ? `<p class="research-text"><strong>优先解除研究阻塞：</strong>${job.blocked_deliveries.map(id => escape(id)).join("、")}</p>` : ""}
      ${job.validation?.regression_boards != null ? `<p>程序验证：${job.validation.replayed_tables}张原反例桌；
        差分回归 ${job.validation.regression_boards}副／${job.validation.regression_tables}桌，
        原正常桌 ${job.validation.original_valid_tables ?? "未开始统计"}，新增失败 ${job.validation.new_failures_count ?? job.validation.new_failures?.length ?? "未开始统计"}。
        原有失败 ${job.validation.pre_existing_failures_count ?? job.validation.pre_existing_failures?.length ?? "未统计"}桌，修后仍失败 ${job.validation.remaining_pre_existing_failures_count ?? job.validation.pre_existing_failures?.filter?.(row => row.after_status === "failed").length ?? "未统计"}桌。
        ${job.validation.authored_tests ? `作者回归 ${job.validation.authored_tests.passed}/${job.validation.authored_tests.tests}。` : ""}
        这是实际协议验证，不是全空间强度证明。</p>`
        : job.validation?.preserved_old_rules != null ? `<p>旧版独立验证：${job.validation.replayed_tables}张原反例桌，已有${job.validation.preserved_old_rules}条规则及公开协议保持不变。</p>`
        : job.validation?.tests ? `<p>实际工程测试：${job.validation.tests.map(value => escape(value)).join("、")}；另用原代码验证反例确实失败。</p>` : ""}
      ${job.status === "resolved_by_revision" ? `<p>${job.validation?.replayed_tables != null
        ? `关联验收版本重放 ${job.validation.replayed_tables}张原反例桌通过，无需重复修改。`
        : "当前引擎已包含关联验收的源码修订。"}</p>` : ""}
      ${job.review ? `<p class="research-text">独立审查：${escape(job.review.reason)}</p>` : ""}
      ${job.diagnosis ? `<p class="research-text">诊断：${escape(job.diagnosis.diagnosis)}</p>
        <p class="research-text">${escape(job.diagnosis.summary || job.diagnosis.repair_plan)}</p>` : ""}
      ${job.deferred_reason ? `<p class="form-error">${escape(job.deferred_reason)}</p>` : ""}
      ${job.repair_error ? `<p class="form-error">修复未完成：${escape(job.repair_error)}</p>` : ""}
      ${job.status === "accepted_for_next_run" ? `<a class="button small" href="/api/evolutions/${escape(run.id)}/repairs/${escape(job.id)}/candidate" download>下载新基线版本（不替换当前运行）</a>` : ""}
      ${job.candidate ? `<a class="button small" href="${escape(protocolReviewURL({ run: run.id, system: job.candidate }))}" target="_blank" rel="noopener">按叫牌进程审查修复版</a>` : ""}
      <a class="button small" href="/api/evolutions/${escape(run.id)}/repairs/${escape(job.id)}" target="_blank" rel="noopener">完整维修证据与工具记录</a>
      </details>`).join("") : "<p>当前没有系统修复工单。</p>"}
    ${!["running", "queued"].includes(run.status) && !run.continuation && workflow.repairs.some(job => ["pending", "requires_review", "requires_engine_revision", "engine_revision_proposed", "deferred", "waiting_dependency", "proposing", "validating", "reviewing", "engineering"].includes(job.status)) && workflow.repair_attempts
      ? `<button class="button small" data-dashboard-repair="${escape(run.id)}">继续维修并自动续接（${workflow.maintenance_budget ? "使用独立维护额度" : "计入模型预算"}）</button>` : ""}
    </section>`;
}
