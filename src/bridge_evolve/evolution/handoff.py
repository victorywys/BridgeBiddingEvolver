"""Explicit experiment epochs after maintenance; reuse work, never scores or spent budgets."""

import copy
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from bridge_evolve.evolution.config import CONFIG_FIELDS
from bridge_evolve.evolution.costs import budget_usage


class EngineRevisionReady(RuntimeError):
    pass


def remaining_config(source, *, mode=None):
    from bridge_evolve.evolution.controller import status
    state = status(source)
    if state["status"] in ("running", "queued"):
        raise ValueError("Stop the source controller before creating its maintenance continuation.")
    config = json.loads((source / "config.json").read_text())
    from bridge_evolve.evaluation.data import canonical_hash
    if canonical_hash(config) != state["config_hash"]:
        raise ValueError("Continuation source configuration changed.")
    raw = {key: copy.deepcopy(value) for key, value in config.items() if key in CONFIG_FIELDS}
    versions = state.get("repair_versions", {})
    research = budget_usage(state, "research")
    maintenance = budget_usage(state, "maintenance")
    raw.update(
        name=config["name"] + " · 维护续接",
        baselines=[versions[value["hash"]]["path"] if value["hash"] in versions else
                   str(source / "systems" / f"{value['hash']}.json") for value in config["baselines"]],
        research_seed_run=None, champion_seed_run=str(source), repair_seed_run=str(source),
        research_seed_parent="source", evaluation_sources=list(dict.fromkeys([*config["evaluation_sources"], str(source)])),
        max_agent_requests=config["max_agent_requests"] - research["requests"],
        max_agent_tokens=config["max_agent_tokens"] - research["charged_tokens"],
        max_pairs=config["max_pairs"] - state["evaluated_pairs"],
        max_seconds=int(config["max_seconds"] - state["spent_seconds"] + state.get("maintenance_seconds", 0)),
        evaluation_only=False,
        repair_continuation_mode=mode or config.get("repair_continuation_mode", "research"),
    )
    if any(value["hash"] in versions and versions[value["hash"]]["hash"] != value["hash"]
           for value in config["baselines"]):
        raw["benchmark_revision_reason"] = "继承维护者已验证的基线修订；新引擎重新评测，旧成绩不迁移。"
    if (raw["max_agent_requests"] < 1 or raw["max_agent_tokens"] < 1000 or raw["max_pairs"] < 1
            or raw["max_seconds"] < 1):
        raise ValueError("原研究剩余请求／token／牌例／时间不足以续接；旧账不重置，需明确追加资源。")
    raw["evaluation_seconds"] = min(config["evaluation_seconds"], raw["max_seconds"])
    raw["growth_seconds"] = min(config["growth_seconds"], raw["max_seconds"])
    if config.get("maintenance_budget"):
        budget = config["maintenance_budget"]
        remaining = {"max_requests": budget["max_requests"] - maintenance["requests"],
                     "max_tokens": budget["max_tokens"] - maintenance["charged_tokens"],
                     "max_seconds": int(budget["max_seconds"] - state.get("maintenance_seconds", 0))}
        if remaining["max_requests"] < 1 or remaining["max_tokens"] < 1000 or remaining["max_seconds"] < 1:
            raise ValueError("独立维护额度已用完；不借用研究池，需明确追加维护资源后续接。")
        raw["maintenance_budget"] = remaining
    if config.get("module_contract"):
        raw["module_contract"] = config["module_contract"]["path"]
    return raw


def initialize_maintenance(source, *, output=None, mode=None):
    from bridge_evolve.evolution.controller import initialize
    source = source.resolve()
    raw = remaining_config(source, mode=mode)
    raw.update(research_architecture="director", research_budget_mode="campaign",
               repair_attempts=max(1, raw["repair_attempts"]), auto_continue_repairs=True)
    return initialize(raw, output, Path(json.loads((source / "config.json").read_text())["project"]))


def prepare(controller):
    from bridge_evolve.evolution.controller import initialize
    if controller.state.get("continuation"):
        return controller.state["continuation"]
    versions = controller.state.get("repair_versions", {})
    engine = controller.state.get("repair_engine")
    incident = controller.state.get("recovery_incident")
    if controller.state["status"] == "failed":
        job = controller.state.get("repair_jobs", {}).get(incident, {})
        if (not incident or job.get("status") != "accepted_engine_revision" or not engine
                or incident not in engine.get("covers", [engine["repair"]])):
            return None
    if engine and engine["hash"] == controller.state["engine_hash"]:
        engine = None
    if not versions and not engine:
        return None
    incident = incident or (engine["repair"] if engine else None)
    fingerprint = controller.state.get("repair_jobs", {}).get(incident, {}).get("fingerprint", incident)
    if engine and sum(row.get("fingerprint", row.get("incident")) == fingerprint
                      for row in controller.state.get("recovery_history", [])) >= 3:
        controller.state["recovery_blocked"] = {
            "incident": incident, "reason": "The same framework incident exhausted three verified-engine recovery attempts."}
        controller.checkpoint()
        return None
    raw = remaining_config(controller.directory)
    raw["repair_seed_run"] = str(controller.directory)
    current = controller.config["baselines"]
    changed = any(item["hash"] in versions and versions[item["hash"]]["hash"] != item["hash"] for item in current)
    if not changed and not engine:
        return None
    raw.update(name=controller.config["name"] + " · 修复后新评测",
        benchmark_revision_reason="维护者修复原始工程故障并通过原反例、差分回归及独立复核；旧成绩不迁移。",
        evaluation_only=controller.config.get("repair_continuation_mode") == "evaluation")
    output = controller.directory.parent / ("evo-" + uuid.uuid4().hex)
    handoff = {"id": output.name, "path": str(output), "source_run": controller.state["id"],
               "status": "ready", "mode": "evaluation" if raw["evaluation_only"] else "research",
               "resource_policy": "仅继承各自资源池的剩余额度；不重置原账，不复用旧分数。",
               "baseline_revisions": versions, "engine_revision": engine}
    if engine:
        handoff["recovery"] = {"source_run": controller.state["id"], "incident": incident or engine["repair"],
                              "fingerprint": fingerprint,
                              "expected_engine": engine["hash"], "status": "initialized"}
    if engine:
        plan = controller.directory / "repair-handoff.json"
        controller.artifact(plan, {"config": raw, "output": str(output), "project": controller.config["project"]})
        handoff["plan"] = str(plan)
    else:
        initialize(raw, output, Path(controller.config["project"]))
    controller.state.update(continuation=handoff, prior_status=controller.state["status"], status="continued")
    controller.checkpoint()
    return handoff


def run_engine_handoff(handoff):
    from bridge_evolve.evolution.config import engine_identity
    root = Path(handoff["engine_revision"]["path"])
    if engine_identity(root) != handoff["engine_revision"]["hash"]:
        raise ValueError("已验收的工程副本发生变化，必须重新验证，不能伪装成原修订启动。")
    from bridge_evolve.evolution.runtime import validate_runtime
    validate_runtime(root)
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root / "src")
    result = subprocess.run([sys.executable, "-B", "-m", "bridge_evolve.evolution.handoff", handoff["plan"]],
                            cwd=root, env=environment, check=False)
    if result.returncode:
        raise RuntimeError(f"独立工程修订的续接进程失败，退出码 {result.returncode}；原证据保留。")
    from bridge_evolve.evolution.controller import status
    value = status(Path(handoff["path"]))
    if value["engine_hash"] != handoff["engine_revision"]["hash"]:
        raise ValueError("The continuation did not start the accepted engine revision.")
    return value


if __name__ == "__main__":
    from bridge_evolve.evolution.controller import initialize, run
    plan = json.loads(Path(sys.argv[1]).read_text())
    directory = initialize(plan["config"], Path(plan["output"]), Path(plan["project"]))
    result = run(directory)
    print(json.dumps({"id": result["id"], "status": result["status"]}, ensure_ascii=False))
