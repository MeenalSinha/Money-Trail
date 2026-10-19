"""The seven registered tools. Each wraps an existing, validated Phase 2/3 operation; none writes to the graph.

No handler accepts Cypher, a function name or a file path. Labels, relationship types and thresholds never come from
the model: detection rules come from the validated detection configuration, bounds from the agent configuration.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from money_trail.agent.config import ToolLimits
from money_trail.agent.tools.base import DetectorRun, Facts, Tool, ToolOutput, ToolResult
from money_trail.agent.tools.schema import Arg
from money_trail.database import FalkorDBConnection, GraphStore
from money_trail.investigation.config import DetectionConfig
from money_trail.investigation.detectors.models import Detection, evidence_id_for
from money_trail.investigation.engine import InvestigationEngine, InvestigationRequest
from money_trail.investigation.findings import finding_id, finding_rows, get_evidence, get_finding, list_findings
from money_trail.investigation.queries import InvestigationQueries, QueryLimits

ANCHOR_TYPES = ("transaction", "account")


@dataclass
class ToolContext:
    conn: FalkorDBConnection
    detection_config: DetectionConfig
    limits: ToolLimits
    engine: InvestigationEngine

    def queries(self, **limit_overrides) -> InvestigationQueries:
        lim = QueryLimits(max_results=self.limits.max_results, max_hops=self.limits.max_hops,
                          max_paths=self.limits.max_paths, max_gap_ms=None).override(**limit_overrides)
        return InvestigationQueries(self.conn, lim, timeout_ms=self.limits.query_timeout_ms)


def _tx_rec(props: Dict[str, Any], src: Optional[str], dst: Optional[str]) -> Dict[str, Any]:
    return {"transaction_id": props.get("transaction_id"), "source_account": src, "destination_account": dst,
            "amount": props.get("amount"), "currency": props.get("currency"), "timestamp": props.get("timestamp")}


# ---- 1. get_transaction_context ---------------------------------------------------------------
def get_transaction_context(ctx: ToolContext, a: Dict[str, Any]) -> ToolOutput:
    out = ctx.queries().transaction_context(a["transaction_id"])
    if out.status == "not_found":
        return ToolOutput(ToolResult("not_found", {"transaction_id": a["transaction_id"]}))
    d = out.data
    tx = d["transaction"]
    srcs = [x["account_id"] for x in d["source_accounts"]]
    dsts = [x["account_id"] for x in d["destination_accounts"]]
    stored = list_findings(ctx.conn, transaction_id=a["transaction_id"], limit=ctx.limits.max_listed_findings)
    facts = Facts(accounts=set(srcs) | set(dsts), alerts={x["alert_id"] for x in d["alerts"]})
    facts.transactions[tx["transaction_id"]] = _tx_rec(tx, srcs[0] if len(srcs) == 1 else None,
                                                       dsts[0] if len(dsts) == 1 else None)
    for f in stored["findings"]:
        facts.findings[f["finding_id"]] = {"finding_id": f["finding_id"], "kind": f["kind"],
                                           "evidence_ids": f["evidence_ids"], "listed_only": True}
        facts.investigations |= set(f["investigation_ids"])
        facts.evidence.update({e: {"evidence_id": e, "listed_only": True} for e in f["evidence_ids"]})
    data = {"transaction": {k: tx.get(k) for k in ("transaction_id", "amount", "currency", "timestamp", "channel", "dataset")},
            "source_accounts": srcs, "destination_accounts": dsts,
            "alerts": [{k: x.get(k) for k in ("alert_id", "alert_type", "severity", "created_at", "source")} for x in d["alerts"]],
            "stored_findings": stored["findings"], "stored_findings_truncated": stored["truncated"],
            "amount_unit_note": "amount is an integer in minor units of the stated currency; timestamp is epoch milliseconds UTC"}
    return ToolOutput(ToolResult("ok", data, truncated=stored["truncated"], warnings=list(out.warnings)), facts)


# ---- 2. get_account_activity ------------------------------------------------------------------
def get_account_activity(ctx: ToolContext, a: Dict[str, Any]) -> ToolOutput:
    limit = a.get("limit", ctx.limits.max_results)
    out = ctx.queries(max_results=limit).account_activity(a["account_id"], a.get("direction", "both"),
                                                         a.get("start_ms"), a.get("end_ms"))
    if out.status == "not_found":
        return ToolOutput(ToolResult("not_found", {"account_id": a["account_id"]}))
    facts, rows = Facts(accounts={a["account_id"]}), []
    for item in out.data["activity"]:
        t, cp = item["transaction"], item["counterparty"]
        src, dst = (a["account_id"], cp) if item["direction"] == "out" else (cp, a["account_id"])
        facts.accounts.add(cp)
        facts.transactions[t["transaction_id"]] = _tx_rec(t, src, dst)
        rows.append({"direction": item["direction"], "counterparty": cp, **{k: t.get(k) for k in
                     ("transaction_id", "amount", "currency", "timestamp")}})
    data = {"account_id": a["account_id"], "direction": out.data["direction"], "start_ms": out.data["start_ms"],
            "end_ms": out.data["end_ms"], "limit": out.data["limit"], "activity": rows}
    warns = ["truncated: more activity exists than the limit; the list is the earliest matching transactions"] if out.truncated else []
    return ToolOutput(ToolResult("incomplete" if out.truncated else "ok", data, out.truncated, warns), facts)


# ---- 3. trace_transaction_network -------------------------------------------------------------
def trace_transaction_network(ctx: ToolContext, a: Dict[str, Any]) -> ToolOutput:
    q = ctx.queries()
    kwargs = dict(direction=a.get("direction", "forward"), max_hops=a.get("max_hops", ctx.limits.max_hops),
                  max_paths=a.get("max_paths", ctx.limits.max_paths), max_gap_ms=a.get("max_gap_ms"))
    out = (q.trace_network(a["anchor_id"], **kwargs) if a["anchor_type"] == "transaction"
           else q.trace_network_from_account(a["anchor_id"], **kwargs))
    if out.status == "not_found":
        return ToolOutput(ToolResult("not_found", {"anchor_type": a["anchor_type"], "anchor_id": a["anchor_id"]}))
    d = out.data
    facts = Facts(accounts=set(d["accounts"]))
    for t in d["transactions"]:
        ep = d["transaction_endpoints"].get(t["transaction_id"], {})
        facts.transactions[t["transaction_id"]] = _tx_rec(t, ep.get("source"), ep.get("destination"))
    paths = [[s["transaction"]["transaction_id"] for s in p if "transaction" in s] for p in d["paths"]]
    txs = [facts.transactions[t["transaction_id"]] for t in d["transactions"]]
    data = {"anchor_type": a["anchor_type"], "anchor_id": a["anchor_id"], "direction": d["direction"],
            "max_hops": d["max_hops"], "max_paths": d["max_paths"], "max_gap_ms": d["max_gap_ms"],
            "path_count": len(paths), "paths": paths, "transactions": txs, "accounts": d["accounts"],
            "evidence_references": {"transaction_ids": [t["transaction_id"] for t in txs]}}
    return ToolOutput(ToolResult("incomplete" if out.truncated else "ok", data, out.truncated, out.warnings), facts)


# ---- 4. run_pattern_detector and 7. run_investigation -----------------------------------------
def _detection_view(ctx: ToolContext, d: Detection, store: GraphStore) -> Dict[str, Any]:
    hyp = ctx.detection_config.hypothesis_findings
    fids = {r["kind"]: r["finding_id"] for r in finding_rows(d, hyp)}
    eid = evidence_id_for(d.detection_id)
    stored_f = store.fetch_nodes("Finding", list(fids.values()))
    return {"detection_id": d.detection_id, "pattern_type": d.pattern_type, "anchor_id": d.anchor_id,
            "account_ids": list(d.account_ids), "transaction_ids": list(d.transaction_ids),
            "transactions": [t.to_dict() for t in d.ordered_path], "criteria": d.criteria, "roles": d.roles,
            "rule_version": d.rule_version, "warnings": list(d.warnings), "truncated": d.truncated,
            "evidence_id": eid, "finding_ids": fids,
            "persisted": {"evidence": bool(store.fetch_nodes("Evidence", [eid])),
                          "findings": sorted(k for k, v in fids.items() if v in stored_f)}}


def _engine_output(ctx: ToolContext, tool: str, res, requested: Tuple[str, ...], a: Dict[str, Any]) -> ToolOutput:
    if res.status == "anchor_not_found":
        return ToolOutput(ToolResult("not_found", {"anchor_type": a["anchor_type"], "anchor_id": a["anchor_id"]}))
    if res.status == "database_error":
        return ToolOutput(ToolResult("error", {"error": res.error or "database error"}))
    store, facts = GraphStore(ctx.conn), Facts()
    views = []
    for d in res.detections:
        views.append(_detection_view(ctx, d, store))
        facts.detections.append((d, tool))
        for t in d.ordered_path:
            facts.transactions[t.transaction_id] = t.to_dict()
        facts.accounts |= set(d.account_ids)
    for name in requested:
        rep = res.detector_reports.get(name, {})
        facts.detector_runs.append(DetectorRun(name, a["anchor_type"], a["anchor_id"],
                                               not rep.get("truncated", True), rep.get("detections", 0)))
    warns = [f"{name}: {w}" for name, r in res.detector_reports.items() for w in r.get("warnings", [])]
    if res.evidence_failures:
        warns.append(f"{len(res.evidence_failures)} detection(s) failed evidence verification and were excluded")
    data = {"engine_status": res.status, "complete": res.complete, "anchor_type": a["anchor_type"],
            "anchor_id": a["anchor_id"], "detectors_run": list(requested), "rule_fingerprint": res.rule_fingerprint,
            "detection_count": len(views), "detections": views, "detector_reports": {
                n: r for n, r in res.detector_reports.items() if not n.startswith("_")},
            "evidence_failures": res.evidence_failures,
            "interpretation_note": "A detection is a configured structural pattern, not a finding of wrongdoing. "
                                   "An empty result applies only to the listed detectors, anchor and rules."}
    return ToolOutput(ToolResult("ok" if res.complete else "incomplete", data, not res.complete, warns), facts)


def run_pattern_detector(ctx: ToolContext, a: Dict[str, Any]) -> ToolOutput:
    res = ctx.engine.investigate(InvestigationRequest(a["anchor_type"], a["anchor_id"]), detectors=[a["detector"]])
    return _engine_output(ctx, "run_pattern_detector", res, (a["detector"],), a)


def run_investigation(ctx: ToolContext, a: Dict[str, Any]) -> ToolOutput:
    res = ctx.engine.investigate(InvestigationRequest(a["anchor_type"], a["anchor_id"]))
    return _engine_output(ctx, "run_investigation", res, ctx.detection_config.enabled_names(), a)


# ---- 5. get_finding_details and 6. get_evidence_details ---------------------------------------
def _evidence_facts(ev: Dict[str, Any], facts: Facts) -> None:
    e = ev["evidence"]
    facts.evidence[e["evidence_id"]] = {**e, "transaction_ids": ev["transaction_ids"], "complete": ev["complete"]}
    for tid, t in ev["transactions"].items():
        s, r = t["source_accounts"], t["destination_accounts"]
        facts.transactions[tid] = _tx_rec(t, s[0] if len(s) == 1 else None, r[0] if len(r) == 1 else None)
        facts.accounts |= set(s) | set(r)


def get_finding_details(ctx: ToolContext, a: Dict[str, Any]) -> ToolOutput:
    f = get_finding(ctx.conn, a["finding_id"], limit=ctx.limits.max_results)
    if f is None:
        return ToolOutput(ToolResult("not_found", {"finding_id": a["finding_id"]}))
    facts = Facts(investigations=set(f["investigation_ids"]))
    facts.findings[a["finding_id"]] = {**f["finding"], "evidence_ids": [e["evidence"]["evidence_id"] for e in f["evidence"]],
                                       "references_valid": f["complete"]}
    for ev in f["evidence"]:
        _evidence_facts(ev, facts)
    data = {**f, "references_valid": f["complete"]}
    return ToolOutput(ToolResult("ok" if f["complete"] else "incomplete", data, False, list(f["problems"])), facts)


def get_evidence_details(ctx: ToolContext, a: Dict[str, Any]) -> ToolOutput:
    ev = get_evidence(ctx.conn, a["evidence_id"], limit=ctx.limits.max_results)
    if ev is None:
        return ToolOutput(ToolResult("not_found", {"evidence_id": a["evidence_id"]}))
    facts = Facts()
    _evidence_facts(ev, facts)
    for fid in ev["supports_findings"]:
        facts.findings.setdefault(fid, {"finding_id": fid, "listed_only": True})
    return ToolOutput(ToolResult("ok" if ev["complete"] else "incomplete", {**ev, "references_valid": ev["complete"]},
                                 ev["truncated"], list(ev["problems"])), facts)


# ---- registry construction --------------------------------------------------------------------
def build_tools(detection: DetectionConfig, lim: ToolLimits) -> List[Tool]:
    anchor = (Arg("anchor_type", "string", "What the anchor identifier refers to.", enum=ANCHOR_TYPES),
              Arg("anchor_id", "string", "Canonical identifier of the transaction or account, exactly as returned by earlier tools.", is_id=True))
    detectors = detection.enabled_names()
    return [
        Tool("get_transaction_context",
             "Read one transaction from the graph: amount (minor units), currency, timestamp, source and destination "
             "accounts, alerts attached to it, and findings already stored for investigations started from it. "
             "Returns status not_found if the transaction does not exist.",
             (Arg("transaction_id", "string", "Canonical transaction identifier.", is_id=True),),
             get_transaction_context, ("transaction", "source_accounts", "destination_accounts", "alerts", "stored_findings"),
             result_description="transaction, accounts, alerts, stored_findings"),
        Tool("get_account_activity",
             f"List incoming and/or outgoing transactions of an account, earliest first, optionally inside a time window. "
             f"At most {lim.max_results} transactions; a truncated list is flagged and is not the full history.",
             (Arg("account_id", "string", "Canonical account identifier.", is_id=True),
              Arg("direction", "string", "Which transactions to list.", required=False, enum=("in", "out", "both")),
              Arg("start_ms", "integer", "Window start, epoch milliseconds (inclusive).", required=False, minimum=0),
              Arg("end_ms", "integer", "Window end, epoch milliseconds (inclusive).", required=False, minimum=0),
              Arg("limit", "integer", "Maximum transactions to return.", required=False, minimum=1, maximum=lim.max_results)),
             get_account_activity, ("account_id", "activity"), result_description="activity rows"),
        Tool("trace_transaction_network",
             "Follow time-respecting chains of transactions through the graph from a transaction or an account, forward "
             f"(downstream) or backward (upstream). At most {lim.max_hops} transactions per path and {lim.max_paths} paths; "
             "a truncated search is flagged and does not cover every connected account.",
             anchor + (Arg("direction", "string", "Follow funds downstream or upstream.", required=False, enum=("forward", "backward")),
                       Arg("max_hops", "integer", "Maximum transactions per path (at least 2 for an account anchor).", required=False, minimum=1, maximum=lim.max_hops),
                       Arg("max_paths", "integer", "Maximum paths to return.", required=False, minimum=1, maximum=lim.max_paths),
                       Arg("max_gap_ms", "integer", "Maximum milliseconds between consecutive transactions.", required=False,
                           minimum=1, maximum=lim.max_gap_ms)),
             trace_transaction_network, ("paths", "transactions", "accounts", "evidence_references"),
             result_description="paths, transactions, accounts, evidence_references"),
        Tool("run_pattern_detector",
             "Run ONE configured structural detector anchored at a transaction or account. Thresholds come from the "
             "validated detection configuration and cannot be changed here. Returns verified detections with their "
             "transactions and criteria, plus warnings and truncation. No detection means none under that detector's "
             "configured rule for this anchor, not that the activity is legitimate.",
             (Arg("detector", "string", "Detector to run.", enum=detectors),) + anchor,
             run_pattern_detector, ("detections", "detector_reports", "complete"), result_description="detections"),
        Tool("get_finding_details",
             "Read a finding that is already stored in the graph (not one you expect to exist) with its supporting evidence "
             "and the referenced transactions. Reports not_found, or incomplete with the problems, if records are missing.",
             (Arg("finding_id", "string", "Stored finding identifier, as listed by get_transaction_context.", is_id=True),),
             get_finding_details, ("finding", "evidence", "references_valid"), result_description="finding, evidence"),
        Tool("get_evidence_details",
             "Read an evidence record already stored in the graph: producing detector, rule version, description and the "
             "transactions it references. Reports not_found, or incomplete with the problems, if the record is missing or partial.",
             (Arg("evidence_id", "string", "Stored evidence identifier.", is_id=True),),
             get_evidence_details, ("evidence", "transactions", "references_valid"), result_description="evidence record"),
        Tool("run_investigation",
             "Run EVERY enabled detector anchored at a transaction or account in one call (broad analysis). Prefer the "
             "narrower tools for follow-up. Same limits and interpretation as run_pattern_detector.",
             anchor, run_investigation, ("detections", "detector_reports", "complete"), result_description="detections"),
    ]
