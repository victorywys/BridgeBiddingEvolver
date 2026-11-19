import assert from "node:assert/strict";
import test from "node:test";
import { clauseHighlights, renderResearchReport } from "./research-report.mjs";

const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);

test("the readable report distinguishes current, latest, intended changes and actual evidence", () => {
  const text = renderResearchReport({ id: "evo-" + "a".repeat(32), research_summary: {
    conclusion: "2个候选，1次开发升级", stability: "复核出现回退", current: ["V1"], latest: "V2",
    reading_guide: "作者设想不是裁判结论", objective: "逼叫性Pass", baseline_fault_explanation: "对手弃权单列",
    versions: [{ label: "V2", hash: "b".repeat(64), name: "<script>", decision: "评价受阻，未判负",
      is_current: false, rules: 35, states: 9, fallback: "P", author_intent: "未验证的设想",
      parent_label: "V1", evidence: { tests: { passed: 11, tests: 11 }, gain: null, errors: [] },
      opening_scope: "完整条件", opening: [{ text: "16+ → P" }], opening_total: 1, branches: [],
      changes: { total: 1, counts: { 修改决策规则: 1 }, scope: "真实语义", items: [{
        operation: "修改", kind: "决策规则", key: "rule:test", before: "旧条件 → 1C", after: "新条件 → 1D",
      }] } }],
  } }, escape);
  for (const phrase of ["研究结论先读", "体系是什么", "最新不等于最好", "作者设计意图", "实际验证与裁决",
    "原来：", "现在：", "1C", "1D", "11/11", "未判负"]) assert.ok(text.includes(phrase), phrase);
  assert.ok(text.includes("&lt;script&gt;"));
  assert.ok(!text.includes("<script>"));
  assert.ok(!text.includes("null IMP"));
  assert.ok(text.includes("data-dashboard-system"));
});

test("an absent readable report does not break legacy minimal views", () => {
  assert.equal(renderResearchReport({}, escape), "");
});

test("human rule highlights retain actions and point boundaries without claiming to replace complete conditions", () => {
  const text = '优先级900；条件：（根据全部公开叫牌重建的协议状态 等于 "idle"）且（大牌点（A=4、K=3、Q=2、J=1） 不小于 16）；行动：P';
  const result = clauseHighlights(text);
  assert.ok(result.includes("行动：P"));
  assert.ok(result.includes("本人点力边界：不小于16"));
  assert.ok(result.includes("阶段：idle"));
  assert.equal(clauseHighlights("原来没有此分支"), "原来没有此分支");
});
