"""Loads the evaluation labels. Imported only by the evaluation path; detectors never see this module."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

from money_trail.exceptions import DatasetConfigError
from money_trail.investigation.detectors.models import PATTERN_ORDER

SUPPORTED_FORMAT_VERSION = 1


@dataclass(frozen=True)
class GTPattern:
    pattern_id: str
    pattern_type: str
    transaction_ids: Tuple[str, ...]
    account_ids: Tuple[str, ...]


@dataclass(frozen=True)
class GroundTruth:
    namespace: str
    patterns: Tuple[GTPattern, ...]


def _id_list(v, where: str) -> Tuple[str, ...]:
    if not (isinstance(v, list) and v and all(isinstance(x, str) and x for x in v)):
        raise DatasetConfigError(f"{where} must be a non-empty list of identifiers.")
    return tuple(v)


def parse_ground_truth(raw: Dict, expected_namespace: str) -> GroundTruth:
    if not isinstance(raw, dict) or raw.get("format_version") != SUPPORTED_FORMAT_VERSION:
        raise DatasetConfigError(f"ground truth must be a JSON object with format_version {SUPPORTED_FORMAT_VERSION}.")
    if raw.get("namespace") != expected_namespace:
        raise DatasetConfigError("ground truth namespace does not match the evaluated namespace.")
    pats: List[GTPattern] = []
    seen = set()
    for i, p in enumerate(raw.get("patterns") or []):
        where = f"patterns[{i}]"
        if not isinstance(p, dict) or p.get("type") not in PATTERN_ORDER:
            raise DatasetConfigError(f"{where}.type must be one of: {', '.join(PATTERN_ORDER)}.")
        pid = p.get("pattern_id")
        if not isinstance(pid, str) or not pid or pid in seen:
            raise DatasetConfigError(f"{where}.pattern_id must be a unique non-empty string.")
        seen.add(pid)
        pats.append(GTPattern(pid, p["type"], _id_list(p.get("transaction_ids"), f"{where}.transaction_ids"),
                              _id_list(p.get("account_ids"), f"{where}.account_ids")))
    return GroundTruth(expected_namespace, tuple(pats))


def load_ground_truth(path, expected_namespace: str) -> GroundTruth:
    p = Path(path)
    if not p.is_file():
        raise DatasetConfigError(f"Ground truth file not found: {p.name}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DatasetConfigError(f"Ground truth is not valid JSON ({type(exc).__name__}).") from None
    return parse_ground_truth(raw, expected_namespace)
