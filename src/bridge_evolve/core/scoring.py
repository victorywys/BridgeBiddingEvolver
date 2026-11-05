"""Duplicate bridge scoring and signed IMP conversion."""

from __future__ import annotations

from bridge_evolve.core.auction import Contract
from bridge_evolve.core.types import Partnership, Vulnerability


def score_contract(contract: Contract, tricks: int, declarer_vulnerable: bool) -> int:
    target = contract.level + 6
    result = tricks - target
    if result >= 0:
        return _made_score(contract, overtricks=result, declarer_vulnerable=declarer_vulnerable)
    return -_down_penalty(contract, undertricks=-result, declarer_vulnerable=declarer_vulnerable)


def score_ns(contract: Contract, tricks: int, vulnerability: Vulnerability) -> int:
    declarer_vulnerable = vulnerability.is_vulnerable(contract.declarer)
    declarer_score = score_contract(contract, tricks=tricks, declarer_vulnerable=declarer_vulnerable)
    if contract.declarer.partnership == Partnership.NS:
        return declarer_score
    return -declarer_score


def signed_imp(point_difference: int) -> int:
    thresholds = (
        20,
        50,
        90,
        130,
        170,
        220,
        270,
        320,
        370,
        430,
        500,
        600,
        750,
        900,
        1100,
        1300,
        1500,
        1750,
        2000,
        2250,
        2500,
        3000,
        3500,
        4000,
    )
    magnitude = abs(point_difference)
    imps = 0
    for boundary in thresholds:
        if magnitude < boundary:
            break
        imps += 1
    if point_difference < 0:
        return -imps
    return imps


def _made_score(contract: Contract, overtricks: int, declarer_vulnerable: bool) -> int:
    contract_points_undoubled = _trick_points(contract.level, contract.strain.value)
    contract_points = contract_points_undoubled * contract.doubling
    overtrick_points = _overtrick_points(
        overtricks=overtricks,
        strain=contract.strain.value,
        doubling=contract.doubling,
        declarer_vulnerable=declarer_vulnerable,
    )

    if contract_points >= 100:
        game_bonus = 500 if declarer_vulnerable else 300
        part_score_bonus = 0
    else:
        game_bonus = 0
        part_score_bonus = 50

    slam_bonus = 0
    if contract.level == 6:
        slam_bonus = 750 if declarer_vulnerable else 500
    if contract.level == 7:
        slam_bonus = 1500 if declarer_vulnerable else 1000

    insult_bonus = 0
    if contract.doubling == 2:
        insult_bonus = 50
    if contract.doubling == 4:
        insult_bonus = 100

    return (
        contract_points
        + overtrick_points
        + game_bonus
        + part_score_bonus
        + slam_bonus
        + insult_bonus
    )


def _down_penalty(contract: Contract, undertricks: int, declarer_vulnerable: bool) -> int:
    if contract.doubling == 1:
        return undertricks * (100 if declarer_vulnerable else 50)

    doubled_penalty = 0
    if declarer_vulnerable:
        doubled_penalty += 200
        if undertricks > 1:
            doubled_penalty += (undertricks - 1) * 300
    else:
        doubled_penalty += 100
        if undertricks > 1:
            doubled_penalty += min(undertricks - 1, 2) * 200
        if undertricks > 3:
            doubled_penalty += (undertricks - 3) * 300

    if contract.doubling == 4:
        return doubled_penalty * 2
    return doubled_penalty


def _trick_points(level: int, strain: str) -> int:
    if strain in ("C", "D"):
        return level * 20
    if strain in ("H", "S"):
        return level * 30
    return 40 + max(level - 1, 0) * 30


def _overtrick_points(
    overtricks: int,
    strain: str,
    doubling: int,
    declarer_vulnerable: bool,
) -> int:
    if overtricks <= 0:
        return 0
    if doubling == 1:
        if strain in ("C", "D"):
            return overtricks * 20
        return overtricks * 30
    if doubling == 2:
        return overtricks * (200 if declarer_vulnerable else 100)
    return overtricks * (400 if declarer_vulnerable else 200)

