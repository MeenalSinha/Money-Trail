"""Deterministic one-to-one matching of detections to labelled instances (per pattern type).

A detection D and a labelled instance G are *eligible* when they share at least one transaction and
  |D and G| * 100 >= min_gt_coverage_percent    * |G|   (the detection covers enough of the instance)
  |D and G| * 100 >= min_detection_purity_percent * |D|  (enough of the detection belongs to the instance)
using integer arithmetic. Eligible pairs are assigned greedily, best first, ordered by
(larger overlap, smaller |D|+|G|, detection id, pattern id); each detection and each instance is used
at most once, so one detection can never count as a match for several instances. Greedy assignment is
deterministic but not guaranteed to maximize the number of matches in contrived overlap structures.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Sequence, Tuple


@dataclass(frozen=True)
class DetItem:
    detection_id: str
    pattern_type: str
    transaction_ids: FrozenSet[str]
    account_ids: FrozenSet[str]


@dataclass(frozen=True)
class TruthItem:
    pattern_id: str
    pattern_type: str
    transaction_ids: FrozenSet[str]
    account_ids: FrozenSet[str]


@dataclass
class MatchResult:
    pairs: List[Tuple[DetItem, TruthItem, int]]
    unmatched_detections: List[DetItem]
    unmatched_truths: List[TruthItem]
    ambiguous_detections: int       # detections eligible for more than one instance (resolved by the ordering rule)


def match_type(dets: Sequence[DetItem], truths: Sequence[TruthItem], min_cov_pct: int, min_pur_pct: int) -> MatchResult:
    eligible = []
    per_det: Dict[str, int] = {}
    for d in dets:
        for g in truths:
            ov = len(d.transaction_ids & g.transaction_ids)
            if ov and ov * 100 >= min_cov_pct * len(g.transaction_ids) and ov * 100 >= min_pur_pct * len(d.transaction_ids):
                eligible.append((-ov, len(d.transaction_ids) + len(g.transaction_ids), d.detection_id, g.pattern_id, d, g, ov))
                per_det[d.detection_id] = per_det.get(d.detection_id, 0) + 1
    eligible.sort(key=lambda e: e[:4])
    used_d, used_g, pairs = set(), set(), []
    for *_, d, g, ov in eligible:
        if d.detection_id in used_d or g.pattern_id in used_g:
            continue
        used_d.add(d.detection_id)
        used_g.add(g.pattern_id)
        pairs.append((d, g, ov))
    return MatchResult(
        pairs=pairs,
        unmatched_detections=sorted((d for d in dets if d.detection_id not in used_d), key=lambda d: d.detection_id),
        unmatched_truths=sorted((g for g in truths if g.pattern_id not in used_g), key=lambda g: g.pattern_id),
        ambiguous_detections=sum(1 for n in per_det.values() if n > 1))
