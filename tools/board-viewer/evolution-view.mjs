export const liveStatuses = new Set(["queued", "running"]);

export function comparisonStateText(run) {
  if (liveStatuses.has(run.status)) return "存在固定对手检查问题；研究仍在运行，正式比较受阻。";
  if (run.continuation) return "本轮已交接新实验；原比较问题与旧成绩保留，不迁移旧分数。";
  return "研究已停止；固定对手检查问题尚未解决，正式比较受阻。";
}

export const researchToolNames = {
  navigate_system: "定位体系与依赖", read_result: "读取完整证据片段",
  read_contract: "查阅冻结模块合同", run_contract: "执行模块合同",
  read_system: "阅读体系", read_workspace: "查询研究工作区", read_reference: "查阅协议语法",
  read_evidence: "分析历史牌例", edit_system: "修改体系", generate_cases: "生成诊断牌",
  add_cases: "保存回归牌", inspect_case: "调试叫牌", write_tests: "编写测试",
  run_tests: "执行测试", validate_system: "校验完整体系", trial: "真实DDS探索比赛",
  consult: "独立讨论", research_note: "记录研究结论", publish_candidate: "发布完整候选",
  finish_research: "总结本次研究", handoff: "保存上下文交接",
  update_implementation_state: "保存已验证事实与下一步",
  read_incident: "读取完整故障证据", replay_incidents: "从空叫牌重放原始反例",
  audit_repair: "运行维护差分回归（CPU工作，不等待模型）",
  submit_repair: "提交修订体系", review_repair: "独立复核修订体系",
  read_engine_file: "阅读隔离框架源码", edit_engine_file: "修改隔离框架源码",
  run_engine_tests: "运行实际框架回归测试", read_engine_result: "读取完整工程验证结果",
  submit_engine_revision: "交付修订引擎", review_engine_revision: "独立复核修订引擎",
};

export function researchEvidenceURL(base, reference) {
  const match = /^(\/api\/evolutions\/evo-[0-9a-f]{32}\/research\/)(g[0-9]{3}-[ABZ](?:-h[0-9]{2})?|eval-q-[0-9a-f]{20})$/.exec(base);
  const session = reference.session || match?.[2];
  if (!match || !/^(g[0-9]{3}-[ABZ](?:-h[0-9]{2})?|eval-q-[0-9a-f]{20})$/.test(session || "")) throw new Error("无效的研究证据来源");
  if (reference.result_id) {
    if (!/^[0-9a-f]{64}$/.test(reference.result_id)) throw new Error("无效的完整结果标识");
    return `${match[1]}${session}/results/${reference.result_id}`;
  }
  if (typeof reference.pointer !== "string" || reference.pointer && !reference.pointer.startsWith("/")) throw new Error("无效的证据路径");
  return `${match[1]}${session}/evidence?pointer=${encodeURIComponent(reference.pointer)}`;
}

export function impNumber(value) {
  return typeof value === "number" && Number.isFinite(value)
    ? `${value > 0 ? "+" : ""}${Number(value.toFixed(3))}` : "—";
}

export function creditNumber(value) {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(2) : "—";
}

export function elapsedSeconds(timestamp, now = Date.now()) {
  const parsed = Date.parse(timestamp);
  return Number.isFinite(parsed) ? Math.max(0, Math.floor((now - parsed) / 1000)) : null;
}

export function durationLabel(seconds) {
  if (!Number.isFinite(seconds)) return "时间未记录";
  if (seconds < 60) return `${Math.max(0, Math.floor(seconds))}秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}分${Math.floor(seconds % 60)}秒`;
  return `${Math.floor(seconds / 3600)}小时${Math.floor(seconds % 3600 / 60)}分`;
}

export function activityText(run) {
  if (run.error && !liveStatuses.has(run.status)) return run.error;
  const activity = run.activity;
  if (!liveStatuses.has(run.status)) return run.status === "completed" ? "本次实验已结束，记录与当前体系已保存。" : "任务未在运行，可查看已保存记录。";
  if (activity?.kind === "model") {
    if (activity.label?.includes(":manager:")) return `全局管理者正在查证交付／调整任务 · 第${activity.request}次请求`;
    if (activity.label?.includes(":director:")) return `研究负责人正在构思／复盘 · 第${activity.request}次请求`;
    if (activity.label?.startsWith("repair:")) return `待命修复者正在诊断／独立审查 · 第${activity.request}次请求`;
    const review = activity.label?.endsWith(":review");
    return `${review ? "独立讨论正在等待模型返回" : "正在等待Astra研究结果"} · 第${activity.request}次请求`;
  }
  if (activity?.kind === "model_retry") return `模型网关暂时不可用，${activity.delay}秒退避后进行第${activity.attempt}次重试；仍使用${activity.model}。`;
  if (activity?.kind === "tool") return researchToolNames[activity.name] || activity.name;
  if (activity?.kind === "director") return activity.stage === "planning" ? "研究负责人正在提出实验假设。" : "研究负责人正在根据实际汇报决策。";
  if (activity?.kind === "manager") return "全局管理者正在验收阶段成果、处理慢分支并派发补缺任务。";
  if (activity?.kind === "repair") return "独立修复者处理系统异常；不会替换本轮冻结基线。";
  if (activity?.kind === "evaluation" && activity.stage === "regressions") return "正在实际重跑冻结候选的回归与变化见证，不调用模型。";
  if (activity?.kind === "evaluation" || run.evaluation_progress?.status === "running") return "正在运行共同牌评测，叫牌与真实DDS求解中。";
  if (activity?.kind === "research") return activity.ok === false ? "调试发现问题，已返回研究Agent继续处理。" : "研究Agent正在设计、验证或准备下一项实验。";
  return run.status === "queued" ? "等待执行" : "正在准备本轮研究";
}

export const milestoneNames = {
  generation_started: "开始新一代", generation_completed: "本代完成",
  research_started: "开始自主研究", research_completed: "研究会话完成", research_budget: "工作已保存，留待后续研究",
  research_checkpoint: "研究单元软检查点",
  hypothesis: "研究假设与结论", review: "独立讨论", design_change: "体系修改",
  tests: "实际测试结果", trial: "探索实验结果", evaluation: "共同牌评测完成",
  release: "发布完整候选", promotion: "新牌晋级确认", candidate_invalid: "候选发现硬错误",
  tool_issue: "调试问题", run_stopped: "实验停止",
  comparison_blocked: "正式比较受阻",
  model_retry: "模型网关有限重试",
  evaluation_queued: "候选已进入评价队列", candidate_evaluated: "候选开发评价完成",
  evaluation_stopped: "候选评价暂停",
  growth_verified: "共同新牌成长验证",
};

export function milestoneSummary(event) {
  if (event.kind === "tests") {
    const tests = event.tests;
    return tests ? `${tests.passed}/${tests.tests}通过，${tests.failed}失败；${tests.change_witnesses}项行为变化验证。` : "测试详情见研究日志。";
  }
  if (event.kind === "promotion") {
    if (!event.statistics) return `确认状态：${event.status}`;
    return `${event.status === "promoted" ? "已晋级" : "本次未确认晋级"}：相对父版本 ${impNumber(event.statistics.mean)} IMP/副，单侧下界 ${impNumber(event.statistics.lower)}。`;
  }
  if (event.kind === "trial") return `${event.boards}副探索 · ${event.status === "checked" ? "执行有效" : "存在检查问题"}${typeof event.delta === "number" ? ` · 相对父版本 ${impNumber(event.delta)} IMP/副` : ""}；不是晋级证据。`;
  if (event.kind === "design_change") return `${event.edits}处编辑${typeof event.rules === "number" ? `，工作版本现有${event.rules}条规则` : ""}；尚未等同于发布或晋级。`;
  if (event.kind === "evaluation") return `${event.boards}副共同新牌；${Object.values(event.statuses || {}).filter(value => value === "checked").length}个版本完整有效。`;
  if (event.kind === "candidate_invalid") return `${event.error_count}项检查问题；${event.errors?.[0]?.message || "详见对应牌例。"}`;
  if (event.kind === "comparison_blocked") return `${event.error_count}项固定对手检查问题，正式比较受阻、禁止据此晋级。${event.summary || event.errors?.[0]?.message || ""}`;
  if (event.kind === "generation_completed") return `${event.candidates}个候选，${event.promotions}次晋级。`;
  if (event.kind === "tool_issue") return `${researchToolNames[event.tool] || event.tool}：${event.error}`;
  if (event.kind === "research_budget") return `${event.turns}轮研究已保存，${event.releases}个候选已发布。`;
  return event.summary || event.error || (event.kind === "research_started" ? "允许整体修改协议；通过测试后提交冻结版本。" : "");
}

export function versionLabels(run) {
  const names = new Map((run.baselines || []).map(base => [base.hash, `${base.lineage} 原始对手`]));
  for (const generation of run.generations || []) {
    for (const [lineage, hash] of Object.entries(generation.parents || {})) if (!names.has(hash)) names.set(hash, `${lineage} 第${generation.number}代父本`);
    generation.candidates.forEach((candidate, index) => {
      if (candidate.hash && !names.has(candidate.hash)) names.set(candidate.hash, `${candidate.lineage} 第${generation.number}代候选${index + 1}`);
    });
  }
  for (const system of run.current_systems || []) names.set(system.hash,
    `${system.lineage} ${run.selection_policy === "parent-improvement" ? "当前开发亲本" : "当前冠军"}`);
  return names;
}

export function scoredRounds(run) {
  return (run.rounds || []).filter(round => Object.keys(round.means || {}).length > 0);
}

export function comparisonRows(run, roundId) {
  const round = (run.rounds || []).find(item => item.id === roundId);
  if (!round) return [];
  const names = versionLabels(run);
  const hashes = [...new Set([...(round.systems || []), ...Object.keys(round.means || {})])];
  return hashes.map(hash => {
    const means = (run.baselines || []).map(base => {
      const value = round.means?.[hash]?.[base.hash];
      return typeof value === "number" && Number.isFinite(value) ? value : null;
    });
    const value = round.objective?.[hash];
    return { hash, label: names.get(hash) || hash.slice(0, 10), means,
      objective: typeof value === "number" && Number.isFinite(value) ? value : null,
      status: round.candidate_statuses?.[hash] || "unrecorded", boards: round.boards, scope: round.scope };
  });
}

export function currentDrafts(run) {
  const latest = new Map();
  for (const session of run.research_sessions || []) {
    if (!latest.has(session.lineage) || latest.get(session.lineage).id < session.id) latest.set(session.lineage, session);
  }
  return [...latest.values()];
}
