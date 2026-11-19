"""Explicit, versioned adjudication of an opponent's unsupported protocol branch."""

import copy
import logging

from bridge_evolve.core import Seat

LOGGER = logging.getLogger(__name__)
FORFEIT = "baseline-forfeit-24"
POLICIES = ("strict", FORFEIT)


def baseline_branch_fault(record, error, candidate, opponent):
    if (candidate == opponent or error.get("stage") != "auction"
            or error.get("type") != "SystemError" or error.get("system_hash") != opponent
            or not error.get("message", "").startswith("Unimplemented protocol branch:")
            or error.get("actor") not in ("N", "E", "S", "W")):
        return False
    table = next((table for table in record["tables"] if table.get("id") == error.get("table")), None)
    if table is None:
        return False
    side = Seat(error["actor"]).partnership.value
    other_side = "EW" if side == "NS" else "NS"
    return (table.get("systems", {}).get(side, {}).get("hash") == opponent
            and table.get("systems", {}).get(other_side, {}).get("hash") == candidate
            and table.get("error", {}).get("message") == error["message"])


def validate_adjudication(record):
    decision = record.get("adjudication")
    if (not isinstance(decision, dict) or decision.get("schema") != "bridge-baseline-adjudication/v1"
            or decision.get("policy") != FORFEIT or decision.get("awarded_imp") != 24
            or type(decision.get("awarded_imp")) is not int or type(record.get("imp")) is not int
            or record.get("imp") != 24 or record.get("point_difference") is not None
            or record.get("audit_result", {}).get("status") != "adjudicated"
            or record["audit_result"].get("errors") != []
            or record.get("dds", {}).get("oracle", {}).get("oracle_kind") != "dds"
            or len(record.get("tables", [])) != 2
            or not isinstance(decision.get("errors"), list) or not decision["errors"]):
        raise ValueError("Invalid baseline-forfeit adjudication; no DDS/table score may be fabricated.")
    candidate, opponent = decision.get("candidate"), decision.get("opponent")
    if not isinstance(candidate, str) or not isinstance(opponent, str) or candidate == opponent:
        raise ValueError("A baseline forfeit requires distinct, explicitly identified candidate and opponent.")
    if not all(baseline_branch_fault(record, error, candidate, opponent) for error in decision["errors"]):
        raise ValueError("An adjudicated error is not an attributable baseline unimplemented-branch fault.")
    table_errors = [table["error"] for table in record["tables"] if table.get("error")]
    if not table_errors or any(error not in decision["errors"] for error in table_errors):
        raise ValueError("The adjudication must preserve every original failed table's error.")
    return decision


def adjudicate_baseline(record, candidate, opponent, policy):
    if policy not in POLICIES:
        raise ValueError("Unknown frozen baseline fault policy.")
    errors = record.get("audit_result", {}).get("errors", [])
    if (policy == "strict" or not errors
            or record.get("dds", {}).get("oracle", {}).get("oracle_kind") != "dds"
            or not all(baseline_branch_fault(record, error, candidate, opponent) for error in errors)):
        return record
    result = copy.deepcopy(record)
    result["adjudication"] = {
        "schema": "bridge-baseline-adjudication/v1", "policy": FORFEIT,
        "candidate": candidate, "opponent": opponent, "awarded_imp": 24,
        "errors": copy.deepcopy(errors),
        "reason": "Baseline cannot implement its declared branch; the whole paired board is forfeited.",
        "scope": "A declared adjudication, not a played contract, DDS score, substituted Pass or dropped board.",
    }
    result["audit_result"].update(status="adjudicated", errors=[])
    result.update(imp=24, point_difference=None)
    validate_adjudication(result)
    LOGGER.warning("Baseline %s forfeited board %s: %s", opponent[:12], result.get("number"),
                   "; ".join(error["message"] for error in errors))
    return result


def score_contributions(result, candidate, parent):
    components = result.get("score_components")
    if components is None:
        return None
    count = result["boards"] * len(result["anchors"])
    candidate_faults = sum(row["forfeit_pairs"] for row in components[candidate].values())
    parent_faults = sum(row["forfeit_pairs"] for row in components[parent].values())
    normal = sum(components[candidate][anchor]["played_imp_total"] - components[parent][anchor]["played_imp_total"]
                 for anchor in result["anchors"]) / count
    forfeits = sum(components[candidate][anchor]["forfeit_imp_total"] - components[parent][anchor]["forfeit_imp_total"]
                   for anchor in result["anchors"]) / count
    return {"policy": result["baseline_fault_policy"], "candidate_forfeit_pairs": candidate_faults,
            "parent_forfeit_pairs": parent_faults, "played_contribution": normal, "forfeit_contribution": forfeits,
            "total_delta": normal + forfeits,
            "scope": "Additive contributions over the full shared sample, not a filtered error-free subset."}


def archive_legacy_baseline_issues(state, config, issues, *, source_run, previous_policy="strict"):
    """Keep old opponent branch failures as evidence, not perpetual new-run blockers."""
    active, historical = {}, {}
    anchors = {item["hash"] for item in config["baselines"]}
    for key, issue in issues.items():
        errors = issue.get("errors", [])
        eligible = (config.get("baseline_fault_policy", "strict") == FORFEIT and errors
                    and issue.get("error_count") == len(errors)
                    and all(error.get("stage") == "auction" and error.get("type") == "SystemError"
                            and error.get("system_hash") in anchors and error.get("actor") in ("N", "E", "S", "W")
                            and error.get("table") in ("table1", "table2")
                            and error.get("message", "").startswith("Unimplemented protocol branch:")
                            for error in errors))
        (historical if eligible else active)[key] = copy.deepcopy(issue)
    if historical:
        revision = state.setdefault("baseline_policy_revision", {
            "previous": previous_policy, "current": FORFEIT, "sources": [], "historical_comparison_issues": {}})
        if source_run not in revision["sources"]:
            revision["sources"].append(source_run)
        for key, issue in historical.items():
            saved_key = key
            if key in revision["historical_comparison_issues"] and revision["historical_comparison_issues"][key] != issue:
                saved_key = source_run + ":" + key
            revision["historical_comparison_issues"][saved_key] = issue
    return active


def recover_completed_reassessment(directory, *, reason):
    """Repair only a stale imported-blocker status, preserving every frozen score and its engine."""
    import fcntl
    import hashlib
    import json
    from bridge_evolve.evaluation.data import canonical_hash
    from bridge_evolve.evaluation.experiment import timestamp
    from bridge_evolve.evolution.config import engine_identity, save_json

    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
        raise ValueError("A metadata recovery requires an explicit bounded reason.")
    with (directory / "controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = (directory / "state.json").read_bytes()
        state = json.loads(before)
        config = json.loads((directory / "config.json").read_text())
        if canonical_hash(config) != state["config_hash"]:
            raise ValueError("Cannot recover metadata for a changed frozen configuration.")
        if state["status"] == "completed" and state.get("recoveries", []) and (
                state["recoveries"][-1].get("kind") == "baseline-policy-metadata"):
            return state["recoveries"][-1]
        jobs = list(state.get("evaluation_queue", {}).values())
        if (state["status"] != "blocked"
                or not (config.get("evaluation_only") or state.get("execution_mode") == "evaluation-only") or not jobs
                or not state.get("comparison_issues") or any("full_delta" not in job.get("outcome", {}) for job in jobs)):
            raise ValueError("Recovery only applies to a stopped, fully scored evaluation-only run with stale blockers.")
        if any(job["status"] not in ("development_complete", "no_full_gain", "insufficient_coverage")
               or job.get("rounds", {}).get("full") not in state["rounds"] for job in jobs):
            raise ValueError("Every recovered candidate needs a real completed full round, not only an outcome label.")
        summaries = {}
        for identity, saved in state["rounds"].items():
            path = directory / "rounds" / identity / "summary.json"
            contents = path.read_bytes()
            if hashlib.sha256(contents).hexdigest() != saved["summary_sha256"]:
                raise ValueError("A frozen round summary changed; no metadata recovery was made.")
            summary = json.loads(contents)
            summaries[identity] = summary
            if (summary.get("baseline_fault_policy") != FORFEIT
                    or any(value != "checked" for value in summary["status"].values())):
                raise ValueError("An actual unfinished or invalid round cannot be repaired into success.")
            for name, digest in summary["log_sha256"].items():
                if hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest:
                    raise ValueError("A frozen scored log changed; no metadata recovery was made.")
        for job in jobs:
            result = summaries[job["rounds"]["full"]]
            actual = result["objective"][job["candidate"]] - result["objective"][job["parent"]]
            if actual != job["outcome"]["full_delta"]:
                raise ValueError("The reported outcome differs from the frozen full-round scores.")
        active = archive_legacy_baseline_issues(state, config, state["comparison_issues"],
                                              source_run="pre-recovery-import")
        if active:
            raise ValueError("The historical blockers include errors outside the explicitly adjudicated baseline policy.")
        folder = directory / "recoveries"
        folder.mkdir(exist_ok=True)
        digest = hashlib.sha256(before).hexdigest()
        backup = folder / ("before-baseline-policy-" + digest[:20] + ".json")
        if backup.exists() and backup.read_bytes() != before:
            raise ValueError("The immutable pre-recovery state conflicts with an existing backup.")
        if not backup.exists():
            backup.write_bytes(before)
        recovery = {"kind": "baseline-policy-metadata", "at": timestamp(), "reason": reason,
                    "before_state": backup.relative_to(directory).as_posix(), "before_sha256": digest,
                    "scoring_engine_unchanged": state["engine_hash"], "recovery_engine": engine_identity(),
                    "scope": "Only imported blocker/status metadata repaired; no configuration, scores, rounds or model usage changed."}
        state["artifacts"][recovery["before_state"]] = digest
        state.setdefault("recoveries", []).append(recovery)
        state.pop("comparison_issues", None)
        state.pop("error", None)
        state.update(status="completed", updated_at=recovery["at"])
        state["activity"] = {"kind": "stopped", "status": "completed", "started_at": recovery["at"]}
        save_json(directory / "state.json", state)
        LOGGER.warning("Recovered completed reassessment metadata for %s; scoring engine and raw scores preserved.",
                       directory.name)
        return recovery
