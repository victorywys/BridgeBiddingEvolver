from pathlib import Path

from bridge_evolve.dsl import load_system
from bridge_evolve.dsl.matcher import interpret_partner_bid
from bridge_evolve.reflection.patch import (
    Patch,
    PatchOp,
    apply_patch,
)

SEED_PATH = Path(__file__).resolve().parent.parent / "systems" / "seed_v0"


def test_partner_bid_meaning_recovered():
    sys = load_system(SEED_PATH)
    meaning = interpret_partner_bid("1NT", [], sys)
    assert meaning["Convention"] == "Open1NT"
    assert "HCP" in meaning["Meaning"]


def test_apply_patch_adds_convention():
    sys = load_system(SEED_PATH)
    sys.remove_convention("Stayman")
    patch = Patch(
        name="reintroduce_stayman",
        rationale="testing",
        operations=[PatchOp(
            op="add_convention",
            target="Stayman",
            payload={
                "Name": "Stayman",
                "Priority": 90,
                "Trigger": {"AuctionPattern": ["1NT"], "Seat": "Responder"},
                "Requirements": {"HCP": ">=8", "FourCardMajor": True},
                "Meaning": {"AsksForFourCardMajor": True},
                "Action": {"Bid": "2C"},
            },
        )],
    )
    new = apply_patch(sys, patch)
    assert any(c.name == "Stayman" for c in new.conventions)
