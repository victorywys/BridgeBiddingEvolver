import assert from "node:assert/strict";
import test from "node:test";
import { navigationPage, parseCallText, positionRequest, renderNavigationResult } from "./system-browser.mjs";

const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);

test("call input preserves passes and converts bridge symbols without guessing missing passes", () => {
  assert.deepEqual(parseCallText("1♠ pass 2♥ X XX"), ["1S", "P", "2H", "X", "XX"]);
  assert.deepEqual(parseCallText(""), []);
  assert.throws(() => parseCallText("1S-2H"));
});

test("a hand query sends only the actor's hand and the public position", () => {
  const request = positionRequest({ history: "1NT P", hand: "AKQ.JT9.KQ32.876", dealer: "E" }, true);
  assert.equal(request.observation.seat, "W");
  assert.equal(request.view, "hand");
  assert.deepEqual(Object.keys(request.observation).sort(), ["dealer", "hand", "history", "seat", "vulnerability"]);
  assert.throws(() => positionRequest({ history: "" }, true));
});

test("meaning queries do not accidentally attach a hand", () => {
  const request = positionRequest({ history: "1NT P", call: "4NT", hand: "not-sent" });
  assert.deepEqual(request, { view: "auction", history: ["1NT", "P"], dealer: "N", vulnerability: "none", call: "4NT" });
  assert.throws(() => positionRequest({ history: "", call: "1S P" }));
});

test("definitions, identifiers and public explanations are HTML escaped", () => {
  const rendered = renderNavigationResult({ view: "read", definitions: { items: [
    { key: 'rule:"<script>', kind: "rule", id: "<img>", action: "1S", data: { x: "</pre><script>" }, description: "<svg>" },
  ] }, dependencies: { complete_in_response: false } }, escape);
  assert.ok(rendered.includes("&lt;script&gt;"));
  assert.ok(!rendered.includes("<script>"));
  assert.ok(rendered.includes("依赖尚未全部展开"));
  const meaning = renderNavigationResult({ view: "auction", state: "nt", explanation: {
    purpose: "<script>", summary: "4NT定量", new_information: [], notes: [], warnings: ["<img>"],
  }, compatible_rules: { total: 0, items: [] }, suggested_keys: [] }, escape);
  assert.ok(!meaning.includes("<script>"));
  assert.ok(meaning.includes("4NT定量"));
  assert.ok(meaning.includes("是否命中仍需手牌判断"));
});

test("all views expose explicit continuation pages including zero and missing results", () => {
  for (const field of ["modules", "matches", "definitions", "users", "changes", "compatible_rules"]) {
    const page = { total: 0, offset: 0, items: [], next_offset: null };
    assert.equal(navigationPage({ [field]: page }), page);
  }
});
