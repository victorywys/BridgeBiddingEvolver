"""Paired bootstrap CIs for IMP-delta evaluation.

We can't trust a single point estimate of "candidate − baseline avg IMPs"
because per-board IMP swings are heavy-tailed: a single 10-IMP slam swing
shifts the mean by 0.05 IMPs/board on a 200-board sample. The session
evaluator must distinguish "the patch is actually better" from "the
patch got lucky on a couple of slam boards".

This module provides the simplest defensible answer: a paired bootstrap
over the per-board deltas. Pairing is justified because both the
candidate and baseline see the *same* deal on every board (teams-of-four
duplication), so per-board noise (DDS-par variance, contract noise)
mostly cancels.

The resulting :class:`BootstrapResult` reports the mean delta, the
percentile CI at the requested level, and a one-sided p-value
``P(delta <= 0)`` under the bootstrap distribution.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import List, Sequence


@dataclass(frozen=True)
class BootstrapResult:
    mean: float
    ci_low: float
    ci_high: float
    n: int
    n_resamples: int
    level: float          # e.g. 0.95
    p_value_one_sided: float   # bootstrap P(delta <= 0)

    def as_dict(self) -> dict:
        return {
            "mean": round(self.mean, 4),
            "ci_low": round(self.ci_low, 4),
            "ci_high": round(self.ci_high, 4),
            "n": self.n,
            "n_resamples": self.n_resamples,
            "level": self.level,
            "p_value_one_sided": round(self.p_value_one_sided, 4),
        }

    def significant_positive(self) -> bool:
        """True iff the CI lies strictly above zero."""
        return self.ci_low > 0.0


def paired_bootstrap(deltas: Sequence[float], *, n_resamples: int = 2000,
                     level: float = 0.95, seed: int = 0) -> BootstrapResult:
    """Compute a percentile-bootstrap CI for the mean of ``deltas``.

    Parameters
    ----------
    deltas
        Per-board paired differences (candidate IMPs − baseline IMPs).
    n_resamples
        Number of bootstrap resamples. 2000 is enough for stable two-
        decimal CIs at typical N. Cost is ``O(n_resamples * n)``.
    level
        Confidence level (e.g. 0.95 for a 95% CI).
    seed
        Bootstrap RNG seed for reproducibility.
    """
    deltas = list(deltas)
    n = len(deltas)
    if n == 0:
        return BootstrapResult(0.0, 0.0, 0.0, 0, 0, level, 1.0)
    rng = random.Random(seed)
    means: List[float] = []
    le_zero = 0
    for _ in range(n_resamples):
        s = 0.0
        for _i in range(n):
            s += deltas[rng.randrange(n)]
        m = s / n
        means.append(m)
        if m <= 0.0:
            le_zero += 1
    means.sort()
    alpha = (1.0 - level) / 2.0
    lo_idx = max(0, int(alpha * n_resamples))
    hi_idx = min(n_resamples - 1, int((1.0 - alpha) * n_resamples))
    mean = sum(deltas) / n
    return BootstrapResult(
        mean=mean,
        ci_low=means[lo_idx],
        ci_high=means[hi_idx],
        n=n,
        n_resamples=n_resamples,
        level=level,
        p_value_one_sided=le_zero / n_resamples,
    )
