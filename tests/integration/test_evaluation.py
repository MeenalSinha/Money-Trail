"""Evaluation against the separate ground-truth file, on a real FalkorDB (test M and label-order checks)."""
import json

import pytest

from money_trail.evaluation import report as report_mod
from money_trail.exceptions import DatasetConfigError
from money_trail.investigation.config import parse_detection_config
from money_trail.investigation.engine import InvestigationEngine, InvestigationRequest
from support import detection_dict
from test_detection import CTL, build, cfg, only  # noqa: F401  (shared builders for controlled graphs)


def write_gt(path, patterns, ns):
    path.write_text(json.dumps({"format_version": 1, "namespace": ns, "patterns": patterns}))
    return path


@pytest.fixture
def loaded(ready, run_ingest, make_dataset):
    ds = make_dataset("ev-ns", seed=11)
    assert run_ingest(ds)["status"] == "succeeded"
    return ds


def test_evaluation_matches_an_independent_recomputation(ready, loaded):
    config = cfg()
    rep = report_mod.run_evaluation(ready, config, "ev-ns", loaded.dir / "ground_truth.json")
    assert rep["detection"]["status"] == "patterns_detected" and rep["evaluation"] is not None
    truth = json.loads((loaded.dir / "ground_truth.json").read_text())["patterns"]
    res = InvestigationEngine(ready, config).investigate(InvestigationRequest("dataset", namespace="ev-ns"))
    for ptype in ("circular", "pass_through", "fan_in", "fan_out"):
        gts = [set(p["transaction_ids"]) for p in truth if p["type"] == ptype]
        dets = [set(d.transaction_ids) for d in res.detections if d.pattern_type == ptype]
        tp = sum(1 for g in gts if g in dets)                   # thresholds are 100%/100% => exact set equality
        got = rep["evaluation"]["per_type"][ptype]
        assert (got["ground_truth_instances"], got["detected_instances"]) == (len(gts), len(dets))
        assert (got["true_positives"], got["false_positives"], got["false_negatives"]) == (tp, len(dets) - tp, len(gts) - tp)
        if dets:
            assert got["precision"]["fraction"] in (f"{tp}/{len(dets)}", "1/1") or tp / len(dets) == got["precision"]["value"]
        if gts:
            assert abs(got["recall"]["value"] - tp / len(gts)) < 1e-6
        assert got["search_truncated"] is False
    tot_tp = sum(rep["evaluation"]["per_type"][t]["true_positives"] for t in rep["evaluation"]["per_type"])
    assert rep["evaluation"]["overall_micro"]["true_positives"] == tot_tp
    assert rep["evaluation"]["truncated_searches"] == 0


def test_evaluation_is_deterministic(ready, loaded):
    a = report_mod.run_evaluation(ready, cfg(), "ev-ns", loaded.dir / "ground_truth.json")
    b = report_mod.run_evaluation(ready, cfg(), "ev-ns", loaded.dir / "ground_truth.json")
    strip = lambda r: {k: v for k, v in r["evaluation"].items()} | {"t": 0}
    assert strip(a) == strip(b)
    assert [d for d in a["detection"]["detectors"]] == [d for d in b["detection"]["detectors"]]


def test_labels_are_read_only_after_detection_has_finished(ready, loaded, monkeypatch):
    state = {"detected": False, "loaded_before_detection": None}
    real_inv, real_load = InvestigationEngine.investigate, report_mod.load_ground_truth

    def inv(self, *a, **k):
        r = real_inv(self, *a, **k)
        state["detected"] = True
        return r

    def load(*a, **k):
        state["loaded_before_detection"] = not state["detected"]
        return real_load(*a, **k)

    monkeypatch.setattr(InvestigationEngine, "investigate", inv)
    monkeypatch.setattr(report_mod, "load_ground_truth", load)
    report_mod.run_evaluation(ready, cfg(), "ev-ns", loaded.dir / "ground_truth.json")
    assert state["loaded_before_detection"] is False and state["detected"]


def test_wrong_namespace_or_missing_file_is_a_clear_error(ready, loaded):
    rep = report_mod.run_evaluation(ready, cfg(), "other", loaded.dir / "ground_truth.json")   # namespace absent from the graph
    assert rep["detection"]["status"] == "anchor_not_found" and rep["evaluation"] is None
    with pytest.raises(DatasetConfigError, match="namespace"):
        report_mod.load_ground_truth(loaded.dir / "ground_truth.json", "other")
    with pytest.raises(DatasetConfigError, match="not found"):
        report_mod.run_evaluation(ready, cfg(), "ev-ns", loaded.dir / "missing.json")


def test_instances_missing_from_the_graph_are_excluded_and_reported(ready, loaded):
    gt = json.loads((loaded.dir / "ground_truth.json").read_text())["patterns"]
    victim = [p for p in gt if p["type"] == "fan_in"][0]
    ready.graph.query("MATCH (t:Transaction {transaction_id: $t}) DETACH DELETE t", {"t": victim["transaction_ids"][0]})
    rep = report_mod.run_evaluation(ready, cfg(), "ev-ns", loaded.dir / "ground_truth.json")
    fi = rep["evaluation"]["per_type"]["fan_in"]
    assert fi["excluded_instances"] == 1
    assert fi["ground_truth_instances"] == len([p for p in gt if p["type"] == "fan_in"]) - 1


def test_truncated_searches_are_counted_in_the_report(ready, loaded):
    capped = cfg(x=lambda r: r["limits"].update(max_candidate_paths=1))
    rep = report_mod.run_evaluation(ready, capped, "ev-ns", loaded.dir / "ground_truth.json")
    assert rep["detection"]["complete"] is False and rep["evaluation"]["truncated_searches"] >= 1
    assert any(x["search_truncated"] for x in rep["evaluation"]["per_type"].values())


def test_disabled_detector_is_not_evaluated(ready, loaded):
    rep = report_mod.run_evaluation(ready, cfg(x=only("circular")), "ev-ns", loaded.dir / "ground_truth.json")
    assert rep["evaluation"]["per_type"]["fan_in"]["evaluated"] is False
    assert rep["evaluation"]["per_type"]["circular"]["evaluated"] is True


def test_m_controlled_case_matches_hand_computed_metrics(ready, tmp_path):
    # circular instances: R (a real cycle) and G (labelled, but its gap exceeds the rule); plus an unlabelled real cycle U
    h = 3_600_000
    build(ready, [("r1", "A", "B", 0), ("r2", "B", "C", 10), ("r3", "C", "A", 20),
                  ("g1", "D", "E", 0), ("g2", "E", "F", 2 * h), ("g3", "F", "D", 2 * h + 10),
                  ("u1", "P", "Q", 0), ("u2", "Q", "R", 10), ("u3", "R", "P", 20)])
    gt = write_gt(tmp_path / "gt.json", [
        {"pattern_id": "R", "type": "circular", "transaction_ids": ["r1", "r2", "r3"], "account_ids": ["A", "B", "C"]},
        {"pattern_id": "G", "type": "circular", "transaction_ids": ["g1", "g2", "g3"], "account_ids": ["D", "E", "F"]}], CTL)
    rep = report_mod.run_evaluation(ready, cfg(x=only("circular")), CTL, gt)
    c = rep["evaluation"]["per_type"]["circular"]
    # detections: R and U; labelled: R and G  =>  TP=1, FP=1 (U), FN=1 (G)
    assert (c["ground_truth_instances"], c["detected_instances"], c["true_positives"], c["false_positives"], c["false_negatives"]) == (2, 2, 1, 1, 1)
    assert c["precision"] == {"value": 0.5, "fraction": "1/2"} and c["recall"] == {"value": 0.5, "fraction": "1/2"}
    assert c["f1"] == {"value": 0.5, "fraction": "1/2"}
    assert c["account_coverage"]["fraction"] == "3/6" and c["transaction_coverage"]["fraction"] == "3/6"
    assert c["false_positive_examples"][0]["classification"] == "no_overlap_with_any_labelled_instance"
    assert c["false_negative_examples"][0]["pattern_id"] == "G" and c["false_negative_examples"][0]["best_overlap_transactions"] == 0
    text = report_mod.render_text(rep)
    assert "precision 0.5000 (1/2)" in text and "FN G" in text and "synthetic" in text.lower()


def test_m_overlap_thresholds_change_the_verdict_as_documented(ready, tmp_path):
    build(ready, [("a1", "A", "B", 0), ("a2", "B", "C", 10), ("a3", "C", "D", 20), ("a4", "D", "E", 30)])
    gt = write_gt(tmp_path / "gt.json", [{"pattern_id": "L", "type": "pass_through", "transaction_ids": ["a1", "a2", "a3"],
                                           "account_ids": ["A", "B", "C", "D"]}], CTL)
    # the detector reports the maximal chain a1..a4 (4 transactions): covers 3/3 of the label, purity 3/4 = 75%
    strict = cfg(x=lambda r: (only("pass_through")(r), r["evaluation"]["match"].update(min_detection_purity_percent=100)))
    loose = cfg(x=lambda r: (only("pass_through")(r), r["evaluation"]["match"].update(min_detection_purity_percent=75)))
    s = report_mod.run_evaluation(ready, strict, CTL, gt)["evaluation"]["per_type"]["pass_through"]
    l = report_mod.run_evaluation(ready, loose, CTL, gt)["evaluation"]["per_type"]["pass_through"]
    assert (s["true_positives"], s["false_positives"], s["false_negatives"]) == (0, 1, 1)
    assert s["false_positive_examples"][0]["classification"] == "partial_overlap_with_same_type_instance"
    assert (l["true_positives"], l["false_positives"], l["false_negatives"]) == (1, 0, 0)
