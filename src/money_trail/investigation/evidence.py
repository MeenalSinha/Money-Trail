"""Evidence verification: re-read every supporting transaction from the graph and check the detection.

A detection is only persisted or reported as verified after its transactions, amounts, currencies,
timestamps, account endpoints and structural continuity have been confirmed against FalkorDB.
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from money_trail.database import GraphStore
from money_trail.investigation.detectors.models import Detection

_CHUNK = 1000


def _check_structure(d: Detection) -> str:
    path = d.ordered_path
    if len(path) < 2:
        return "fewer than two transactions"
    if d.pattern_type in ("circular", "pass_through"):
        if any(a.destination_account != b.source_account for a, b in zip(path, path[1:])):
            return "consecutive transfers are not connected through a shared account"
        if any(b.timestamp < a.timestamp for a, b in zip(path, path[1:])):
            return "transfers are not in chronological order"
        if d.pattern_type == "circular" and path[-1].destination_account != path[0].source_account:
            return "path does not return to its starting account"
    elif d.pattern_type == "fan_in":
        focus = d.roles.get("collector")
        if any(t.destination_account != focus for t in path):
            return "not every transfer is received by the collector account"
    elif d.pattern_type == "fan_out":
        focus = d.roles.get("distributor")
        if any(t.source_account != focus for t in path):
            return "not every transfer is sent by the distributor account"
    return ""


def validate_detections(store: GraphStore, detections: Sequence[Detection]) -> Tuple[List[Detection], List[Dict[str, Any]]]:
    ids = sorted({t for d in detections for t in d.transaction_ids})
    nodes: Dict[str, Dict[str, Any]] = {}
    ends: Dict[str, Tuple[set, set]] = {}
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i:i + _CHUNK]
        nodes.update(store.fetch_nodes("Transaction", chunk))
        ends.update(store.fetch_transaction_endpoints(chunk))
    valid, failures = [], []
    for d in detections:
        reason = _check_structure(d)
        if not reason:
            for t in d.ordered_path:
                n = nodes.get(t.transaction_id)
                if n is None:
                    reason = "a supporting transaction no longer exists in the graph"
                elif (n.get("amount"), n.get("currency"), n.get("timestamp")) != (t.amount, t.currency, t.timestamp):
                    reason = "a supporting transaction differs from the detected values"
                else:
                    senders, receivers = ends.get(t.transaction_id, (set(), set()))
                    if senders != {t.source_account} or receivers != {t.destination_account}:
                        reason = "a supporting transaction's account endpoints differ from the detected values"
                if reason:
                    break
        (failures if reason else valid).append(
            {"detection_id": d.detection_id, "pattern_type": d.pattern_type, "reason": reason} if reason else d)
    return valid, failures
