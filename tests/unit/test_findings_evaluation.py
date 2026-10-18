import ast
import json
from fractions import Fraction
from pathlib import Path

import pytest

from money_trail.evaluation import metrics as m
from money_trail.evaluation.ground_truth import parse_ground_truth
from money_trail.evaluation.matching import DetItem, TruthItem, match_type
from money_trail.evaluation.report import evaluate_detections
from money_trail.exceptions import DatasetConfigError
from money_trail.investigation.config import parse_detection_config
from money_trail.investigation.detectors.models import Detection, TxRecord
from money_trail.investigation.evidence import _check_structure
from money_trail.investigation.findings import (
    HYPOTHESIS_TEXT, evidence_row, finding_id, finding_rows, investigation_id,
)
from support import detection_dict

SRC = Path(__file__).resolve().parents[2] / "src" / "money_trail"


def det(ptype="circular", ids=("t1", "t2", "t3"), rule="circular:r@1+x", roles=None, observed=None, created=111):
    recs = tuple(TxRecord(i, f"A{k}", f"A{(k + 1) % len(ids)}", 100, "USD", 10 * k) for k, i in enumerate(ids))
    return Detection(f"det:{ptype}{'-'.join(ids)}", ptype, "t1", "ns", tuple(sorted({r.source_account for r in recs})), ids, recs,
                     {"configured": {"max_gap_ms": 5}, "observed": observed or {"transactions": len(ids), "distinct_accounts": len(ids),
                                                                                "accounts": len(ids) + 1, "window_span_ms": 20,
                                                                                "distinct_counterparties": len(ids)}},
                     ("evd:x",), False, (), rule, created, roles or {})


# ---- finding semantics ---------------------------------------------------------------------------
def test_finding_kinds_and_text():
    d = det()
    rows = finding_rows(d, hypothesis=True)
    assert [r["kind"] for r in rows] == ["observation", "hypothesis"]
    assert rows[1]["statement"] == HYPOTHESIS_TEXT and "unconfirmed" in rows[1]["statement"]
    assert "does not establish wrongdoing" in rows[0]["statement"]
    assert [r["kind"] for r in finding_rows(d, hypothesis=False)] == ["observation"]
    for r in rows:
        text = r["statement"].lower()
        assert not any(w in text for w in ("laundering confirmed", "guilty", "fraud confirmed", "criminal"))
        assert r["created_at"] == 111 and "confidence" not in r            # no invented confidence


def test_findings_never_embed_amounts_or_account_ids():
    d = det()
    for r in finding_rows(d, True) + [evidence_row(d)]:
        blob = json.dumps(r)
        assert "A0" not in blob and "100" not in [t for t in blob.split() if t.isdigit()]


def test_ids_are_deterministic_and_distinct_per_kind():
    d = det()
    assert finding_id(d.detection_id, "observation") == finding_id(d.detection_id, "observation")
    assert finding_id(d.detection_id, "observation") != finding_id(d.detection_id, "hypothesis")
    assert evidence_row(d)["evidence_id"] == evidence_row(d)["evidence_id"]
    cfg = parse_detection_config(detection_dict())
    assert investigation_id("ns", "t1", cfg) == investigation_id("ns", "t1", cfg)
    assert investigation_id("ns", "t1", cfg) != investigation_id("ns", "t2", cfg)
    cfg2 = parse_detection_config({**detection_dict(), "persistence": {"hypothesis_findings": False}})
    assert investigation_id("ns", "t1", cfg) == investigation_id("ns", "t1", cfg2)     # not a detection rule


def test_evidence_row_names_tool_and_rule():
    e = evidence_row(det("fan_in"))
    assert e["produced_by_tool"] == "money_trail.detector.fan_in" and e["query_ref"] == "circular:r@1+x"
    assert "3 transaction" in e["description"] and len(e["description"]) < 2000


def test_detection_serialization_roundtrips_through_json():
    d = det()
    back = json.loads(json.dumps(d.to_dict()))
    assert back["transaction_ids"] == ["t1", "t2", "t3"] and back["ordered_path"][0]["amount"] == 100
    assert set(back) >= {"detection_id", "pattern_type", "anchor_id", "dataset_namespace", "account_ids", "ordered_path",
                         "criteria", "evidence_refs", "truncated", "warnings", "rule_version", "created_at"}


# ---- structural evidence checks ------------------------------------------------------------------
def test_structure_checks_catch_broken_paths():
    good = det()
    assert _check_structure(good) == ""
    broken = TxRecord("t2", "ZZ", "A2", 100, "USD", 10)
    bad = Detection(**{**good.__dict__, "ordered_path": (good.ordered_path[0], broken, good.ordered_path[2])})
    assert "not connected" in _check_structure(bad)
    backwards = Detection(**{**good.__dict__, "ordered_path": (good.ordered_path[0], TxRecord("t2", "A1", "A2", 1, "USD", -5),
                                                              good.ordered_path[2])})
    assert "chronological" in _check_structure(backwards)
    open_cycle = Detection(**{**good.__dict__, "ordered_path": good.ordered_path[:2]})
    assert "return" in _check_structure(open_cycle)
    fan = det("fan_in", roles={"collector": "A0"})
    assert "collector" in _check_structure(fan)
    assert "fewer" in _check_structure(Detection(**{**good.__dict__, "ordered_path": good.ordered_path[:1]}))


# ---- ground truth loading -------------------------------------------------------------------------
def gt_raw(**over):
    raw = {"format_version": 1, "namespace": "ns", "patterns": [
        {"pattern_id": "p1", "type": "circular", "transaction_ids": ["t1", "t2"], "account_ids": ["a1"]}]}
    raw.update(over)
    return raw


def test_ground_truth_validation():
    assert parse_ground_truth(gt_raw(), "ns").patterns[0].pattern_id == "p1"
    for bad, match in [(gt_raw(format_version=2), "format_version"), (gt_raw(namespace="other"), "namespace"),
                       (gt_raw(patterns=[{"pattern_id": "p", "type": "smurf", "transaction_ids": ["t"], "account_ids": ["a"]}]), "type"),
                       (gt_raw(patterns=[{"pattern_id": "p", "type": "fan_in", "transaction_ids": [], "account_ids": ["a"]}]), "transaction_ids"),
                       (gt_raw(patterns=[gt_raw()["patterns"][0]] * 2), "unique")]:
        with pytest.raises(DatasetConfigError, match=match):
            parse_ground_truth(bad, "ns")


# ---- metric arithmetic (expected values worked out by hand) -------------------------------------
def D(i, txs):
    return DetItem(i, "circular", frozenset(txs), frozenset())


def G(i, txs):
    return TruthItem(i, "circular", frozenset(txs), frozenset())


def test_matching_exact_case_hand_computed():
    truths = [G("A", "abc"), G("B", "def"), G("C", "ghi")]
    dets = [D("d1", "abc"), D("d2", "de"), D("d3", "xyz")]          # exact; 2/3 of B; unrelated
    r = match_type(dets, truths, 100, 100)
    assert [(d.detection_id, g.pattern_id) for d, g, _ in r.pairs] == [("d1", "A")]
    assert {d.detection_id for d in r.unmatched_detections} == {"d2", "d3"}
    assert {g.pattern_id for g in r.unmatched_truths} == {"B", "C"}
    tp, fp, fn = 1, 2, 2
    p, rc = m.ratio(tp, tp + fp), m.ratio(tp, tp + fn)
    assert (p, rc, m.f1(p, rc)) == (Fraction(1, 3), Fraction(1, 3), Fraction(1, 3))


def test_matching_with_lower_thresholds_accepts_the_partial_detection():
    truths = [G("A", "abc"), G("B", "def"), G("C", "ghi")]
    dets = [D("d1", "abc"), D("d2", "de"), D("d3", "xyz")]
    r = match_type(dets, truths, 60, 100)                           # 2/3 = 66.7% >= 60%
    assert sorted(g.pattern_id for _, g, _ in r.pairs) == ["A", "B"]
    tp, fp, fn = 2, 1, 1
    p, rc = m.ratio(tp, tp + fp), m.ratio(tp, tp + fn)
    assert (p, rc, m.f1(p, rc)) == (Fraction(2, 3), Fraction(2, 3), Fraction(2, 3))
    assert match_type(dets, truths, 67, 100).pairs[0][1].pattern_id == "A" and len(match_type(dets, truths, 67, 100).pairs) == 1   # 66.7 < 67


def test_threshold_boundaries_use_integer_arithmetic():
    truths = [G("A", "abcd")]
    assert len(match_type([D("d", "abc")], truths, 75, 100).pairs) == 1         # 3/4 = exactly 75%
    assert len(match_type([D("d", "abc")], truths, 76, 100).pairs) == 0
    # purity: overlap 3 of detection size 5 = 60%: exactly the threshold
    assert len(match_type([D("d", "abcxy")], truths, 75, 60).pairs) == 1
    assert len(match_type([D("d", "abcxy")], truths, 75, 61).pairs) == 0


def test_one_detection_cannot_match_two_instances_and_ties_are_deterministic():
    truths = [G("A", "ab"), G("B", "ab")]                           # two instances share all transactions
    dets = [D("d1", "ab")]
    r = match_type(dets, truths, 100, 100)
    assert len(r.pairs) == 1 and r.ambiguous_detections == 1 and r.pairs[0][1].pattern_id == "A"
    assert r.unmatched_truths[0].pattern_id == "B"
    r2 = match_type(list(reversed(dets)), list(reversed(truths)), 100, 100)
    assert [(d.detection_id, g.pattern_id) for d, g, _ in r2.pairs] == [("d1", "A")]
    # two detections for one instance: only one counts as TP, the other is a false positive
    r3 = match_type([D("d1", "ab"), D("d2", "ab")], [G("A", "ab")], 100, 100)
    assert len(r3.pairs) == 1 and len(r3.unmatched_detections) == 1


def test_undefined_metrics_are_explicit():
    assert m.ratio(0, 0) is None
    assert m.f1(None, Fraction(1)) is None and m.f1(Fraction(1), None) is None
    assert m.f1(Fraction(0), Fraction(0)) == Fraction(0)
    assert m.as_json(None) is None and m.as_json(Fraction(1, 3)) == {"value": 0.333333, "fraction": "1/3"}
    assert m.as_json(Fraction(2, 2), (2, 2)) == {"value": 1.0, "fraction": "2/2"}      # unreduced counts stay visible
    assert m.f1(Fraction(1, 2), Fraction(1)) == Fraction(2, 3)


def cfg_eval(**ev):
    raw = detection_dict()
    raw["evaluation"]["match"].update(ev)
    return parse_detection_config(raw)


def test_evaluate_detections_report_structure_and_conventions():
    truth = parse_ground_truth({"format_version": 1, "namespace": "ns", "patterns": [
        {"pattern_id": "p1", "type": "circular", "transaction_ids": ["t1", "t2", "t3"], "account_ids": ["a1", "a2", "a3"]},
        {"pattern_id": "p2", "type": "circular", "transaction_ids": ["u1", "u2", "u3"], "account_ids": ["b1", "b2", "b3"]},
        {"pattern_id": "p3", "type": "fan_in", "transaction_ids": ["f1", "f2"], "account_ids": ["c1", "c2", "c3"]}]}, "ns")
    dets = [det("circular", ("t1", "t2", "t3")), det("circular", ("x1", "x2", "x3"))]
    present = {"t1", "t2", "t3", "u1", "u2", "u3"}                   # f1, f2 were never ingested
    cfg = cfg_eval()
    r = evaluate_detections(dets, truth, cfg, {"circular": False}, present)
    c = r["per_type"]["circular"]
    assert (c["ground_truth_instances"], c["detected_instances"], c["true_positives"], c["false_positives"], c["false_negatives"]) == (2, 2, 1, 1, 1)
    assert c["precision"]["fraction"] == "1/2" and c["recall"]["fraction"] == "1/2" and c["f1"]["fraction"] == "1/2"
    assert c["transaction_coverage"]["fraction"] == "3/6"            # 3 of the 6 labelled transactions were detected
    assert c["false_positive_examples"][0]["classification"] == "no_overlap_with_any_labelled_instance"
    assert c["false_negative_examples"][0]["pattern_id"] == "p2" and c["false_negative_examples"][0]["best_overlap_transactions"] == 0
    fi = r["per_type"]["fan_in"]
    assert fi["excluded_instances"] == 1 and fi["ground_truth_instances"] == 0
    assert fi["precision"] is None and fi["recall"] is None and fi["f1"] is None      # nothing detected, nothing labelled
    assert r["per_type"]["pass_through"]["detected_instances"] == 0
    assert r["overall_micro"]["true_positives"] == 1


def test_disabled_detectors_are_reported_as_not_evaluated():
    raw = detection_dict()
    raw["detectors"]["fan_out"] = {"enabled": False}
    truth = parse_ground_truth({"format_version": 1, "namespace": "ns", "patterns": [
        {"pattern_id": "p", "type": "fan_out", "transaction_ids": ["t"], "account_ids": ["a"]}]}, "ns")
    r = evaluate_detections([], truth, parse_detection_config(raw), {}, {"t"})
    assert r["per_type"]["fan_out"] == {"evaluated": False, "reason": "detector disabled in configuration",
                                        "ground_truth_instances": 1}


def test_false_positive_classification_distinguishes_partial_overlap_and_other_types():
    truth = parse_ground_truth({"format_version": 1, "namespace": "ns", "patterns": [
        {"pattern_id": "p1", "type": "circular", "transaction_ids": ["t1", "t2", "t3"], "account_ids": ["a"]},
        {"pattern_id": "q1", "type": "fan_in", "transaction_ids": ["f1", "f2"], "account_ids": ["a"]}]}, "ns")
    dets = [det("circular", ("t1", "t2", "z9")), det("circular", ("f1", "z8", "z7"))]
    r = evaluate_detections(dets, truth, cfg_eval(), {}, {"t1", "t2", "t3", "f1", "f2"})
    kinds = sorted(e["classification"] for e in r["per_type"]["circular"]["false_positive_examples"])
    assert kinds == ["overlaps_instance_of_other_type", "partial_overlap_with_same_type_instance"]


# ---- ground-truth isolation, enforced by inspecting the source ---------------------------------------
def _py_files(sub):
    return sorted((SRC / sub).rglob("*.py"))


def _imports(path):
    tree = ast.parse(path.read_text())
    names = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            names.append(n.module or "")
    return names


def test_investigation_and_ingestion_never_import_the_evaluation_package():
    for sub in ("investigation", "ingestion", "database", "config"):
        for f in _py_files(sub):
            assert not [i for i in _imports(f) if i.startswith("money_trail.evaluation") or i.endswith("ground_truth")], f


def test_investigation_code_never_mentions_ground_truth_or_the_label_file():
    for f in _py_files("investigation"):
        text = f.read_text().lower()
        assert "ground_truth" not in text and "ground truth" not in text and "pattern_id" not in text, f


def test_only_the_evaluation_and_generation_packages_know_the_ground_truth_filename():
    for f in SRC.rglob("*.py"):
        if "ground_truth.json" in f.read_text():
            assert f.parts[-2] in ("generation", "evaluation") or f.name == "cli.py", f
