import { createStageReview, reviewLink } from "./stage-review.mjs";
import { parseCallText } from "./system-browser.mjs";

const root = document.getElementById("protocol-review");

async function fetchJSON(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const error = await response.json();
    throw new Error(error.error || `HTTP ${response.status}`);
  }
  return response.json();
}

async function open() {
  const query = new URLSearchParams(location.search);
  const run = query.get("evolution") || undefined;
  const history = parseCallText(query.get("history") || "");
  const turn = query.has("turn") ? Number(query.get("turn")) : undefined;
  if (turn !== undefined && (!Number.isInteger(turn) || turn < 1 || turn > history.length || query.has("call"))) {
    throw new Error("历史位置无效，不能同时指定历史步数与新的预览叫品。");
  }
  const sourceQuery = run ? "?evolution=" + encodeURIComponent(run) : "";
  const catalog = await fetchJSON("/api/protocol-sources" + sourceQuery);
  document.getElementById("review-back").href = run ? "/?evolution=" + encodeURIComponent(run) : "/";
  document.getElementById("review-run-name").textContent = catalog.name || "本机已登记的冻结体系";
  const view = createStageReview(root, {
    query: payload => fetchJSON("/api/protocol-review", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
    onPosition: state => window.history.replaceState(null, "", reviewLink(state)),
  });
  await view.open({ run, sources: catalog.sources,
    value: query.has("session") ? "draft:" + query.get("session") : query.has("system") ? "frozen:" + query.get("system") : undefined,
    snapshot: query.get("snapshot") || undefined, side: query.get("side") || undefined, opponent: query.get("opponent") || undefined,
    compareTo: query.get("compare_to") || undefined,
    initialRequest: { history, dealer: query.get("dealer") || "N", vulnerability: query.get("vulnerability") || "none",
      call: query.get("call") || "", turn } });
}

document.getElementById("review-copy").addEventListener("click", async () => {
  const status = document.getElementById("review-copy-status");
  try {
    await navigator.clipboard.writeText(location.href);
    status.textContent = "链接已复制（仅含公开进程与版本，不含手牌）";
  } catch (error) {
    status.textContent = `无法自动复制：${error.message}。请复制浏览器地址栏中的链接。`;
  }
});

open().catch(error => {
  root.textContent = "体系审查页面无法加载：" + error.message;
  root.classList.add("form-error");
});
