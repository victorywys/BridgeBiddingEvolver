import assert from "node:assert/strict";
import test from "node:test";
import { buildAuctionStatistics } from "./auction-stats.mjs";

const systems = { NS: { hash: "A" }, EW: { hash: "B" } };
const table = (calls, definitions = systems) => ({ systems: definitions, auction: calls.split(" ").map(call => ({ call })) });
const board = (tables, dealer = "N", vulnerability = "none") => ({ dealer, vulnerability, tables });

test("conditional frequency uses the whole exact prefix and distinct-board denominator", () => {
  const first = board([table("1C P 1H P"), table("1C P 1H P")]);
  const second = board([table("1C P 1S P"), table("1C P 1S P")]);
  const stats = buildAuctionStatistics([first, second]).get(first, 0, 2);
  assert.equal(stats.matches, 2);
  assert.equal(stats.opportunities, 4);
  assert.equal(stats.matchedBoards, 1);
  assert.equal(stats.conditionalPercent, 50);
  assert.equal(stats.allBoardsPercent, 50);
  assert.equal(stats.allTablesPercent, 50);
  assert.deepEqual(stats.prefix, ["1C", "P"]);
  assert.equal(stats.alternatives.reduce((sum, item) => sum + item.matches, 0), 4);
});
test("Pass, double and every previous call matter; identical isolated calls do not match", () => {
  const first = board([table("1C P 1H P")]);
  const different = board([table("1D P 1H P"), table("1C X 1H P")]);
  assert.equal(buildAuctionStatistics([first, different]).get(first, 0, 2).opportunities, 1);
});
test("strict mode keeps system versions, assignments, dealer and vulnerability separate", () => {
  const first = board([table("1C P 1H P")]);
  const others = [
    board([table("1C P 1H P", { NS: { hash: "A2" }, EW: { hash: "B" } })]),
    board([table("1C P 1H P")], "S"),
    board([table("1C P 1H P")], "N", "both"),
  ];
  assert.equal(buildAuctionStatistics([first, ...others]).get(first, 0, 2).matches, 1);
});
test("sequence mode ignores absolute seats and vulnerability but preserves relative systems", () => {
  const first = board([table("1C P 1H P")]);
  const rotated = board([table("1C P 1H P", { NS: { hash: "B" }, EW: { hash: "A" } })], "E", "both");
  assert.equal(buildAuctionStatistics([first, rotated]).get(first, 0, 2, "sequence").matches, 2);
});
test("future calls and private fields cannot affect an earlier-prefix rate", () => {
  const first = board([table("1C P 1H P")]);
  const other = board([table("1C P 1H X 2H")]);
  other.tables[0].auction[2].private_trace = { rule_id: "SECRET" };
  other.dds = { unrelated: 99 };
  assert.equal(buildAuctionStatistics([first, other]).get(first, 0, 2).matches, 2);
});
test("unfinished prefixes with no recorded next decision do not inflate opportunities", () => {
  const first = board([table("1C P 1H")]);
  const other = board([table("1C P")]);
  assert.equal(buildAuctionStatistics([first, other]).get(first, 0, 2).opportunities, 1);
});
test("failed boards remain in the batch denominator but missing auctions do not manufacture Pass", () => {
  const first = board([table("1C P 1H")]);
  const failed = board([{ systems, auction: [], error: { message: "policy failed" } }]);
  failed.audit_result = { status: "failed" };
  const result = buildAuctionStatistics([first, failed]).get(first, 0, 0);
  assert.equal(result.totalBoards, 2);
  assert.equal(result.totalTables, 2);
  assert.equal(result.opportunities, 1);
  assert.equal(result.matches, 1);
  assert.equal(result.allBoardsPercent, 50);
  assert.deepEqual(result.alternatives.map(item => item.call), ["1C"]);
});
test("human references never enter system statistics", () => {
  const first = board([table("1C P 1H")]);
  first.human_references = [table("1C P 1S")];
  assert.equal(buildAuctionStatistics([first]).get(first, 0, 2).matches, 1);
});
test("unseen interactive decisions report zero matches, not a fake denominator", () => {
  const first = board([table("1C P 1H")]);
  const interactive = board([table("1D P 1H")]);
  const result = buildAuctionStatistics([first]).get(interactive, 0, 2);
  assert.equal(result.matches, 0);
  assert.equal(result.conditionalPercent, null);
  assert.equal(result.allBoardsPercent, 0);
});
test("invalid selections fail explicitly", () => {
  const first = board([table("P P P P")]);
  const statistics = buildAuctionStatistics([first]);
  assert.throws(() => statistics.get(first, 0, 4));
  assert.throws(() => statistics.get(first, 0, 1, "bad"));
});
