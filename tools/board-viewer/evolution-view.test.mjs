import assert from "node:assert/strict";
import test from "node:test";
import { activityText, comparisonRows, currentDrafts, durationLabel, elapsedSeconds, impNumber, milestoneSummary, scoredRounds } from "./evolution-view.mjs";

test("live model waits are distinct from tools, DDS evaluation and stopped jobs", () => {
  assert.match(activityText({ status: "running", activity: { kind: "model", request: 8, label: "g001-Z:turn:8" } }), /等待Astra.*第8次/);
  assert.match(activityText({ status: "running", activity: { kind: "model", request: 9, label: "g001-Z:review" } }), /独立讨论/);
  assert.equal(activityText({ status: "running", activity: { kind: "tool", name: "run_tests" } }), "执行测试");
  assert.match(activityText({ status: "running", activity: { kind: "evaluation" } }), /真实DDS/);
  assert.match(activityText({ status: "running", activity: { kind: "model_retry", delay: 5, attempt: 1, model: "gpt-6-astra" } }), /第1次重试.*gpt-6-astra/);
  assert.match(activityText({ status: "completed", activity: { kind: "model" } }), /已结束/);
  assert.equal(activityText({ status: "failed", error: "DDS unavailable" }), "DDS unavailable");
});

test("waiting duration and missing scores never become invented zero results", () => {
  assert.equal(elapsedSeconds("2026-11-04T00:00:00Z", Date.parse("2026-11-04T00:01:05Z")), 65);
  assert.equal(elapsedSeconds(undefined), null);
  assert.equal(elapsedSeconds("2026-11-05T00:00:00Z", Date.parse("2026-11-04T00:00:00Z")), 0);
  assert.equal(durationLabel(65), "1分5秒");
  assert.equal(durationLabel(3661), "1小时1分");
  assert.equal(impNumber(0), "0");
  for (const value of [null, undefined, true, NaN, Infinity]) assert.equal(impNumber(value), "—");
});

const run = {
  baselines: [{ lineage: "A", hash: "B1" }, { lineage: "B", hash: "B2" }],
  current_systems: [{ lineage: "Z", hash: "C" }],
  generations: [{ number: 1, parents: { Z: "P" }, candidates: [{ lineage: "Z", hash: "C" }] }],
  rounds: [
    { id: "g001-full", systems: ["P", "C", "broken"], boards: 1000, scope: "development",
      objective: { P: 0, C: .25 }, means: { P: { B1: 0, B2: 0 }, C: { B1: -.5, B2: 1 } },
      candidate_statuses: { P: "checked", C: "checked", broken: "invalid" } },
    { id: "r-g001-Z-trial", systems: ["C"], boards: 2, scope: "exploration",
      objective: { C: 7 }, means: { C: { B1: 8, B2: 6 } }, candidate_statuses: { C: "checked" } },
    { id: "unfinished", systems: ["C"], status: "running", boards: 1000 },
  ],
};

test("comparison rows keep the selected round's common sample, opponents and per-version errors", () => {
  const rows = comparisonRows(run, "g001-full");
  assert.equal(rows[1].label, "Z 当前冠军");
  assert.deepEqual(rows[1].means, [-.5, 1]);
  assert.equal(rows[1].objective, .25);
  assert.equal(rows[1].boards, 1000);
  assert.deepEqual(rows[2].means, [null, null]);
  assert.equal(rows[2].objective, null);
  assert.equal(rows[2].status, "invalid");
  assert.deepEqual(rows[0].means, [0, 0]);
  assert.deepEqual(comparisonRows(run, "missing"), []);
});

test("exploratory scores are explicitly separated and cannot overwrite a formal round", () => {
  assert.equal(scoredRounds(run).length, 2);
  assert.equal(comparisonRows(run, "r-g001-Z-trial")[0].scope, "exploration");
  assert.equal(comparisonRows(run, "g001-full")[1].objective, .25);
  assert.equal(comparisonRows(run, "unfinished")[0].objective, null);
});

test("important milestones distinguish hypotheses, test coverage, exploration and confirmed promotion", () => {
  assert.match(milestoneSummary({ kind: "hypothesis", summary: "探索新编码，不是已确认提升" }), /不是已确认/);
  assert.match(milestoneSummary({ kind: "tests", tests: { passed: 4, tests: 5, failed: 1, change_witnesses: 2 } }), /4\/5通过，1失败/);
  assert.match(milestoneSummary({ kind: "trial", boards: 16, status: "checked", delta: .5 }), /不是晋级证据/);
  assert.match(milestoneSummary({ kind: "promotion", status: "promoted", statistics: { mean: .2, lower: .01 } }), /已晋级.*\+0.2.*\+0.01/);
  assert.match(milestoneSummary({ kind: "promotion", status: "not_confirmed", statistics: { mean: .2, lower: -.01 } }), /未确认晋级/);
});

test("drafts use each lineage's latest episode, never label the draft as a champion", () => {
  const sessions = [
    { id: "g001-Z", lineage: "Z", working: { hash: "old-draft" } },
    { id: "g002-Z", lineage: "Z", working: { hash: "new-draft" } },
    { id: "g001-A", lineage: "A", working: { hash: "A-draft" } },
  ];
  assert.deepEqual(currentDrafts({ research_sessions: sessions }).map(item => item.working.hash), ["new-draft", "A-draft"]);
  assert.equal(run.current_systems[0].hash, "C");
});
