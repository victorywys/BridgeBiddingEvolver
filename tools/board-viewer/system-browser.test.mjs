import assert from "node:assert/strict";
import test from "node:test";
import { navigationPage, parseCallText, positionRequest, protocolReviewURL, renderNavigationResult } from "./system-browser.mjs";
import { ReviewPosition, meaningChanges, renderReviewDetail, reviewLink } from "./stage-review.mjs";

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

test("state IR stays separate from clickable transition navigation metadata", () => {
  const rendered = renderNavigationResult({ view: "read", definitions: { items: [
    { key: "state:nt", kind: "state", id: "nt", data: {
      otherwise: "nt", transitions: [{ id: "accept", when: true, to: "done", set: {} }],
    }, transition_keys: ["transition:nt/accept"], dependencies: [] },
  ] } }, escape);
  assert.ok(rendered.includes('data-nav-key="transition:nt/accept"'));
  assert.ok(rendered.includes("&quot;when&quot;: true"));
  assert.ok(!rendered.includes("&quot;on&quot;"));
});

test("review previews and historical branches do not alter the original line or send future calls", () => {
  const position = new ReviewPosition({ history: ["1C", "P", "1H"], dealer: "E" });
  position.inspect(0);
  position.preview("1D");
  assert.deepEqual(position.request(), { history: [], dealer: "E", vulnerability: "none", call: "1D" });
  assert.deepEqual(position.history, ["1C", "P", "1H"]);
  position.commit();
  assert.deepEqual(position.history, ["1D"]);
  assert.deepEqual(position.original.history, ["1C", "P", "1H"]);
  position.undo();
  assert.deepEqual(position.history, []);
  position.reset();
  assert.deepEqual(position.request().history, ["1C", "P", "1H"]);
  assert.throws(() => position.inspect(3));
});

test("human review links retain the public context and side ownership but never attach a hand", () => {
  const link = protocolReviewURL({ run: "evo-example", system: "own", initialRequest: {
    history: ["P", "P"], dealer: "E", vulnerability: "both", call: "1C", hand: "never-share-this-hand" },
    publicSystems: { NS: "opponent", EW: "own" }, hand: "not-shared" });
  const query = new URL(link, "http://localhost").searchParams;
  assert.equal(query.get("side"), "EW");
  assert.equal(query.get("opponent"), "opponent");
  assert.equal(query.get("history"), "P P");
  assert.equal(query.get("call"), "1C");
  assert.ok(!link.includes("hand"));
  const draft = reviewLink({ source: { kind: "draft", session: "saved", snapshot: "pinned" },
    run: "evo-example", initialRequest: { history: ["P"] }, side: "NS", compareTo: "parent" });
  assert.ok(draft.includes("snapshot=pinned"));
  assert.ok(draft.includes("compare_to=parent"));
});

test("human call explanations escape text and keep engineering identifiers out of the main meaning", () => {
  const result = { actor: "S", selected: { call: "1C", status: "defined", reason: "one branch",
    senders: [{ condition: '<img src=x onerror="bad">', readable: true, exclusions: ["大牌点至少16点"],
      source_key: "rule:opaque-internal-label" }], public_meaning: {}, public_error: null,
    explanation: { summary: "人工叫品，不按叫品名称猜含义", new_information: [], notes: [], warnings: [] } } };
  const html = renderReviewDetail(result);
  assert.ok(html.includes("&lt;img"));
  assert.ok(!html.includes("<img"));
  assert.ok(!html.includes("opaque-internal-label"));
  assert.ok(html.includes("本分支先排除"));
  assert.ok(html.includes("大牌点至少16点"));
  assert.ok(html.includes("没有登记新的量化保证"));
  result.selected.public_error = "当前逼叫责任禁止Pass";
  assert.match(renderReviewDetail(result), /data-review-commit disabled/);
});

test("priority exclusions name exact opening or continuation branches without excluding whole calls", () => {
  const exclusions = [
    { condition: "15–17点、均型", calls: ["1NT"] },
    { condition: "另一类方片手牌", calls: ["1D"] },
    { condition: "优先处理高花", calls: ["1H", "1S"] },
    { condition: "<未知的手牌条件>", calls: [] },
  ];
  const result = { actor: "N", history: ["P"], selected: { call: "1D", status: "defined",
    senders: [{ condition: "当前方片分支", readable: true,
      exclusions: exclusions.map(item => item.condition), exclusion_details: exclusions }],
    public_meaning: {}, public_error: null } };
  let html = renderReviewDetail(result);
  for (const text of ["1NT 开叫的优先分支", "1♦ 的更优先分支（同一叫品）",
    "1♥／1♠ 的动态优先分支", "叫品未确定或未实现的优先分支",
    "不代表排除这些叫品的全部含义", "不是对某副手牌的检测结果", "15–17点、均型",
    "&lt;未知的手牌条件&gt;"]) assert.ok(html.includes(text), text);
  assert.ok(!html.includes("<未知的手牌条件>"));
  result.history = ["1C", "P"];
  html = renderReviewDetail(result);
  assert.ok(html.includes("1NT的优先分支"));
  assert.ok(!html.includes("1NT 开叫"));
});

test("semantic comparison ignores renamed source keys but exposes changed conditions, promises and responsibilities", () => {
  const current = { status: "defined", senders: [{ fingerprint: "meaning", source_key: "rule:renamed" }],
    explanation: { purpose: "人工应叫", notes: ["同伴必须再叫"] }, public_error: null,
    public_information_after: [{ seat: "N", claims: ["黑桃至少5张"] }] };
  const previous = structuredClone(current);
  previous.senders[0].source_key = "rule:old";
  assert.deepEqual(meaningChanges(current, previous), []);
  previous.senders[0].fingerprint = "different-condition";
  previous.explanation.notes = ["同伴可以Pass"];
  previous.public_information_after[0].claims = ["黑桃至少4张"];
  assert.deepEqual(meaningChanges(current, previous), [
    "发送条件或优先级排除不同", "逼叫／接续责任不同", "预览后的公开牌情不同",
  ]);
});
