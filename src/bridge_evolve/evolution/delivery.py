"""Timeboxed executor calls that return a concrete, explicitly qualified solution."""

import copy
import json
import math
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from bridge_evolve.evaluation.data import canonical_hash
from bridge_evolve.evaluation.experiment import timestamp
from bridge_evolve.evolution.models import request_body, token_reservation
from bridge_evolve.evolution.context import assemble_context

TEXT = {"type": "string", "minLength": 1, "maxLength": 16000, "pattern": r"[\s\S]*\S[\s\S]*"}
RETURN_SOLUTION = {
    "type": "function", "name": "return_solution",
    "description": (
        "Return this period's concrete best available solution NOW, even if imperfect. "
        "Specify the actual chosen design or repair, public evidence/basis and known limitations; "
        "a status report, blocker list or promise to investigate is not a solution. "
        "The host attaches actual saved work and verification; this cannot publish or accept a repair."),
    "parameters": {"type": "object", "additionalProperties": False,
                   "properties": {"proposal": TEXT, "basis": TEXT, "limitations": TEXT, "next_action": TEXT,
                       "provisional_patch": {
                           "type": "object", "additionalProperties": False,
                           "description": "Optional research-only atomic IR patch to save an immature design into the draft NOW; never publication or engineering deployment.",
                           "properties": {"document_hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                                          "edits": {"type": "array", "minItems": 1, "maxItems": 128,
                                                    "items": {"type": "object"}},
                                          "falsification": TEXT},
                           "required": ["document_hash", "edits", "falsification"]}},
                   "required": ["proposal", "basis", "limitations", "next_action"]},
}
RETURN_CHOICE = {"type": "function", "name": RETURN_SOLUTION["name"]}
RETURN_PROMPT = (
    "Your bounded executor call must now RETURN a solution, not ask for more time or further queries. "
    "Choose the best concrete scheme supported by the research already available. Imperfection is allowed. "
    "Describe the chosen calls/meanings/continuations or exact engineering change, its public evidence, "
    "tradeoffs and remaining gaps. Do not invent implementation or test success. "
    "Call return_solution exactly once; no discovery or other tools are available. Research may attach "
    "provisional_patch against the exact supplied document_hash: save a concrete atomic IR design now, "
    "even if immature or untested, with explicit limitations and falsification. It remains a marked draft, "
    "not publication or acceptance. Engineering returns must omit provisional_patch. "
    "Use Chinese user-facing text, not private reasoning.")


def enabled(config):
    return bool((config.get("campaign_manager") or {}).get("solution_return"))


def solution_messages(config, context, *, tail=(), required=(), archive_tail=None):
    maximum = min(18000, config["max_context_bytes"] - 2048)
    return assemble_context(config, RETURN_PROMPT, context, [RETURN_SOLUTION], maximum=maximum,
                            tail=tail, required=required, tool_choice=RETURN_CHOICE, archive_tail=archive_tail)


def solution_reservation(config):
    maximum = min(18000, config["max_context_bytes"] - 2048)
    return token_reservation(bytes(maximum), config["max_output_tokens"]) + 2048


class DeliveryDeadline(RuntimeError):
    pass


class SolutionReturned(Exception):
    def __init__(self, value):
        self.value = value
        super().__init__("The executor returned its current solution; implementation acceptance remains separate.")


class DeliveryWindow:
    def __init__(self, controller, path, label, *, start_turn=0, deadline_at=None, session=None, repair=None):
        self.controller, self.path, self.label = controller, path, label
        limits = controller.config["campaign_manager"]
        self.data = controller.read_artifact(path)
        if self.data is None:
            now = datetime.now(timezone.utc)
            deadline = datetime.fromisoformat(deadline_at) if deadline_at else now + timedelta(seconds=limits["seconds"])
            identity = "solution-" + canonical_hash({
                "run": controller.state["id"], "path": path.relative_to(controller.directory).as_posix()})[:24]
            self.data = {
                "id": identity, "label": label, "session": session, "repair": repair,
                "status": "working", "start_turn": start_turn, "turns": limits["turns"],
                "requests": 0,
                "seconds": limits["seconds"], "started_at": (deadline - timedelta(seconds=limits["seconds"])).isoformat(),
                "deadline_at": deadline.isoformat(), "reminders": [],
                "return_seconds": min(120, max(1, limits["seconds"] // 4))}
            self.save()
        elif self.data["label"] != label:
            raise ValueError("A delivery window cannot be reused for a different executor call.")
        self.closing = self.data.get("closing", False)
        self.reason = self.data.get("return_reason")
        self.reserve = {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.controller.artifact(self.path, self.data)
        self.controller.state.setdefault("delivery_windows", {})[self.data["id"]] = {
            key: copy.deepcopy(self.data[key]) for key in
            ("id", "label", "session", "repair", "status", "started_at", "deadline_at", "turns", "seconds")}
        self.controller.state["delivery_windows"][self.data["id"]].update(
            requests=self.data["requests"], error=self.data.get("error"),
            last_countdown=copy.deepcopy(self.data["reminders"][-1]) if self.data["reminders"] else None)
        self.controller.checkpoint()

    def seconds_left(self):
        return max(0, (datetime.fromisoformat(self.data["deadline_at"]) - datetime.now(timezone.utc)).total_seconds())

    def should_return(self, turn):
        return (self.closing or self.data["requests"] >= self.data["turns"] - 1
                or self.seconds_left() <= self.data["return_seconds"])

    def timeout(self):
        if self.data["status"] != "working":
            return float("inf") if self.data["status"] == "returned" else 0
        return max(0, self.seconds_left() - (0 if self.closing else self.data["return_seconds"]))

    def check(self):
        if self.timeout() <= 0:
            raise DeliveryDeadline("The executor's solution-return boundary was reached.")

    @contextmanager
    def active(self):
        previous = getattr(self.controller, "delivery_window", None)
        self.controller.delivery_window = self
        try:
            yield self
        finally:
            self.controller.delivery_window = previous

    def preview_reminder(self):
        value = {
            "delivery_id": self.data["id"], "deadline_at": self.data["deadline_at"],
            "seconds_remaining": math.ceil(self.seconds_left()),
            "turns_remaining": self.data["turns"] - self.data["requests"],
            "phase": "return_solution_now" if self.closing else "work_then_return",
            "instruction": (
                "必须在本周期内返回具体的当前最佳方案，可以不完美；不能只返回进度、阻塞或继续研究的计划。"
                "研究方案可附provisional_patch临时写入当前草稿，明确局限和证伪条件，不必等全部回归通过才保存设计。"
                "最后一轮只用于交付，宿主附带实际草稿和验证状态，不把未测试方案冒充已验证成果。")}
        return {"role": "user", "content": json.dumps({"delivery_countdown": value}, ensure_ascii=False)}

    def reminder(self, turn):
        if self.data["requests"] >= self.data["turns"]:
            raise DeliveryDeadline("The executor exhausted its request slots without a valid solution return.")
        message = self.preview_reminder()
        value = json.loads(message["content"])["delivery_countdown"]
        self.data["reminders"].append({"turn": turn, "timestamp": timestamp(), **value})
        self.data["requests"] += 1
        self.data.update(closing=self.closing, return_reason=self.reason)
        self.save()
        return message

    def return_messages(self, context, **options):
        return solution_messages(self.controller.config, context, **options)

    def hold_return_request(self, messages, budget):
        body = request_body(self.controller.config, messages, [RETURN_SOLUTION], tool_choice=RETURN_CHOICE)
        if len(body) > min(18000, self.controller.config["max_context_bytes"] - 2048):
            raise ValueError("The solution-return request exceeds its reserved input allowance.")
        self.reserve = {"budget": budget, "requests": 1, "tokens": solution_reservation(self.controller.config)}
        return self.reserve

    def complete(self, solution, evidence, turn, *, source="model_return"):
        if self.data["status"] == "returned":
            reference = self.data["result"]
            if self.controller.state["artifacts"].get(reference["path"]) != reference["sha256"]:
                raise ValueError("The completed solution return differs from its immutable reference.")
            value = self.controller.read_artifact(self.controller.directory / self.data["result"]["path"])
            if value["solution"] != solution or value["source"] != source:
                raise ValueError("A completed executor call cannot return a different solution.")
            return value
        value = {
            "schema": "bridge-solution-return/v1", "id": self.data["id"], "label": self.label,
            "session": self.data["session"], "repair": self.data["repair"], "status": "returned",
            "source": source, "solution": copy.deepcopy(solution), "evidence": evidence,
            "started_at": self.data["started_at"], "deadline_at": self.data["deadline_at"],
            "returned_at": timestamp(), "turns_used": self.data["requests"],
            "turns_allowed": self.data["turns"], "seconds_allowed": self.data["seconds"],
            "return_reason": self.reason or "executor_return",
            "countdown_history": copy.deepcopy(self.data["reminders"]),
            "late_seconds": max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(self.data["deadline_at"])).total_seconds()),
            "scope": "A concrete current-best solution, not proof of implementation, validation, repair acceptance or promotion."}
        if evidence.get("provisional_patch"):
            patch = evidence["provisional_patch"]
            value["provisional_application"] = {key: copy.deepcopy(patch[key]) for key in
                ("id", "status", "paths", "before", "after", "error", "verification", "acceptance") if key in patch}
        path = self.path.with_name(self.path.stem + ".return.json")
        existing = self.controller.read_artifact(path)
        if existing is not None:
            if any(existing[key] != value[key] for key in ("id", "solution", "source")):
                raise ValueError("An immutable solution return differs from its replayed response.")
            value = existing
        else:
            self.controller.artifact(path, value)
        reference = {"path": path.relative_to(self.controller.directory).as_posix(),
                     "sha256": self.controller.state["artifacts"][path.relative_to(self.controller.directory).as_posix()]}
        self.data.update(status="returned", result=reference)
        self.controller.state.setdefault("solution_deliveries", {})[self.data["id"]] = {
            **{key: value[key] for key in ("id", "label", "session", "repair", "status", "solution", "returned_at",
                                         "turns_used", "turns_allowed", "seconds_allowed", "return_reason", "late_seconds", "scope")},
            "result": reference}
        if "provisional_application" in value:
            self.controller.state["solution_deliveries"][self.data["id"]]["provisional_application"] = value["provisional_application"]
        self.save()
        return value

    def fail(self, error, turn):
        if self.data["status"] == "returned":
            return
        self.data.update(status="failed", error=str(error), stopped_at=timestamp(),
                         turns_used=self.data["requests"])
        self.save()
        self.controller.milestone(self.data["id"] + ":failed", "solution_return_failed",
                                  error=str(error), session=self.data["session"], repair=self.data["repair"])
