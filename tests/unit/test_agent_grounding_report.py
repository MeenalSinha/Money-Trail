"""Grounding filter, submission parsing, report building/validation, and runtime isolation (no database)."""
import ast
import pathlib
import re

import pytest

from money_trail.agent.agent import SubmissionError, parse_submission
from money_trail.agent.config import parse_agent_config
from money_trail.agent.grounding import GroundingChecker
from money_trail.agent.ledger import Ledger
from money_trail.agent.report import (
    RunState, Submission, build_report, render_markdown, render_text, validate_report,
)
from money_trail.agent.tools.base import DetectorRun, Facts
from money_trail.agent.tools.registry import ExecutedTool
from money_trail.agent.tools.base import ToolResult
from money_trail.agent.verify import VerifiedDetection
from money_trail.investigation.detectors.models import Detection, TxRecord, evidence_id_for, make_detection_id
from support import agent_dict

CFG = parse_agent_config(agent_dict())
SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "money_trail"
TS = 1767398700675


def tx(i, s, d, amt, ts):
    return TxRecord(f"ns:T{i}", f"ns:{s}", f"ns:{d}", amt, "USD", ts)


def detection(pattern="circular"):
    path = (tx(1, "A", "B", 110982, TS), tx(2, "B", "C", 109872, TS + 1000), tx(3, "C", "A", 108773, TS + 2000))
    did = make_detection_id("ns", pattern, "circular:r@1+abc", "k")
    return Detection(did, pattern, "ns:T1", "ns", ("ns:A", "ns:B", "ns:C"), tuple(t.transaction_id for t in path), path,
                     {"configured": {"max_hops": 5}, "observed": {"transactions": 3, "distinct_accounts": 3, "window_span_ms": 2000}},
                     (evidence_id_for(did),), False, (), "circular:r@1+abc", TS)


def ledger_with(det=None):
    L, f = Ledger(), Facts()
    det = det or detection()
    f.detections.append((det, "run_pattern_detector"))
    for t in det.ordered_path:
        f.transactions[t.transaction_id] = t.to_dict()
    f.accounts |= set(det.account_ids)
    f.detector_runs.append(DetectorRun("circular", "transaction", "ns:T1", True, 1))
    L.apply(f)
    return L, det


def state(L, verified, **kw):
    base = dict(request_id="r1", request={"anchor_id": "ns:T1"}, request_status="valid", request_problems=[], request_warnings=[],
                anchor_type="transaction", anchor_id="ns:T1", started_ms=TS, completed_ms=TS + 50, run_status="completed",
                reasons=[], agent_summary={"provider": "x"}, rule_set="rs@1", rule_fingerprint="fp", ledger=L, verified=verified)
    base.update(kw)
    return RunState(**base)


def checker(L):
    return GroundingChecker(CFG.grounding, L)


# ---- grounding -------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,kept", [
    ("ns:T1 sent 110982 USD to ns:B.", True),
    ("ns:T9 sent 110982 USD.", False),                        # identifier never retrieved
    ("It moved 1,109.82 USD.", False),                        # model arithmetic / conversion
    ("The cycle lasted 2000 ms across 3 accounts.", True),    # values present in the observed criteria / counts
    ("The cycle lasted 2001 ms.", False),
    ("Booked on 2026-01-03.", True),
    ("Booked on 2026-03-03.", False),
    ("Reference TXN123456 is related.", False),               # bare identifier-like token
    ("This is money laundering.", False),
    ("This could be laundering and may warrant review.", True),
    ("This does not establish fraud.", True),
    ("Review is suggested.", True),
])
def test_sentence_level_grounding(text, kept):
    L, _ = ledger_with()
    assert bool(checker(L).check(text).text) is kept


def test_only_failing_sentences_are_dropped_and_stats_are_consistent():
    L, _ = ledger_with()
    r = checker(L).check("ns:T1 is in the cycle. ns:NOPE is also involved. The pattern may warrant review. It is a criminal ring.")
    assert r.text == "ns:T1 is in the cycle. The pattern may warrant review."
    assert r.sentences == 4 and r.factual == 2 and r.factual_grounded == 1 and len(r.rejected) == 2
    assert all(x["reasons"] for x in r.rejected)


def test_forbidden_and_hedge_lists_come_from_configuration():
    L, _ = ledger_with()
    cfg = parse_agent_config(agent_dict(grounding={"forbidden_conclusion_terms": ["bogus"], "hedging_markers": ["maybe"]})).grounding
    g = GroundingChecker(cfg, L)
    assert g.check("This is money laundering.").text                  # not in this configuration's list
    assert not g.check("This is bogus.").text and g.check("Maybe this is bogus.").text


def test_ledger_only_contains_what_was_delivered():
    L = Ledger()
    assert checker(L).check("ns:T1 is mentioned.").text == ""


# ---- submission parsing ----------------------------------------------------------------------------
GOOD = {"summary": "ok", "question_assessment": {"supported": True}}


def test_good_submission_parses():
    s = parse_submission({**GOOD, "detections": [{"detection_id": "d", "hypothesis": "h"}],
                          "outstanding_questions": ["q"], "recommended_next_steps": ["n"]}, 2000)
    assert s.supported and s.detections == {"d": {"hypothesis": "h"}} and s.outstanding_questions == ["q"]


@pytest.mark.parametrize("raw", [
    "x", {}, {"summary": "ok"}, {**GOOD, "extra": 1}, {"summary": "", "question_assessment": {"supported": True}},
    {"summary": "x" * 3000, "question_assessment": {"supported": True}},
    {"summary": "ok", "question_assessment": {"supported": "yes"}},
    {**GOOD, "detections": [{"interpretation": "no id"}]},
    {**GOOD, "detections": [{"detection_id": "d", "verdict": "guilty"}]},
    {**GOOD, "outstanding_questions": "not a list"}, {**GOOD, "outstanding_questions": ["q"] * 21},
    {**GOOD, "recommended_next_steps": [5]},
])
def test_malformed_submissions_are_rejected_not_repaired(raw):
    with pytest.raises(SubmissionError):
        parse_submission(raw, 2000)


# ---- report building -------------------------------------------------------------------------------
def ver(det, ok=True, reason=""):
    return VerifiedDetection(det, "run_pattern_detector", ok, reason, {"in_detector_result": True})


def test_verified_detection_yields_deterministic_findings_even_if_the_model_says_nothing():
    L, det = ledger_with()
    r = build_report(state(L, [ver(det)], submission=Submission("Fine.", True, None, {}, [], [])), checker(L))
    assert validate_report(r) == []
    assert r["summary"]["outcome"] == "patterns_identified"
    kinds = sorted((f["classification"], f["source"]) for f in r["findings"])
    assert kinds == [("hypothesis", "deterministic_rule"), ("observation", "deterministic_rule")]
    obs = next(f for f in r["findings"] if f["classification"] == "observation")
    assert obs["supporting_transaction_ids"] == list(det.transaction_ids) and obs["evidence_ids"] == [evidence_id_for(det.detection_id)]
    assert [t["amount"] for t in obs["transactions"]] == [110982, 109872, 108773]       # from detection records, not text


def test_model_commentary_is_filtered_labelled_and_cannot_create_findings():
    L, det = ledger_with()
    sub = Submission("ns:T1 and 110982 USD appear in the cycle.", True, None,
                     {det.detection_id: {"interpretation": "Funds may have returned to ns:A.", "hypothesis": "This is fraud."},
                      "det:INVENTED": {"interpretation": "Invented detection."}}, [], [])
    r = build_report(state(L, [ver(det)], submission=sub), checker(L))
    model = [f for f in r["findings"] if f["source"] == "model"]
    assert [f["classification"] for f in model] == ["interpretation"] and "Model-written" in model[0]["note"]
    assert all(f["detection_id"] == det.detection_id for f in model)
    assert not any("INVENTED" in repr(f) for f in r["findings"])
    rej = r["verification"]["model_text"]["rejected"]
    assert {x["field"] for x in rej} == {f"detection:{det.detection_id}:hypothesis", "detection:det:INVENTED:interpretation"}
    assert all("sentence" not in x and x["chars"] > 0 and len(x["sha256_12"]) == 12 for x in rej)
    assert r["verification"]["model_text"]["sentences_rejected"] == 2 and validate_report(r) == []


def test_unverified_detection_is_listed_separately_and_excluded_from_outcome_and_commentary():
    L, det = ledger_with()
    sub = Submission("Summary.", True, None, {det.detection_id: {"interpretation": "Looks like a loop."}}, [], [])
    r = build_report(state(L, [ver(det, False, "a supporting transaction no longer exists in the graph")], submission=sub), checker(L))
    assert r["findings"] == [] and r["unverified_findings"][0]["reason"].startswith("a supporting transaction")
    assert r["summary"]["outcome"] == "not_determined" and "failed verification" in r["summary"]["headline"]
    assert any("failed verification" in q for q in r["outstanding_questions"]) and validate_report(r) == []


def test_no_pattern_wording_never_claims_legitimacy():
    L = Ledger()
    L.apply(Facts(detector_runs=[DetectorRun("circular", "transaction", "ns:T1", True, 0)]))
    r = build_report(state(L, []), checker(L))
    h = r["summary"]["headline"]
    assert r["summary"]["outcome"] == "no_pattern_identified_under_tested_criteria"
    assert "tested criteria" in h and "does not establish that the activity is legitimate" in h


def test_incomplete_search_is_not_reported_as_no_pattern():
    L = Ledger()
    L.apply(Facts(detector_runs=[DetectorRun("circular", "transaction", "ns:T1", False, 0)]))
    r = build_report(state(L, []), checker(L))
    assert r["summary"]["outcome"] == "not_determined"
    L2 = Ledger()
    assert build_report(state(L2, []), checker(L2))["summary"]["outcome"] == "not_determined"      # nothing ran


def test_failures_truncation_and_provider_errors_become_outstanding_questions():
    L, det = ledger_with()
    trace = [ExecutedTool("trace_transaction_network", {"anchor_id": "ns:T1"}, ToolResult("incomplete", {}, True, ["truncated"]), 3, True, "{}"),
             ExecutedTool("run_pattern_detector", {"detector": "fan_in", "anchor_id": "ns:T1"}, ToolResult("error", {"error": "x"}), 1, True, "{}", "DB down")]
    r = build_report(state(L, [ver(det)], trace=trace, run_status="incomplete", reasons=["provider_error:rate_limited", "tool_call_limit_reached"]), checker(L))
    q = " ".join(r["outstanding_questions"])
    assert "truncated or incomplete" in q and "failed: DB down" in q and "provider failure (rate_limited)" in q and "tool-call limit" in q
    assert len(r["findings"]) == 2 and r["metadata"]["completed_within_limits"] is False        # earlier valid findings are kept
    assert validate_report(r) == []


def test_unsupported_question_assessment_is_reported_without_conclusion():
    L, det = ledger_with()
    sub = Submission("Structural summary.", False, "A legal determination cannot be made from graph data.", {}, [], [])
    r = build_report(state(L, [ver(det)], submission=sub), checker(L))
    assert r["request"]["question_assessment"]["supported"] is False
    assert any("cannot establish" in q for q in r["outstanding_questions"])


def test_validate_report_catches_broken_reports():
    L, det = ledger_with()
    r = build_report(state(L, [ver(det)]), checker(L))
    import copy
    for mut, frag in [(lambda x: x["findings"][0].update(classification="verdict"), "bad classification"),
                      (lambda x: x["findings"][0].update(verified=False), "only verified"),
                      (lambda x: x["findings"][0].update(evidence_ids=[]), "missing supporting"),
                      (lambda x: x["metadata"].update(run_status="great"), "run_status"),
                      (lambda x: x["summary"].update(outcome="guilty"), "outcome"),
                      (lambda x: x["metadata"].update(completion_reasons=["x"]), "completed run"),
                      (lambda x: x.pop("verification"), "missing section"),
                      (lambda x: x["metadata"].update(error="api_key=abc"), "credential")]:
        bad = copy.deepcopy(r)
        mut(bad)
        assert any(frag in p for p in validate_report(bad)), frag


def test_renderers_show_status_findings_limitations_and_trace():
    L, det = ledger_with()
    r = build_report(state(L, [ver(det)]), checker(L))
    t = render_text(r, trace=True)
    assert "patterns_identified" in t and det.detection_id[:10] not in "" and "Limitations:" in t and "not a finding of wrongdoing" in t
    assert "# Investigation report" in render_markdown(r) and "## Limitations" in render_markdown(r)


# ---- isolation and hygiene -------------------------------------------------------------------------
def _imports(path):
    tree = ast.parse(path.read_text())
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            yield from (a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            yield (n.module or "")


@pytest.mark.parametrize("pkg", ["agent", "investigation", "database", "ingestion", "config"])
def test_runtime_packages_never_import_the_evaluation_package(pkg):
    for f in (SRC / pkg).rglob("*.py"):
        assert not any(m.startswith("money_trail.evaluation") for m in _imports(f)), f


def test_runtime_code_never_reads_ground_truth_or_takes_file_paths():
    for f in (SRC / "agent").rglob("*.py"):
        text = f.read_text().lower()
        assert "ground_truth" not in text.replace("ground-truth", "").replace("ground truth", "") or f.name in ("__init__.py",), f
    for f in (SRC / "agent" / "tools").rglob("*.py"):
        assert "open(" not in f.read_text() and "read_text" not in f.read_text(), f


def test_no_credentials_or_model_identifiers_in_source():
    for f in SRC.rglob("*.py"):
        t = f.read_text()
        assert not re.search(r"sk-ant|sk-[A-Za-z0-9]{20,}", t), f
        assert not re.search(r"claude-(opus|sonnet|haiku|fable|mythos)", t), f
    assert "claude-" in pathlib.Path("examples/agent.example.json").read_text()      # the one place a model id lives


def test_numbers_stated_by_delivered_tool_results_are_grounded_but_others_are_not():
    L, _ = ledger_with()
    assert not checker(L).check("The forward trace found 7 path(s).").text            # nothing delivered states 7
    L.add_payload_numbers({"path_count": 7, "paths": [[1], [2], [3]], "ok": True, "name": "x"})
    c = checker(L)
    assert c.check("The forward trace found 7 path(s).").text and c.check("It returned 3 lists.").text
    assert not c.check("It found 8 paths.").text and 1 not in L.payload_numbers - {7, 3}       # bool is not a number
