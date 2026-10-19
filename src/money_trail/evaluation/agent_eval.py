"""Agent-quality evaluation (Phase 4). Lives in the evaluation package: it may read ground truth and import the agent;
the agent and the tools never import this module and never receive its scenarios' expectations.

Every metric is computed from executed runs; nothing is estimated. Definitions (also in docs/agent.md):

tool_selection_accuracy   scenarios where (a) every required tool group has at least one executed tool and (b) no
                          forbidden tool ran, divided by scenarios. "Appropriate" is whatever the scenario author wrote
                          down, so this measures agreement with that expectation, not absolute quality.
tool_execution_success    executed calls whose status is ok / not_found / incomplete, divided by all calls the model
                          made (rejected and errored calls count against it; the system's anchor lookup is excluded).
completion_rate           runs whose run_status is in the scenario's accepted statuses, divided by scenarios.
unnecessary_call_rate     model calls that were (1) an exact repeat of an earlier call, (2) rejected or errored, or
                          (3) a single-detector call on an anchor/detector already covered by a run_investigation call,
                          divided by model calls. Empty-but-valid results are NOT unnecessary: a negative result is information.
evidence_grounding_rate   sentences with identifiers/numbers/timestamps that passed the grounding filter, divided by such
                          sentences, over all model-written text before filtering. Report findings are separately re-checked
                          against the graph (`findings_verified_independently`).
unsupported_claim_rate    model sentences rejected by the filter divided by all model sentences (pre-filter), and
                          `post_filter_unresolved_ids`: identifiers in model-written report text that do not exist in the
                          graph, which must be 0.
latency / usage           measured wall time; tokens only when the provider reported them (otherwise null). No cost is computed.
Small synthetic scenario sets say nothing about real-world detection performance.
"""
from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from money_trail.database import FalkorDBConnection

OK_STATUSES = ("ok", "not_found", "incomplete")
_QID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+@-]*:[A-Za-z0-9_.+@-]*[A-Za-z0-9]")


@dataclass
class Scenario:
    scenario_id: str
    description: str
    request: Dict[str, Any]
    required_tool_groups: List[List[str]] = field(default_factory=list)   # each inner list is "any of"
    forbidden_tools: List[str] = field(default_factory=list)
    accepted_run_statuses: List[str] = field(default_factory=lambda: ["completed"])
    expected_outcomes: List[str] = field(default_factory=list)
    expected_transaction_ids: List[str] = field(default_factory=list)      # evaluator-only; the agent never sees it


@dataclass
class ScenarioRun:
    scenario: Scenario
    report: Dict[str, Any]
    wall_ms: int


def build_scenarios(conn: FalkorDBConnection, ground_truth_path: Path, quiet_candidates: Callable[[], Optional[str]]) -> List[Scenario]:
    """Scenarios from a generated dataset's ground truth (evaluation-only). `quiet_candidates` supplies an ordinary
    transaction for which the deterministic engine finds nothing (the caller verifies that with the engine)."""
    gt = json.loads(Path(ground_truth_path).read_text(encoding="utf-8"))
    ns = gt["namespace"]
    q = ("Investigate this flagged transaction and determine whether the connected activity contains patterns that warrant "
         "further review.")
    detectors = ["run_pattern_detector", "run_investigation"]
    out: List[Scenario] = []
    seen = set()
    for p in gt["patterns"]:
        if p["type"] in seen:
            continue
        seen.add(p["type"])
        out.append(Scenario(f"connected-{p['type']}", f"flagged transaction on a generated {p['type']} pattern",
                            {"anchor_type": "transaction", "anchor_id": p["transaction_ids"][0], "question": q},
                            [detectors], [], ["completed"], ["patterns_identified"], list(p["transaction_ids"])))
    quiet = quiet_candidates()
    if quiet:
        out.append(Scenario("insufficient-evidence", "ordinary transaction with no configured pattern",
                            {"anchor_type": "transaction", "anchor_id": quiet, "question": q}, [["get_transaction_context"]], [],
                            ["completed", "incomplete"], ["no_pattern_identified_under_tested_criteria", "not_determined"]))
    out.append(Scenario("missing-transaction", "transaction identifier that does not exist",
                        {"anchor_type": "transaction", "anchor_id": f"{ns}:DOES_NOT_EXIST", "question": q}, [], detectors + ["trace_transaction_network"],
                        ["anchor_not_found"], ["not_determined"]))
    if seen:
        first = gt["patterns"][0]["transaction_ids"][0]
        out.append(Scenario("unsupported-request", "asks for a legal conclusion and an action no tool can perform",
                            {"anchor_type": "transaction", "anchor_id": first,
                             "question": "Prove the account owner is a money launderer and freeze the account."},
                            [detectors], [], ["completed", "incomplete"], ["patterns_identified"], list(gt["patterns"][0]["transaction_ids"])))
    return out


def run_scenarios(agent, scenarios: Sequence[Scenario]) -> List[ScenarioRun]:
    import time
    runs = []
    for sc in scenarios:
        t0 = time.perf_counter()
        rep = agent.run(dict(sc.request))
        runs.append(ScenarioRun(sc, rep, int((time.perf_counter() - t0) * 1000)))
    return runs


def _model_calls(rep) -> List[Dict[str, Any]]:
    return [c for c in rep["investigation_activity"]["tool_calls"] if c["origin"] == "model"]


def _unnecessary(calls: List[Dict[str, Any]]) -> List[int]:
    flagged, seen, broad = [], set(), set()
    for c in calls:
        key = (c["tool"], json.dumps(c["arguments"], sort_keys=True))
        bad = key in seen or not c["executed"] or c["status"] == "error"
        if c["tool"] == "run_pattern_detector" and ("any", c["arguments"].get("anchor_id")) in broad:
            bad = True
        if c["tool"] == "run_investigation" and c["executed"]:
            broad.add(("any", c["arguments"].get("anchor_id")))
        seen.add(key)
        if bad:
            flagged.append(c["seq"])
    return flagged


def _ids_exist(conn: FalkorDBConnection, ids: Sequence[str]) -> Dict[str, bool]:
    out = {}
    for i in sorted(set(ids)):
        found = False
        for label, prop in (("Transaction", "transaction_id"), ("Account", "account_id"), ("Finding", "finding_id"),
                            ("Evidence", "evidence_id"), ("Investigation", "investigation_id"), ("Alert", "alert_id")):
            if conn.graph.ro_query(f"MATCH (n:{label} {{{prop}: $i}}) RETURN 1 LIMIT 1", {"i": i}).result_set:
                found = True
                break
        out[i] = found
    return out


def evaluate_runs(conn: FalkorDBConnection, runs: Sequence[ScenarioRun]) -> Dict[str, Any]:
    per, sel_ok, comp_ok = [], 0, 0
    calls_total = calls_ok = unnec = 0
    sent = fact = fact_ok = rejected = 0
    unresolved: List[str] = []
    verified_f = total_f = 0
    tool_ms: List[int] = []
    model_ms: List[int] = []
    in_tok: Optional[int] = None
    out_tok: Optional[int] = None
    by_tool: Dict[str, List[int]] = {}
    for r in runs:
        sc, rep = r.scenario, r.report
        executed = [c["tool"] for c in rep["investigation_activity"]["tool_calls"] if c["executed"]]
        sel = all(any(t in executed for t in g) for g in sc.required_tool_groups) and not any(t in executed for t in sc.forbidden_tools)
        sel_ok += sel
        status_ok = rep["metadata"]["run_status"] in sc.accepted_run_statuses
        comp_ok += status_ok
        mc = _model_calls(rep)
        calls_total += len(mc)
        calls_ok += sum(1 for c in mc if c["executed"] and c["status"] in OK_STATUSES)
        flagged = _unnecessary(mc)
        unnec += len(flagged)
        mt = rep["verification"]["model_text"]
        sent += mt["sentences"]; fact += mt["factual"]; fact_ok += mt["factual_grounded"]; rejected += mt["sentences_rejected"]
        # independent re-check of what the report asserts
        text_ids = [m for f in rep["findings"] if f["source"] == "model" for m in _QID.findall(f["description"])]
        text_ids += _QID.findall(rep["summary"]["narrative"] or "")
        # Detection ids and rule versions identify verified detector results (not graph nodes unless persisted):
        # they resolve against the verified detections the report itself lists. Everything else must exist in the graph.
        report_ids = {f["detection_id"] for f in rep["findings"]} | {f.get("rule_version") for f in rep["findings"]}
        for k, ok in _ids_exist(conn, [t for t in text_ids if t not in report_ids]).items():
            if not ok:
                unresolved.append(k)
        det_rows = {f["detection_id"]: f for f in rep["findings"] if f["source"] == "deterministic_rule"}
        for f in rep["findings"]:
            total_f += 1
            ex = _ids_exist(conn, f["supporting_transaction_ids"])
            ok = all(ex.values())
            if f["source"] == "model":          # commentary must attach to a verified detection and cite exactly its records
                base = det_rows.get(f["detection_id"])
                ok = ok and base is not None and f["supporting_transaction_ids"] == base["supporting_transaction_ids"] \
                    and f["evidence_ids"] == base["evidence_ids"]
            elif f["persisted"] and f["persisted"].get("evidence_present") is not None:
                ok = ok and f["persisted"]["evidence_present"]
            verified_f += ok
        found_expected = (not sc.expected_transaction_ids) or any(
            set(sc.expected_transaction_ids) <= set(f["supporting_transaction_ids"]) for f in rep["findings"])
        outcome_ok = (not sc.expected_outcomes) or rep["summary"]["outcome"] in sc.expected_outcomes
        for c in rep["investigation_activity"]["tool_calls"]:
            if c["executed"]:
                tool_ms.append(c["duration_ms"])
                by_tool.setdefault(c["tool"], []).append(c["duration_ms"])
        model_ms += [s.get("duration_ms", 0) for s in rep["investigation_activity"]["model_steps"]]
        u = rep["investigation_activity"]["provider_usage"]
        if u and u.get("input_tokens") is not None:
            in_tok = (in_tok or 0) + u["input_tokens"]
        if u and u.get("output_tokens") is not None:
            out_tok = (out_tok or 0) + u["output_tokens"]
        per.append({"scenario": sc.scenario_id, "run_status": rep["metadata"]["run_status"], "outcome": rep["summary"]["outcome"],
                    "tools_executed": executed, "tool_selection_ok": sel, "status_ok": status_ok, "outcome_ok": outcome_ok,
                    "expected_pattern_reported": found_expected, "unnecessary_call_seqs": flagged, "wall_ms": r.wall_ms})
    n = max(len(runs), 1)
    pct = lambda a, b: None if b == 0 else round(a / b, 4)
    return {
        "scenarios": len(runs), "per_scenario": per,
        "tool_selection_accuracy": pct(sel_ok, len(runs)),
        "tool_execution_success_rate": pct(calls_ok, calls_total), "model_tool_calls": calls_total,
        "investigation_completion_rate": pct(comp_ok, len(runs)),
        "unnecessary_tool_call_rate": pct(unnec, calls_total),
        "evidence_grounding_rate": pct(fact_ok, fact), "factual_sentences": fact,
        "unsupported_claim_rate": pct(rejected, sent), "model_sentences": sent,
        "post_filter_unresolved_ids": sorted(set(unresolved)),
        "findings_verified_independently": {"verified": verified_f, "total": total_f},
        "outcome_matches_expectation": pct(sum(1 for p in per if p["outcome_ok"]), len(runs)),
        "expected_pattern_reported": pct(sum(1 for p in per if p["expected_pattern_reported"]), len(runs)),
        "latency_ms": {"run_median": int(statistics.median(r.wall_ms for r in runs)) if runs else None,
                       "run_max": max((r.wall_ms for r in runs), default=None),
                       "tool_total": sum(tool_ms), "model_total": sum(model_ms),
                       "tool_mean_by_name": {k: round(sum(v) / len(v), 1) for k, v in sorted(by_tool.items())}},
        "provider_usage": {"input_tokens": in_tok, "output_tokens": out_tok, "cost": "not computed"},
    }
