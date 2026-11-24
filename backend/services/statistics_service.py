"""Statistical comparison: descriptive + resampling-based (honest small-sample labels).

Paired comparisons because the same scenarios run against both architectures.
Results are labeled exploratory / promising / confirmed_within_suite /
inconclusive / rejected / safety_rejected — never 'statistically significant'
on tiny samples.
"""
from __future__ import annotations

import random
import statistics as pystats


def describe(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": None, "median": None, "std": None}
    return {
        "n": len(values),
        "mean": round(pystats.fmean(values), 3),
        "median": round(pystats.median(values), 3),
        "std": round(pystats.stdev(values), 3) if len(values) > 1 else 0.0,
        "min": min(values), "max": max(values),
    }


def paired_deltas(control: dict[str, float], variant: dict[str, float]) -> list[float]:
    """Per-scenario deltas (variant - control) over shared scenario keys."""
    return [variant[k] - control[k] for k in sorted(set(control) & set(variant))]


def bootstrap_ci(deltas: list[float], n_boot: int = 2000, alpha: float = 0.05,
                 seed: int = 1234) -> dict:
    if not deltas:
        return {"lower": None, "upper": None, "n_boot": 0}
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        sample = [rng.choice(deltas) for _ in deltas]
        means.append(pystats.fmean(sample))
    means.sort()
    lo_idx = int((alpha / 2) * n_boot)
    hi_idx = int((1 - alpha / 2) * n_boot) - 1
    return {"lower": round(means[lo_idx], 3), "upper": round(means[hi_idx], 3),
            "n_boot": n_boot, "alpha": alpha,
            "method": "nonparametric bootstrap of paired mean delta"}


def effect_size(deltas: list[float]) -> float | None:
    """Cohen's d for paired differences."""
    if len(deltas) < 2:
        return None
    sd = pystats.stdev(deltas)
    if sd == 0:
        return None
    return round(pystats.fmean(deltas) / sd, 3)


def win_tie_loss(deltas: list[float], tie_band: float = 1.0) -> dict:
    wins = sum(1 for d in deltas if d > tie_band)
    losses = sum(1 for d in deltas if d < -tie_band)
    return {"wins": wins, "ties": len(deltas) - wins - losses, "losses": losses}


def result_label(deltas: list[float], min_effect_points: float,
                 hard_constraint_regression: bool,
                 repetitions: int) -> str:
    if hard_constraint_regression:
        return "safety_rejected"
    if not deltas:
        return "inconclusive"
    mean_delta = pystats.fmean(deltas)
    wtl = win_tie_loss(deltas)
    ci = bootstrap_ci(deltas)
    if mean_delta <= -min_effect_points and wtl["losses"] > wtl["wins"]:
        return "rejected"
    if mean_delta >= min_effect_points and wtl["wins"] > wtl["losses"]:
        if repetitions >= 3 and ci["lower"] is not None and ci["lower"] > 0:
            return "confirmed_within_suite"
        if len(deltas) >= 4:
            return "promising"
        return "exploratory"
    return "inconclusive"


def single_scenario_driver(control_scores: dict[str, dict[str, float]],
                           variant_scores: dict[str, dict[str, float]]) -> bool:
    """True when an overall improvement depends on one scenario.

    Averages each scenario's delta across all metrics, then checks whether the
    overall mean delta is positive only because of the single best scenario:
    if dropping that scenario leaves a mean delta of zero or less, the "win"
    is not robust and gets flagged for review.
    """
    per_scenario: dict[str, list[float]] = {}
    for metric, c in control_scores.items():
        v = variant_scores.get(metric, {})
        for key in set(c) & set(v):
            per_scenario.setdefault(key, []).append(v[key] - c[key])
    if len(per_scenario) < 2:
        return False
    means = sorted(pystats.fmean(d) for d in per_scenario.values())
    if pystats.fmean(means) <= 0:
        return False
    return pystats.fmean(means[:-1]) <= 0


def compare_arms(control_scores: dict[str, dict[str, float]],
                 variant_scores: dict[str, dict[str, float]],
                 min_effect_points: float = 3.0,
                 hard_constraint_regression: bool = False,
                 repetitions: int = 2) -> dict:
    """control_scores/variant_scores: {metric_key: {scenario_key: mean_score}}."""
    per_metric = {}
    all_deltas: list[float] = []
    for metric in sorted(set(control_scores) | set(variant_scores)):
        c = control_scores.get(metric, {})
        v = variant_scores.get(metric, {})
        deltas = paired_deltas(c, v)
        all_deltas.extend(deltas)
        per_metric[metric] = {
            "control": describe(list(c.values())),
            "variant": describe(list(v.values())),
            "paired_deltas": [round(d, 3) for d in deltas],
            "mean_delta": round(pystats.fmean(deltas), 3) if deltas else None,
            "bootstrap_ci": bootstrap_ci(deltas),
            "effect_size_d": effect_size(deltas),
            "win_tie_loss": win_tie_loss(deltas),
        }
    label = result_label(all_deltas, min_effect_points,
                         hard_constraint_regression, repetitions)
    single_driver = single_scenario_driver(control_scores, variant_scores)
    return {
        "per_metric": per_metric,
        "overall": {
            "mean_delta": round(pystats.fmean(all_deltas), 3) if all_deltas else None,
            "bootstrap_ci": bootstrap_ci(all_deltas),
            "effect_size_d": effect_size(all_deltas),
            "win_tie_loss": win_tie_loss(all_deltas),
            "result_label": label,
            "single_scenario_driver_warning": single_driver,
            "sample_note": ("Small-sample research result; labels are honest "
                            "descriptions, not claims of conventional significance."),
        },
    }


def pareto_frontier(points: list[dict], quality_key: str = "quality",
                    cost_key: str = "cost") -> list[dict]:
    """Non-dominated set: higher quality, lower cost preferred."""
    frontier = []
    for p in points:
        dominated = any(
            q[quality_key] >= p[quality_key] and q[cost_key] <= p[cost_key]
            and (q[quality_key] > p[quality_key] or q[cost_key] < p[cost_key])
            for q in points if q is not p)
        p = dict(p)
        p["dominated"] = dominated
        frontier.append(p)
    return frontier
