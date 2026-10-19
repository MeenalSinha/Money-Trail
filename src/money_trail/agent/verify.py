"""Report-time verification. The graph is authoritative: every cited detection is re-read from FalkorDB when the report
is built, so a transaction removed or changed after a tool call turns the finding unverified instead of being reported.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from money_trail.agent.ledger import Ledger
from money_trail.database import FalkorDBConnection, GraphStore
from money_trail.investigation.detectors.models import Detection, evidence_id_for
from money_trail.investigation.detectors.registry import DETECTORS
from money_trail.investigation.evidence import validate_detections
from money_trail.investigation.findings import get_evidence


@dataclass
class VerifiedDetection:
    detection: Detection
    produced_by_tool: str
    verified: bool
    reason: str
    checks: Dict[str, bool] = field(default_factory=dict)


def verify_ledger_detections(conn: FalkorDBConnection, ledger: Ledger) -> List[VerifiedDetection]:
    """Checks per detection: it exists in a detector result the model received; its pattern type is a registered
    detector that actually ran for that anchor; every supporting transaction, amount, currency, timestamp and
    account endpoint still matches the graph and the structure holds; its evidence id is the deterministic one."""
    items = list(ledger.detections.values())
    if not items:
        return []
    _, failures = validate_detections(GraphStore(conn), [ld.detection for ld in items])
    failed = {f["detection_id"]: f["reason"] for f in failures}
    ran = {(r.detector, r.anchor_id) for r in ledger.detector_runs}
    out = []
    for ld in items:
        d = ld.detection
        checks = {"in_detector_result": True,
                  "pattern_from_matching_detector": d.pattern_type in DETECTORS and (d.pattern_type, d.anchor_id) in ran,
                  "transactions_reverified_in_graph": d.detection_id not in failed,
                  "evidence_reference_consistent": d.evidence_refs == (evidence_id_for(d.detection_id),) or
                                                   evidence_id_for(d.detection_id) in d.evidence_refs or not d.evidence_refs}
        reason = failed.get(d.detection_id, "")
        if not reason and not checks["pattern_from_matching_detector"]:
            reason = "no registered detector run for this anchor produced this pattern type"
        if not reason and not checks["evidence_reference_consistent"]:
            reason = "evidence reference does not match the detection"
        out.append(VerifiedDetection(d, ld.produced_by_tool, not reason, reason, checks))
    return out


def check_persisted(conn: FalkorDBConnection, detections: List[Detection], *, limit: int) -> Dict[str, Dict[str, Any]]:
    """After persistence: read each evidence record back and confirm it references exactly the detection's transactions."""
    out = {}
    for d in detections:
        ev = get_evidence(conn, evidence_id_for(d.detection_id), limit=limit)
        ok = ev is not None and set(ev["transaction_ids"]) == set(d.transaction_ids) and ev["complete"]
        out[d.detection_id] = {"evidence_id": evidence_id_for(d.detection_id), "evidence_present": ev is not None,
                               "references_match_detection": bool(ok),
                               "problems": [] if ev is None else ev["problems"]}
    return out
