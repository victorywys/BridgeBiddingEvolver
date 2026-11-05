"""Post-hoc slam diagnostics, not bidding rewards or omniscient policy inputs."""

from collections import Counter

from bridge_evolve.oracle import DDTable


def slam_opportunities(table: DDTable, side: str, level: int) -> list[dict]:
    return [{"declarer": seat, "strain": strain, "tricks": tricks}
            for seat, row in table.to_dict().items() if seat in side
            for strain, tricks in row.items() if tricks >= level + 6]


def table_slam_diagnostics(played: dict, table: DDTable) -> dict:
    contract = played.get("contract")
    bid = bool(contract and not contract.get("passed_out"))
    side = ("NS" if contract["declarer"] in "NS" else "EW") if bid else None
    level = contract["level"] if bid else 0
    called = level >= 6
    made = called and played.get("tricks") is not None and played["tricks"] >= 6 + level
    result = {"called": called, "made": made, "level": level if called else None,
              "declaring_side": side, "small_available": [], "grand_available": [],
              "small_not_bid": [], "grand_not_bid": [], "small_wrong_contract": [], "grand_wrong_contract": []}
    for partnership in ("NS", "EW"):
        for name, target in (("small", 6), ("grand", 7)):
            opportunities = slam_opportunities(table, partnership, target)
            if not opportunities:
                continue
            result[name + "_available"].append(partnership)
            if side != partnership or level < target:
                result[name + "_not_bid"].append(partnership)
            elif not made:
                result[name + "_wrong_contract"].append(partnership)
    return result


def summarize_slams(records: list[dict], systems: dict[str, str]) -> tuple[dict, list[dict]]:
    called, made, failed = Counter(), Counter(), Counter()
    opportunity_counts = Counter()
    by_system = {label: Counter() for label in systems.values()}
    cases = []
    families_small, families_grand = set(), set()
    for record in records:
        table = DDTable.from_dict(record["dds"]["table"])
        family = record["family_id"] or record["id"]
        if any(slam_opportunities(table, side, 6) for side in ("NS", "EW")):
            families_small.add(family)
        if any(slam_opportunities(table, side, 7) for side in ("NS", "EW")):
            families_grand.add(family)
        for played in record["tables"]:
            diagnostics = table_slam_diagnostics(played, table)
            if diagnostics["called"]:
                kind = "small" if diagnostics["level"] == 6 else "grand"
                called[kind] += 1
                (made if diagnostics["made"] else failed)[kind] += 1
                label = systems[played["systems"][diagnostics["declaring_side"]]["hash"]]
                by_system[label]["called_" + kind] += 1
                by_system[label][("made_" if diagnostics["made"] else "failed_") + kind] += 1
            for field in ("small_available", "grand_available", "small_not_bid", "grand_not_bid",
                          "small_wrong_contract", "grand_wrong_contract"):
                opportunity_counts[field] += len(diagnostics[field])
                for side in diagnostics[field]:
                    label = systems[played["systems"][side]["hash"]]
                    by_system[label][field] += 1
                    if field.endswith("_available"):
                        continue
                    cases.append({"board_number": record["number"], "index": record["index"], "family_id": family,
                                  "table": played["id"], "side": side, "system": label, "kind": field,
                                  "available": slam_opportunities(table, side, 7 if field.startswith("grand") else 6),
                                  "actual_contract": played["contract"], "actual_tricks": played["tricks"]})
    def totals(counts):
        return {key: counts[key] for key in ("small", "grand")} | {"total": counts["small"] + counts["grand"]}
    return {"boards": len(records), "tables": sum(len(record["tables"]) for record in records),
            "called": totals(called), "made": totals(made), "failed": totals(failed),
            "unique_families_with_small_slam": len(families_small), "unique_families_with_grand_slam": len(families_grand),
            "opportunity_unit": "table-partnership; two tables each contain NS and EW; not independent samples",
            "opportunities": dict(opportunity_counts), "by_system": {key: dict(value) for key, value in by_system.items()},
            "scope": "DDS perfect-information makeability only; not an instruction to bid every available slam."}, cases


def summarize_games(records: list[dict], systems: dict[str, str]) -> tuple[dict, list[dict]]:
    levels = {"C": 5, "D": 5, "H": 4, "S": 4, "NT": 3}
    counts = Counter()
    families = set()
    by_system = {label: Counter() for label in systems.values()}
    cases = []
    signals = Counter()
    for record in records:
        table = DDTable.from_dict(record["dds"]["table"])
        family = record["family_id"] or record["id"]
        for played in record["tables"]:
            contract = played["contract"]
            bid = bool(contract and not contract.get("passed_out"))
            side = ("NS" if contract["declarer"] in "NS" else "EW") if bid else None
            reached = bid and contract["level"] >= levels[contract["strain"]]
            made = reached and played["tricks"] is not None and played["tricks"] >= 6 + contract["level"]
            if reached:
                counts["called"] += 1
                counts["made" if made else "failed"] += 1
                label = systems[played["systems"][side]["hash"]]
                by_system[label]["called"] += 1
                by_system[label]["made" if made else "failed"] += 1
            for step in played["auction"]:
                before, after = step.get("public_context", {}), step.get("public_context_after", {})
                if after.get("forcing") == "invite_game" and before.get("forcing") != "invite_game":
                    signals["game_invitations"] += 1
                if after.get("state") == "slam_cue" and before.get("state") != "slam_cue":
                    signals["slam_explorations"] += 1
                if after.get("state") in ("rkcb", "void_answer", "gerber") and before.get("state") != after["state"]:
                    signals["slam_questions"] += 1
            for partnership in ("NS", "EW"):
                available = [{"declarer": seat, "strain": strain, "level": levels[strain], "tricks": tricks}
                             for seat, row in table.to_dict().items() if seat in partnership
                             for strain, tricks in row.items() if tricks >= levels[strain] + 6]
                if not available:
                    continue
                families.add(family)
                counts["available"] += 1
                label = systems[played["systems"][partnership]["hash"]]
                by_system[label]["available"] += 1
                category = "not_bid" if side != partnership or not reached else "wrong_contract" if not made else None
                if category:
                    counts[category] += 1
                    by_system[label][category] += 1
                    cases.append({"board_number": record["number"], "index": record["index"], "family_id": family,
                                  "table": played["id"], "side": partnership, "system": label, "kind": category,
                                  "available": available, "actual_contract": contract, "actual_tricks": played["tricks"]})
    return {"boards": len(records), "tables": sum(len(record["tables"]) for record in records),
            "called": counts["called"], "made": counts["made"], "failed": counts["failed"],
            "unique_families_with_game": len(families),
            "opportunities": {name: counts[name] for name in ("available", "not_bid", "wrong_contract")},
            "signals": dict(signals), "by_system": {key: dict(value) for key, value in by_system.items()},
            "scope": "Post-hoc DDS game opportunities include both partnerships and sacrifices; not must-bid labels or independent samples."}, cases
