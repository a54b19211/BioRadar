"""Descriptive exposure statistics and finite-state batch burst detection."""

import math


def exposure_metrics(current: int, baseline: int, current_total: int, baseline_total: int) -> dict:
    current_rate = current / current_total if current_total else None
    baseline_rate = baseline / baseline_total if baseline_total else None
    ratio = ((current + 0.5) / (current_total + 1)) / ((baseline + 0.5) / (baseline_total + 1))
    return {
        "current_count": current,
        "baseline_count": baseline,
        "current_denominator": current_total,
        "baseline_denominator": baseline_total,
        "current_rate": current_rate,
        "baseline_rate": baseline_rate,
        "rate_difference": current_rate - baseline_rate
        if current_rate is not None and baseline_rate is not None
        else None,
        "smoothed_rate_ratio": round(ratio, 8),
        "r2_baseline_monthly": round(baseline * 30 / 365, 8),
        "r2_growth": round((current + 1) / (baseline * 30 / 365 + 1), 8),
        "statistical_inference": "descriptive_only",
    }


def batch_bursts(
    relevant: list[int],
    exposure: list[int],
    *,
    scale: float = 2,
    penalty: float = 1,
    states: int = 3,
) -> dict:
    """Kleinberg-style batch-binomial DP; missing exposure breaks observations.

    All batches supplied must be an as-of prefix. Scores are costs, not p-values.
    """
    if len(relevant) != len(exposure) or any(not 0 <= r <= d for r, d in zip(relevant, exposure)):
        raise ValueError("burst counts must satisfy 0 <= relevant <= exposure")
    total = sum(exposure)
    if not total:
        return {"states": [None] * len(exposure), "intervals": [], "reason": "no_exposure"}
    base = (sum(relevant) + 0.5) / (total + 1)
    probabilities = [min(base * scale**i, 1 - 1e-9) for i in range(states)]
    transition = penalty * math.log(max(2, len(exposure)))
    costs = [0.0] + [math.inf] * (states - 1)
    parents = []
    for relevant_count, exposed_count in zip(relevant, exposure):
        next_costs, path = [], []
        for state, probability in enumerate(probabilities):
            emissions = -relevant_count * math.log(probability)
            emissions -= (exposed_count - relevant_count) * math.log1p(-probability)
            options = [cost + max(0, state - prior) * transition for prior, cost in enumerate(costs)]
            best = min(range(states), key=lambda x: (options[x], x))
            next_costs.append(options[best] + emissions)
            path.append(best)
        parents.append(path)
        costs = next_costs
    state = min(range(states), key=lambda x: (costs[x], x))
    sequence = []
    for index in reversed(range(len(exposure))):
        sequence.append(state if exposure[index] else None)
        state = parents[index][state]
    sequence.reverse()
    intervals = []
    start = None
    for index, state in enumerate([*sequence, 0]):
        if state is not None and state > 0 and start is None:
            start = index
        if (state is None or state == 0) and start is not None:
            intervals.append({"start_bin": start, "end_bin": index - 1})
            start = None
    return {
        "states": sequence,
        "intervals": intervals,
        "cost": round(min(costs), 8),
        "base_probability": round(base, 8),
        "probabilities": probabilities,
        "statistical_inference": "descriptive_only",
    }
