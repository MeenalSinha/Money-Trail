"""Strict parsing helpers for detector settings: every key is required, unknown keys are errors."""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from money_trail.config.settings import QUERY_CEILINGS
from money_trail.exceptions import DatasetConfigError

MAX_MS = 10 * 365 * 86_400_000          # ten years: sanity bound for time settings
MAX_BPS = 1_000_000


def no_extra(raw: Dict[str, Any], allowed: Iterable[str], where: str) -> None:
    extra = sorted(set(raw) - set(allowed))
    if extra:
        raise DatasetConfigError(f"{where} has unknown keys: {', '.join(extra)}.")


def req_int(raw: Dict[str, Any], key: str, where: str, lo: int, hi: int) -> int:
    if key not in raw:
        raise DatasetConfigError(f"{where}.{key} is required.")
    v = raw[key]
    if isinstance(v, bool) or not isinstance(v, int) or not (lo <= v <= hi):
        raise DatasetConfigError(f"{where}.{key} must be an integer between {lo} and {hi}.")
    return v


def req_bool(raw: Dict[str, Any], key: str, where: str) -> bool:
    if key not in raw:
        raise DatasetConfigError(f"{where}.{key} is required.")
    if not isinstance(raw[key], bool):
        raise DatasetConfigError(f"{where}.{key} must be true or false.")
    return raw[key]


def hops_range(raw: Dict[str, Any], where: str, min_floor: int) -> tuple:
    lo = req_int(raw, "min_hops", where, min_floor, QUERY_CEILINGS["hops"])
    hi = req_int(raw, "max_hops", where, min_floor, QUERY_CEILINGS["hops"])
    if lo > hi:
        raise DatasetConfigError(f"{where}.min_hops must not exceed max_hops.")
    return lo, hi


def nullable_retention(raw: Dict[str, Any], key: str, where: str) -> Optional[Dict[str, int]]:
    """Explicit null disables the amount criterion; otherwise {min_bps, max_bps} (basis points of the prior amount)."""
    if key not in raw:
        raise DatasetConfigError(f"{where}.{key} is required (use null for no amount criterion).")
    v = raw[key]
    if v is None:
        return None
    if not isinstance(v, dict):
        raise DatasetConfigError(f"{where}.{key} must be null or an object with min_bps and max_bps.")
    no_extra(v, ("min_bps", "max_bps"), f"{where}.{key}")
    lo = req_int(v, "min_bps", f"{where}.{key}", 0, MAX_BPS)
    hi = req_int(v, "max_bps", f"{where}.{key}", 1, MAX_BPS)
    if lo > hi:
        raise DatasetConfigError(f"{where}.{key}.min_bps must not exceed max_bps.")
    return {"min_bps": lo, "max_bps": hi}
