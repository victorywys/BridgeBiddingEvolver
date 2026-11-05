from pathlib import Path

from bridge_evolve.core import Seat, Vulnerability
from bridge_evolve.systems.inputs import parse_hand
from bridge_evolve.systems.runtime import Observation

ROOT = Path(__file__).resolve().parents[2]


def raw_system():
    return {
        "schema_version": 1, "operator_version": 1, "name": "test", "scope": "general",
        "features": {},
        "protocol": {"initial": "start", "states": {"start": {"on": {}, "otherwise": "start"}}},
        "rules": [], "fallback": {"call": "P"},
    }


def rule(identifier, condition, call="1C", priority=10):
    return {"id": identifier, "priority": priority, "when": condition, "call": call}


def observation(hand="AKQ.JT9.KQ32.876", history=(), seat=Seat.NORTH):
    return Observation(parse_hand(hand), seat, Seat.NORTH, Vulnerability.NONE, tuple(history))
