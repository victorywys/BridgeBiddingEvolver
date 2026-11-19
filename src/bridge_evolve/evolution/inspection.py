"""Small status views and checked audit-compatible exports for evolution runs."""

import hashlib
import io
import json
import time
import zipfile
from itertools import islice
from pathlib import Path

from bridge_evolve.evaluation.data import canonical_hash
from bridge_evolve.evaluation.record_log import encode_records, iter_records
from bridge_evolve.evolution.controller import status
from bridge_evolve.evolution.feedback import bounded, short_text
from bridge_evolve.evolution.costs import resource_summary
from bridge_evolve.systems.navigation import at_pointer, escape_pointer


def research_view(value, session, pointer="", limit=12000):
    return bounded(value, limit=limit, reference={"session": session, "pointer": pointer})


def system_metadata(directory: Path, state: dict, identity: str) -> dict:
    known = state.get("system_metadata", {}).get(identity)
    if known is not None:
        return dict(known)
    name = f"systems/{identity}.json"
    contents = (directory / name).read_bytes()
    if hashlib.sha256(contents).hexdigest() != state["system_files"].get(name):
        raise ValueError("Current system metadata differs from its frozen identity.")
    raw = json.loads(contents)
    return {"hash": identity, "name": raw["name"], "rules": len(raw["rules"]), "features": len(raw["features"]),
            "states": len(raw["protocol"]["states"])}


def timeline(directory: Path, *, offset=None, limit=50) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Timeline page limit must be in [1,100].")
    events = list(status(directory).get("milestones", {}).values())
    if offset is None:
        offset = max(0, len(events) - limit)
    if type(offset) is not int or offset < 0:
        raise ValueError("Timeline offset must be nonnegative.")
    return {"events": [research_view(event, event.get("session"), "/milestones/" + escape_pointer(event["id"]), 8000)
                       for event in events[offset:offset + limit]],
            "offset": offset, "limit": limit, "total": len(events)}


def overview(directory: Path, *, details=False) -> dict:
    state = status(directory)
    config = json.loads((directory / "config.json").read_text())
    result = {key: state[key] for key in ("id", "name", "status", "created_at", "updated_at", "generation",
                                        "phase", "champions", "agent_requests", "evaluated_pairs", "goal")}
    result.update(profile=config["profile"], source=config["source"], max_generations=config["generations"],
                  model=config["model"], reasoning_effort=config.get("reasoning_effort"),
                  model_usage=state.get("model_usage"), spent_seconds=state["spent_seconds"],
                  research_seeds=state.get("research_seeds", {}),
                  comparison_issues=bounded(state.get("comparison_issues", {}), limit=12000,
                                            reference={"run": state["id"], "pointer": "/comparison_issues"}),
                  benchmark_revision=state.get("benchmark_revision"),
                  continuation=state.get("continuation"), continuation_error=state.get("continuation_error"),
                  budgets={key: config[key] for key in ("max_agent_requests", "max_pairs", "max_seconds", "max_agent_tokens",
                           "max_output_tokens", "max_research_turns", "max_tool_calls", "max_releases", "max_lab_boards",
                           "max_trial_boards", "max_context_bytes", "max_promotions", "maintenance_budget", "research_seconds") if key in config},
                  promotions=sum(value["status"] == "promoted" for generation in state["generations"].values()
                                 for value in generation["promotions"].values()), error=short_text(state["error"]) if state.get("error") else None,
                  report_available=(directory / "report.md").is_file(), cancel_requested=(directory / "cancel-requested").exists(),
                  baselines=[{"lineage": label, "name": baseline["name"], "hash": baseline["hash"]}
                             for label, baseline in zip(("A", "B"), config["baselines"], strict=True)])
    result["activity"] = bounded(state.get("activity"), limit=6000, reference={"run": state["id"], "pointer": "/activity"})
    queued = list(state.get("evaluation_queue", {}).values())
    result["execution_mode"] = state.get("execution_mode", "research")
    result["resources"] = resource_summary(state)
    result["module_contract"] = state.get("module_contract")
    result["development_sampling"] = config.get("development_sampling")
    result["selection_policy"] = config.get("selection_policy", "confirmed")
    result["protocol_goal"] = config.get("protocol_goal")
    result["workflow"] = workflow_view(state, config)
    result["growth_checks"] = [bounded({"generation": int(number), **job}, limit=12000,
                                      reference={"run": state["id"], "pointer": "/growth_checks/" + number})
                               for number, job in state.get("growth_checks", {}).items()]
    result["evaluation_queue_total"] = len(queued)
    result["evaluation_queue_unique_candidates"] = len({job["candidate"] for job in queued})
    result["evaluation_queue"] = [
        {key: bounded(job[key], limit=8000, reference={"run": state["id"], "pointer": "/state/evaluation_queue/" + job["id"] + "/" + key})
         for key in ("id", "candidate", "parent", "lineage", "status", "stage", "reserved_pairs", "remaining_pairs",
                     "reserved_seconds", "remaining_seconds", "spent_seconds", "outcome", "error", "tests", "test_session",
                     "module_contract", "protocol_goal", "round_resources", "research_resources_at_publication")
         if key in job} for job in queued[-50:]]
    result["active_generation"] = max((int(key) for key in state["generations"]), default=0)
    result["milestone_count"] = len(state.get("milestones", {}))
    result["latest_milestone"] = bounded(next(reversed(state.get("milestones", {}).values()), None), limit=8000,
                                        reference={"run": state["id"], "pointer": "/milestones"})
    current_round = state["rounds"].get(state["phase"])
    result["evaluation_progress"] = ({
        "id": current_round["id"], "status": current_round["status"], "boards": current_round["boards"],
        "completed_pairs": current_round.get("completed_pairs", 0),
        "total_pairs": len(current_round["systems"]) * len(config["baselines"]) * current_round["boards"],
    } if current_round is not None else None)
    if state.get("activity", {}).get("kind") == "model":
        request = next((row for row in reversed(state.get("model_requests", []))
                        if row["request"] == state["activity"]["request"]), None)
        if request is not None:
            result["active_request"] = {key: request[key] for key in ("request", "label", "model", "status", "started_at") if key in request}
    if details:
        from bridge_evolve.evolution.presentation import research_summary
        result["research_summary"] = research_summary(directory, state, config)
        result["generations"] = [{
            "number": int(number), "status": generation["status"], "parents": generation["parents"],
            "candidates": [{**{key: research_view(value[key], value.get("research_session"),
                              "/state/generations/" + escape_pointer(number) + "/candidates/" + escape_pointer(candidate_id) + "/" + escape_pointer(key), 6000)
                              for key in ("lineage", "hash", "status", "error", "delta", "full_delta", "errors", "error_count", "changes", "research_session", "tests", "release") if key in value},
                            "hypothesis": short_text(value["proposal_summary"].get("hypothesis", ""), 2000)}
                           for candidate_id, value in generation["candidates"].items()],
            "promotions": generation["promotions"], "analysis": generation.get("analysis", {}),
        } for number, generation in state["generations"].items()]
        result["research_sessions"] = [
            {**value, "working": research_view(value.get("working"), key, "", 16000),
             "status": "interrupted" if value["status"] == "running" and state["status"] not in ("queued", "running") else value["status"]}
            for key, value in state.get("research_sessions", {}).items()]
        result["milestones"] = [research_view(value, value.get("session"), "/milestones/" + escape_pointer(value["id"]), 8000)
                                for value in list(state.get("milestones", {}).values())[-60:]]
        result["current_systems"] = [
            {**system_metadata(directory, state, identity), "lineage": lineage,
             "initial_hash": state.get("initial_champions", state.get("origin_hashes", {})).get(lineage),
             "source": state.get("research_seeds", {}).get(lineage)}
            for lineage, identity in state["champions"].items()]
        result["rounds"] = []
        for identity, round in state["rounds"].items():
            row = {key: round[key] for key in ("id", "status", "boards", "completed_pairs", "systems") if key in round}
            row["total_pairs"] = len(round["systems"]) * len(config["baselines"]) * round["boards"]
            row["scope"] = "exploration" if identity.startswith("r-") else "goal" if identity.startswith("goal") else "confirmation" if "-promotion-" in identity else "development"
            if round.get("status") in ("checked", "blocked"):
                path = directory / "rounds" / identity / "summary.json"
                contents = path.read_bytes()
                if hashlib.sha256(contents).hexdigest() != round["summary_sha256"]:
                    raise ValueError("Evolution round summary hash mismatch.")
                summary = json.loads(contents)
                if "score_components" in summary:
                    row.update(score_components=summary["score_components"],
                               baseline_fault_policy=summary.get("baseline_fault_policy", "strict"))
                if "protocol_coverage" in summary:
                    row["protocol_coverage"] = summary["protocol_coverage"]
                if summary.get("evaluation_scope") == "conditional":
                    row.update(evaluation_scope="conditional", development_sampling=summary["development_sampling"])
                row["objective"] = summary["objective"]
                row["means"], row["candidate_statuses"] = summary["means"], summary["status"]
                row["pairs"] = [{"candidate": key.split("/")[0], "opponent": key.split("/")[1]} for key in summary["logs"]]
            result["rounds"].append(row)
        for system in result["current_systems"]:
            latest = next((row for row in reversed(result["rounds"]) if row["scope"] != "exploration"
                           and system["hash"] in row.get("objective", {})), None)
            if latest is not None:
                system["latest_result"] = {
                    "round": latest["id"], "boards": latest["boards"], "scope": latest["scope"],
                    "objective": latest["objective"][system["hash"]], "means": latest["means"][system["hash"]],
                }
            promotion = next((value for generation in reversed(list(state["generations"].values()))
                              for value in generation["promotions"].values()
                              if value["status"] == "promoted" and value["candidate"] == system["hash"]), None)
            if promotion is not None:
                system["promotion"] = promotion
    return result


def workflow_view(state, config):
    from bridge_evolve.evolution.manager import management_view
    units = []
    for unit in list(state.get("research_units", {}).values())[-20:]:
        row = {key: unit[key] for key in ("id", "status", "parent", "worker_turns", "finished_at", "error") if key in unit}
        row["plan"] = short_text(unit.get("plan", "尚未提交计划"), 2400)
        row["jobs"] = []
        for job in unit["jobs"]:
            item = {key: job[key] for key in ("id", "status", "session") if key in job}
            item["idea"] = {key: short_text(job["idea"][key], 1200) for key in
                            ("title", "hypothesis", "mechanism", "falsification")}
            if "report" in job:
                report = job["report"]
                tests = report.get("actual_tests")
                item["report"] = {"summary": short_text(report.get("summary") or report["worker_status"], 2400),
                    "worker_status": report["worker_status"],
                    "actual_tests": {key: tests[key] for key in ("tests", "passed", "failed") if key in tests} if tests else None,
                    "releases": [{"hash": release["hash"], "decision": release["decision"], "upgrade": release.get("upgrade"),
                        "development": {key: release["development"][key] for key in ("status", "full_delta", "score_contributions")
                                        if key in (release.get("development") or {})}}
                                 for release in report["releases"]]}
            if "review" in job:
                item["review"] = {"decision": job["review"]["decision"], "learning": short_text(job["review"]["learning"], 2400)}
            row["jobs"].append(item)
        units.append(row)
    repairs = []
    for job in list(state.get("repair_jobs", {}).values())[-50:]:
        row = {key: bounded(job[key], limit=5000, reference={"run": state["id"], "pointer": "/repair_jobs/" + job["id"] + "/" + key})
               for key in ("id", "kind", "status", "baseline", "occurrences", "error", "permissions",
                           "review", "candidate", "candidate_path", "deployment", "deferred_reason", "diagnosis", "repair_error", "blocked_deliveries",
                           "tool_calls", "iteration", "next_action", "blocked_by", "workspace", "engine_path", "validation_progress")
               if key in job}
        if "validation" in job:
            validation = job["validation"]
            row["validation"] = {key: validation[key] for key in (
                "status", "replayed_tables", "regression_boards", "regression_tables", "original_valid_tables",
                "changed_tables", "preserved_old_rules", "public_protocol_unchanged", "scope", "tests") if key in validation}
            for name in ("incident_failures", "dependency_failures", "new_failures", "pre_existing_failures"):
                if name in validation:
                    row["validation"][name + "_count"] = len(validation[name])
            if "pre_existing_failures" in validation:
                row["validation"]["remaining_pre_existing_failures_count"] = sum(
                    item["after_status"] == "failed" for item in validation["pre_existing_failures"])
            if "authored_tests" in validation:
                row["validation"]["authored_tests"] = {key: validation["authored_tests"][key] for key in ("tests", "passed", "failed")}
        repairs.append(row)
    return {"architecture": config.get("research_architecture", "legacy"), "units": units, "repairs": repairs,
            "recovery": state.get("recovery"), "recovery_blocked": state.get("recovery_blocked"),
            "provisional_proposals": list({value["id"]: {**value, "session": session_id}
                for session_id, session in state.get("research_sessions", {}).items()
                for value in (session.get("working") or {}).get("provisional_proposals", [])}.values())[-20:],
            "management": management_view(state, config),
            "hypotheses_per_unit": config.get("hypotheses_per_unit", 2),
            "budget_mode": config.get("research_budget_mode", "episode"),
            "maintenance_budget": config.get("maintenance_budget"),
            "maintenance_events": list(state.get("maintenance_events", {}).values())[-50:],
            "challenge_queue": list(state.get("challenge_queue", {}).values())[-100:],
            "portfolio": [{**{key: row[key] for key in ("hash", "parent", "lineage", "hypothesis", "status", "last_challenge",
                                                       "historical_provenance") if key in row},
                           "champion": row.get("hash") in state["champions"].values()}
                          for row in list(state.get("candidate_archive", {}).values())[-100:]],
            "branches": list(state.get("research_branches", {}).values())[-100:],
            "repair_attempts": config.get("repair_attempts", 0), "single_writer": True,
            "deployment_policy": "维护者查询完整依赖、修订旧规则或工程副本；原反例／回归／独立复核通过后自动续接新冻结实验，旧分数不迁移。"}


def repair_detail(directory: Path, identity: str):
    state = status(directory)
    job = state.get("repair_jobs", {}).get(identity)
    if job is None:
        raise ValueError("Unknown maintenance incident.")
    workspace = checked_artifact(directory, job["workspace"]) if job.get("workspace") else {}
    validation = workspace.get("repair_validation", job.get("validation"))
    return {"id": identity, "status": job["status"], "error": job["error"],
            "next_action": job.get("next_action"), "iteration": job.get("iteration", 0),
            "implementation_state": workspace.get("implementation_state"),
            "validation": validation, "review": job.get("review"),
            "evidence": [checked_artifact(directory, name) for name in job["evidence"]],
            "tests": workspace.get("tests", []),
            "operations": [{key: row[key] for key in ("name", "status", "arguments", "result", "started_at", "finished_at")
                            if key in row} for row in workspace.get("operations", {}).values()],
            "changes": workspace.get("changes"),
            "continuation": state.get("continuation"), "continuation_error": state.get("continuation_error")}


def checked_artifact(directory: Path, name: str):
    for attempt in range(4):
        state = status(directory)
        digest = state["artifacts"].get(name)
        path = (directory / name).resolve()
        if digest is None or not path.is_relative_to(directory.resolve()):
            raise ValueError("Unknown approved research artifact.")
        contents = path.read_bytes()
        if hashlib.sha256(contents).hexdigest() == digest:
            return json.loads(contents)
        if attempt < 3:
            time.sleep(.025)
    raise ValueError("Research artifact identity mismatch; retry after the current checkpoint.")


def solution_export(directory: Path, identity: str) -> bytes:
    record = status(directory).get("solution_deliveries", {}).get(identity)
    if record is None:
        raise ValueError("Unknown solution return.")
    reference = record["result"]
    value = checked_artifact(directory, reference["path"])
    if value.get("schema") != "bridge-solution-return/v1" or value.get("id") != identity:
        raise ValueError("The solution return does not match its approved identity.")
    contents = (directory / reference["path"]).read_bytes()
    if hashlib.sha256(contents).hexdigest() != reference["sha256"]:
        raise ValueError("The immutable solution return was modified.")
    return contents


def research_detail(directory: Path, identity: str, *, offset=None, limit=50, tool_call=None) -> dict:
    state = status(directory)
    metadata = state.get("research_sessions", {}).get(identity)
    if metadata is None:
        raise ValueError("Unknown research session.")
    session = checked_artifact(directory, metadata["path"])
    workspace = checked_artifact(directory, metadata["workspace"])
    if tool_call is not None:
        result = workspace["operations"].get(tool_call)
        if result is None:
            raise ValueError("Unknown research tool call.")
        return {"session": identity, "call_id": tool_call,
                **research_view(result, identity, "/operations/" + escape_pointer(tool_call), 24000)}
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Research page limit must be in [1,100].")
    total = len(session["events"])
    if offset is None:
        offset = max(0, total - limit)
    if type(offset) is not int or offset < 0:
        raise ValueError("Research offset must be nonnegative.")
    session_status = "interrupted" if session["status"] == "running" and state["status"] not in ("queued", "running") else session["status"]
    return {**metadata, "working": research_view(metadata.get("working"), identity, "", 16000),
            "status": session_status, "summary": short_text(workspace["summary"], 4000) if workspace.get("summary") else None,
            "continued_from": session.get("continued_from"),
            "implementation_state": workspace.get("implementation_state"),
            "provisional_proposals": [research_view(value, identity,
                "/provisional_proposals/" + escape_pointer(key), 6000)
                for key, value in list(workspace.get("provisional_proposals", {}).items())[-20:]],
            "prompt": session["messages"][0]["content"],
            "notes": [research_view(note, identity, f"/notes/{index}", 4000)
                      for index, note in list(enumerate(workspace["notes"]))[-20:]],
            "tests": research_view(workspace.get("last_tests"), identity, "/last_tests", 6000),
            "trial": research_view(workspace.get("last_trial"), identity, "/last_trial", 6000),
            "releases": [{key: value[key] for key in ("hash", "hypothesis", "tests", "path")} for value in workspace["releases"]],
            "events": [research_view(event, identity, f"/events/{index}", 6000)
                       for index, event in list(enumerate(session["events"]))[offset:offset + limit]],
            "full_results": [{"result_id": key, "bytes": item["bytes"]} for key, item in workspace.get("full_results", {}).items()],
            "offset": offset, "limit": limit, "total_events": total,
            "working_hash": canonical_hash(workspace["system"]),
            "working_rules": len(workspace["system"]["rules"]) if isinstance(workspace["system"].get("rules"), list) else None}


def research_evidence_export(directory: Path, identity: str, *, result_id=None, pointer="") -> bytes:
    state = status(directory)
    metadata = state.get("research_sessions", {}).get(identity)
    if metadata is None:
        job = next((job for job in state.get("evaluation_queue", {}).values() if job.get("test_session") == identity), None)
        if job is not None:
            metadata = {"workspace": job["test_workspace"]}
    if metadata is None:
        raise ValueError("Unknown research session.")
    workspace = checked_artifact(directory, metadata["workspace"])
    if result_id is not None:
        saved = workspace.get("full_results", {}).get(result_id)
        if saved is None:
            raise ValueError("Unknown immutable research result.")
        if status(directory)["artifacts"].get(saved["path"]) != saved["sha256"]:
            raise ValueError("Research result identity changed.")
        return json.dumps(checked_artifact(directory, saved["path"]), ensure_ascii=False).encode()
    if pointer.startswith("/management/checkpoints/"):
        identity_key = pointer.removeprefix("/management/checkpoints/")
        management = state.get("management", {})
        review = management.get("reviews", {}).get(identity_key) or management.get("checkpoints", {}).get(identity_key)
        if review is None or review["session"] != identity:
            raise ValueError("Unknown management checkpoint for this research session.")
        reference = review["checkpoint"]
        if state["artifacts"].get(reference["path"]) != reference["sha256"]:
            raise ValueError("Management checkpoint identity changed.")
        value = checked_artifact(directory, reference["path"])
    elif pointer.startswith("/state/"):
        value = at_pointer(state, pointer.removeprefix("/state"))
    elif pointer.startswith("/events/"):
        if "path" not in metadata:
            raise ValueError("Evaluation-only regressions do not have model session events.")
        value = at_pointer(checked_artifact(directory, metadata["path"]), pointer)
    elif pointer.startswith("/milestones/"):
        value = at_pointer(state, pointer)
    else:
        value = at_pointer(workspace, pointer)
    return json.dumps(value, ensure_ascii=False).encode()


def frozen_system_export(directory: Path, identity: str, extension: str) -> bytes:
    if extension not in ("json", "md"):
        raise ValueError("System export format must be json or md.")
    name = f"systems/{identity}.{extension}"
    state = status(directory)
    digest = state["system_files"].get(name)
    if digest is None:
        raise ValueError("Unknown frozen system.")
    path = (directory / name).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError("System export must remain inside this experiment.")
    contents = path.read_bytes()
    if hashlib.sha256(contents).hexdigest() != digest:
        raise ValueError("Frozen system export identity mismatch.")
    return contents


def module_contract_export(directory: Path) -> bytes:
    state = status(directory)
    entry = state.get("module_contract")
    if entry is None:
        raise ValueError("No module contract was frozen for this experiment.")
    name = entry["path"]
    contents = (directory / name).read_bytes()
    if hashlib.sha256(contents).hexdigest() != state["system_files"].get(name):
        raise ValueError("Frozen module contract identity mismatch.")
    return contents


def readable_report(directory: Path) -> bytes:
    from bridge_evolve.evolution.presentation import render_report, research_summary
    state = status(directory)
    config = json.loads((directory / "config.json").read_text())
    return render_report(research_summary(directory, state, config), state["name"]).encode("utf8")


def pair_export(directory: Path, round_id: str, candidate: str, opponent: str, *, offset=0, limit=1000) -> bytes:
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Evolution exports require a nonnegative offset and 1-1000 boards.")
    state = status(directory)
    round = state["rounds"].get(round_id)
    if round is None or round.get("status") not in ("checked", "blocked"):
        raise ValueError("This evolution round is not complete.")
    contents = (directory / "rounds" / round_id / "summary.json").read_bytes()
    if hashlib.sha256(contents).hexdigest() != round["summary_sha256"]:
        raise ValueError("Evolution round summary hash mismatch.")
    summary = json.loads(contents)
    name = summary["logs"].get(f"{candidate}/{opponent}")
    if name is None:
        raise ValueError("Unknown frozen candidate/opponent pair.")
    source = (directory / name).resolve()
    if not source.is_relative_to(directory.resolve()) or hashlib.sha256(source.read_bytes()).hexdigest() != summary["log_sha256"][name]:
        raise ValueError("Evolution pair log identity mismatch.")
    records = list(islice(iter_records(source), offset, offset + limit))
    if not records:
        raise ValueError("No boards in the selected export range.")
    data = {"boards.bridge.jsonl": encode_records(iter(records)),
            "input.json": json.dumps({"source_run": state["id"], "source_round": round_id, "source_boards": summary["boards"],
                                     "offset": offset, "limit": limit, "candidate": candidate, "opponent": opponent}).encode()}
    for identity in {candidate, opponent}:
        key = f"systems/{identity}.json"
        path = directory / key
        contents = path.read_bytes()
        if hashlib.sha256(contents).hexdigest() != state["system_files"].get(key):
            raise ValueError("Evolution frozen system identity mismatch.")
        data[key] = contents
    failures = [{"index": record["index"], "board_number": record["number"], "board_id": record["id"],
                 "errors": record["audit_result"]["errors"]} for record in records if record["audit_result"]["errors"]]
    report = {"schema": "bridge-batch-audit/v1", "id": f"{state['id']}:{round_id}", "label": f"{state['name']} · {round_id}",
              "status": "completed-with-findings" if any(record["audit_result"]["status"] != "checked" for record in records) else "checked", "completed_boards": len(records),
              "valid_boards": len(records) - len(failures), "failed_boards": len(failures), "board_failures": failures,
              "systems": {"A": candidate, "B": opponent}, "sampling": {"count": len(records), "seed": summary["seed"]},
              "scope": "evolution-pair-diagnostic; selected range, not the whole experiment fitness",
              "source_boards": summary["boards"], "source_range": [offset, offset + len(records)],
              "evidence_sha256": {key: hashlib.sha256(value).hexdigest() for key, value in data.items()}}
    if summary.get("evaluation_scope") == "conditional":
        report.update(evaluation_scope="conditional", development_sampling=summary["development_sampling"])
    data["audit-report.json"] = json.dumps(report, ensure_ascii=False).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in data.items():
            archive.writestr("evolution-pair/" + name, value)
    return output.getvalue()
