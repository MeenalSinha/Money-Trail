"""Shared detection models and identifier policy.

Identifier policy: every id is "<prefix>:" + the first 32 hex characters of a SHA-256 over the
named parts joined with a unit separator. Identical inputs always give identical ids, so reruns
reconcile instead of duplicating; a different dataset namespace, detector rule version or
evidence set gives a different id. Random identifiers are never used.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

PATTERN_ORDER = ("circular", "pass_through", "fan_in", "fan_out")


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}:{digest}"


@dataclass(frozen=True)
class TxRecord:
    """One transaction as read from the graph (canonical units: minor-unit integer, epoch ms UTC)."""
    transaction_id: str
    source_account: str
    destination_account: str
    amount: int
    currency: str
    timestamp: int

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)

    @property
    def sort_key(self) -> Tuple[int, str]:
        return (self.timestamp, self.transaction_id)


@dataclass(frozen=True)
class Scope:
    """What an investigation starts from. Built by the engine from validated graph records."""
    anchor_type: str                       # "transaction" | "account" | "dataset"
    anchor_id: Optional[str]
    namespace: Optional[str]
    anchor_tx_ids: Tuple[str, ...] = ()    # transaction anchor: (id,); account anchor: its incident transactions
    tx_source: Optional[str] = None        # transaction anchor only
    tx_destination: Optional[str] = None   # transaction anchor only
    anchor_truncated: bool = False         # account anchor had more incident transactions than the cap


@dataclass(frozen=True)
class Detection:
    detection_id: str
    pattern_type: str
    anchor_id: Optional[str]
    dataset_namespace: Optional[str]
    account_ids: Tuple[str, ...]
    transaction_ids: Tuple[str, ...]
    ordered_path: Tuple[TxRecord, ...]
    criteria: Dict[str, Any]
    evidence_refs: Tuple[str, ...]
    truncated: bool
    warnings: Tuple[str, ...]
    rule_version: str
    created_at: int
    roles: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "detection_id": self.detection_id, "pattern_type": self.pattern_type, "anchor_id": self.anchor_id,
            "dataset_namespace": self.dataset_namespace, "account_ids": list(self.account_ids),
            "transaction_ids": list(self.transaction_ids),
            "ordered_path": [t.to_dict() for t in self.ordered_path], "criteria": self.criteria,
            "evidence_refs": list(self.evidence_refs), "truncated": self.truncated,
            "warnings": list(self.warnings), "rule_version": self.rule_version,
            "created_at": self.created_at, "roles": self.roles,
        }


@dataclass
class DetectorOutput:
    detections: List[Detection] = field(default_factory=list)
    truncated: bool = False
    warnings: List[str] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)


def canonical_rotation(txs: Sequence[TxRecord]) -> List[TxRecord]:
    """Rotate a directed cycle so it starts at its earliest (timestamp, id) transaction.

    Only rotation is applied. The order is never reversed: a reversed cycle is a different
    cycle and is reported only if it exists in the graph on its own.
    """
    if not txs:
        return []
    start = min(range(len(txs)), key=lambda i: txs[i].sort_key)
    return list(txs[start:]) + list(txs[:start])


def detection_key(pattern_type: str, ordered: Sequence[TxRecord], focus: Optional[str]) -> str:
    """Canonical identity of a detection (what makes two detections 'the same')."""
    if pattern_type in ("fan_in", "fan_out"):
        return json.dumps([focus, sorted(t.transaction_id for t in ordered)])
    return json.dumps([t.transaction_id for t in ordered])   # circular is already canonically rotated


def make_detection_id(namespace: Optional[str], pattern_type: str, rule_version: str, key: str) -> str:
    return stable_id("det", namespace or "", pattern_type, rule_version, key)


def evidence_id_for(detection_id: str) -> str:
    return stable_id("evd", detection_id)
