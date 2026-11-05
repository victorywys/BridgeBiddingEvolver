#!/usr/bin/env python3
"""Require actual DDS, check published tables, and measure uncached full deals.

Run from the project root after tools/dds/build.py:
  .venv/bin/python tools/dds/verify.py --deals 10

The JSON report is local to .runtime/dds/verification.json by default.
Published fixtures are read from the clean, pinned official source checkout;
benchmark deals are independently shuffled full decks, never fixture lookups.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import re
import subprocess
from time import perf_counter

from bridge_evolve.core.cards import Deal, Hand, full_deck
from bridge_evolve.oracle import DDSAdapter, DDTable, deal_from_pbn


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deals", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20261023)
    parser.add_argument("--source", type=Path, default=Path(".runtime/dds/source"))
    parser.add_argument("--output", type=Path, default=Path(".runtime/dds/verification.json"))
    arguments = parser.parse_args()
    if arguments.deals < 1:
        parser.error("--deals must be positive")
    adapter = DDSAdapter()
    commit = subprocess.check_output(
        ["git", "-C", str(arguments.source), "rev-parse", "HEAD"], text=True,
    ).strip()
    dirty = subprocess.check_output(
        ["git", "-C", str(arguments.source), "status", "--porcelain", "--untracked-files=no"],
        text=True,
    ).strip()
    if commit != adapter.provenance.source_commit or dirty:
        raise RuntimeError("published fixture source must be the clean pinned DDS commit")
    fixtures = []
    pbn = None
    for line in (arguments.source / "hands/list100.txt").read_text().splitlines():
        if line.startswith("PBN "):
            pbn = re.search(r'"([^"]+)"', line).group(1)
        elif line.startswith("TABLE "):
            entries = list(map(int, line.split()[1:]))
            expected = DDTable.from_dict({
                seat: {
                    strain: entries[4 * strain_index + seat_index]
                    for strain_index, strain in enumerate(("S", "H", "D", "C", "NT"))
                }
                for seat_index, seat in enumerate(("N", "E", "S", "W"))
            })
            actual = adapter.solve(deal_from_pbn(pbn))
            if actual != expected:
                raise RuntimeError(f"published DDS table mismatch: {pbn}")
            fixtures.append({"pbn": pbn, "expected": expected.to_dict(), "actual": actual.to_dict()})
            if len(fixtures) == 5:
                break
    if len(fixtures) != 5:
        raise RuntimeError("did not find five published DDS full-table fixtures")
    rng = random.Random(arguments.seed)
    deals = []
    for _ in range(arguments.deals):
        deck = list(full_deck())
        rng.shuffle(deck)
        deals.append(Deal(*(Hand(tuple(deck[index:index + 13])) for index in range(0, 52, 13))))
    elapsed = []
    tables = []
    for deal in deals:
        start = perf_counter()
        tables.append(adapter.solve(deal))
        elapsed.append(perf_counter() - start)
    repeated = adapter.solve(deals[0])
    cached = DDTable.from_dict(json.loads(json.dumps(tables[0].to_dict())))
    if repeated != tables[0] or cached != tables[0]:
        raise RuntimeError("repeated solve or JSON cache roundtrip changed the DDS table")
    report = {
        "status": "verified",
        "provenance": adapter.provenance.to_dict(),
        "published_fixture_count": len(fixtures),
        "published_fixture_source": "hands/list100.txt at source_commit",
        "published_fixtures": fixtures,
        "benchmark": {
            "full_deals": len(deals),
            "entries_per_deal": 20,
            "seed": arguments.seed,
            "generator": "random.Random(seed).shuffle(core.full_deck()), 13 cards per N/E/S/W",
            "cache": "none; DDSAdapter.solve called for every deal",
            "total_seconds": sum(elapsed),
            "deals_per_second": len(deals) / sum(elapsed),
            "per_deal_seconds": elapsed,
        },
        "repeated_solve_and_json_cache_identical": True,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "status": report["status"],
        "published_fixture_count": len(fixtures),
        "benchmark": report["benchmark"],
        "report": str(arguments.output),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
