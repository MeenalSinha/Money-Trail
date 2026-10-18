"""Exact metric arithmetic (fractions). Undefined values are None, never silently 0 or 1.

precision = TP / (TP + FP)            undefined (None) when nothing was detected
recall    = TP / (TP + FN)            undefined (None) when there are no labelled instances
F1        = 2PR / (P + R)             None when precision or recall is undefined; 0 when both are
                                      defined and P + R = 0
coverage  = |labelled items found in any detection of the type| / |labelled items of the type|
            (None when there are no labelled items)
"""
from __future__ import annotations

from fractions import Fraction
from typing import Any, Dict, Optional


def ratio(num: int, den: int) -> Optional[Fraction]:
    return None if den == 0 else Fraction(num, den)


def f1(p: Optional[Fraction], r: Optional[Fraction]) -> Optional[Fraction]:
    if p is None or r is None:
        return None
    if p + r == 0:
        return Fraction(0)
    return 2 * p * r / (p + r)


def as_json(x: Optional[Fraction], counts: Optional[tuple] = None) -> Optional[Dict[str, Any]]:
    """JSON form of a metric. `counts=(numerator, denominator)` keeps the unreduced counts (2/2, not 1/1)."""
    if x is None:
        return None
    frac = f"{counts[0]}/{counts[1]}" if counts else f"{x.numerator}/{x.denominator}"
    return {"value": round(float(x), 6), "fraction": frac}
