"""Strict post-hoc DDS/par payloads; never part of a player's observation."""

import re

from bridge_evolve.core import Contract, Seat, Strain, Vulnerability, score_ns
from bridge_evolve.oracle.table import DDTable


def validate_diagnostics(raw: dict, dealer: str, vulnerability: str) -> dict:
    if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise ValueError("Invalid DDS diagnostics schema.")
    if raw.get("dealer") != dealer or raw.get("vulnerability") != vulnerability:
        raise ValueError("DDS/par dealer or vulnerability does not match the board.")
    table = DDTable.from_dict(raw["table"])
    if raw.get("makeable_levels") != table.makeable_levels():
        raise ValueError("DDS makeable levels do not match the twenty trick entries.")
    oracle = raw.get("oracle")
    if (not isinstance(oracle, dict) or oracle.get("oracle_kind") != "dds"
            or not isinstance(oracle.get("library_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", oracle["library_sha256"])):
        raise ValueError("DDS diagnostics must include real solver provenance.")
    par = raw.get("par")
    if (not isinstance(par, dict) or type(par.get("score_ns")) is not int
            or type(par.get("passed_out")) is not bool or not isinstance(par.get("contracts"), list)
            or par.get("api") != "DealerParBin"):
        raise ValueError("Invalid dealer-aware par result.")
    if par["passed_out"]:
        if par["score_ns"] != 0 or par["contracts"] or any(value for row in table.makeable_levels().values() for value in row.values()):
            raise ValueError("Invalid passed-out par result.")
    elif not 1 <= len(par["contracts"]) <= 10:
        raise ValueError("Par must include its contract positions.")
    for item in par["contracts"]:
        if (not isinstance(item, dict) or any(type(item.get(field)) is not int for field in ("level", "doubling", "tricks"))
                or not isinstance(item.get("declarers"), list) or not item["declarers"]
                or len(item["declarers"]) != len(set(item["declarers"]))
                or set(item["declarers"]) not in ({"N"}, {"E"}, {"S"}, {"W"}, {"N", "S"}, {"E", "W"})):
            raise ValueError("Invalid par contract or declarer positions.")
        for seat in item["declarers"]:
            contract = Contract(item["level"], Strain(item["strain"]), Seat(seat), item["doubling"])
            if (table.tricks(contract.declarer, contract.strain) != item["tricks"]
                    or score_ns(contract, item["tricks"], Vulnerability(vulnerability)) != par["score_ns"]):
                raise ValueError("Par contract does not match its DDS matrix or NS score.")
    return {"schema_version": 1, "dealer": dealer, "vulnerability": vulnerability,
            "table": table.to_dict(), "makeable_levels": table.makeable_levels(), "par": par, "oracle": oracle}
