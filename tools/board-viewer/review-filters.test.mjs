import assert from "node:assert/strict";
import test from "node:test";
import { compileFilters, matchesAdvancedFilters } from "./review-filters.mjs";

const board = {
  vulnerability: "ns", imp: -10, review: { status: "flagged" },
  hand_summary: { N: { hcp: 16, shape: [5, 3, 3, 2] }, E: { hcp: 8, shape: [2, 6, 4, 1] },
    S: { hcp: 12, shape: [4, 4, 4, 1] }, W: { hcp: 4, shape: [2, 0, 2, 9] } },
  tables: [
    { contract: { level: 3, strain: "NT", declarer: "N", doubling: 1 }, tricks: 9, score_ns: 600 },
    { contract: { level: 4, strain: "S", declarer: "E", doubling: 2 }, tricks: 9, score_ns: 100 },
  ],
};
const matches = (values, record = board) => matchesAdvancedFilters(record, compileFilters(values));

test("blank filters preserve every valid deal including an unplayed custom deal", () => {
  assert.ok(matches({}));
  assert.ok(matches({}, { ...board, tables: [], imp: null }));
});
test("shape, HCP and suit length must match the same hand", () => {
  assert.ok(matches({ "shape-kind": "balanced", "hcp-min": "16", "length-min": "5" }));
  assert.equal(matches({ "shape-kind": "balanced", "hcp-min": "17", "length-min": "5" }), false);
  assert.equal(matches({ "shape-kind": "six-plus", "hcp-min": "16" }), false);
});
test("seat and partnership selectors do not search the wrong hand", () => {
  assert.ok(matches({ "hand-seat": "EW", "shape-kind": "void" }));
  assert.equal(matches({ "hand-seat": "NS", "shape-kind": "void" }), false);
  assert.ok(matches({ "hand-seat": "E", "shape-kind": "two-suited" }));
  assert.ok(matches({ "hand-seat": "S", "shape-kind": "three-suited" }));
});
test("exact SHDC and wildcard patterns are suit ordered", () => {
  assert.ok(matches({ "hand-seat": "N", "shape-pattern": "5332" }));
  assert.ok(matches({ "hand-seat": "N", "shape-pattern": "5-3-*-*" }));
  assert.equal(matches({ "hand-seat": "N", "shape-pattern": "3-5-3-2" }), false);
  assert.ok(matches({ "hand-seat": "W", "length-suit": "H", "length-max": "0" }));
});
test("all contract predicates must match a single table", () => {
  assert.ok(matches({ "contract-strain": "NT", "contract-level": "3", "contract-result": "made" }));
  assert.equal(matches({ "contract-strain": "NT", "contract-result": "down" }), false);
  assert.equal(matches({ "contract-strain": "NT", "contract-doubling": "2" }), false);
});
test("table quantifiers distinguish either, both and a specific table", () => {
  assert.ok(matches({ "contract-kind": "game", "contract-table": "both" }));
  assert.equal(matches({ "contract-result": "made", "contract-table": "both" }), false);
  assert.equal(matches({ "contract-result": "down", "contract-table": "table1" }), false);
  assert.ok(matches({ "contract-result": "down", "contract-table": "table2" }));
});
test("declarer partnership, doubling and result match together", () => {
  assert.ok(matches({ "contract-declarer": "EW", "contract-doubling": "2", "contract-result": "down" }));
  assert.equal(matches({ "contract-declarer": "NS", "contract-doubling": "2" }), false);
});
test("passed out, unfinished and unscored are not made contracts or fake zero IMP", () => {
  const passed = { ...board, imp: 0, tables: [{ contract: { passed_out: true }, score_ns: 0, tricks: null }] };
  assert.ok(matches({ "contract-kind": "passed-out" }, passed));
  assert.equal(matches({ "contract-result": "made" }, passed), false);
  assert.equal(matches({ "contract-result": "unscored" }, passed), false);
  const unfinished = { ...board, imp: null, tables: [{ contract: null, score_ns: null, tricks: null }] };
  assert.ok(matches({ "contract-kind": "unfinished", "contract-result": "unscored" }, unfinished));
  assert.equal(matches({ "imp-min": "0" }, unfinished), false);
});
test("combined review, vulnerability and inclusive absolute IMP bounds", () => {
  assert.ok(matches({ "board-vul": "ns", "review-filter": "flagged", "imp-min": "10" }));
  assert.equal(matches({ "imp-min": "11" }), false);
  assert.equal(matches({ "review-filter": "approved" }), false);
});
test("natural contract categories use the proper denomination thresholds", () => {
  const record = (level, strain) => ({ ...board, tables: [{ contract: { level, strain, declarer: "N", doubling: 1 } }] });
  assert.ok(matches({ "contract-kind": "partscore" }, record(4, "D")));
  assert.ok(matches({ "contract-kind": "game" }, record(5, "D")));
  assert.ok(matches({ "contract-kind": "slam" }, record(6, "NT")));
  assert.equal(matches({ "contract-kind": "game" }, record(6, "NT")), false);
});
test("invalid or impossible ranges fail explicitly, including non-integers", () => {
  for (const values of [{ "hcp-min": "16", "hcp-max": "15" }, { "length-min": "14" },
    { "hcp-min": "NaN" }, { "hcp-max": "12.5" }, { "imp-min": "25" },
    { "shape-pattern": "5-4-4-1" }, { "shape-pattern": "5-4-4" }, { "shape-pattern": "8-8-*-*" }]) {
    assert.throws(() => compileFilters(values));
  }
});
test("slam filters use a single table and do not invent DDS for legacy records", () => {
  const record = { ...board, tables: [
    { ...board.tables[0], slam_diagnostics: { called: false, made: false, small_not_bid: ["NS"], grand_not_bid: ["NS"] } },
    { ...board.tables[1], slam_diagnostics: { called: true, made: true, small_not_bid: [], grand_not_bid: [] } },
  ] };
  assert.ok(matches({ "slam-filter": "missed-small", "contract-table": "table1" }, record));
  assert.equal(matches({ "slam-filter": "missed-small", "contract-table": "table2" }, record), false);
  assert.ok(matches({ "slam-filter": "made", "contract-table": "table2" }, record));
  assert.equal(matches({ "slam-filter": "called" }, board), false);
  const legacy = { ...board, tables: [{ contract: { level: 6, strain: "H", declarer: "N", doubling: 1 }, tricks: 12, score_ns: 980 }] };
  assert.ok(matches({ "slam-filter": "called" }, legacy));
  assert.ok(matches({ "slam-filter": "made" }, legacy));
  assert.equal(matches({ "slam-filter": "available-small" }, legacy), false);
  const cached = { ...board, dds: { table: { N: { C: 6, D: 12, H: 12, S: 13, NT: 9 }, E: { C: 7 }, S: { C: 6 }, W: { C: 7 } } } };
  assert.ok(matches({ "slam-filter": "missed-small" }, cached));
});
