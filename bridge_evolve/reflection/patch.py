"""Patches: structured edits to a :class:`BiddingSystem`.

A patch is a list of *operations*. Each op acts on a named element of
the system (convention / opening rule / principle) so patches are
inspectable, diffable, rollback-able and ablation-friendly.

Supported operations:

* ``add_convention`` — insert a new ``Convention``.
* ``remove_convention`` — delete by name.
* ``modify_convention`` — apply a shallow ``field_updates`` dict.
* ``add_opening`` — append/replace an ``OpeningRule`` by name.
* ``remove_opening`` — delete by name.
* ``add_principle`` — append a ``Principle``.

A patch carries a rationale (markdown), a list of source diagnoses, and
a target version label (assigned when applied).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

from ..dsl.schema import (
    BiddingSystem,
    Convention,
    Meaning,
    OpeningRule,
    Principle,
    Requirements,
    Trigger,
)
from .analyzer import AuctionDiagnosis, DiagnosisKind


@dataclass
class PatchOp:
    op: str
    target: str
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_yaml(self) -> Dict[str, Any]:
        return {"Op": self.op, "Target": self.target, "Payload": self.payload}


@dataclass
class Patch:
    name: str
    rationale: str
    operations: List[PatchOp] = field(default_factory=list)
    diagnoses: List[str] = field(default_factory=list)
    parent_version: str = ""

    def to_yaml(self) -> str:
        return yaml.safe_dump({
            "Patch": {
                "Name": self.name,
                "Rationale": self.rationale,
                "ParentVersion": self.parent_version,
                "Diagnoses": list(self.diagnoses),
                "Operations": [op.to_yaml() for op in self.operations],
            }
        }, sort_keys=False, allow_unicode=True)

    @classmethod
    def from_yaml(cls, text: str) -> "Patch":
        body = (yaml.safe_load(text) or {}).get("Patch", {})
        ops = [PatchOp(o["Op"], o["Target"], dict(o.get("Payload", {}) or {}))
               for o in body.get("Operations", [])]
        return cls(
            name=body.get("Name", "unnamed"),
            rationale=body.get("Rationale", ""),
            operations=ops,
            diagnoses=list(body.get("Diagnoses", [])),
            parent_version=str(body.get("ParentVersion", "")),
        )


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def apply_patch(system: BiddingSystem, patch: Patch) -> BiddingSystem:
    """Return a *deep copy* of ``system`` with patch applied. Pure function."""
    out = copy.deepcopy(system)
    for op in patch.operations:
        _apply_op(out, op)
    return out


def _apply_op(system: BiddingSystem, op: PatchOp) -> None:
    if op.op == "add_convention":
        system.replace_convention(_payload_to_convention(op.payload))
    elif op.op == "remove_convention":
        system.remove_convention(op.target)
    elif op.op == "modify_convention":
        conv = system.find_convention(op.target)
        if conv is None:
            return
        for k, v in op.payload.items():
            if k == "Priority":
                conv.priority = int(v)
            elif k == "Description":
                conv.description = str(v)
            elif k == "Requirements":
                conv.requirements = Requirements(constraints=dict(v))
            elif k == "Meaning":
                conv.meaning = Meaning(fields=dict(v))
            elif k == "Action":
                conv.action = (v or {}).get("Bid", v) if isinstance(v, dict) else v
            elif k == "Trigger":
                conv.trigger = Trigger(
                    auction_pattern=list(v.get("AuctionPattern", [])),
                    seat=v.get("Seat"),
                    extras={kk: vv for kk, vv in v.items()
                            if kk not in ("AuctionPattern", "Seat")},
                )
    elif op.op == "add_opening":
        rule = _payload_to_opening(op.payload)
        # replace existing by name
        for i, r in enumerate(system.opening_table):
            if r.name == rule.name:
                system.opening_table[i] = rule
                break
        else:
            system.opening_table.append(rule)
        system.opening_table.sort(key=lambda r: (-r.priority, r.name))
    elif op.op == "remove_opening":
        system.opening_table = [r for r in system.opening_table if r.name != op.target]
    elif op.op == "add_principle":
        system.principles.append(
            Principle(name=op.target, text=str(op.payload.get("Text", "")))
        )
    elif op.op == "remove_principle":
        system.principles = [p for p in system.principles if p.name != op.target]


def _payload_to_convention(p: Dict[str, Any]) -> Convention:
    trig = p.get("Trigger", {}) or {}
    return Convention(
        name=p["Name"],
        description=p.get("Description", ""),
        priority=int(p.get("Priority", 50)),
        trigger=Trigger(
            auction_pattern=list(trig.get("AuctionPattern", [])),
            seat=trig.get("Seat"),
            extras={k: v for k, v in trig.items() if k not in ("AuctionPattern", "Seat")},
        ),
        requirements=Requirements(constraints=dict(p.get("Requirements", {}) or {})),
        meaning=Meaning(fields=dict(p.get("Meaning", {}) or {})),
        action=(p.get("Action", {}) or {}).get("Bid") if isinstance(p.get("Action"), dict) else p.get("Action"),
        tags=list(p.get("Tags", []) or []),
    )


def _payload_to_opening(p: Dict[str, Any]) -> OpeningRule:
    return OpeningRule(
        name=p["Name"],
        priority=int(p.get("Priority", 50)),
        description=p.get("Description", ""),
        requirements=Requirements(constraints=dict(p.get("Requirements", {}) or {})),
        action=(p.get("Action", {}) or {}).get("Bid", p.get("Action", "")) if isinstance(p.get("Action"), dict) else p.get("Action", ""),
    )


# ---------------------------------------------------------------------------
# Heuristic patch proposal
# ---------------------------------------------------------------------------


def propose_patches(system: BiddingSystem,
                    diagnoses: List[AuctionDiagnosis]) -> List[Patch]:
    """Map symbolic diagnoses to candidate patches.

    The mapping is deliberately small and explicit so the search remains
    legible. An LLM-driven proposer can replace or augment this layer
    (see :mod:`bridge_evolve.reflection.llm_reflector`).
    """
    patches: List[Patch] = []
    counts: Dict[str, int] = {}
    for d in diagnoses:
        counts[d.kind.value] = counts.get(d.kind.value, 0) + 1

    # 1) passed-out with values -> raise OpenWeak priority or add a 1-of-a-suit opener
    if counts.get(DiagnosisKind.PASSED_OUT_WITH_VALUES.value, 0) >= 2:
        patches.append(_patch_open_weaker(system))

    # 2) overreach -> tighten game-forcing thresholds
    if counts.get(DiagnosisKind.OVERREACH.value, 0) >= 2:
        patches.append(_patch_tighten_game(system))

    # 3) wrong strain -> ensure Stayman/Transfer exists
    if counts.get(DiagnosisKind.WRONG_STRAIN.value, 0) >= 1:
        patches.append(_patch_add_stayman(system))

    # 4) no rule fired -> add a default natural overcall
    if counts.get(DiagnosisKind.NO_RULE_FIRED.value, 0) >= 3:
        patches.append(_patch_default_pass_principle(system))

    return [p for p in patches if p.operations]


def _patch_open_weaker(system: BiddingSystem) -> Patch:
    """Add a weak-two-style 6-card opening — addresses pass-outs with values."""
    ops: List[PatchOp] = []
    needed = [
        ("Open1S_Light", "1S", {"HCP": "11..14", "Spades": ">=6"}),
        ("Open1H_Light", "1H", {"HCP": "11..14", "Hearts": ">=6"}),
    ]
    for name, bid, req in needed:
        if any(r.name == name for r in system.opening_table):
            continue
        ops.append(PatchOp(
            op="add_opening",
            target=name,
            payload={
                "Name": name,
                "Description": f"Light opening with a 6-card suit ({bid}).",
                "Priority": 85,
                "Requirements": req,
                "Action": {"Bid": bid},
            },
        ))
    return Patch(
        name="open_with_values",
        rationale="Too many auctions passed out with combined values >=22 HCP; "
                  "introduce light single-suited openings.",
        operations=ops,
        diagnoses=[DiagnosisKind.PASSED_OUT_WITH_VALUES.value],
    )


def _patch_tighten_game(system: BiddingSystem) -> Patch:
    ops: List[PatchOp] = []
    # tighten any "GameForce" convention thresholds by +1 HCP, capped
    for c in system.conventions:
        if "GameForce" in c.tags or "Force" in c.meaning.fields.keys():
            cur = c.requirements.constraints.get("HCP", "")
            if isinstance(cur, str) and cur.startswith(">="):
                try:
                    n = int(cur.replace(">=", "").strip())
                    ops.append(PatchOp(
                        op="modify_convention", target=c.name,
                        payload={"Requirements": {**c.requirements.constraints, "HCP": f">={n+1}"}},
                    ))
                except ValueError:
                    pass
    return Patch(
        name="tighten_game_force",
        rationale="Multiple overreached contracts; raise force thresholds.",
        operations=ops,
        diagnoses=[DiagnosisKind.OVERREACH.value],
    )


def _patch_add_stayman(system: BiddingSystem) -> Patch:
    ops: List[PatchOp] = []
    if system.find_convention("Stayman") is None:
        ops.append(PatchOp(
            op="add_convention",
            target="Stayman",
            payload={
                "Name": "Stayman",
                "Description": "Ask 1NT opener for a 4-card major.",
                "Priority": 85,
                "Trigger": {"AuctionPattern": ["1NT"], "Seat": "Responder"},
                "Requirements": {"HCP": ">=8", "FourCardMajor": True},
                "Meaning": {"AsksForFourCardMajor": True, "Forcing": 1},
                "Action": {"Bid": "2C"},
                "Tags": ["MajorFitFinder"],
            },
        ))
    return Patch(
        name="add_stayman",
        rationale="Missed 4-4 / 5-3 major fits; introduce Stayman over 1NT.",
        operations=ops,
        diagnoses=[DiagnosisKind.WRONG_STRAIN.value],
    )


def _patch_default_pass_principle(system: BiddingSystem) -> Patch:
    name = "WhenInDoubtPass"
    if any(p.name == name for p in system.principles):
        return Patch(name="noop", rationale="", operations=[])
    return Patch(
        name="record_default_principle",
        rationale="Many bids fell into default; document the safety rule.",
        operations=[PatchOp(
            op="add_principle", target=name,
            payload={"Text":
                "When no convention matches and partner has not promised values, prefer Pass. "
                "Symbolic communication is more valuable than guessing."},
        )],
        diagnoses=[DiagnosisKind.NO_RULE_FIRED.value],
    )
