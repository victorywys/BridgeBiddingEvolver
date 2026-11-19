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
CONFIG_FIELDS = {
    "name", "profile", "baselines", "source", "model", "generations", "workers", "boards", "resamples",
    "modules", "max_agent_requests", "max_pairs", "max_seconds", "max_promotions",
    "min_promotion_delta", "max_opponent_regression", "promotion_alpha", "goal_alpha", "goal_delta",
    "automatic_goal_check", "random_seed", "replay", "agent_timeout", "reasoning_effort",
    "max_output_tokens", "max_agent_tokens", "max_research_turns", "max_tool_calls",
    "max_releases", "max_lab_boards", "research_seconds", "max_context_bytes", "origins", "research_task",
    "plateau_limit", "research_seed_run", "champion_seed_run", "benchmark_revision_reason",
    "model_retries", "model_retry_delay", "evaluation_sources", "evaluation_seconds", "evaluation_only",
    "module_contract", "allow_promotion", "development_sampling", "research_seed_parent",
    "protocol_goal", "selection_policy", "growth_boards", "growth_seconds", "baseline_fault_policy",
    "research_architecture", "hypotheses_per_unit", "repair_attempts",
    "research_budget_mode", "max_trial_boards", "maintenance_budget", "repair_seed_run",
    "auto_continue_repairs", "repair_continuation_mode", "campaign_manager",
}


def normalize_config(raw: dict, project: Path = ROOT, *, preloaded: dict | None = None) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Evolution configuration must be an object.")
    if set(raw) - CONFIG_FIELDS:
        raise ValueError(f"Unknown evolution configuration fields: {sorted(set(raw) - CONFIG_FIELDS)}")
    profile = raw.get("profile", "pilot")
    if not isinstance(profile, str) or profile not in PROFILES:
        raise ValueError("Unknown evolution profile.")
    defaults = PROFILES[profile]
    from bridge_evolve.evolution.faults import FORFEIT, POLICIES
    fault_policy = raw.get("baseline_fault_policy", FORFEIT)
    if fault_policy not in POLICIES:
        raise ValueError(f"baseline_fault_policy must be one of {POLICIES}.")

    def integer(value, name, low, high):
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name} must be an integer in [{low},{high}].")
        return value

    def number(value, name, low, high):
        if type(value) not in (int, float) or not low <= value <= high:
            raise ValueError(f"{name} must be a number in [{low},{high}].")
        return value

    paths = raw.get("baselines", ["systems/classics-v5.5.5-compact/ccba.json", "systems/classics-v5.5.5-compact/prec.json"])
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
    architecture = raw.get("research_architecture", "legacy")
    if architecture not in ("legacy", "director") or architecture == "director" and source != "research":
        raise ValueError("research_architecture must be legacy, or director with research source.")
    budget_mode = raw.get("research_budget_mode", "episode")
    if budget_mode not in ("episode", "campaign") or budget_mode == "campaign" and architecture != "director":
        raise ValueError("Campaign budgets require director research.")
    manager = raw.get("campaign_manager")
    if manager is not None:
        if (budget_mode != "campaign" or not isinstance(manager, dict)
                or set(manager) - {"turns", "seconds", "max_missed_deliveries", "solution_return"}
                or not {"turns", "seconds", "max_missed_deliveries"} <= set(manager)):
            raise ValueError("campaign_manager requires campaign budgets and turns, seconds, max_missed_deliveries.")
        solution_return = manager.get("solution_return")
        if "solution_return" in manager and type(solution_return) is not bool:
            raise ValueError("campaign_manager.solution_return must be boolean.")
        manager = {
            "turns": integer(manager["turns"], "manager turns", 1, 256),
            "seconds": integer(manager["seconds"], "manager seconds", 1, 14400),
            "max_missed_deliveries": integer(manager["max_missed_deliveries"], "manager missed deliveries", 1, 10),
        }
        if solution_return is not None:
            manager["solution_return"] = solution_return
    maintenance = raw.get("maintenance_budget")
    if maintenance is not None:
        if not isinstance(maintenance, dict) or set(maintenance) != {"max_requests", "max_tokens", "max_seconds"}:
            raise ValueError("maintenance_budget requires max_requests, max_tokens and max_seconds.")
        maintenance = {
            "max_requests": integer(maintenance["max_requests"], "maintenance max_requests", 1, 10000),
            "max_tokens": integer(maintenance["max_tokens"], "maintenance max_tokens", 1000, 1000000000),
            "max_seconds": integer(maintenance["max_seconds"], "maintenance max_seconds", 1, 604800),
        }
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
    if (not isinstance(origins, list) or not origins or any(value not in ("existing", "blank", "module") for value in origins)
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
    seed_parent = raw.get("research_seed_parent", "source")
    if seed_parent not in ("source", "origin"):
        raise ValueError("research_seed_parent must be source or origin.")
    if seed_parent == "origin" and raw.get("research_seed_run") is None:
        raise ValueError("Retaining an origin parent requires research_seed_run.")
    if raw.get("research_seed_run") is not None and raw.get("champion_seed_run") is not None:
        raise ValueError("Choose either an unpublished workspace or a frozen champion continuation.")
    for field in ("research_seed_run", "champion_seed_run", "repair_seed_run"):
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
    evaluation_sources = raw.get("evaluation_sources", [])
    if (not isinstance(evaluation_sources, list) or any(not isinstance(value, str) or not value.strip() for value in evaluation_sources)
            or len(evaluation_sources) > 20):
        raise ValueError("evaluation_sources must contain at most 20 project-local run/archive paths.")
    resolved_sources = []
    for value in evaluation_sources:
        candidate = project / ".runtime/evolution" / value if value.startswith("evo-") and "/" not in value else project / value
        candidate = candidate.resolve()
        if not candidate.is_relative_to(project.resolve()) or not candidate.exists():
            raise ValueError("Evaluation input must be an existing project-local run or published archive.")
        resolved_sources.append(str(candidate))
    maximum_seconds = integer(raw.get("max_seconds", 21600), "max_seconds", 1, 604800)
    evaluation_only = raw.get("evaluation_only", False)
    if type(evaluation_only) is not bool:
        raise ValueError("evaluation_only must be boolean.")
    auto_repairs = raw.get("auto_continue_repairs", budget_mode == "campaign")
    if type(auto_repairs) is not bool:
        raise ValueError("auto_continue_repairs must be boolean.")
    repair_mode = raw.get("repair_continuation_mode", "research")
    if repair_mode not in ("research", "evaluation"):
        raise ValueError("repair_continuation_mode must be research or evaluation.")
    contract = None
    if raw.get("module_contract") is not None:
        from bridge_evolve.evolution.contracts import ModuleContract
        value = raw["module_contract"]
        if (not isinstance(value, str) or source != "research" or origins != ["module"]
                or seed_runs["research_seed_run"] and seed_parent != "origin"):
            raise ValueError("A module contract requires research source, origins=['module'], and its own frozen anchor.")
        path = (project / value).resolve()
        if not path.is_relative_to(project.resolve()):
            raise ValueError("Module contracts must be project-local.")
        data = json.loads(path.read_text(encoding="utf8"))
        anchor_name = data.get("anchor")
        if not isinstance(anchor_name, str) or Path(anchor_name).name != anchor_name:
            raise ValueError("Module anchor must be a sibling system definition.")
        anchor_path = (path.parent / anchor_name).resolve()
        if not anchor_path.is_relative_to(project.resolve()):
            raise ValueError("Module anchor must remain project-local.")
        anchor = load_system(anchor_path)
        ModuleContract(data, anchor)
        contract = {"path": str(path), "definition": data, "anchor_path": str(anchor_path)}
    elif "module" in origins:
        raise ValueError("Module research origins require module_contract.")
    promotion = raw.get("allow_promotion", True)
    if type(promotion) is not bool:
        raise ValueError("allow_promotion must be boolean.")
    selection_policy = raw.get("selection_policy", "confirmed")
    if selection_policy not in ("confirmed", "parent-improvement"):
        raise ValueError("selection_policy must be confirmed or parent-improvement.")
    if selection_policy == "parent-improvement" and (source != "research" or not promotion or automatic):
        raise ValueError("Parent-improvement development requires research, allow_promotion=true and automatic_goal_check=false.")
    goal = raw.get("protocol_goal")
    if goal is not None:
        from bridge_evolve.evolution.goals import protocol_goal
        protocol_goal(goal)
        if source != "research" or origins != ["blank"] or seed_runs["research_seed_run"] or contract is not None:
            raise ValueError("A protocol-goal experiment requires origins=['blank'], no draft seed or module contract; published-parent continuation is allowed after goal validation.")
    growth_boards = integer(raw.get("growth_boards", 0), "growth_boards", 0, 1000)
    if growth_boards and selection_policy != "parent-improvement":
        raise ValueError("Growth verification is reserved for explicitly labelled parent-improvement development.")
    development_sampling = None
    if raw.get("development_sampling") is not None:
        from bridge_evolve.evolution.sampling import normalize_sampling
        if source != "research" or promotion or automatic:
            raise ValueError("Conditional development requires research, allow_promotion=false and automatic_goal_check=false.")
        development_sampling = normalize_sampling(raw["development_sampling"])
    return {
        "schema": "bridge-evolution-config/v1", "name": name.strip(), "profile": profile, "project": str(project.resolve()),
        "baselines": [{"path": str(path), "hash": system.content_hash, "name": system.name} for path, system in zip(paths, systems, strict=True)],
        "source": source, "model": model, "modules": modules, "boards": boards,
        "research_architecture": architecture,
        "research_budget_mode": budget_mode, "maintenance_budget": maintenance, "campaign_manager": manager,
        "auto_continue_repairs": auto_repairs, "repair_continuation_mode": repair_mode,
        "max_trial_boards": integer(raw.get("max_trial_boards", raw.get("max_lab_boards", 256)),
                                    "max_trial_boards", 1, 10000000),
        "hypotheses_per_unit": integer(raw.get("hypotheses_per_unit", 2), "hypotheses_per_unit", 1, 16),
        "repair_attempts": integer(raw.get("repair_attempts", 1 if architecture == "director" else 0), "repair_attempts", 0, 1000),
        "baseline_fault_policy": fault_policy,
        "generations": integer(raw.get("generations", defaults["generations"]), "generations", 1, 100),
        "workers": integer(raw.get("workers", 2), "workers", 1, 4),
        "resamples": integer(raw.get("resamples", defaults["resamples"]), "resamples", 100, 50000),
        "max_agent_requests": integer(raw.get("max_agent_requests", 576 if source == "research" else 40), "max_agent_requests", 1, 10000),
        "max_pairs": integer(raw.get("max_pairs", 200000), "max_pairs", 1, 10000000),
        "max_seconds": maximum_seconds,
        "evaluation_sources": list(dict.fromkeys(resolved_sources)),
        "evaluation_only": evaluation_only,
        "module_contract": contract, "allow_promotion": promotion, "development_sampling": development_sampling,
        "protocol_goal": goal, "selection_policy": selection_policy, "growth_boards": growth_boards,
        "growth_seconds": integer(raw.get("growth_seconds", min(600, maximum_seconds)), "growth_seconds", 1, maximum_seconds),
        "evaluation_seconds": integer(raw.get("evaluation_seconds", min(1800, max(1, maximum_seconds // 4))),
                                      "evaluation_seconds", 1, maximum_seconds),
        "max_promotions": integer(raw.get("max_promotions", 10), "max_promotions", 1, 10000),
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
        "research_seed_parent": seed_parent,
        "reasoning_effort": effort, "origins": origins, "research_task": task.strip(),
        "max_output_tokens": integer(raw.get("max_output_tokens", 32768), "max_output_tokens", 256, 128000),
        "max_agent_tokens": integer(raw.get("max_agent_tokens", 12000000), "max_agent_tokens", 1000, 1000000000),
        "max_research_turns": integer(raw.get("max_research_turns", 16 if profile == "smoke" else 48), "max_research_turns", 1, 10000),
        "max_tool_calls": integer(raw.get("max_tool_calls", 256), "max_tool_calls", 1, 5000),
        "max_releases": integer(raw.get("max_releases", 4), "max_releases", 1, 100),
        "max_lab_boards": integer(raw.get("max_lab_boards", 256), "max_lab_boards", 1, 10000000),
        "research_seconds": integer(raw.get("research_seconds", 1800), "research_seconds", 10, 604800),
        "max_context_bytes": integer(raw.get("max_context_bytes", 1500000), "max_context_bytes",
                                     24000 if architecture == "director" else 16000, 10000000),
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


def engine_identity(root: Path = ROOT) -> str:
    from bridge_evolve.evolution.runtime import asset_hashes
    tools = {name: hashlib.sha256((root / "tools" / name).read_bytes()).hexdigest()
             for name in ("board_viewer.py", "verify_classics.py")}
    return canonical_hash({"runtime": source_snapshot(root / "src/bridge_evolve"), "tools": tools,
                           "assets": asset_hashes(root)})


def save_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf8")
    temporary.replace(path)
