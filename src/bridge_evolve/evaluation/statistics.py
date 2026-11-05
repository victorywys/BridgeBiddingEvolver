"""Paired board-level statistics; each two-table pair is one sample."""

import math
import random
import statistics


def paired_summary(values: list[int], *, seed: int, resamples: int = 2000) -> dict[str, object]:
    if not values or type(seed) is not int or type(resamples) is not int or resamples < 100:
        raise ValueError("Statistics require nonempty paired results and >=100 frozen resamples.")
    rng = random.Random(seed)
    count = len(values)
    means = sorted(sum(values[rng.randrange(count)] for _ in range(count)) / count for _ in range(resamples))
    low = means[math.floor((resamples - 1) * .025)]
    high = means[math.ceil((resamples - 1) * .975)]
    return {
        "boards": count, "tables": 2 * count,
        "mean_imp": statistics.mean(values), "total_imp": sum(values),
        "ci95": [low, high], "ci_method": "paired-base-family-bootstrap-percentile",
        "bootstrap_resamples": resamples, "bootstrap_seed": seed,
        "a_wins": sum(value > 0 for value in values),
        "b_wins": sum(value < 0 for value in values),
        "ties": sum(value == 0 for value in values),
        "standard_deviation": statistics.stdev(values) if count > 1 else 0.0,
    }
