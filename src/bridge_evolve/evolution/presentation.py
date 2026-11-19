"""Human-facing system, change and conclusion views derived from frozen evidence."""

import hashlib
import json
from collections import Counter
from functools import lru_cache
from pathlib import Path

from bridge_evolve.evolution.feedback import short_text
from bridge_evolve.systems import validate_system
from bridge_evolve.systems.navigation import PublicFilter, system_index
from bridge_evolve.systems.render import render_action, render_expression

DECISIONS = {
    "queued": "已发布，等待评价", "allocating": "已发布，预留评价资源", "running": "正在评价",
    "development_complete": "开发评价已完成，尚不等于升级", "no_full_gain": "本轮未优于亲本",
    "insufficient_coverage": "样本未触及改动，尚不能判断强弱", "comparison_blocked": "评价受阻，未判负",
    "invalid": "候选未通过有效性检查", "budget_blocked": "评价资源不足", "budget_exhausted": "评价预算用尽",
}
KINDS = {"rule": "决策规则", "transition": "公开接续", "announcement": "公开含义",
         "feature": "共享条件", "parameter": "协议参数", "state": "协议阶段", "fallback": "兜底"}


@lru_cache(maxsize=32)
def _system(path, digest):
    contents = Path(path).read_bytes()
    if hashlib.sha256(contents).hexdigest() != digest:
        raise ValueError("The readable report's frozen system differs from its registered identity.")
    system = validate_system(json.loads(contents))
    if system.content_hash != Path(path).stem:
        raise ValueError("The readable report's system content identity is inconsistent.")
    return system


def frozen_system(directory, state, identity):
    name = f"systems/{identity}.json"
    if name not in state["system_files"]:
        raise ValueError("Readable reports require registered frozen versions.")
    return _system(str(directory / name), state["system_files"][name])


def clause(index, key):
    node = index.nodes[key]
    parts = []
    if node.priority is not None:
        parts.append(f"优先级{node.priority}")
    if key in index.expressions:
        parts.append("条件：" + short_text(render_expression(index.expressions[key]), 900))
    if key in index.actions:
        parts.append("行动：" + short_text(render_action(index.actions[key]), 600))
    elif node.kind == "transition":
        parts.append("转入：" + node.data["to"])
        if node.data.get("forcing"):
            parts.append("逼叫义务：" + node.data["forcing"])
        if node.data["set"]:
            parts.append("公开更新：" + ", ".join(node.data["set"]))
    elif node.kind == "announcement":
        parts.append("声明：" + ", ".join(node.data["tags"]))
    elif node.kind == "state":
        parts.append("其他叫品转入：" + node.data["otherwise"])
    elif not parts:
        parts.append(json.dumps(node.data, ensure_ascii=False))
    return short_text("；".join(parts), 1600)


@lru_cache(maxsize=32)
def system_description(system):
    index = system_index(system)
    public = PublicFilter(system.initial)
    opening = [rule for rule in sorted(system.rules, key=lambda rule: -rule.priority)
               if public.value(rule.when) is not False]
    return {
        "name": system.name, "hash": system.content_hash, "rules": len(system.rules),
        "states": len(system.states), "features": len(system.features), "initial_state": system.initial,
        "fallback": render_action(system.fallback),
        "opening": [{"key": "rule:" + rule.id, "id": rule.id, "text": clause(index, "rule:" + rule.id)}
                    for rule in opening[:10]],
        "opening_total": len(opening),
        "opening_scope": "初始阶段可用的规则，按真实优先级排列；完整条件保留，命中仍取决于手牌和公开历史。",
        "branches": [{"state": state.name, "transitions": len(state.transitions),
                      "targets": sorted({item.target for item in state.transitions} | {state.otherwise})}
                     for state in system.states][:30],
        "branches_total": len(system.states),
    }


@lru_cache(maxsize=48)
def changes_between(after, before):
    newer, older = system_index(after), system_index(before)
    counts, changes = Counter(), []
    order = {"rule": 0, "transition": 1, "announcement": 2, "parameter": 3, "state": 4, "feature": 5, "fallback": 6}
    for key in sorted(newer.nodes.keys() | older.nodes.keys(),
                      key=lambda key: (order[(newer.nodes.get(key) or older.nodes[key]).kind], key)):
        node = newer.nodes.get(key) or older.nodes[key]
        if node.kind == "feature" and node.identifier.startswith("shared."):
            continue
        if key in newer.nodes and key in older.nodes and newer.fingerprint(key) == older.fingerprint(key):
            continue
        operation = "新增" if key not in older.nodes else "删除" if key not in newer.nodes else "修改"
        counts[operation + KINDS[node.kind]] += 1
        changes.append({"key": key, "kind": KINDS[node.kind], "operation": operation,
                        "before": clause(older, key) if key in older.nodes else "原来没有此分支",
                        "after": clause(newer, key) if key in newer.nodes else "此分支已删除"})
    return {"total": len(changes), "counts": dict(counts), "items": changes[:16],
            "shown": min(16, len(changes)),
            "scope": "按解析后的实际语义比较，不把数组重排当修改；这里只展示前16项，完整定义可按版本打开。"}


def research_summary(directory, state, config):
    jobs = list(state.get("evaluation_queue", {}).values())
    promotions = [promotion for generation in state["generations"].values()
                  for promotion in generation.get("promotions", {}).values()]
    accepted = {row["candidate"]: row for row in promotions if row["status"] == "promoted"}
    identities = list(state.get("origin_hashes", state["champions"]).values())
    for job in jobs:
        for identity in (job["parent"], job["candidate"]):
            if identity not in identities:
                identities.append(identity)
    for identity in state["champions"].values():
        if identity not in identities:
            identities.append(identity)
    labels = {identity: f"V{position}" for position, identity in enumerate(identities)}
    versions = []
    for identity in identities:
        system = frozen_system(directory, state, identity)
        job = next((row for row in reversed(jobs) if row["candidate"] == identity), None)
        promotion = accepted.get(identity)
        row = {"label": labels[identity], **system_description(system),
               "is_current": identity in state["champions"].values(),
               "decision": "本次起点（不代表目标已完成）", "author_intent": None, "evidence": None,
               "parent_label": labels.get(job["parent"]) if job else None}
        if job is not None:
            path = job["release"]
            contents = (directory / path).read_bytes()
            if hashlib.sha256(contents).hexdigest() != state["artifacts"].get(path):
                raise ValueError("The readable report's release provenance does not match the recorded artifact.")
            release = json.loads(contents)
            hypothesis = release.get("hypothesis")
            row["author_intent"] = (short_text(hypothesis, 2200) if isinstance(hypothesis, str) and hypothesis.strip()
                                    else "来源发布物未登记设计假设；以可执行定义和实际结果为准。")
            row["decision"] = DECISIONS.get(job["status"], job["status"])
            row["changes"] = changes_between(system, frozen_system(directory, state, job["parent"]))
            tests = job.get("tests") or release.get("tests")
            outcome = job.get("outcome", {})
            row["evidence"] = {
                "tests": {key: tests[key] for key in ("tests", "passed", "failed", "change_witnesses", "protocol_witnesses")
                          if key in tests} if tests else None,
                "protocol": job.get("protocol_goal") or job.get("module_contract") or release.get("protocol_goal"),
                "gain": outcome.get("full_delta"), "boards": config["boards"]["full"] if "full_delta" in outcome else None,
                "kind": "条件开发评价" if outcome.get("evaluation_scope") == "conditional" else "开发评价",
                "score_contributions": outcome.get("score_contributions"),
                "errors": outcome.get("errors", [])[:4], "error_count": outcome.get("error_count", 0),
                "behavior": outcome.get("changes"), "natural_coverage": outcome.get("protocol_coverage"),
                "statistics": outcome.get("statistics"),
            }
        if promotion is not None:
            development = promotion.get("global_confirmed") is False or config.get("selection_policy") == "parent-improvement"
            row["decision"] = "已接受为开发亲本（不是全局确认）" if development else "通过本实验新牌晋级门槛"
            row["evidence"] = {"tests": None, "protocol": None, "errors": [], "score_contributions": None,
                               **(row["evidence"] or {}), "gain": promotion["statistics"]["mean"],
                               "boards": promotion["statistics"]["boards"], "kind": "新牌开发升级" if development else "新牌确认",
                               "statistics": promotion["statistics"],
                               "score_contributions": promotion.get("score_contributions")}
        versions.append(row)
    blocked = sum(job["status"] == "comparison_blocked" for job in jobs)
    scored = sum("full_delta" in job.get("outcome", {}) for job in jobs)
    running = state["status"] in ("running", "queued")
    evaluation_only = state.get("execution_mode") == "evaluation-only" or config.get("evaluation_only", False)
    if evaluation_only:
        conclusion = (f"{'无模型复评进行中' if running else '本次无模型复评已停止'}："
                      f"载入{len({job['candidate'] for job in jobs})}个已有冻结发布物，{scored}个获得完整分数；不修改来源研究的亲本。")
    else:
        conclusion = f"{'研究进行中' if running else '本次研究已停止'}：发布{len({job['candidate'] for job in jobs})}个不同候选，{len(accepted)}次接受为后续亲本，{scored}个候选获得完整分数。"
    if blocked:
        conclusion += f"{blocked}个候选评价受阻，不表示已经判负。"
    growth = [job for job in state.get("growth_checks", {}).values() if job["status"] == "completed"]
    reversals = [row for job in growth for row in job.get("curve", []) if row.get("against_previous", {}).get("mean", 0) < 0]
    stability = ("共同新牌复核出现后版低于前版，尚未证明持续变强。" if reversals else
                 "已有同批新牌成长检查；仍须分清开发选择和独立强度结论。" if growth else
                 "尚无已完成的同批历代重测，不能据候选数量判断越来越强。")
    objective = ("从全Pass起步，设计公开强制同伴回应的逼叫性Pass完整体系；点力及叫品映射由研究者决定。"
                 if config.get("protocol_goal") == "forcing-pass" else
                 "在冻结外围接口内研究自然1NT完整后续。" if config.get("module_contract") else
                 "研究可执行的完整搭档叫牌协议。")
    policy = config.get("baseline_fault_policy", "strict")
    policy_explanation = ("baseline未实现分支按配对弃权计候选+24 IMP；保留原错误，单列弃权贡献；候选和基础设施错误仍严格失败。"
                          if policy == "baseline-forfeit-24" else
                          "本历史运行按严格策略计分，baseline错误会阻塞比较；新策略不会篡改其旧分数。")
    if state.get("baseline_policy_revision"):
        policy_explanation += "来源运行的旧baseline错误已单独归档，不作为本次新策略评价的永久阻塞条件。"
    if any(row.get("kind") == "baseline-policy-metadata" for row in state.get("recoveries", [])):
        policy_explanation += "本次只修复过继承的阻塞标记；原计分、模型用量和计分引擎身份均未修改，恢复前状态已备份。"
    return {"schema": "bridge-readable-research/v1", "objective": objective, "conclusion": conclusion,
            "stability": stability, "running": running, "updated_at": state["updated_at"],
            "current_title": "本次评价起点（不改来源亲本）" if evaluation_only else "当前接受版本",
            "current": [labels[identity] for identity in state["champions"].values() if identity in labels],
            "latest": labels[jobs[-1]["candidate"]] if jobs else None,
            "candidate_count": len({job["candidate"] for job in jobs}), "accepted_count": len(accepted),
            "research_units": list(state.get("research_units", {}).values()),
            "research_budget_mode": config.get("research_budget_mode", "episode"),
            "maintenance_budget": config.get("maintenance_budget"),
            "challenge_queue": list(state.get("challenge_queue", {}).values()),
            "retained_branches": list(state.get("research_branches", {}).values()),
            "repair_jobs": list(state.get("repair_jobs", {}).values()),
            "scored_count": scored, "blocked_count": blocked,
            "baseline_fault_policy": policy,
            "baseline_fault_explanation": policy_explanation,
            "versions": versions,
            "reading_guide": "V号是版本顺序，不代表强弱。作者设想不是裁判结论；不同新牌块的绝对成绩不可直接相加。",
            "growth": [{"id": job["id"], "boards": job["boards"],
                        "rows": [{"label": labels.get(row["hash"], row["hash"][:12]),
                                  "score": row["objective"], "gain_from_seed": row["delta_to_seed"],
                                  "gain_from_previous": row.get("against_previous", {}).get("mean")}
                                 for row in job.get("curve", [])]} for job in growth]}


def render_report(summary, name):
    lines = ["# " + name, "", "## 结论先读", "", summary["conclusion"], "", summary["stability"], "",
             summary["reading_guide"], "", "## 体系是什么", "", summary["objective"],
             f"{summary['current_title']}：{', '.join(summary['current']) or '无'}；最新发布：{summary['latest'] or '无'}。", "",
             "## baseline失误如何计分", "", summary["baseline_fault_explanation"], ""]
    if summary.get("research_units"):
        lines.extend(["## 研究负责人、执行者与实验闭环", ""])
        for unit in summary["research_units"]:
            lines.extend([f"### {unit['id']}：{unit['status']}", "", str(unit.get("plan", "尚未提交计划"))])
            for job in unit["jobs"]:
                lines.extend([f"- 假设：{job['idea']['title']}", f"  机制：{job['idea']['mechanism']}",
                              f"  证伪标准：{job['idea']['falsification']}", f"  执行状态：{job['status']}",
                              f"  实际汇报：{job.get('report', {}).get('summary', '尚无结果')}",
                              f"  负责人结论：{job.get('review', {}).get('learning', '尚未反馈')}"])
                if job.get("report", {}).get("actual_trial"):
                    trial = job["report"]["actual_trial"]
                    coverage = trial.get("effect_coverage", {})
                    lines.append(f"  机制试验：{trial['boards']}副；变化牌族{coverage.get('changed_families', '未记录')}，"
                                 f"目标{coverage.get('required', '未记录')}；"
                                 f"{'条件样本不等于总体强度。' if trial.get('evaluation_scope') == 'conditional' else '探索结果不是晋升证据。'}")
    if summary.get("research_budget_mode") == "campaign":
        lines.extend(["", "## 挑战队列与可继续研究", "",
                      "任务不再均分回合／时间；候选挑战执行时的当前冠军，早先胜者可被后续候选替换。",
                      "未晋升候选及未完成草稿保留；融合后必须重新测试、冻结和共同新牌评价。"])
        for job in summary["challenge_queue"]:
            score = f"；{job['boards']}副，{job['statistics']['mean']:+.6f} IMP/副" if job.get("statistics") else ""
            lines.append(f"- {job['candidate'][:12]} 对 {job['parent'][:12]}：{job['status']}{score}")
        lines.append(f"继承研究分支{len(summary['retained_branches'])}个，旧分数不作为本轮结果。")
    if summary.get("maintenance_budget"):
        budget = summary["maintenance_budget"]
        lines.extend(["", "## 独立维护额度", "",
                      f"维护请求上限{budget['max_requests']}次，token上限{budget['max_tokens']}，时间上限{budget['max_seconds']}秒。",
                      "接口／格式／传输失败及纠正、baseline补全使用维护额度；费用与原始失败保留，不占研究额度。"])
    if summary.get("repair_jobs"):
        lines.extend(["", "## 待命修复与权限验收", "",
                      "修复仅产出隔离版本，不更换本轮基线或评分；框架源码变更需独立引擎修订。"])
        lines.extend(f"- {job['id']}：{job['status']}；{job.get('error', '')}" for job in summary["repair_jobs"])
    for version in summary["versions"]:
        lines.extend([f"## {version['label']}：{version['name']}", "", version["decision"],
                      f"{version['rules']}条规则，{version['states']}个公开阶段；兜底：{version['fallback']}。"])
        if version["author_intent"]:
            lines.extend(["", "### 作者设计意图（不是成绩结论）", "", version["author_intent"]])
        lines.extend(["", "### 可执行叫牌约定", "", version["opening_scope"]])
        lines.extend(f"- {row['text']}" for row in version["opening"])
        if not version["opening"]:
            lines.append("- 没有决策规则，所有决定使用上述兜底。")
        lines.extend(["", "### 相对亲本修改了什么", ""])
        changes = version.get("changes")
        if changes:
            lines.append("；".join(f"{key}{value}项" for key, value in changes["counts"].items()))
            for change in changes["items"]:
                lines.extend([f"- {change['operation']}{change['kind']} `{change['key']}`",
                              f"  - 原来：{change['before']}", f"  - 现在：{change['after']}"])
            if changes["shown"] < changes["total"]:
                lines.append(f"展示{changes['shown']}/{changes['total']}项；完整语义见该版本的同源体系说明。")
        else:
            lines.append("这是起始版本。")
        evidence = version["evidence"]
        lines.extend(["", "### 实际证据与裁决", "", version["decision"]])
        if evidence:
            if evidence["tests"]:
                lines.append(f"回归：{evidence['tests']['passed']}/{evidence['tests']['tests']}通过。")
            if evidence["gain"] is not None:
                lines.append(f"{evidence['kind']}：{evidence['boards']}副，相对亲本{evidence['gain']:+.6f} IMP/副。")
            for error in evidence["errors"]:
                lines.append(f"原始问题：第{error.get('board', '未知')}副，{error.get('message', '未记录说明')}。")
            contribution = evidence["score_contributions"]
            if contribution:
                lines.append(f"收益分解：正常计分贡献{contribution['played_contribution']:+.6f}，"
                             f"baseline弃权贡献{contribution['forfeit_contribution']:+.6f} IMP/副；"
                             f"候选/亲本遇对手弃权配对数{contribution['candidate_forfeit_pairs']}/{contribution['parent_forfeit_pairs']}。")
        lines.extend(["", f"[完整同源体系说明](systems/{version['hash']}.md)", ""])
    lines.extend(["## 同一批新牌的成长复核", "",
                  "每行同一牌块内才可直接比较；下降同样保留。", "",
                  "| 牌块 | 副数 | 版本 | 成绩 | 相对前版 |", "|---|---:|---|---:|---:|"])
    for block in summary["growth"]:
        for row in block["rows"]:
            delta = "—" if row["gain_from_previous"] is None else f"{row['gain_from_previous']:+.4f}"
            lines.append(f"| {block['id']} | {block['boards']} | {row['label']} | {row['score']:+.4f} | {delta} |")
    return "\n".join(lines) + "\n"
