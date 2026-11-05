"""Small consumed regression inputs, independent of local experiment archives."""

import copy
import gzip
import json
from functools import lru_cache
from pathlib import Path

from bridge_evolve.systems import validate_system

FIXTURES = Path(__file__).parent / "fixtures"
V4_HASHES = {
    "ccba": "22656ec34f7f3c2ad491faa96835a79a9209cdb085dcf34516566085f2888ef4",
    "prec": "4320b31f32b2b5cbea74dd1546e98394f9c2868df0c5010ea98081cbd8c8eb0a",
}


@lru_cache(maxsize=2)
def frozen_v4(kind):
    expected = V4_HASHES[kind]
    with gzip.open(FIXTURES / "classics-v4" / f"{kind}.json.gz", "rt", encoding="utf8") as contents:
        system = validate_system(json.load(contents))
    if system.content_hash != expected:
        raise ValueError(f"Frozen v4 regression identity mismatch: {kind}")
    return system


def regression_cases(group):
    data = json.loads((FIXTURES / "classic-regressions.json").read_text(encoding="utf8"))
    return copy.deepcopy(data["cases"][group])
