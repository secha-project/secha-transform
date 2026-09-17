from __future__ import annotations

import pytest
from codegen.metrics import micro, pass_at_k


@pytest.mark.parametrize(
    ("n", "c", "k", "expected"),
    [
        (5, 0, 1, 0.0),
        (5, 5, 1, 1.0),
        (5, 1, 1, 0.2),
        (5, 1, 5, 1.0),
        (5, 2, 2, 0.7),  # 1 - C(3, 2) / C(5, 2)
        (10, 3, 1, 0.3),
    ],
)
def test_pass_at_k_matches_the_unbiased_estimator(n, c, k, expected):
    assert pass_at_k(n, c, k) == pytest.approx(expected)


@pytest.mark.parametrize(("n", "c", "k"), [(5, 6, 1), (5, -1, 1), (5, 1, 0), (5, 1, 6)])
def test_pass_at_k_rejects_impossible_arguments(n, c, k):
    with pytest.raises(ValueError):
        pass_at_k(n, c, k)


def test_micro_average_weighs_large_cases_more():
    scores = [
        {"exact_rows": 90, "oracle_rows": 100, "script_rows": 100},
        {"exact_rows": 0, "oracle_rows": 10, "script_rows": 10},
    ]
    result = micro(scores)
    assert result["precision"] == pytest.approx(90 / 110)
    assert result["recall"] == pytest.approx(90 / 110)
