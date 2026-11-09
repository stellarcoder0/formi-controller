"""Deterministic statistics. No randomness and no LLM anywhere in winner selection.

Everything here is a classic two-proportion z-test (normal approximation). It is fine at the sample
sizes we act on (we never test with fewer than a few hundred calls per arm) and easy to explain.
"""
from __future__ import annotations

import math


def rate(s: int, n: int) -> float:
    return s / n if n else 0.0


def z_two_prop(s1: int, n1: int, s2: int, n2: int) -> float:
    """z statistic for p1 - p2 using the pooled standard error."""
    if n1 <= 0 or n2 <= 0:
        return 0.0
    p = (s1 + s2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0
    return (s1 / n1 - s2 / n2) / se


def p_upper(z: float) -> float:
    """P(Z > z) for a standard normal."""
    return 0.5 * math.erfc(z / math.sqrt(2))


def p_greater(s1: int, n1: int, s2: int, n2: int) -> float:
    """One-sided p-value for H1: p1 > p2."""
    if n1 <= 0 or n2 <= 0:
        return 1.0
    return p_upper(z_two_prop(s1, n1, s2, n2))


def p_rate_above(k: int, n: int, p0: float) -> float:
    """One-sided p-value that k successes in n trials come from a rate above p0 (normal approx)."""
    if n <= 0 or p0 <= 0 or p0 >= 1:
        return 1.0
    z = (k - 0.5 - n * p0) / math.sqrt(n * p0 * (1 - p0))
    return p_upper(z)


def tv_distance(a: dict, b: dict) -> float:
    """Total-variation distance between two {option: weight} distributions (normalised first)."""
    sa, sb = sum(a.values()) or 1.0, sum(b.values()) or 1.0
    keys = set(a) | set(b)
    return 0.5 * sum(abs(a.get(k, 0) / sa - b.get(k, 0) / sb) for k in keys)


def normalise(d: dict) -> dict:
    s = sum(d.values())
    return {k: v / s for k, v in d.items() if v > 0} if s > 0 else dict(d)


def step_towards(cur: dict, target: dict, max_tv: float) -> dict:
    """Move from `cur` towards `target` by at most `max_tv` total-variation distance.

    TV is linear along the straight line between two distributions, so scaling the move by
    max_tv / TV(cur, target) lands exactly on the limit.
    """
    cur, target = normalise(cur), normalise(target)
    d = tv_distance(cur, target)
    if d <= max_tv:
        return target
    a = max_tv / d
    keys = set(cur) | set(target)
    out = {k: (1 - a) * cur.get(k, 0) + a * target.get(k, 0) for k in keys}
    return {k: round(v, 6) for k, v in out.items() if v > 1e-6}
