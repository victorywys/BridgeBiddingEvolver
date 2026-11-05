"""Pinned, real DDS tables; no fixture or random-score fallback.

Build the local library with ``.venv/bin/python tools/dds/build.py``.
DDS 2.9.0 itself is Apache-2.0 licensed; the separate GPL ``ddd`` driver
is neither downloaded nor linked. See the build helper for the recorded
Linux memory-probe compatibility patch.
"""

from bridge_evolve.oracle.dds import (
    DDSAdapter,
    DDSConfigurationError,
    DDSError,
    DDSProvenance,
    DDSSolveError,
    DDSParError,
    DDSUnavailableError,
)
from bridge_evolve.oracle.pbn import deal_from_pbn, deal_to_pbn
from bridge_evolve.oracle.table import DECLARERS, STRAINS, DDTable

__all__ = [
    "DECLARERS",
    "STRAINS",
    "DDSAdapter",
    "DDSConfigurationError",
    "DDSError",
    "DDSProvenance",
    "DDSSolveError",
    "DDSParError",
    "DDSUnavailableError",
    "DDTable",
    "deal_from_pbn",
    "deal_to_pbn",
]
