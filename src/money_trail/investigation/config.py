"""Validated detection configuration (JSON), following the generator/ingestion config conventions.

Every business threshold, traversal limit and persistence option is explicit. Unknown keys are
errors, missing mandatory settings are errors, nothing is defaulted. Units: time values are integer
milliseconds, amounts are integer minor units, ratios are integer basis points (10000 = 100%),
and "hops" counts transactions (a hop is two graph edges: transaction -> account -> transaction).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from money_trail.exceptions import DatasetConfigError
from money_trail.investigation.detectors.base import Limits
from money_trail.investigation.detectors.registry import DETECTORS, ORDER
from money_trail.investigation.detectors.settings_util import no_extra, req_bool, req_int

# Operational safety ceilings (not business thresholds): configured limits may not exceed these.
DETECTION_CEILINGS = {"candidates": 100_000, "detections": 100_000, "anchor_transactions": 10_000,
                      "evidence": 10_000, "timeout_ms": 3_600_000}


@dataclass(frozen=True)
class EvaluationSettings:
    min_gt_coverage_percent: int       # share of a labelled instance's transactions a detection must cover
    min_detection_purity_percent: int  # share of a detection's transactions that must belong to the instance


@dataclass(frozen=True)
class DetectionConfig:
    rule_set_name: str
    rule_set_version: str
    limits: Limits
    settings: Mapping[str, Any]          # detector name -> parsed settings (all four are present)
    enabled: Mapping[str, bool]
    hypothesis_findings: bool
    evaluation: EvaluationSettings
    path: Optional[Path]

    def enabled_names(self) -> Tuple[str, ...]:
        return tuple(n for n in ORDER if self.enabled[n])

    def rule_version(self, name: str) -> str:
        """Identity of the rule as applied: detector + rule set + a hash of its settings and result caps.

        Changing any threshold or cap changes this string, so findings produced under a different
        policy are new findings rather than silent overwrites. Timeouts and evaluation settings do
        not alter which patterns exist, so they are excluded.
        """
        caps = {"c": self.limits.max_candidate_paths, "d": self.limits.max_detections_per_detector,
                "a": self.limits.max_anchor_transactions, "e": self.limits.max_evidence_transactions}
        payload = json.dumps({"s": self.settings[name].as_dict(), "caps": caps}, sort_keys=True)
        h = hashlib.sha256(payload.encode()).hexdigest()[:12]
        return f"{name}:{self.rule_set_name}@{self.rule_set_version}+{h}"

    def fingerprint(self) -> str:
        """Identity of the whole enabled rule configuration (used for investigation identity)."""
        versions = {n: self.rule_version(n) for n in self.enabled_names()}
        return hashlib.sha256(json.dumps(versions, sort_keys=True).encode()).hexdigest()[:16]


def _percent(raw: Mapping, key: str, where: str) -> int:
    return req_int(raw, key, where, 1, 100)


def parse_detection_config(raw: Mapping[str, Any], path: Optional[Path] = None) -> DetectionConfig:
    if not isinstance(raw, dict):
        raise DatasetConfigError("Detection config must be a JSON object.")
    no_extra(raw, ("rule_set", "limits", "detectors", "persistence", "evaluation"), "detection config")
    for k in ("rule_set", "limits", "detectors", "persistence", "evaluation"):
        if not isinstance(raw.get(k), dict):
            raise DatasetConfigError(f"{k} is required and must be an object.")

    rs = raw["rule_set"]
    no_extra(rs, ("name", "version"), "rule_set")
    name, version = rs.get("name"), rs.get("version")
    for key, val in (("name", name), ("version", version)):
        if not (isinstance(val, str) and val and val.isprintable() and "@" not in val and "+" not in val and ":" not in val):
            raise DatasetConfigError(f"rule_set.{key} must be a non-empty string without '@', '+' or ':'.")

    lim = raw["limits"]
    no_extra(lim, ("max_candidate_paths", "max_detections_per_detector", "max_anchor_transactions",
                   "max_evidence_transactions", "query_timeout_ms"), "limits")
    timeout = lim.get("query_timeout_ms", "missing")
    if timeout == "missing":
        raise DatasetConfigError("limits.query_timeout_ms is required (use null for no server-side timeout).")
    if timeout is not None:
        timeout = req_int(lim, "query_timeout_ms", "limits", 1, DETECTION_CEILINGS["timeout_ms"])
    limits = Limits(
        req_int(lim, "max_candidate_paths", "limits", 1, DETECTION_CEILINGS["candidates"]),
        req_int(lim, "max_detections_per_detector", "limits", 1, DETECTION_CEILINGS["detections"]),
        req_int(lim, "max_anchor_transactions", "limits", 1, DETECTION_CEILINGS["anchor_transactions"]),
        req_int(lim, "max_evidence_transactions", "limits", 2, DETECTION_CEILINGS["evidence"]),
        timeout)

    dets = raw["detectors"]
    unknown = sorted(set(dets) - set(DETECTORS))
    if unknown:
        raise DatasetConfigError(f"detectors has unknown detector names: {', '.join(unknown)}. "
                                 f"Supported: {', '.join(ORDER)}.")
    settings, enabled = {}, {}
    for n in ORDER:
        if n not in dets or not isinstance(dets[n], dict):
            raise DatasetConfigError(f"detectors.{n} is required (set \"enabled\": false to turn it off).")
        body = dict(dets[n])
        where = f"detectors.{n}"
        if "enabled" not in body:
            raise DatasetConfigError(f"{where}.enabled is required.")
        enabled[n] = req_bool(body, "enabled", where)
        body.pop("enabled")
        if enabled[n] or body:
            settings[n] = DETECTORS[n].parse_settings(body, where)    # validates strictly, even when disabled
        else:
            settings[n] = _Disabled()
    for n in ORDER:
        if enabled[n] and hasattr(settings[n], "max_hops") and settings[n].max_hops > limits.max_evidence_transactions:
            raise DatasetConfigError(f"limits.max_evidence_transactions must be at least detectors.{n}.max_hops.")

    per = raw["persistence"]
    no_extra(per, ("hypothesis_findings",), "persistence")
    hyp = req_bool(per, "hypothesis_findings", "persistence")

    ev = raw["evaluation"]
    no_extra(ev, ("match",), "evaluation")
    if not isinstance(ev.get("match"), dict):
        raise DatasetConfigError("evaluation.match is required and must be an object.")
    m = ev["match"]
    no_extra(m, ("min_gt_coverage_percent", "min_detection_purity_percent"), "evaluation.match")
    evaluation = EvaluationSettings(_percent(m, "min_gt_coverage_percent", "evaluation.match"),
                                    _percent(m, "min_detection_purity_percent", "evaluation.match"))
    return DetectionConfig(name, version, limits, settings, enabled, hyp, evaluation, path)


class _Disabled:
    """Placeholder settings for a detector that is turned off and has no settings."""
    def as_dict(self) -> Dict[str, Any]:
        return {"enabled": False}


def load_detection_config(path) -> DetectionConfig:
    p = Path(path)
    if not p.is_file():
        raise DatasetConfigError(f"Detection config not found: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DatasetConfigError(f"Detection config is not valid JSON ({type(exc).__name__}).") from None
    return parse_detection_config(raw, p.resolve())
