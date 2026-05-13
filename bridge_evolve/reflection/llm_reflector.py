"""Pluggable reflectors.

A *reflector* turns a batch of (record, diagnoses) tuples into a list of
:class:`Patch` objects. Two implementations are provided:

* :class:`HeuristicReflector` — the deterministic fallback used by tests
  and offline runs. Wraps :func:`propose_patches`.
* :class:`LLMReflector` — accepts a ``llm_fn(prompt)->str`` callable and
  asks it to emit a YAML patch. The output is validated through
  :meth:`Patch.from_yaml`, so a malformed reply is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

from ..agent.base import AuctionRecord
from ..dsl.schema import BiddingSystem
from .analyzer import AuctionDiagnosis, aggregate_diagnoses
from .patch import Patch, propose_patches


@dataclass
class HeuristicReflector:
    """Deterministic reflector backed by :func:`propose_patches`."""

    def reflect(self, system: BiddingSystem,
                records_and_diags: Sequence[Tuple[AuctionRecord, List[AuctionDiagnosis]]]) -> List[Patch]:
        all_diags: List[AuctionDiagnosis] = []
        for _, diags in records_and_diags:
            all_diags.extend(diags)
        return propose_patches(system, all_diags)


LLMCallable = Callable[[str], str]


@dataclass
class LLMReflector:
    """LLM-backed reflector. The model writes a YAML patch.

    Falls back to the heuristic reflector if ``llm_fn`` is ``None`` or
    raises, so the rest of the pipeline keeps working without network
    access.
    """
    llm_fn: Optional[LLMCallable] = None
    fallback: HeuristicReflector = field(default_factory=HeuristicReflector)

    def reflect(self, system: BiddingSystem,
                records_and_diags) -> List[Patch]:
        if self.llm_fn is None:
            return self.fallback.reflect(system, records_and_diags)
        diag_summary = aggregate_diagnoses(d for _, ds in records_and_diags for d in ds)
        prompt = _build_prompt(system, diag_summary)
        try:
            raw = self.llm_fn(prompt)
            patch = Patch.from_yaml(raw)
            if not patch.operations:
                return self.fallback.reflect(system, records_and_diags)
            return [patch]
        except Exception:
            return self.fallback.reflect(system, records_and_diags)


def _build_prompt(system: BiddingSystem, diag_summary) -> str:
    return (
        "You are revising a bridge bidding system documented entirely in YAML. "
        "Emit a single Patch object (YAML) using the documented schema. "
        "Do NOT include explanations outside the YAML. "
        f"Current system: {system.name}@{system.version}. "
        f"Diagnosis counts: {dict(diag_summary)}. "
        "Operations supported: add_convention, remove_convention, "
        "modify_convention, add_opening, remove_opening, add_principle."
    )
