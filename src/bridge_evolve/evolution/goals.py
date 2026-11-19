"""Executable protocol requirements, separate from experimental fitness."""

from bridge_evolve.core import ALL_BID_CALLS, Call, Seat, Vulnerability
from bridge_evolve.evolution.config import blank_system
from bridge_evolve.systems.language import SystemError
from bridge_evolve.systems.v2_runtime import PublicContext, PublicWorld


class ForcingPassGoal:
    data = {
        "schema": "bridge-protocol-goal/v1", "name": "Opening forcing Pass", "goal": "forcing-pass",
        "opening_pass": "The partnership's first action is Pass before any contract bid, in first or second position.",
        "obligation": "Partner must make a legal non-Pass response, including after opponent interference.",
        "public_requirement": "The public protocol must enforce the obligation and reject partner Pass, not only label it.",
        "natural_witness": "An authored empty-prefix whole-auction change witness must naturally open with our Pass and show partner's non-Pass response.",
        "free_design": "No HCP range, suit mapping, response convention, rule IDs or internal states are prescribed.",
        "seed_exception": "The all-Pass origin is an incomplete research scaffold, not a goal-compliant released candidate.",
    }

    def cases(self):
        return []

    def check(self, system, *, stop=lambda: None):
        failures, probes = [], 0
        other = blank_system()
        for dealer in Seat:
            for position in (1, 2):
                opener = dealer if position == 1 else dealer.next()
                side = opener.partnership.value
                definitions = {side: system, "EW" if side == "NS" else "NS": other}
                for reply in (Call.pass_call(), *ALL_BID_CALLS):
                    stop()
                    history = (["P"] if position == 2 else []) + ["P", str(reply)]
                    probes += 1
                    try:
                        world = PublicWorld(definitions, dealer, Vulnerability.NONE)
                        if position == 2:
                            world.step(Call.pass_call())
                        world.step(Call.pass_call())
                        world.step(reply)
                        responder = opener.next().next()
                        if not PublicContext(world, side, responder).value("must_bid"):
                            raise SystemError("Opening Pass did not publicly oblige partner to respond.")
                        try:
                            world.step(Call.pass_call())
                        except SystemError as error:
                            if "forcing violation" not in str(error):
                                raise
                        else:
                            raise SystemError("Partner Pass was accepted despite the opening-Pass obligation.")
                    except (SystemError, ValueError) as error:
                        failures.append({"dealer": dealer.value, "history": history, "error": str(error)})
        return {"schema": "bridge-protocol-goal-result/v1", "goal": "forcing-pass",
                "status": "failed" if failures else "passed", "system_hash": system.content_hash,
                "probes": probes, "passed": probes - len(failures), "failure_count": len(failures),
                "failures": failures[:12], "scope": "Public opening-Pass obligation; not exhaustive private-hand coverage or a strength score."}

    @staticmethod
    def opening_pass_event(result, side):
        seen_bid = False
        first_own = False
        opener = None
        for step in result["auction"]:
            actor = Seat(step["seat"])
            call = step["call"]
            if actor.partnership.value == side and not first_own:
                first_own = True
                if not seen_bid and call == "P":
                    opener = actor
            elif opener is not None and actor == opener.next().next():
                return {"opening_pass": True, "partner_response": call}
            if call not in ("P", "X", "XX"):
                seen_bid = True
        return {"opening_pass": opener is not None, "partner_response": None}

    @classmethod
    def natural_witness(cls, result, side):
        event = cls.opening_pass_event(result, side)
        return event["opening_pass"] and event["partner_response"] not in (None, "P")


def protocol_goal(name):
    if name is None:
        return None
    if name == "forcing-pass":
        return ForcingPassGoal()
    raise ValueError(f"Unknown protocol goal: {name}")
