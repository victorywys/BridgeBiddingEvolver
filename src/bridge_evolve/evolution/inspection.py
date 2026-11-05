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
    return {"events": events[offset:offset + limit], "offset": offset, "limit": limit, "total": len(events)}


def overview(directory: Path, *, details=False) -> dict:
    state = status(directory)
    config = json.loads((directory / "config.json").read_text())
    result = {key: state[key] for key in ("id", "name", "status", "created_at", "updated_at", "generation",
                                        "phase", "champions", "agent_requests", "evaluated_pairs", "goal")}
    result.update(profile=config["profile"], source=config["source"], max_generations=config["generations"],
                  model=config["model"], reasoning_effort=config.get("reasoning_effort"),
                  model_usage=state.get("model_usage"), spent_seconds=state["spent_seconds"],
                  research_seeds=state.get("research_seeds", {}),
                  comparison_issues=state.get("comparison_issues", {}),
                  benchmark_revision=state.get("benchmark_revision"),
                  budgets={key: config[key] for key in ("max_agent_requests", "max_pairs", "max_seconds", "max_agent_tokens",
                           "max_output_tokens", "max_research_turns", "max_tool_calls", "max_releases", "max_lab_boards", "research_seconds") if key in config},
                  promotions=sum(value["status"] == "promoted" for generation in state["generations"].values()
                                 for value in generation["promotions"].values()), error=state.get("error"),
                  report_available=(directory / "report.md").is_file(), cancel_requested=(directory / "cancel-requested").exists(),
                  baselines=[{"lineage": label, "name": baseline["name"], "hash": baseline["hash"]}
                             for label, baseline in zip(("A", "B"), config["baselines"], strict=True)])
    result["activity"] = state.get("activity")
    result["active_generation"] = max((int(key) for key in state["generations"]), default=0)
    result["milestone_count"] = len(state.get("milestones", {}))
    result["latest_milestone"] = next(reversed(state.get("milestones", {}).values()), None)
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
        result["generations"] = [{
            "number": int(number), "status": generation["status"], "parents": generation["parents"],
            "candidates": [{**{key: value[key] for key in ("lineage", "hash", "status", "error", "delta", "full_delta", "errors", "error_count", "changes", "research_session", "tests", "release") if key in value},
                            "hypothesis": value["proposal_summary"].get("hypothesis", "")}
                           for value in generation["candidates"].values()],
            "promotions": generation["promotions"], "analysis": generation.get("analysis", {}),
        } for number, generation in state["generations"].items()]
        result["research_sessions"] = [
            {**value, "status": "interrupted" if value["status"] == "running" and state["status"] not in ("queued", "running") else value["status"]}
            for value in state.get("research_sessions", {}).values()]
        result["milestones"] = list(state.get("milestones", {}).values())[-60:]
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
        return {"session": identity, "call_id": tool_call, **result}
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Research page limit must be in [1,100].")
    total = len(session["events"])
    if offset is None:
        offset = max(0, total - limit)
    if type(offset) is not int or offset < 0:
        raise ValueError("Research offset must be nonnegative.")
    session_status = "interrupted" if session["status"] == "running" and state["status"] not in ("queued", "running") else session["status"]
    return {**metadata, "status": session_status, "summary": workspace.get("summary"), "continued_from": session.get("continued_from"),
            "prompt": session["messages"][0]["content"], "notes": workspace["notes"][-20:],
            "tests": workspace.get("last_tests"), "trial": workspace.get("last_trial"),
            "releases": [{key: value[key] for key in ("hash", "hypothesis", "tests", "path")} for value in workspace["releases"]],
            "events": session["events"][offset:offset + limit], "offset": offset, "limit": limit, "total_events": total,
            "working_hash": canonical_hash(workspace["system"]),
            "working_rules": len(workspace["system"]["rules"]) if isinstance(workspace["system"].get("rules"), list) else None}


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
    data["audit-report.json"] = json.dumps(report, ensure_ascii=False).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in data.items():
            archive.writestr("evolution-pair/" + name, value)
    return output.getvalue()
