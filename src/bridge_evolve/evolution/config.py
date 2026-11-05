from __future__ import annotations

import hashlib
import json
from pathlib import Path

from bridge_evolve.evaluation.data import canonical_hash
from bridge_evolve.evaluation.experiment import source_snapshot
from bridge_evolve.systems import load_system, validate_system

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MODEL = "gpt-6-astra"
PROFILES = {
    "smoke": {"generations": 3, "diagnosis": 16, "screen": 16, "full": 32, "promotion": 64, "resamples": 200},
    "pilot": {"generations": 3, "diagnosis": 128, "screen": 200, "full": 1000, "promotion": 2000, "resamples": 2000},
    "research": {"generations": 20, "diagnosis": 2000, "screen": 1000, "full": 5000, "promotion": 5000, "resamples": 20000},
}


def normalize_config(raw: dict, project: Path = ROOT, *, preloaded: dict | None = None) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Evolution configuration must be an object.")
    allowed = {"name", "profile", "baselines", "source", "model", "generations", "workers", "boards", "resamples",
               "modules", "max_agent_requests", "max_pairs", "max_seconds", "max_promotions",
               "min_promotion_delta", "max_opponent_regression", "promotion_alpha", "goal_alpha", "goal_delta",
               "automatic_goal_check", "random_seed", "replay", "agent_timeout", "reasoning_effort",
               "max_output_tokens", "max_agent_tokens", "max_research_turns", "max_tool_calls",
               "max_releases", "max_lab_boards", "research_seconds", "max_context_bytes", "origins", "research_task",
               "plateau_limit", "research_seed_run", "champion_seed_run", "benchmark_revision_reason",
               "model_retries", "model_retry_delay"}
    if set(raw) - allowed:
        raise ValueError(f"Unknown evolution configuration fields: {sorted(set(raw) - allowed)}")
    profile = raw.get("profile", "pilot")
    if not isinstance(profile, str) or profile not in PROFILES:
        raise ValueError("Unknown evolution profile.")
    defaults = PROFILES[profile]

    def integer(value, name, low, high):
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name} must be an integer in [{low},{high}].")
        return value

    def number(value, name, low, high):
        if type(value) not in (int, float) or not low <= value <= high:
            raise ValueError(f"{name} must be a number in [{low},{high}].")
        return value

    paths = raw.get("baselines", ["systems/classics-v5.5.4-compact/ccba.json", "systems/classics-v5.5.4-compact/prec.json"])
    if not isinstance(paths, list) or len(paths) != 2 or any(not isinstance(value, str) for value in paths):
        raise ValueError("Exactly two baseline system paths are required.")
    paths = [(project / value).resolve() for value in paths]
    systems = [preloaded[path] if preloaded is not None and path in preloaded else load_system(path) for path in paths]
    if any(system.version != 2 or system.scope != "general" for system in systems):
        raise ValueError("Evolution requires two general v2 systems.")
    if systems[0].content_hash == systems[1].content_hash:
        raise ValueError("The two benchmark identities must be distinct.")
    source = raw.get("source", "research")
    if source not in ("research", "agent", "numbers", "replay"):
        raise ValueError("Proposal source must be research, agent, numbers or replay.")
    boards = {key: defaults[key] for key in ("diagnosis", "screen", "full", "promotion")}
    supplied = raw.get("boards", {})
    if not isinstance(supplied, dict) or set(supplied) - {*boards, "validity"}:
        raise ValueError("Unknown evolution board stage.")
    boards.update(supplied)
    boards = {key: integer(value, key, 1, 10000) for key, value in boards.items()}
    boards.setdefault("validity", min(8 if profile == "smoke" else 128, boards["screen"]))
    seed = raw.get("random_seed")
    if seed is not None:
        integer(seed, "random_seed", 0, 2**53 - 1)
    modules = raw.get("modules", ["*"] if source == "research" else ["quantitative", "invite", "nt.", "slam_controls"])
    if not isinstance(modules, list) or not modules or any(not isinstance(value, str) or not value for value in modules):
        raise ValueError("Allowed rule/module selectors must be nonempty strings.")
    model = raw.get("model", DEFAULT_MODEL)
    name = raw.get("name", f"E002 {profile}")
    if not isinstance(model, str) or not model or not isinstance(name, str) or not name.strip():
        raise ValueError("Evolution name and model must be nonempty strings.")
    automatic = raw.get("automatic_goal_check", profile == "research")
    if type(automatic) is not bool:
        raise ValueError("automatic_goal_check must be boolean.")
    replay = raw.get("replay")
    if source == "replay" and (not isinstance(replay, str) or not replay):
        raise ValueError("Replay source requires a proposal JSON path.")
    origins = raw.get("origins", ["existing", "blank"] if source == "research" else ["existing"])
    if (not isinstance(origins, list) or not origins or any(value not in ("existing", "blank") for value in origins)
            or len(set(origins)) != len(origins) or source != "research" and origins != ["existing"]):
        raise ValueError("Research origins must be a distinct selection of existing and/or blank.")
    effort = raw.get("reasoning_effort", "high")
    if effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ValueError("Unknown reasoning effort.")
    task = raw.get("research_task", "Design a stronger complete partnership bidding protocol; structural redesign and original systems are welcome.")
    if not isinstance(task, str) or not task.strip() or len(task) > 16000:
        raise ValueError("research_task must contain 1-16000 characters.")
    revision = raw.get("benchmark_revision_reason")
    if revision is not None and (not isinstance(revision, str) or not revision.strip() or len(revision) > 4000):
        raise ValueError("benchmark_revision_reason must contain 1-4000 characters.")
    seed_runs = {}
    if raw.get("research_seed_run") is not None and raw.get("champion_seed_run") is not None:
        raise ValueError("Choose either an unpublished workspace or a frozen champion continuation.")
    for field in ("research_seed_run", "champion_seed_run"):
        seed_run = raw.get(field)
        if seed_run is not None:
            if source != "research" or not isinstance(seed_run, str) or not seed_run.strip():
                raise ValueError(f"{field} is only available to autonomous research.")
            candidate = project / ".runtime/evolution" / seed_run if seed_run.startswith("evo-") and "/" not in seed_run else project / seed_run
            candidate = candidate.resolve()
            if not candidate.is_relative_to(project.resolve()) or not (candidate / "state.json").is_file():
                raise ValueError(f"{field} must name an existing project-local evolution run.")
            seed_run = str(candidate)
        seed_runs[field] = seed_run
    return {
        "schema": "bridge-evolution-config/v1", "name": name.strip(), "profile": profile, "project": str(project.resolve()),
        "baselines": [{"path": str(path), "hash": system.content_hash, "name": system.name} for path, system in zip(paths, systems, strict=True)],
        "source": source, "model": model, "modules": modules, "boards": boards,
        "generations": integer(raw.get("generations", defaults["generations"]), "generations", 1, 100),
        "workers": integer(raw.get("workers", 2), "workers", 1, 4),
        "resamples": integer(raw.get("resamples", defaults["resamples"]), "resamples", 100, 50000),
        "max_agent_requests": integer(raw.get("max_agent_requests", 576 if source == "research" else 40), "max_agent_requests", 1, 10000),
        "max_pairs": integer(raw.get("max_pairs", 200000), "max_pairs", 1, 10000000),
        "max_seconds": integer(raw.get("max_seconds", 21600), "max_seconds", 1, 604800),
        "max_promotions": integer(raw.get("max_promotions", 10), "max_promotions", 1, 100),
        "min_promotion_delta": number(raw.get("min_promotion_delta", .05), "min_promotion_delta", 0, 24),
        "max_opponent_regression": number(raw.get("max_opponent_regression", .05), "max_opponent_regression", 0, 24),
        "promotion_alpha": number(raw.get("promotion_alpha", .05), "promotion_alpha", .001, .2),
        "goal_alpha": number(raw.get("goal_alpha", .05), "goal_alpha", .001, .2),
        "goal_delta": number(raw.get("goal_delta", .2), "goal_delta", 0, 24),
        "goal_boards": 10000, "max_goal_checks": 1, "automatic_goal_check": automatic,
        "random_seed": seed, "agent_timeout": integer(raw.get("agent_timeout", 300), "agent_timeout", 10, 1800),
        "model_retries": integer(raw.get("model_retries", 2), "model_retries", 0, 5),
        "model_retry_delay": integer(raw.get("model_retry_delay", 5), "model_retry_delay", 0, 300),
        "benchmark_revision_reason": revision.strip() if revision is not None else None,
        "reasoning_effort": effort, "origins": origins, "research_task": task.strip(),
        "max_output_tokens": integer(raw.get("max_output_tokens", 32768), "max_output_tokens", 256, 128000),
        "max_agent_tokens": integer(raw.get("max_agent_tokens", 12000000), "max_agent_tokens", 1000, 1000000000),
        "max_research_turns": integer(raw.get("max_research_turns", 16 if profile == "smoke" else 48), "max_research_turns", 1, 500),
        "max_tool_calls": integer(raw.get("max_tool_calls", 256), "max_tool_calls", 1, 5000),
        "max_releases": integer(raw.get("max_releases", 4), "max_releases", 1, 100),
        "max_lab_boards": integer(raw.get("max_lab_boards", 256), "max_lab_boards", 1, 10000),
        "research_seconds": integer(raw.get("research_seconds", 1800), "research_seconds", 10, 86400),
        "max_context_bytes": integer(raw.get("max_context_bytes", 1500000), "max_context_bytes", 16000, 10000000),
        "plateau_limit": integer(raw.get("plateau_limit", 0 if source == "research" else 3), "plateau_limit", 0, 100),
        **seed_runs,
        "replay": str((project / replay).resolve()) if replay is not None else None,
    }


def blank_system():
    return validate_system({
        "schema_version": 2, "operator_version": 2, "name": "Blank-origin research scaffold", "scope": "general",
        "features": {}, "announcements": [], "rules": [], "fallback": {"kind": "call", "call": "P"},
        "protocol": {"initial": "idle", "parameters": {}, "states": {"idle": {"transitions": [], "otherwise": "idle"}}},
    })


def engine_identity() -> str:
    tools = {name: hashlib.sha256((ROOT / "tools" / name).read_bytes()).hexdigest()
             for name in ("board_viewer.py", "verify_classics.py")}
    return canonical_hash({"runtime": source_snapshot(), "tools": tools})


def save_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf8")
    temporary.replace(path)
