"""Trick estimation: protocol + heuristic fallback + DDS implementation.

The estimator is the *only* place that turns (deal, declarer, strain) into
a number of tricks. Everything else — contract scoring, IMP matches, the
reflection diagnoses — flows from this single number, so swapping the
estimator changes only one boundary.

Implementations
---------------

* :class:`HeuristicEstimator` — the fast, dependency-free estimator that
  was used before DDS was introduced. Kept as a CI/offline fallback.

* :class:`DDSEstimator` — wraps the ``endplay`` package
  (`Bo Haglund's Double Dummy Solver`__). Returns the exact number of
  tricks declarer can take against best defence assuming perfect
  information. Calls are cached on disk by deal hash.

__ https://github.com/dds-bridge/dds

The :class:`DDSEstimator` is the *default* whenever ``endplay`` is
importable; otherwise the heuristic is used and a warning is logged so
results are clearly marked.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Protocol, Tuple

from .deal import Deal, hand_features
from .deck import Suit


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class TrickEstimator(Protocol):
    """Returns the number of tricks declarer can take in a given strain.

    Implementations must be **deterministic** for a given (deal, declarer,
    strain) — this is what keeps the transfer experiment reproducible.
    """
    name: str

    def estimate(self, deal: Deal, declarer: str, strain: Suit) -> int: ...

    def table(self, deal: Deal) -> Dict[Tuple[str, str], int]:
        """Full double-dummy table: ``{(seat, strain_symbol) -> tricks}``.

        Default implementation calls :meth:`estimate` 20 times.
        """
        out: Dict[Tuple[str, str], int] = {}
        for seat in ("N", "E", "S", "W"):
            for st in (Suit.CLUBS, Suit.DIAMONDS, Suit.HEARTS, Suit.SPADES, Suit.NT):
                out[(seat, st.symbol)] = self.estimate(deal, seat, st)
        return out


# ---------------------------------------------------------------------------
# Heuristic estimator (kept for offline / CI runs)
# ---------------------------------------------------------------------------


@dataclass
class HeuristicEstimator:
    name: str = "heuristic"

    def estimate(self, deal: Deal, declarer: str, strain: Suit) -> int:
        partner = {"N": "S", "S": "N", "E": "W", "W": "E"}[declarer]
        dec_f = hand_features(deal.hand(declarer))
        par_f = hand_features(deal.hand(partner))
        hcp = dec_f.hcp + par_f.hcp
        base = hcp / 3.0
        if strain == Suit.NT:
            bal_bonus = 0.5 * dec_f.balanced + 0.5 * par_f.balanced
            return int(round(base + bal_bonus))
        sym = strain.symbol
        fit = dec_f.length[sym] + par_f.length[sym]
        length_bonus = max(0, fit - 8) * 0.8
        short_bonus = 0.0
        if fit >= 8:
            for s, n in dec_f.length.items():
                if s == sym:
                    continue
                if n == 0:
                    short_bonus += 1.0
                elif n == 1:
                    short_bonus += 0.5
        misfit_penalty = 0.0 if fit >= 7 else (7 - fit) * 0.7
        return int(round(base + length_bonus + short_bonus - misfit_penalty))

    def table(self, deal: Deal) -> Dict[Tuple[str, str], int]:
        out: Dict[Tuple[str, str], int] = {}
        for seat in ("N", "E", "S", "W"):
            for st in (Suit.CLUBS, Suit.DIAMONDS, Suit.HEARTS, Suit.SPADES, Suit.NT):
                out[(seat, st.symbol)] = self.estimate(deal, seat, st)
        return out


# ---------------------------------------------------------------------------
# DDS estimator
# ---------------------------------------------------------------------------


_STRAIN_INDEX_TO_SUIT = {0: Suit.SPADES, 1: Suit.HEARTS, 2: Suit.DIAMONDS,
                         3: Suit.CLUBS, 4: Suit.NT}
_SUIT_TO_INDEX = {v: k for k, v in _STRAIN_INDEX_TO_SUIT.items()}
_SEATS = ("N", "E", "S", "W")


def _deal_hash(deal: Deal) -> str:
    """Stable hash of a deal that ignores dealer (DDS is rotationally invariant)."""
    parts = []
    for seat in _SEATS:
        bs = deal.hand(seat).by_suit()
        for s in (Suit.SPADES, Suit.HEARTS, Suit.DIAMONDS, Suit.CLUBS):
            parts.append("".join(c.rank for c in bs[s]))
        parts.append("|")
    blob = ":".join(parts)
    return hashlib.sha1(blob.encode("ascii")).hexdigest()[:16]


def _deal_to_pbn(deal: Deal) -> str:
    rows = []
    for seat in _SEATS:
        bs = deal.hand(seat).by_suit()
        rows.append(".".join(
            "".join(c.rank for c in bs[s])
            for s in (Suit.SPADES, Suit.HEARTS, Suit.DIAMONDS, Suit.CLUBS)
        ))
    return "N:" + " ".join(rows)


class DDSEstimator:
    """Exact double-dummy estimator backed by ``endplay``.

    Parameters
    ----------
    cache_path : str, optional
        If given, results are cached on disk at this path. Multiple
        processes may share the cache safely (read-only after warm-up).
    """

    name: str = "dds"

    def __init__(self, cache_path: Optional[str | Path] = None) -> None:
        # Import lazily so the package remains importable without endplay.
        try:
            from endplay.dds import calc_dd_table  # noqa: F401
            from endplay.types import Deal as _EDeal  # noqa: F401
        except Exception as exc:  # pragma: no cover - depends on env
            raise RuntimeError(
                "DDSEstimator requires the 'endplay' package "
                "(`pip install endplay`)."
            ) from exc
        self._calc = calc_dd_table
        self._EDeal = _EDeal
        self._cache_path: Optional[Path] = Path(cache_path) if cache_path else None
        self._memory: Dict[str, list] = {}
        if self._cache_path and self._cache_path.exists():
            try:
                with self._cache_path.open("rb") as f:
                    self._memory = pickle.load(f)
                log.info("DDS cache loaded: %d entries", len(self._memory))
            except Exception:
                log.warning("could not load DDS cache; starting empty")

    # ------------------------------------------------------------------
    def _table_rows(self, deal: Deal) -> list:
        key = _deal_hash(deal)
        rows = self._memory.get(key)
        if rows is not None:
            return rows
        ed = self._EDeal(_deal_to_pbn(deal))
        rows = self._calc(ed).to_list()
        # ``rows`` is [strain_index][seat_index] with strain order
        # [S, H, D, C, NT] and seats [N, E, S, W].
        self._memory[key] = rows
        return rows

    def estimate(self, deal: Deal, declarer: str, strain: Suit) -> int:
        rows = self._table_rows(deal)
        return int(rows[_SUIT_TO_INDEX[strain]][_SEATS.index(declarer)])

    def table(self, deal: Deal) -> Dict[Tuple[str, str], int]:
        rows = self._table_rows(deal)
        return {
            (seat, strain.symbol): int(rows[_SUIT_TO_INDEX[strain]][i])
            for strain in (Suit.SPADES, Suit.HEARTS, Suit.DIAMONDS,
                           Suit.CLUBS, Suit.NT)
            for i, seat in enumerate(_SEATS)
        }

    # ------------------------------------------------------------------
    def warm(self, deals) -> None:
        """Precompute DDS tables for ``deals`` (used by precompute_cache)."""
        for d in deals:
            self._table_rows(d)

    def save(self) -> None:
        if self._cache_path is None:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
        with tmp.open("wb") as f:
            pickle.dump(self._memory, f, protocol=4)
        os.replace(tmp, self._cache_path)

    @property
    def size(self) -> int:
        return len(self._memory)


# ---------------------------------------------------------------------------
# Default selection
# ---------------------------------------------------------------------------


_DEFAULT: Optional[TrickEstimator] = None


def get_default_estimator(cache_path: Optional[str | Path] = None) -> TrickEstimator:
    """Return a process-wide default estimator. DDS if available, else heuristic.

    The default cache path lives at ``~/.cache/bridge_evolve/dds.pkl`` so
    it survives across runs.
    """
    global _DEFAULT
    if _DEFAULT is not None and cache_path is None:
        return _DEFAULT
    if cache_path is None:
        cache_path = os.environ.get("BRIDGE_EVOLVE_DDS_CACHE",
                                    str(Path.home() / ".cache" / "bridge_evolve" / "dds.pkl"))
    try:
        est = DDSEstimator(cache_path=cache_path)
        if _DEFAULT is None:
            _DEFAULT = est
        return est
    except RuntimeError as exc:
        log.warning("DDS unavailable (%s); falling back to heuristic estimator. "
                    "Results will be inexact.", exc)
        if _DEFAULT is None:
            _DEFAULT = HeuristicEstimator()
        return _DEFAULT


def set_default_estimator(est: TrickEstimator) -> None:
    """Override the process-wide default (mostly for tests)."""
    global _DEFAULT
    _DEFAULT = est
