"""Aggregate per-case scores into the numbers the experiment reports.

pass@k uses the unbiased estimator of Chen et al. (2021): with n samples of which c are
correct, the probability that at least one of k samples drawn without replacement is correct
is 1 - C(n - c, k) / C(n, k). Reporting pass@1 from several samples at a non-zero temperature
is more stable than a single greedy sample, which can land on either side of a threshold by
chance.
"""

from __future__ import annotations

from collections.abc import Iterable
from math import comb
from statistics import mean, median


def pass_at_k(n: int, c: int, k: int) -> float:
    """Probability that at least one of k samples is correct, given c of n were."""
    if not 0 <= c <= n:
        raise ValueError(f"need 0 <= c <= n, got c={c}, n={n}")
    if not 1 <= k <= n:
        raise ValueError(f"need 1 <= k <= n, got k={k}, n={n}")
    if n - c < k:
        return 1.0
    return 1.0 - comb(n - c, k) / comb(n, k)


def micro(scores: Iterable[dict[str, float]]) -> dict[str, float]:
    """Row precision, recall and F1 summed over cases, so large inputs weigh more."""
    exact = oracle = produced = 0.0
    for score in scores:
        exact += score["exact_rows"]
        oracle += score["oracle_rows"]
        produced += score["script_rows"]
    precision = exact / produced if produced else 0.0
    recall = exact / oracle if oracle else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def safe_mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return mean(items) if items else None


def safe_median(values: Iterable[float]) -> float | None:
    items = list(values)
    return median(items) if items else None
