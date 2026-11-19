import assert from "node:assert/strict";
import test from "node:test";
import { renderResearchWorkflow } from "./research-workflow.mjs";
import { researchEvidenceURL } from "./evolution-view.mjs";
const escape = text => String(text ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;");

test("mandatory period returns show real schemes and failures, not paused workers as delivery", () => {
  const run = { id: `evo-${"a".repeat(32)}`, workflow: { architecture: "director", units: [], repairs: [],
    management: { limits: { turns: 24, seconds: 900, max_missed_deliveries: 2, solution_return: true },
      solution_returns: [{ id: `solution-${"b".repeat(24)}`, session: "g001-Z-h01",
        turns_used: 24, turns_allowed: 24, seconds_allowed: 900, returned_at: "now",
        solution: { proposal: "选择 1C\n<script>不是脚本</script>", basis: "当前公开分区",
          limitations: "<b>还没测试</b>", next_action: "补自然见证" } }],
      delivery_windows: [{ session: "g001-Z-h02", status: "failed", error: "<iframe>network timeout</iframe>" }] } } };
  const html = renderResearchWorkflow(run, escape);
  for (const text of ["每周期最多 24", "最后一轮", "当前最佳方案", "方案已返回（验证另计）", "选择 1C\n",
    "当前公开分区", "还没测试", "补自然见证", "24/24", "方案返回失败", "network timeout",
    `/solutions/solution-${"b".repeat(24)}`, "倒计时历史及实际工作证据"]) assert.ok(html.includes(text), text);
  for (const text of ["<script>", "<b>", "<iframe>", "停止执行者，不撤销目标", "undefined"]) assert.ok(!html.includes(text), text);
  run.workflow.management.solution_returns = [];
  assert.match(renderResearchWorkflow(run, escape), /保存草稿或暂停分支不算方案交付/);
});

test("director plan, worker facts, evidence and independent repair permission are shown separately", () => {
  const run = { id: `evo-${"a".repeat(32)}`, status: "completed", workflow: { architecture: "director",
    units: [{ id: "g001-Z", status: "completed", worker_turns: 4, plan: "大胆假设",
      jobs: [{ idea: { title: "<script>", hypothesis: "信息分区", mechanism: "接力", falsification: "新牌退化" },
        status: "reported", session: "g001-Z-h01", report: { summary: "真实结果", actual_tests: { passed: 3, tests: 3 },
          releases: [{ decision: "development_only", development: { full_delta: -.25 } }] },
        review: { learning: "不能宣称变强", decision: "stop" } }] }],
    repairs: [{ id: `repair-${"b".repeat(20)}`, kind: "baseline-completion", status: "accepted_for_next_run",
      occurrences: 2, error: { message: "missing branch" }, permissions: "append-refusal-completion-only",
      validation: { replayed_tables: 2, preserved_old_rules: 40 } }],
    repair_attempts: 1, deployment_policy: "不可替换本轮baseline" } };
  const html = renderResearchWorkflow(run, escape);
  for (const text of ["研究负责人", "怎样证伪", "执行者汇报", "负责人反馈", "-0.25", "待命系统修复", "不替换当前运行", "g001-Z-h01"]) assert.ok(html.includes(text), text);
  assert.ok(!html.includes("<script>"));
  const base = `/api/evolutions/${run.id}/research/g001-Z-h01`;
  assert.equal(researchEvidenceURL(base, { pointer: "/last_tests" }), `${base}/evidence?pointer=%2Flast_tests`);
  assert.throws(() => researchEvidenceURL(base, { session: "../secret", pointer: "" }));
});

test("provisional writes, rejected patches and verified engine recovery cannot masquerade as acceptance", () => {
  const solution = { proposal: "Try a concrete partition", limitations: "Not validated" };
  const run = { id: `evo-${"c".repeat(32)}`, workflow: { architecture: "director", units: [], repairs: [],
    recovery: { source_run: "<script>old run</script>", incident: "missing-reference", status: "running" },
    provisional_proposals: [{ id: "proposal-one", session: "g001-Z-h01", status: "applied_provisional", paths: ["/protocol/states"] }],
    management: { limits: { turns: 24, seconds: 900, solution_return: true }, solution_returns: [
      { id: "solution-one", solution, provisional_application: { status: "applied_provisional", paths: ["/protocol/states"] } },
      { id: "solution-two", solution, provisional_application: { status: "not_applied", error: "<b>stale base</b>", paths: ["/rules"] } },
    ] } } };
  let html = renderResearchWorkflow(run, escape);
  for (const text of ["临时提案已实际写入草稿", "写入时未验证／未验收", "不是发布或晋升",
    "原草稿未改动", "stale base", "/protocol/states", "等待首次真实研究工具成功"]) assert.ok(html.includes(text), text);
  for (const unsafe of ["<script>", "<b>"]) assert.ok(!html.includes(unsafe));
  assert.ok(!html.includes("已实际执行研究工具"));
  run.workflow.recovery.status = "research_resumed";
  html = renderResearchWorkflow(run, escape);
  assert.ok(html.includes("已实际执行研究工具，恢复凭证已保存"));
});

test("legacy workflow stays explicit without inventing agent reports", () => {
  assert.match(renderResearchWorkflow({ workflow: { architecture: "legacy", units: [], repairs: [] } }, escape), /旧单Agent模式/);
  assert.equal(renderResearchWorkflow({}, escape), "");
});

test("delivery responsibility and engineering wait remain visible after executors stop", () => {
  const run = { id: `evo-${"a".repeat(32)}`, status: "budget_exhausted",
    workflow: { architecture: "director", units: [], repairs: [], management: {
      limits: { turns: 6, seconds: 180, max_missed_deliveries: 2 },
      deliveries: [{ id: "delivery-one", title: "<script>unfinished slice</script>", owner: "research_team",
        status: "blocked_engineering", session: "g001-Z-h01", attempts: ["first", "second"],
        next_action: "<b>replay before hand-back</b>", blocked_by: ["repair-one"],
        last_receipt: { document_hash: "current-draft", tests_current: false, compilation: { status: "invalid" } } }],
      reviews: [{ id: "interrupted-review", session: "g001-Z-h01", created_at: "now",
        interruption: { reason: "<script>resource boundary</script>" } }],
      checkpoints: [{ id: "host-stop", session: "g001-Z-h01", source: "host_run_stop" }] } } };
  const html = renderResearchWorkflow(run, escape);
  for (const text of ["停止执行者，不撤销目标", "研究团队", "接手次数 2", "工程阻塞", "repair-one",
    "未落地，仍不算解除", "current-draft", "编译受阻", "当前稿尚无有效回归结果", "宿主保存，非模型验收",
    "management%2Fcheckpoints%2Fhost-stop", "审查已中断，交付责任与证据保留"]) assert.ok(html.includes(text), text);
  assert.ok(!html.includes("管理者正在查证"));
  for (const unsafe of ["<script>", "<b>", "undefined", "目标已验收"]) assert.ok(!html.includes(unsafe), unsafe);
});

test("global management shows enforced delivery deadlines, real queue work and escaped checkpoint evidence", () => {
  const run = { id: `evo-${"a".repeat(32)}`, workflow: { architecture: "director", units: [], repairs: [],
    management: { limits: { turns: 24, seconds: 900, max_missed_deliveries: 2 },
      remaining_gaps: ["<script>gap</script>"],
      branches: [{ session: "g001-Z-h01", status: "paused", missed_deliveries: 2,
        contract: { instruction: "<b>deliver</b>", start_turn: 24, turns: 24,
          deadline_at: "2026-11-17T05:00:00+00:00", deliverable: "tests" } }],
      reviews: [{ id: "g001-Z-h01-r0002", session: "g001-Z-h01", created_at: "now",
        error: "<iframe>review deadline</iframe>",
        checkpoint: { path: "not-an-arbitrary-link" },
        receipt: { turns_used: 24, new_tool_calls: 20, tests_current: false, met: false, late_seconds: 1.2 },
        decision: { summary: "<img src=x>", decision: "pause", deliverable: "counterexample" } }],
      tasks: [{ priority: 99, idea: { title: "<i>small exit</i>" }, status: "assigned", session: "g002-Z-h01",
        last_management_action: { reason: "<iframe>defer duplicate</iframe>" } }] } } };
  const html = renderResearchWorkflow(run, escape);
  for (const text of ["全局管理者", "24", "900", "连续 2 次", "安全边界", "已派发", "g002-Z-h01",
    "下载阶段快照", "尚未完成", "管理判断不改变成绩", "未验证或已过期", "超时 2 秒",
    "management%2Fcheckpoints%2Fg001-Z-h01-r0002"]) assert.ok(html.includes(text), text);
  assert.ok(html.includes("宿主已暂停"));
  assert.ok(html.includes("最近调度：" + escape("<iframe>defer duplicate</iframe>")));
  for (const unsafe of ["<script>", "<b>", "<img ", "<i>", "<iframe>", "undefined", "not-an-arbitrary-link"]) assert.ok(!html.includes(unsafe), unsafe);
});

test("repairable review backlog has an action and delivery exposes actual regression scope and successor", () => {
  const run = { id: `evo-${"e".repeat(32)}`, status: "blocked", workflow: { architecture: "director",
    units: [], repair_attempts: 1, deployment_policy: "旧成绩保留", repairs: [{
      id: `repair-${"f".repeat(20)}`, kind: "baseline-policy", status: "requires_review", occurrences: 4,
      permissions: "repair-protocol-with-regression-review", iteration: 2, tool_calls: 9,
      next_action: "继续修复，不交给无人处理的终态",
      validation: { replayed_tables: 8, regression_boards: 256, regression_tables: 512,
        original_valid_tables: 510, new_failures: [], authored_tests: { passed: 3, tests: 3 } } }] } };
  let html = renderResearchWorkflow(run, escape);
  for (const text of ["基线规则／公开协议修复", "可修旧规则", "维修迭代 2轮", "实际工具 9次",
    "原正常桌 510", "新增失败 0", "作者回归 3/3", "不是全空间强度证明", "继续维修并自动续接", "/repairs/repair-"]) assert.ok(html.includes(text), text);
  assert.ok(!html.includes("undefined"));
  run.workflow.repairs[0].validation = { replayed_tables: 8, regression_boards: 256, regression_tables: 512,
    original_valid_tables: 410, new_failures_count: 0, pre_existing_failures_count: 102,
    remaining_pre_existing_failures_count: 3, authored_tests: { passed: 3, tests: 3 } };
  html = renderResearchWorkflow(run, escape);
  for (const text of ["原有失败 102桌", "修后仍失败 3桌", "新增失败 0"]) assert.ok(html.includes(text), text);
  run.workflow.repairs[0].status = "validating";
  run.workflow.repairs[0].validation_progress = { completed_tables: 16, total_tables: 512, workers: 2 };
  html = renderResearchWorkflow(run, escape);
  assert.ok(html.includes("16/512个双向桌次"));
  assert.ok(html.includes("2个CPU工作进程"));
  run.workflow.repairs[0].validation_progress = { phase: "incidents", completed_tables: 8, total_tables: 290, workers: 4 };
  html = renderResearchWorkflow(run, escape);
  assert.ok(html.includes("原反例自然重放"));
  assert.ok(html.includes("8/290"));
  assert.ok(html.includes("全部保存的原牌与双方版本组合"));
  run.workflow.repairs.push({ id: `repair-${"d".repeat(20)}`, kind: "framework", status: "resolved_by_revision",
    occurrences: 1, permissions: "isolated-engine-repair-and-tests",
    error: { message: "修订引擎已包含此故障的实际修复" }, validation: { scope: "accepted-engine-tests", tests: ["test_engine"] } });
  html = renderResearchWorkflow(run, escape);
  assert.ok(html.includes("当前引擎已包含关联验收的源码修订"));
  assert.ok(!html.includes("undefined"));
  run.continuation = { id: `evo-${"a".repeat(32)}`, mode: "evaluation", resource_policy: "仅继承剩余额度" };
  html = renderResearchWorkflow(run, escape);
  assert.ok(html.includes(`data-track-evolution="${run.continuation.id}"`));
  assert.ok(html.includes("旧成绩保留"));
  assert.ok(!html.includes("data-dashboard-repair="));
});

test("campaign keeps real maintenance costs and targeted coverage separate from research and global strength", () => {
  const run = { id: `evo-${"c".repeat(32)}`, status: "running",
    budgets: { max_agent_requests: 3600, max_agent_tokens: 120000000 },
    resources: { budget_pools: { research: { requests: 2, charged_tokens: 12000 },
      maintenance: { requests: 3, charged_tokens: 2000, ai_credits: null, known_ai_credits: 1.25 } } },
    workflow: { architecture: "director", budget_mode: "campaign",
      maintenance_budget: { max_requests: 1000, max_tokens: 20000000 },
      maintenance_events: [{ id: "format", kind: "structured_interface", error: "<script>broken</script>" }],
      units: [{ id: "g001-Z", status: "implementing", worker_turns: 5, plan: "机制设计",
        jobs: [{ idea: { title: "定向机制" }, status: "reported",
          report: { summary: "真实试验", actual_trial: { boards: 512, rounds: ["a", "b"], evaluation_scope: "conditional",
            effect_coverage: { changed_families: 64, required: 64, status: "sufficient" },
            sampling_draw: { attempts: 12000, accepted: 512 } }, releases: [] } }] }],
      challenge_queue: [{ candidate: "candidate12345", parent: "incumbent67890", status: "promoted",
        boards: 2048, statistics: { mean: .2 } }],
      portfolio: [{ hash: "candidate12345", champion: true, hypothesis: "机制设计" }],
      branches: [{ title: "保留草稿", status: "retained", source_run: `evo-${"d".repeat(32)}` }], repairs: [] } };
  const html = renderResearchWorkflow(run, escape);
  for (const text of ["2/3600", "12000/120000000", "3/1000", "2000/20000000", "1.25",
    "尚有请求未结算", "变化牌族 64/64", "抽取 12000副", "命中 512副", "不等于总体强度",
    "先成为冠军不占位", "另批2048副", "保留草稿", "实际总费用仍全部保留"]) assert.ok(html.includes(text), text);
  assert.ok(!html.includes("<script>"));
  assert.ok(!html.includes("共享总预算"));
});
