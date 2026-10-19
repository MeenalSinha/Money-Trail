"""Structured, evidence-grounded investigation report (schema version 1).

Policy (documented in docs/agent.md):
* Observations and the deterministic hypothesis come from verified detections (never from model text).
* Model text (summary narrative, interpretations, hypotheses, open questions) is included only after the grounding
  filter, labelled as model-written, and can never create a finding or change a classification.
* Every verified detection the model was shown is reported, whether or not the model mentions it.
* A finding that fails re-verification is listed under `unverified_findings` with the reason and is excluded from
  the headline and the outcome.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from money_trail.agent.grounding import GroundedText, GroundingChecker
from money_trail.agent.ledger import Ledger
from money_trail.agent.tools.registry import ExecutedTool
from money_trail.agent.verify import VerifiedDetection
from money_trail.ingestion.contract import format_epoch_ms_iso
from money_trail.investigation.detectors.models import evidence_id_for, stable_id
from money_trail.investigation.findings import finding_rows

SCHEMA_VERSION = 1
RUN_STATUSES = ("completed", "incomplete", "failed", "anchor_not_found", "invalid_request", "unsupported_request")
OUTCOMES = ("patterns_identified", "no_pattern_identified_under_tested_criteria", "not_determined")
CLASSIFICATIONS = ("observation", "interpretation", "hypothesis")
LIMITATIONS = (
    "A detection is a configured structural pattern in the graph. It is not evidence of fraud, money laundering or any "
    "other wrongdoing, and legitimate activity can satisfy every rule.",
    "No pattern found means none under the listed detectors, anchor, scope and rules; it does not show the activity is legitimate.",
    "Only the registered read-only tools ran. Records outside the graph, and anything the tools did not return, were not examined.",
    "Model-written text is filtered against retrieved records but is not a verified fact; observations and identifiers come from the graph.",
)


@dataclass
class Submission:
    summary: str
    supported: bool
    limitation: Optional[str]
    detections: Dict[str, Dict[str, str]]          # detection id -> {"interpretation"?, "hypothesis"?}
    outstanding_questions: List[str]
    next_steps: List[str]


@dataclass
class RunState:
    request_id: str
    request: Optional[Dict[str, Any]]
    request_status: str
    request_problems: List[str]
    request_warnings: List[str]
    anchor_type: Optional[str]
    anchor_id: Optional[str]
    started_ms: int
    completed_ms: int
    run_status: str
    reasons: List[str]
    agent_summary: Dict[str, Any]
    rule_set: Optional[str]
    rule_fingerprint: Optional[str]
    ledger: Ledger = field(default_factory=Ledger)
    trace: List[ExecutedTool] = field(default_factory=list)
    steps: List[Dict[str, Any]] = field(default_factory=list)
    provider_usage: Dict[str, Any] = field(default_factory=dict)
    verified: List[VerifiedDetection] = field(default_factory=list)
    submission: Optional[Submission] = None
    seed_data: Optional[Dict[str, Any]] = None
    persisted: Optional[Dict[str, Any]] = None     # {"investigation_id", "per_detection": {...}, "report": {...}}
    hypothesis_findings: bool = True
    error: Optional[str] = None


def _iso(ms: int) -> str:
    return format_epoch_ms_iso(ms)


def _trace_entry(i: int, e: ExecutedTool) -> Dict[str, Any]:
    d = e.result.data
    refs = {}
    if "detections" in d:
        refs["detection_ids"] = [x["detection_id"] for x in d["detections"]]
    if "transactions" in d and isinstance(d["transactions"], list):
        refs["transaction_ids"] = [t["transaction_id"] if isinstance(t, dict) else t for t in d["transactions"]][:50]
    if "activity" in d:
        refs["transaction_ids"] = [r["transaction_id"] for r in d["activity"]][:50]
    return {"seq": i, "tool": e.tool, "arguments": e.arguments, "status": e.result.status, "executed": e.executed, "origin": e.origin,
            "duration_ms": e.duration_ms, "truncated": e.result.truncated, "warnings": e.result.warnings[:10],
            "error": e.message if e.is_error or e.message else None, "result_references": refs}


def _redact(where: str, sentence: str, reasons: List[str]) -> Dict[str, Any]:
    """Rejected model text is audited by reason, length and hash only; its content is never echoed into the report."""
    return {"field": where, "reasons": reasons, "chars": len(sentence),
            "sha256_12": hashlib.sha256(sentence.encode("utf-8")).hexdigest()[:12]}


def _model_statement(det_id: str, cls: str, text: str) -> Dict[str, Any]:
    return {"finding_id": stable_id("stm", det_id, cls, text), "classification": cls, "source": "model",
            "detection_id": det_id, "statement": text,
            "note": "Model-written and filtered against retrieved records; not a verified fact."}


def build_report(s: RunState, checker: Optional[GroundingChecker]) -> Dict[str, Any]:
    ledger = s.ledger
    ok = [v for v in s.verified if v.verified]
    bad = [v for v in s.verified if not v.verified]
    g_total = {"sentences": 0, "factual": 0, "factual_grounded": 0}
    rejected: List[Dict[str, Any]] = []

    def filt(text: Optional[str], where: str) -> str:
        if not text or checker is None:
            return ""
        r: GroundedText = checker.check(text)
        g_total["sentences"] += r.sentences
        g_total["factual"] += r.factual
        g_total["factual_grounded"] += r.factual_grounded
        rejected.extend(_redact(where, x["sentence"], x["reasons"]) for x in r.rejected)
        return r.text

    sub = s.submission
    # ---- findings -----------------------------------------------------------------------------
    findings: List[Dict[str, Any]] = []
    per_persist = (s.persisted or {}).get("per_detection", {})
    for v in ok:
        d = v.detection
        eid = evidence_id_for(d.detection_id)
        base = {"detection_id": d.detection_id, "pattern_type": d.pattern_type, "supporting_transaction_ids": list(d.transaction_ids),
                "supporting_account_ids": list(d.account_ids), "evidence_ids": [eid], "rule_version": d.rule_version,
                "applied_criteria": d.criteria.get("configured", {}), "observed": d.criteria.get("observed", {}),
                "warnings": list(d.warnings), "truncated": d.truncated, "verified": True, "verification_checks": v.checks,
                "persisted": per_persist.get(d.detection_id)}
        for row in finding_rows(d, s.hypothesis_findings):
            findings.append({"finding_id": row["finding_id"], "classification": row["kind"], "source": "deterministic_rule",
                             "description": row["statement"], **base,
                             "transactions": [t.to_dict() for t in d.ordered_path]})
        com = (sub.detections.get(d.detection_id) if sub else None) or {}
        for cls in ("interpretation", "hypothesis"):
            text = filt(com.get(cls), f"detection:{d.detection_id}:{cls}")
            if text:
                m = _model_statement(d.detection_id, cls, text)
                findings.append({**m, "description": text, "pattern_type": d.pattern_type, "supporting_transaction_ids": list(d.transaction_ids),
                                 "supporting_account_ids": list(d.account_ids), "evidence_ids": [eid],
                                 "rule_version": d.rule_version, "applied_criteria": d.criteria.get("configured", {}),
                                 "warnings": [], "verified": True,
                                 "verification_checks": {"detection_verified": True, "text_grounded": True}})
    if sub:
        known = {v.detection.detection_id for v in s.verified}
        for did, com in sub.detections.items():
            if did not in known:                       # commentary on a detection the model was never shown
                for cls, text in com.items():
                    g_total["sentences"] += 1
                    g_total["factual"] += 1
                    rejected.append(_redact(f"detection:{did}:{cls}", text or "",
                                            [f"detection id not in retrieved results: {did}"]))
    unverified = [{"detection_id": v.detection.detection_id, "pattern_type": v.detection.pattern_type,
                   "supporting_transaction_ids": list(v.detection.transaction_ids), "verified": False,
                   "reason": v.reason, "verification_checks": v.checks} for v in bad]

    # ---- outcome and headline (deterministic) -------------------------------------------------
    runs = ledger.detector_runs
    if ok:
        outcome = "patterns_identified"
    elif runs and all(r.complete for r in runs) and not bad:
        outcome = "no_pattern_identified_under_tested_criteria"
    else:
        outcome = "not_determined"
    types = sorted({v.detection.pattern_type for v in ok})
    if outcome == "patterns_identified":
        headline = (f"{len(ok)} verified structural pattern(s) identified under the configured rules "
                    f"({', '.join(types)}). This is not a finding of wrongdoing.")
    elif outcome == "no_pattern_identified_under_tested_criteria":
        names = sorted({r.detector for r in runs})
        headline = (f"No pattern identified under the tested criteria (detectors run: {', '.join(names)}; rule set "
                    f"{s.rule_set}). This does not establish that the activity is legitimate.")
    else:
        why = ("a detection failed verification" if bad else "a detector search was incomplete" if any(not r.complete for r in runs)
               else "no detector completed on this anchor")
        headline = f"No conclusion about patterns can be drawn: {why}."
    narrative = filt(sub.summary, "summary") if sub else ""

    # ---- outstanding questions ----------------------------------------------------------------
    out_q: List[str] = []
    for e in s.trace:
        loc = f"{e.tool}({', '.join(f'{k}={v}' for k, v in e.arguments.items() if k in ('anchor_id', 'detector', 'account_id', 'transaction_id', 'finding_id', 'evidence_id'))})"
        if e.result.status == "error":
            out_q.append(f"{loc} failed: {e.message}. Valid earlier results were kept.")
        elif e.result.status == "incomplete" or e.result.truncated:
            out_q.append(f"{loc} was truncated or incomplete: not all connected accounts or patterns were examined.")
        elif e.result.status == "not_found" and e.tool not in ("get_transaction_context",):
            out_q.append(f"{loc} found no record.")
        if e.tool == "get_transaction_context" and e.result.status == "ok":
            out_q += [f"Record anomaly at {loc}: {w}." for w in e.result.warnings]
    for v in bad:
        out_q.append(f"Detection {v.detection.detection_id} failed verification ({v.reason}) and is excluded from the outcome.")
    if not runs and s.run_status not in ("invalid_request", "anchor_not_found", "unsupported_request"):
        out_q.append("No pattern detector was run, so nothing can be said about structural patterns.")
    listed_only = [f for f, rec in ledger.findings.items() if rec.get("listed_only")]
    if listed_only:
        out_q.append(f"{len(listed_only)} stored finding(s) are linked to this anchor but their details were not retrieved.")
    if s.request_warnings:
        out_q += s.request_warnings
    for r in s.reasons:
        out_q.append({"tool_call_limit_reached": "The tool-call limit was reached; further investigation was not performed.",
                      "step_limit_reached": "The step limit was reached; further investigation was not performed.",
                      "time_limit_reached": "The time limit was reached; further investigation was not performed."}.get(r, ""))
        if r.startswith("provider_error:"):
            out_q.append(f"A provider failure ({r.split(':', 1)[1]}) interrupted further reasoning. Evidence collected earlier is preserved.")
        elif r in ("invalid_model_output", "too_many_invalid_tool_calls", "model_refusal", "context_limit"):
            out_q.append(f"The model could not complete the analysis ({r}); no model commentary is available for the remainder.")
    if sub and not sub.supported:
        out_q.append("The question asks for something the available tools cannot establish: " +
                     (filt(sub.limitation, "limitation") or "see the question assessment."))
    for q in (sub.outstanding_questions if sub else []):
        t = filt(q, "outstanding_question")
        if t:
            out_q.append(t)
    steps = [x for x in (filt(t, "next_step") for t in (sub.next_steps if sub else [])) if x]
    out_q = list(dict.fromkeys(x for x in out_q if x))

    # ---- observed data ------------------------------------------------------------------------
    observations = {"anchor": s.seed_data, "retrieved_counts": ledger.counts(),
                    "note": "Values below were read from the graph; amounts are integer minor units, timestamps epoch milliseconds UTC."}
    ts = [t["timestamp"] for t in ledger.transactions.values() if isinstance(t.get("timestamp"), int)]
    if ts:
        observations["retrieved_time_range"] = {"first_ms": min(ts), "last_ms": max(ts),
                                                "first_iso": _iso(min(ts)), "last_iso": _iso(max(ts))}
    stored = []
    for fid, rec in ledger.findings.items():
        if not rec.get("listed_only"):
            stored.append({"kind": "finding", "id": fid, "statement_kind": rec.get("kind"),
                           "references_valid": rec.get("references_valid"), "evidence_ids": rec.get("evidence_ids", [])})
    for eid, rec in ledger.evidence.items():
        if not rec.get("listed_only"):
            stored.append({"kind": "evidence", "id": eid, "complete": rec.get("complete"),
                           "transaction_ids": rec.get("transaction_ids", []), "produced_by_tool": rec.get("produced_by_tool"),
                           "rule_version": rec.get("query_ref")})

    executed = [e for e in s.trace if e.executed]
    completed_in_limits = not any(r.endswith("_limit_reached") for r in s.reasons) and s.run_status == "completed"
    assess = None if not sub else {"supported": sub.supported, "limitation": filt(sub.limitation, "limitation") or None}
    return {
        "schema_version": SCHEMA_VERSION,
        "metadata": {
            "request_id": s.request_id, "investigation_id": (s.persisted or {}).get("investigation_id"),
            "anchor": {"type": s.anchor_type, "id": s.anchor_id, "dataset_namespace": (s.anchor_id or "").split(":", 1)[0] if s.anchor_id and ":" in s.anchor_id else None},
            "started_at": _iso(s.started_ms), "completed_at": _iso(s.completed_ms), "duration_ms": s.completed_ms - s.started_ms,
            "run_status": s.run_status, "completed_within_limits": completed_in_limits,
            "completion_reasons": list(dict.fromkeys(s.reasons)),
            "tools_executed": [e.tool for e in executed], "rule_set": s.rule_set, "rule_fingerprint": s.rule_fingerprint,
            "agent": s.agent_summary, "error": s.error},
        "request": {"parameters": s.request, "status": s.request_status, "problems": s.request_problems,
                    "warnings": s.request_warnings, "question_assessment": assess},
        "summary": {"outcome": outcome, "headline": headline, "narrative": narrative or None,
                    "narrative_source": "model (filtered)" if narrative else None},
        "findings": findings, "unverified_findings": unverified,
        "observations": observations, "stored_records_reviewed": stored,
        "investigation_activity": {"tool_calls": [_trace_entry(i, e) for i, e in enumerate(s.trace, 1)],
                                   "model_steps": s.steps, "provider_usage": s.provider_usage},
        "outstanding_questions": out_q, "recommended_next_steps": steps,
        "verification": {"detections_retrieved": len(s.verified), "detections_verified": len(ok), "detections_failed": len(bad),
                         "model_text": {**g_total, "sentences_rejected": len(rejected), "rejected": rejected[:50]},
                         "persistence_check": (s.persisted or {}).get("per_detection")},
        "limitations": list(LIMITATIONS),
    }


def validate_report(r: Dict[str, Any]) -> List[str]:
    """Structural and invariant checks on a report dict. Returns problems (empty means valid)."""
    p: List[str] = []
    for k in ("schema_version", "metadata", "request", "summary", "findings", "unverified_findings", "observations",
              "investigation_activity", "outstanding_questions", "verification", "limitations"):
        if k not in r:
            p.append(f"missing section: {k}")
    if p:
        return p
    m = r["metadata"]
    if r["schema_version"] != SCHEMA_VERSION:
        p.append("unsupported schema_version")
    if m["run_status"] not in RUN_STATUSES:
        p.append("unknown run_status")
    if r["summary"]["outcome"] not in OUTCOMES:
        p.append("unknown outcome")
    if m["run_status"] == "completed" and (m["completion_reasons"] or not m["completed_within_limits"]):
        p.append("a completed run must have no completion reasons")
    for f in r["findings"]:
        if f.get("classification") not in CLASSIFICATIONS:
            p.append(f"finding {f.get('finding_id')}: bad classification")
        if not f.get("pattern_type"):
            p.append(f"finding {f.get('finding_id')}: missing pattern_type")
        if f.get("verified") is not True:
            p.append(f"finding {f.get('finding_id')}: only verified findings belong in findings")
        if not f.get("supporting_transaction_ids") or not f.get("evidence_ids"):
            p.append(f"finding {f.get('finding_id')}: missing supporting transactions or evidence")
        if f.get("source") == "deterministic_rule" and not str(f.get("finding_id", "")).startswith("fnd:"):
            p.append("deterministic finding id malformed")
    for u in r["unverified_findings"]:
        if u.get("verified") is not False or not u.get("reason"):
            p.append("unverified finding must state its reason")
    if r["summary"]["outcome"] == "patterns_identified" and not any(f["source"] == "deterministic_rule" for f in r["findings"]):
        p.append("patterns_identified without a deterministic finding")
    for c in r["investigation_activity"]["tool_calls"]:
        for k in ("seq", "tool", "arguments", "status", "duration_ms"):
            if k not in c:
                p.append(f"tool call missing {k}")
    blob = repr(r).lower()
    if "api_key" in blob or re.search(r"\bsk-[a-z]+-[a-z0-9_-]{8,}", blob):
        p.append("report appears to contain credential material")
    return p


# ---- renderers ------------------------------------------------------------------------------
def render_text(r: Dict[str, Any], *, trace: bool = False) -> str:
    m, s = r["metadata"], r["summary"]
    L = [f"Investigation report  [{m['run_status']}]  request {m['request_id']}",
         f"  anchor: {m['anchor']['type']} {m['anchor']['id']}  |  {m['started_at']}  ({m['duration_ms']} ms)",
         f"  outcome: {s['outcome']}", f"  {s['headline']}"]
    if m["completion_reasons"]:
        L.append(f"  incomplete because: {', '.join(m['completion_reasons'])}")
    if s["narrative"]:
        L += ["", "Model-written narrative (filtered against retrieved records; not a verified fact):", f"  {s['narrative']}"]
    L += ["", f"Findings ({len(r['findings'])} verified):"]
    for f in r["findings"]:
        tag = f"{f['classification']}/{f['source']}"
        L.append(f"  - [{tag}] {f.get('pattern_type', '')} {f['finding_id']}")
        L.append(f"      {f['description']}")
        L.append(f"      transactions: {', '.join(f['supporting_transaction_ids'])}")
        L.append(f"      evidence: {', '.join(f['evidence_ids'])}")
    for u in r["unverified_findings"]:
        L.append(f"  ! UNVERIFIED {u['pattern_type']} {u['detection_id']}: {u['reason']}")
    if r["outstanding_questions"]:
        L += ["", "Outstanding questions:"] + [f"  - {q}" for q in r["outstanding_questions"]]
    if r["recommended_next_steps"]:
        L += ["", "Suggested next steps (model, filtered):"] + [f"  - {q}" for q in r["recommended_next_steps"]]
    v = r["verification"]
    L += ["", f"Verification: {v['detections_verified']}/{v['detections_retrieved']} detections re-verified against the graph; "
          f"model text: {v['model_text']['sentences_rejected']} of {v['model_text']['sentences']} sentence(s) rejected as ungrounded."]
    if m["investigation_id"]:
        L.append(f"Persisted as investigation {m['investigation_id']}")
    a = r["investigation_activity"]
    L.append(f"Tools executed: {', '.join(m['tools_executed']) or 'none'}")
    if trace:
        L.append("Trace:")
        for c in a["tool_calls"]:
            L.append(f"  {c['seq']:>2}. {c['tool']} {c['arguments']} -> {c['status']} ({c['duration_ms']} ms)"
                     + (" TRUNCATED" if c["truncated"] else "") + (f" ERROR {c['error']}" if c["error"] else ""))
    u = a["provider_usage"]
    if u:
        L.append(f"Provider usage: {u.get('requests')} request(s), {u.get('retries')} retr(ies), tokens in/out: "
                 f"{u.get('input_tokens')}/{u.get('output_tokens')} (None = not reported)")
    L += ["", "Limitations:"] + [f"  - {x}" for x in r["limitations"]]
    return "\n".join(L)


def render_markdown(r: Dict[str, Any], *, trace: bool = False) -> str:
    m, s = r["metadata"], r["summary"]
    L = [f"# Investigation report", f"**Status:** {m['run_status']} · **Outcome:** {s['outcome']}  ",
         f"**Anchor:** `{m['anchor']['id']}` ({m['anchor']['type']}) · **Request:** `{m['request_id']}`", "", f"> {s['headline']}", ""]
    if m["completion_reasons"]:
        L += [f"**Incomplete because:** {', '.join(m['completion_reasons'])}", ""]
    if s["narrative"]:
        L += ["## Model narrative (filtered)", s["narrative"], ""]
    L.append("## Findings")
    for f in r["findings"]:
        L += [f"### {f['classification'].title()} — {f.get('pattern_type', 'model commentary')}", f"`{f['finding_id']}` · source: {f['source']}",
              "", f["description"], "", "- Transactions: " + ", ".join(f"`{t}`" for t in f["supporting_transaction_ids"]),
              "- Evidence: " + ", ".join(f"`{e}`" for e in f["evidence_ids"]), ""]
    for u in r["unverified_findings"]:
        L.append(f"- **Unverified** `{u['detection_id']}`: {u['reason']}")
    if r["outstanding_questions"]:
        L += ["", "## Outstanding questions"] + [f"- {q}" for q in r["outstanding_questions"]]
    if trace:
        L += ["", "## Tool trace"] + [f"{c['seq']}. `{c['tool']}` {c['arguments']} → {c['status']} ({c['duration_ms']} ms)"
                                       for c in r["investigation_activity"]["tool_calls"]]
    L += ["", "## Limitations"] + [f"- {x}" for x in r["limitations"]]
    return "\n".join(L)
