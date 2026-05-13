"""Symbolic schema for the bidding system DSL.

Conventions are first-class structured objects. Their fields are
deliberately constrained so an LLM-generated patch can be validated.

Schema (YAML):

```yaml
Convention:
  Name: Stayman                    # unique within a system
  Description: "Ask opener for 4-card major over 1NT"
  Priority: 80                     # higher = considered first
  Trigger:
    AuctionPattern: ["1NT"]        # tokens, "*" = wildcard
    Seat: "Responder"              # opener/responder/intervenor/advancer
  Requirements:
    HCP: ">=8"                     # numeric comparison strings
    FourCardMajor: true
    Balanced: any                  # "any" means unconstrained
  Meaning:
    AsksForFourCardMajor: true
    Forcing: 1                     # 0 = NF, 1 = one round, 2 = game forcing
  Action:
    Bid: "2C"
  Followups:
    - Pattern: ["2D"]
      Meaning:
        DeniesFourCardMajor: true
    - Pattern: ["2H"]
      Meaning:
        ShowsFourCardHearts: true
```

Opening rules are simpler:

```yaml
OpeningRule:
  Name: Open1NT
  Priority: 100
  Requirements:
    HCP: "15..17"
    Balanced: true
  Action:
    Bid: "1NT"
```

Principles are markdown advisory notes, retrieved when no rule fires.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


@dataclass
class Trigger:
    auction_pattern: List[str] = field(default_factory=list)
    seat: Optional[str] = None  # Opener / Responder / Intervenor / Advancer / any
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_yaml(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"AuctionPattern": list(self.auction_pattern)}
        if self.seat:
            d["Seat"] = self.seat
        d.update(self.extras)
        return d


@dataclass
class Requirements:
    """Symbolic constraints over the bidder's hand features."""
    constraints: Dict[str, Any] = field(default_factory=dict)

    def to_yaml(self) -> Dict[str, Any]:
        return dict(self.constraints)


@dataclass
class Meaning:
    fields: Dict[str, Any] = field(default_factory=dict)

    def to_yaml(self) -> Dict[str, Any]:
        return dict(self.fields)


@dataclass
class Followup:
    pattern: List[str] = field(default_factory=list)
    meaning: Meaning = field(default_factory=Meaning)
    requirements: Requirements = field(default_factory=Requirements)
    action: Optional[str] = None  # bid token

    def to_yaml(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"Pattern": list(self.pattern)}
        if self.meaning.fields:
            d["Meaning"] = self.meaning.to_yaml()
        if self.requirements.constraints:
            d["Requirements"] = self.requirements.to_yaml()
        if self.action:
            d["Action"] = {"Bid": self.action}
        return d


@dataclass
class Convention:
    name: str
    description: str = ""
    priority: int = 50
    trigger: Trigger = field(default_factory=Trigger)
    requirements: Requirements = field(default_factory=Requirements)
    meaning: Meaning = field(default_factory=Meaning)
    action: Optional[str] = None
    followups: List[Followup] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    source: Optional[str] = None  # filename, for round-trip + diffs

    def to_yaml(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "Convention": {
                "Name": self.name,
                "Description": self.description,
                "Priority": self.priority,
                "Trigger": self.trigger.to_yaml(),
                "Requirements": self.requirements.to_yaml(),
                "Meaning": self.meaning.to_yaml(),
            }
        }
        if self.action:
            d["Convention"]["Action"] = {"Bid": self.action}
        if self.followups:
            d["Convention"]["Followups"] = [f.to_yaml() for f in self.followups]
        if self.tags:
            d["Convention"]["Tags"] = list(self.tags)
        return d


@dataclass
class OpeningRule:
    name: str
    priority: int = 50
    requirements: Requirements = field(default_factory=Requirements)
    action: str = ""
    description: str = ""

    def to_yaml(self) -> Dict[str, Any]:
        return {
            "OpeningRule": {
                "Name": self.name,
                "Description": self.description,
                "Priority": self.priority,
                "Requirements": self.requirements.to_yaml(),
                "Action": {"Bid": self.action},
            }
        }


@dataclass
class Principle:
    name: str
    text: str

    def to_yaml(self) -> Dict[str, Any]:
        return {"Principle": {"Name": self.name, "Text": self.text}}


# ---------------------------------------------------------------------------
# Whole-system container
# ---------------------------------------------------------------------------


@dataclass
class BiddingSystem:
    name: str
    version: str
    path: str  # root folder
    opening_table: List[OpeningRule] = field(default_factory=list)
    conventions: List[Convention] = field(default_factory=list)
    principles: List[Principle] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    def find_convention(self, name: str) -> Optional[Convention]:
        for c in self.conventions:
            if c.name == name:
                return c
        return None

    def replace_convention(self, conv: Convention) -> None:
        for i, c in enumerate(self.conventions):
            if c.name == conv.name:
                self.conventions[i] = conv
                return
        self.conventions.append(conv)

    def remove_convention(self, name: str) -> bool:
        for i, c in enumerate(self.conventions):
            if c.name == name:
                del self.conventions[i]
                return True
        return False
