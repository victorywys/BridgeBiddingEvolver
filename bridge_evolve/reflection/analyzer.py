"""Failure analysis: detect classes of pathology in completed auctions.

This module produces *symbolic* diagnoses (an enum + key/value
attributes), not natural-language critique. The reflection layer can
later choose to summarise them in Markdown, but every patch decision is
driven by these structured objects.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Optional

from ..agent.base import AuctionRecord
from ..env.auction import BidType
from ..env.deal import hand_features
from ..env.scoring import evaluate_auction


class DiagnosisKind(str, Enum):
    PASSED_OUT_WITH_VALUES = "PASSED_OUT_WITH_VALUES"
    OVERREACH = "OVERREACH"                  # contract too high, undertricks
    UNDERREACH = "UNDERREACH"                # missed game / slam
    WRONG_STRAIN = "WRONG_STRAIN"            # missed major-suit fit
    AMBIGUOUS_BID = "AMBIGUOUS_BID"          # bid had no explicit meaning
    NO_RULE_FIRED = "NO_RULE_FIRED"          # agent fell into default branch
    PARTNER_MISREAD = "PARTNER_MISREAD"      # partner's meaning lost
    ILLEGAL_FORCED_PASS = "ILLEGAL_FORCED_PASS"


@dataclass
class AuctionDiagnosis:
    kind: DiagnosisKind
    seat: Optional[str] = None
    bid_index: Optional[int] = None
    details: Dict[str, object] = field(default_factory=dict)
    severity: float = 1.0

    def fingerprint(self) -> str:
        kvs = ",".join(f"{k}={v}" for k, v in sorted(self.details.items())
                       if isinstance(v, (str, int, float, bool)))
        return f"{self.kind.value}|{kvs}"


def analyze_auction(record: AuctionRecord) -> List[AuctionDiagnosis]:
    diags: List[AuctionDiagnosis] = []
    deal = record.deal
    auction = record.auction
    result = evaluate_auction(deal, auction)
    ns_hcp = hand_features(deal.hand("N")).hcp + hand_features(deal.hand("S")).hcp
    ew_hcp = hand_features(deal.hand("E")).hcp + hand_features(deal.hand("W")).hcp

    # 1) passed-out with values
    if result.notes.get("passed_out") and max(ns_hcp, ew_hcp) >= 22:
        side = "NS" if ns_hcp >= ew_hcp else "EW"
        diags.append(AuctionDiagnosis(
            kind=DiagnosisKind.PASSED_OUT_WITH_VALUES,
            details={"Side": side, "HCP": max(ns_hcp, ew_hcp)},
            severity=2.0,
        ))

    # 2) over/under-reach
    if result.contract is not None:
        if result.undertricks >= 2:
            diags.append(AuctionDiagnosis(
                kind=DiagnosisKind.OVERREACH,
                details={"Contract": str(result.contract),
                         "Undertricks": result.undertricks},
                severity=float(result.undertricks),
            ))
        if (result.made and result.overtricks >= 2
                and result.contract.level < 4):
            diags.append(AuctionDiagnosis(
                kind=DiagnosisKind.UNDERREACH,
                details={"Contract": str(result.contract),
                         "Overtricks": result.overtricks},
                severity=float(result.overtricks),
            ))

    # 3) wrong strain — missed 8-card major fit
    if result.contract is not None:
        side = "NS" if result.declarer in ("N", "S") else "EW"
        if side == "NS":
            a, b = "N", "S"
        else:
            a, b = "E", "W"
        fa = hand_features(deal.hand(a))
        fb = hand_features(deal.hand(b))
        for m in ("H", "S"):
            if fa.length[m] + fb.length[m] >= 8 and result.contract.strain.symbol != m:
                diags.append(AuctionDiagnosis(
                    kind=DiagnosisKind.WRONG_STRAIN,
                    details={"FitSuit": m,
                             "FitLength": fa.length[m] + fb.length[m],
                             "Contract": str(result.contract)},
                    severity=1.5,
                ))
                break

    # 4) ambiguous bids + 5) no-rule-fired
    for i, bid in enumerate(record.bids):
        if bid.rule_type == "default" and bid.token != "P":
            diags.append(AuctionDiagnosis(
                kind=DiagnosisKind.NO_RULE_FIRED,
                seat=bid.seat, bid_index=i,
                details={"Token": bid.token,
                         "History": " ".join(record.bids[j].token for j in range(i))},
                severity=0.7,
            ))
        if not bid.meaning and bid.token not in ("P",):
            diags.append(AuctionDiagnosis(
                kind=DiagnosisKind.AMBIGUOUS_BID,
                seat=bid.seat, bid_index=i,
                details={"Token": bid.token},
                severity=0.5,
            ))
        if bid.rule_name == "env_correction":
            diags.append(AuctionDiagnosis(
                kind=DiagnosisKind.ILLEGAL_FORCED_PASS,
                seat=bid.seat, bid_index=i,
                details=dict(bid.meaning),
                severity=0.9,
            ))

    return diags


def aggregate_diagnoses(diags: Iterable[AuctionDiagnosis]) -> Dict[str, int]:
    return dict(Counter(d.kind.value for d in diags))
