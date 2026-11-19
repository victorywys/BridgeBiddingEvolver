"""Resource ledgers describe research throughput, never bidding fitness."""

from collections import Counter
from datetime import datetime


def elapsed(start, end):
    if not start or not end:
        return None
    return max(0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())


def receipt_usage(response):
    usage = response.get("usage", {})
    details = usage.get("input_tokens_details") if isinstance(usage, dict) else None
    if not isinstance(details, dict):
        details = {}
    result = {}
    for field in ("cached_tokens", "cache_write_tokens"):
        value = details.get(field)
        if type(value) is int and value >= 0:
            result[field] = value
    receipt = response.get("copilot_usage")
    billed = receipt.get("total_nano_aiu") if isinstance(receipt, dict) else None
    if type(billed) is int and billed >= 0:
        result["nano_ai_credits"] = billed
    elif receipt is not None:
        result["receipt_error"] = "Upstream credit receipt has no valid nonnegative integer amount; billing remains unknown."
    return result


def request_costs(rows):
    known = [row for row in rows if isinstance(row.get("usage"), dict)]
    unknown = [row for row in rows if not isinstance(row.get("usage"), dict)]
    credit_rows = [row for row in rows if type(row.get("nano_ai_credits")) is int]
    read_complete = all("cached_tokens" in row for row in known)
    write_complete = all("cache_write_tokens" in row for row in known)
    uncached = None
    if read_complete and write_complete:
        partitions = [row["usage"]["input_tokens"] - row["cached_tokens"] - row["cache_write_tokens"] for row in known]
        if any(value < 0 for value in partitions):
            raise ValueError("Cached and written token counts exceed reported input; resource accounting is invalid.")
        uncached = sum(partitions)
    return {
        "requests": len(rows), "received": sum(row["status"] == "received" for row in rows),
        "failed": sum(row["status"] in ("error", "model_mismatch") for row in rows),
        "input_tokens": sum(row["usage"]["input_tokens"] for row in known),
        "output_tokens": sum(row["usage"]["output_tokens"] for row in known),
        "unknown_usage_requests": len(unknown),
        "retained_token_reservations": sum(row.get("reserved_tokens", 0) for row in unknown),
        "cache_read_tokens": sum(row["cached_tokens"] for row in known) if read_complete else None,
        "cache_write_tokens": sum(row["cache_write_tokens"] for row in known) if write_complete else None,
        "uncached_input_tokens": uncached,
        "known_ai_credits": sum(row["nano_ai_credits"] for row in credit_rows) / 1e9,
        "ai_credits": sum(row["nano_ai_credits"] for row in credit_rows) / 1e9 if len(credit_rows) == len(rows) else None,
        "missing_credit_receipts": len(rows) - len(credit_rows),
        "credit_receipts": len(credit_rows),
        "invalid_credit_receipts": sum("receipt_error" in row for row in rows),
        "model_wait_seconds": sum(elapsed(row.get("started_at"), row.get("finished_at")) or 0 for row in rows),
        "untimed_or_inflight_requests": sum(not row.get("started_at") or not row.get("finished_at") for row in rows),
    }


def budget_usage(state, budget):
    rows = [row for row in state.get("model_requests", []) if row.get("budget", "research") == budget]
    return {"requests": len(rows),
            "charged_tokens": sum(sum(row["usage"].values()) if isinstance(row.get("usage"), dict)
                                  else row.get("reserved_tokens", 0) for row in rows)}


def move_request_to_maintenance(controller, request, reason):
    if not controller.config.get("maintenance_budget"):
        return
    row = next(row for row in controller.state["model_requests"] if row["request"] == request)
    if row.get("budget", "research") != "maintenance":
        row.update(budget="maintenance", maintenance_reason=reason)
        controller.state["maintenance_seconds"] = controller.state.get("maintenance_seconds", 0) + (
            elapsed(row.get("started_at"), row.get("finished_at")) or 0)
        controller.checkpoint()


def session_costs(state, identity):
    rows = [row for row in state.get("model_requests", []) if row.get("label", "").startswith(identity + ":")]
    return request_costs(rows)


def tool_costs(operations):
    rows = list(operations.values())
    return {"calls": len(rows), "counts": dict(Counter(row["name"] for row in rows)),
            "non_model_tool_seconds": sum(elapsed(row.get("started_at"), row.get("finished_at")) or 0
                                         for row in rows if row["name"] != "consult"),
            "consult_seconds_includes_model": sum(elapsed(row.get("started_at"), row.get("finished_at")) or 0
                                                 for row in rows if row["name"] == "consult"),
            "unfinished_calls": sum(row["status"] != "completed" for row in rows)}


def resource_summary(state):
    requests = request_costs(state.get("model_requests", []))
    jobs = list(state.get("evaluation_queue", {}).values())
    unique = {job["candidate"] for job in jobs}
    completed = {job["candidate"] for job in jobs if "outcome" in job}
    scored = {job["candidate"] for job in jobs if job["status"] in
              ("development_complete", "no_full_gain", "insufficient_coverage")}
    token_total = requests["input_tokens"] + requests["output_tokens"]
    return {
        "schema": "bridge-research-resources/v1", "models": requests,
        "budget_pools": {budget: {**budget_usage(state, budget),
                                **request_costs([row for row in state.get("model_requests", [])
                                                if row.get("budget", "research") == budget])}
                         for budget in ("research", "maintenance")},
        "maintenance_seconds": state.get("maintenance_seconds", 0),
        "maintenance_events": len(state.get("maintenance_events", {})),
        "roles": {role: request_costs([row for row in state.get("model_requests", [])
                   if ("manager" if ":manager:" in row.get("label", "") else
                       "repair" if row.get("label", "").startswith("repair:") else
                       "director" if ":director:" in row.get("label", "") else "implementation") == role])
                  for role in ("manager", "director", "implementation", "repair")},
        "unique_candidates": len(unique), "evaluated_candidates": len(completed), "scored_candidates": len(scored),
        "publication_occurrences": sum(len(job["occurrences"]) for job in jobs),
        "completed_candidate_fraction": len(completed) / len(unique) if unique else None,
        "tokens_per_evaluated_candidate": token_total / len(completed) if completed and not requests["unknown_usage_requests"] else None,
        "ai_credits_per_evaluated_candidate": requests["ai_credits"] / len(completed)
            if completed and requests["ai_credits"] is not None else None,
        "pairs_per_scored_candidate": state["evaluated_pairs"] / len(scored) if scored else None,
        "paired_evaluation_units": state["evaluated_pairs"],
        "active_seconds": state["spent_seconds"], "sessions": {
            identity: {"models": session_costs(state, identity), "wall_seconds": session["spent_seconds"],
                       "tools": session.get("working", {}).get("resource_usage"),
                       "published": session["releases"], "status": session["status"]}
            for identity, session in state.get("research_sessions", {}).items()},
        "scope": "Run/session resource diagnostics; shared research costs are not summed as independent per-release costs.",
    }
