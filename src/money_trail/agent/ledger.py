"""The evidence ledger: every record the model was actually shown, nothing else.

The final report may reference only identifiers that are in the ledger, and every factual value in it is copied from
the ledger (which holds database-read values), never from model text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Set

from money_trail.agent.tools.base import DetectorRun, Facts
from money_trail.investigation.detectors.models import Detection, evidence_id_for


@dataclass
class LedgerDetection:
    detection: Detection
    produced_by_tool: str


def _merge(store: Dict[str, Dict[str, Any]], new: Dict[str, Dict[str, Any]]) -> None:
    """Detailed records replace listing stubs; a stub never downgrades a detailed record."""
    for key, rec in new.items():
        old = store.get(key)
        if old is None:
            store[key] = dict(rec)
        elif rec.get("listed_only"):
            continue
        else:
            merged = {**old, **rec}
            merged.pop("listed_only", None)
            store[key] = merged


class Ledger:
    def __init__(self) -> None:
        self.transactions: Dict[str, Dict[str, Any]] = {}
        self.accounts: Set[str] = set()
        self.alerts: Set[str] = set()
        self.investigations: Set[str] = set()
        self.detections: Dict[str, LedgerDetection] = {}
        self.findings: Dict[str, Dict[str, Any]] = {}
        self.evidence: Dict[str, Dict[str, Any]] = {}
        self.detector_runs: List[DetectorRun] = []
        self.payload_numbers: Set[int] = set()

    def add_payload_numbers(self, data: Dict[str, Any]) -> None:
        """Numbers a delivered result states: its top-level integers and the length of each list it returns."""
        for v in data.values():
            if isinstance(v, bool):
                continue
            if isinstance(v, int):
                self.payload_numbers.add(v)
            elif isinstance(v, list):
                self.payload_numbers.add(len(v))

    def apply(self, facts: Facts) -> None:
        for tid, rec in facts.transactions.items():
            have = self.transactions.get(tid, {})
            self.transactions[tid] = {**have, **{k: v for k, v in rec.items() if v is not None}}
        self.accounts |= facts.accounts
        self.alerts |= facts.alerts
        self.investigations |= facts.investigations
        for det, tool in facts.detections:
            self.detections.setdefault(det.detection_id, LedgerDetection(det, tool))
        _merge(self.findings, facts.findings)
        _merge(self.evidence, facts.evidence)
        self.detector_runs += facts.detector_runs

    # -- identifiers the model has legitimately seen ---------------------------------------------
    def identifiers(self) -> Set[str]:
        ids: Set[str] = set(self.transactions) | self.accounts | self.alerts | self.investigations
        ids |= set(self.detections) | set(self.findings) | set(self.evidence)
        for ld in self.detections.values():
            ids.add(evidence_id_for(ld.detection.detection_id))
            ids.add(ld.detection.rule_version)
        for ev in self.evidence.values():
            if ev.get("query_ref"):
                ids.add(ev["query_ref"])
        return {i for i in ids if i}

    def counts(self) -> Dict[str, int]:
        return {"transactions": len(self.transactions), "accounts": len(self.accounts), "alerts": len(self.alerts),
                "detections": len(self.detections), "stored_findings": len(self.findings),
                "stored_evidence": len(self.evidence)}
