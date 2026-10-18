"""Finding and evidence persistence and retrieval, using only the Phase 1 schema.

Schema use (unchanged): (Investigation)-[:STARTED_FROM]->(Transaction); (Investigation)-[:HAS_FINDING]->(Finding);
(Finding)-[:SUPPORTED_BY]->(Evidence); (Evidence)-[:REFERENCES]->(Transaction).

Identity (deterministic, see detectors.models.stable_id):
  investigation = hash(namespace, anchor transaction, enabled-rule fingerprint)
  detection     = hash(namespace, pattern, detector rule version, canonical evidence set)
  evidence      = hash(detection)          (one evidence item per detection)
  finding       = hash(detection, kind)    (an observation, plus a hypothesis when configured)
Rerunning with unchanged data and rules matches existing nodes and creates nothing. Existing nodes are
never overwritten or deleted, so earlier findings stay as the record of an earlier evaluation.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from money_trail.database import FalkorDBConnection, GraphStore
from money_trail.investigation.config import DetectionConfig
from money_trail.investigation.detectors.models import Detection, evidence_id_for, stable_id

OBSERVATION_NOTE = "Structural observation from configured rules only; it does not establish wrongdoing."
HYPOTHESIS_TEXT = ("Hypothesis (unconfirmed): this transfer structure may warrant human review. It rests only on "
                   "configured structural criteria and has not been checked against any other information.")


def investigation_id(namespace: str, anchor_tx: str, config: DetectionConfig) -> str:
    return stable_id("inv", namespace, anchor_tx, config.fingerprint())


def finding_id(detection_id: str, kind: str) -> str:
    return stable_id("fnd", detection_id, kind)


def _observation_text(d: Detection) -> str:
    o = d.criteria["observed"]
    if d.pattern_type == "circular":
        what = f"a time-respecting directed cycle of {o['transactions']} transactions through {o['distinct_accounts']} accounts"
    elif d.pattern_type == "pass_through":
        what = f"a chronologically ordered chain of {o['transactions']} transfers through {o['accounts']} accounts"
    elif d.pattern_type == "fan_in":
        what = (f"{o['transactions']} transfers from {o['distinct_counterparties']} distinct source accounts into one "
                f"account within {o['window_span_ms']} ms")
    else:
        what = (f"{o['transactions']} transfers from one account to {o['distinct_counterparties']} distinct "
                f"destination accounts within {o['window_span_ms']} ms")
    return f"Observed {what} (rule {d.rule_version}). {OBSERVATION_NOTE}"


def finding_rows(d: Detection, hypothesis: bool) -> List[Dict[str, Any]]:
    rows = [{"finding_id": finding_id(d.detection_id, "observation"), "kind": "observation",
             "statement": _observation_text(d), "created_at": d.created_at}]
    if hypothesis:
        rows.append({"finding_id": finding_id(d.detection_id, "hypothesis"), "kind": "hypothesis",
                     "statement": HYPOTHESIS_TEXT, "created_at": d.created_at})
    return rows


def evidence_row(d: Detection) -> Dict[str, Any]:
    desc = (f"Supporting transactions for a {d.pattern_type} detection; {len(d.transaction_ids)} transaction(s) "
            f"referenced. Configured criteria: {json.dumps(d.criteria['configured'], sort_keys=True)}")
    return {"evidence_id": evidence_id_for(d.detection_id), "created_at": d.created_at,
            "produced_by_tool": f"money_trail.detector.{d.pattern_type}", "query_ref": d.rule_version,
            "description": desc[:1900]}


def _create_missing(store: GraphStore, label: str, id_prop: str, rows: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    if not rows:
        return {"created": 0, "existing": 0}
    have = store.fetch_nodes(label, [r[id_prop] for r in rows])
    new = [r for r in rows if r[id_prop] not in have]
    created = store.create_nodes(label, new) if new else 0
    return {"created": created, "existing": len(rows) - len(new)}


def persist_investigation(store: GraphStore, *, config: DetectionConfig, namespace: str, anchor_tx: str,
                          detections: Sequence[Detection], now_ms: int) -> Dict[str, Any]:
    """Create or reconcile the investigation, findings and evidence. Idempotent; never overwrites."""
    inv_id = investigation_id(namespace, anchor_tx, config)
    report: Dict[str, Any] = {"investigation_id": inv_id}
    report["investigation"] = _create_missing(store, "Investigation", "investigation_id", [{
        "investigation_id": inv_id, "status": "open", "created_at": now_ms,
        "title": f"{config.rule_set_name} v{config.rule_set_version} investigation from a transaction"}])
    rels = {k: {"created": 0, "existing": 0} for k in ("STARTED_FROM", "HAS_FINDING", "SUPPORTED_BY", "REFERENCES")}

    def link(rel: str, pairs):
        matched, created = store.link_batch(rel, pairs)
        rels[rel]["created"] += created
        rels[rel]["existing"] += matched - created

    link("STARTED_FROM", [(inv_id, anchor_tx)])
    fnd_rows = [r for d in detections for r in finding_rows(d, config.hypothesis_findings)]
    evd_rows = [evidence_row(d) for d in detections]
    report["evidence"] = _create_missing(store, "Evidence", "evidence_id", evd_rows)
    report["findings"] = _create_missing(store, "Finding", "finding_id", fnd_rows)
    for d in detections:
        eid = evidence_id_for(d.detection_id)
        for r in finding_rows(d, config.hypothesis_findings):
            link("HAS_FINDING", [(inv_id, r["finding_id"])])
            link("SUPPORTED_BY", [(r["finding_id"], eid)])
        link("REFERENCES", [(eid, t) for t in d.transaction_ids])
    report["relationships"] = rels
    report["finding_ids"] = [r["finding_id"] for r in fnd_rows]
    report["evidence_ids"] = [r["evidence_id"] for r in evd_rows]
    return report


# ---- retrieval (read-only) -----------------------------------------------------------------
def list_findings(conn: FalkorDBConnection, *, investigation_id: Optional[str] = None,
                  transaction_id: Optional[str] = None, limit: int) -> Dict[str, Any]:
    where, params = [], {"cap": limit + 1}
    if investigation_id:
        where.append("i.investigation_id = $inv")
        params["inv"] = investigation_id
    if transaction_id:
        where.append("(i)-[:STARTED_FROM]->(:Transaction {transaction_id: $tx})")
        params["tx"] = transaction_id
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.graph.ro_query(
        "MATCH (i:Investigation)-[:HAS_FINDING]->(f:Finding)" + clause +
        " OPTIONAL MATCH (f)-[:SUPPORTED_BY]->(e:Evidence) "
        "RETURN f.finding_id, f.kind, f.statement, f.created_at, collect(DISTINCT i.investigation_id), "
        "collect(DISTINCT e.evidence_id) ORDER BY f.created_at, f.finding_id LIMIT $cap", params).result_set
    out = [{"finding_id": r[0], "kind": r[1], "statement": r[2], "created_at": r[3],
            "investigation_ids": sorted(r[4]), "evidence_ids": sorted(x for x in r[5] if x)} for r in rows[:limit]]
    return {"findings": out, "limit": limit, "truncated": len(rows) > limit}


def get_investigation(conn: FalkorDBConnection, inv_id: str, *, limit: int) -> Optional[Dict[str, Any]]:
    store = GraphStore(conn)
    node = store.fetch_nodes("Investigation", [inv_id]).get(inv_id)
    if node is None:
        return None
    g = conn.graph
    starts = [r[0] for r in g.ro_query("MATCH (:Investigation {investigation_id: $i})-[:STARTED_FROM]->(t:Transaction) "
                                       "RETURN t.transaction_id ORDER BY t.transaction_id", {"i": inv_id}).result_set]
    rows = g.ro_query(
        "MATCH (:Investigation {investigation_id: $i})-[:HAS_FINDING]->(f:Finding)-[:SUPPORTED_BY]->(e:Evidence)"
        "-[:REFERENCES]->(t:Transaction) RETURN f.finding_id, f.kind, f.statement, f.created_at, e.evidence_id, "
        "e.produced_by_tool, e.query_ref, t.transaction_id ORDER BY f.finding_id, e.evidence_id, t.timestamp, "
        "t.transaction_id LIMIT $cap", {"i": inv_id, "cap": limit + 1}).result_set
    truncated = len(rows) > limit
    findings: Dict[str, Dict[str, Any]] = {}
    tx_ids = set()
    for fid, kind, stmt, created, eid, tool, qref, tid in rows[:limit]:
        f = findings.setdefault(fid, {"finding_id": fid, "kind": kind, "statement": stmt, "created_at": created,
                                      "evidence": {}})
        f["evidence"].setdefault(eid, {"evidence_id": eid, "produced_by_tool": tool, "query_ref": qref,
                                       "transaction_ids": []})["transaction_ids"].append(tid)
        tx_ids.add(tid)
    tx_ids = sorted(tx_ids)
    details = store.fetch_nodes("Transaction", tx_ids)
    ends = store.fetch_transaction_endpoints(tx_ids)
    txs = {t: {**{k: details[t].get(k) for k in ("amount", "currency", "timestamp")},
               "source_accounts": sorted(ends.get(t, (set(), set()))[0]),
               "destination_accounts": sorted(ends.get(t, (set(), set()))[1])} for t in tx_ids if t in details}
    return {"investigation": node, "started_from": starts,
            "findings": [{**f, "evidence": list(f["evidence"].values())} for f in findings.values()],
            "transactions": txs, "truncated": truncated}
