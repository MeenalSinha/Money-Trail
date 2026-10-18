"""Evaluation runner and report.

Order is enforced here: the detection run completes first, without any label; only then are the
ground-truth labels loaded and compared with the detector output.
"""
from __future__ import annotations

from typing import Any, Dict, List

from money_trail.database import FalkorDBConnection, GraphStore
from money_trail.evaluation import metrics as m
from money_trail.evaluation.ground_truth import GroundTruth, load_ground_truth
from money_trail.evaluation.matching import DetItem, TruthItem, match_type
from money_trail.investigation.config import DetectionConfig
from money_trail.investigation.detectors.models import Detection, PATTERN_ORDER
from money_trail.investigation.engine import InvestigationEngine, InvestigationRequest

EXAMPLE_LIMIT = 10


def evaluate_detections(detections: List[Detection], truth: GroundTruth, config: DetectionConfig,
                        truncated_by_type: Dict[str, bool], present_tx: set) -> Dict[str, Any]:
    ev = config.evaluation
    per_type: Dict[str, Any] = {}
    tp_all = fp_all = fn_all = 0
    for ptype in PATTERN_ORDER:
        gts_all = [g for g in truth.patterns if g.pattern_type == ptype]
        if not config.enabled[ptype]:
            per_type[ptype] = {"evaluated": False, "reason": "detector disabled in configuration",
                               "ground_truth_instances": len(gts_all)}
            continue
        excluded = [g for g in gts_all if not set(g.transaction_ids) <= present_tx]
        gts = [TruthItem(g.pattern_id, g.pattern_type, frozenset(g.transaction_ids), frozenset(g.account_ids))
               for g in gts_all if g not in excluded]
        dets = [DetItem(d.detection_id, d.pattern_type, frozenset(d.transaction_ids), frozenset(d.account_ids))
                for d in detections if d.pattern_type == ptype]
        res = match_type(dets, gts, ev.min_gt_coverage_percent, ev.min_detection_purity_percent)
        tp, fp, fn = len(res.pairs), len(res.unmatched_detections), len(res.unmatched_truths)
        tp_all, fp_all, fn_all = tp_all + tp, fp_all + fp, fn_all + fn
        p, r = m.ratio(tp, tp + fp), m.ratio(tp, tp + fn)
        det_tx = set().union(*[d.transaction_ids for d in dets]) if dets else set()
        det_ac = set().union(*[d.account_ids for d in dets]) if dets else set()
        gt_tx = set().union(*[g.transaction_ids for g in gts]) if gts else set()
        gt_ac = set().union(*[g.account_ids for g in gts]) if gts else set()
        other_gt_tx = set().union(*[set(g.transaction_ids) for g in truth.patterns if g.pattern_type != ptype] or [set()])
        fp_examples = []
        for d in res.unmatched_detections[:EXAMPLE_LIMIT]:
            same = max((len(d.transaction_ids & g.transaction_ids) for g in gts), default=0)
            kind = ("partial_overlap_with_same_type_instance" if same else
                    "overlaps_instance_of_other_type" if d.transaction_ids & other_gt_tx else
                    "no_overlap_with_any_labelled_instance")
            fp_examples.append({"detection_id": d.detection_id, "transactions": len(d.transaction_ids),
                                "accounts": len(d.account_ids), "classification": kind, "shared_with_same_type": same})
        fn_examples = []
        for g in res.unmatched_truths[:EXAMPLE_LIMIT]:
            best = max(((len(d.transaction_ids & g.transaction_ids), d.detection_id) for d in dets), default=(0, None))
            fn_examples.append({"pattern_id": g.pattern_id, "transactions": len(g.transaction_ids),
                                "best_overlap_transactions": best[0], "best_detection_id": best[1] if best[0] else None})
        per_type[ptype] = {
            "evaluated": True,
            "ground_truth_instances": len(gts), "excluded_instances": len(excluded),
            "detected_instances": len(dets), "true_positives": tp, "false_positives": fp, "false_negatives": fn,
            "precision": m.as_json(p, (tp, tp + fp)), "recall": m.as_json(r, (tp, tp + fn)), "f1": m.as_json(m.f1(p, r)),
            "account_coverage": m.as_json(m.ratio(len(gt_ac & det_ac), len(gt_ac)), (len(gt_ac & det_ac), len(gt_ac))),
            "transaction_coverage": m.as_json(m.ratio(len(gt_tx & det_tx), len(gt_tx)), (len(gt_tx & det_tx), len(gt_tx))),
            "ambiguous_detections_resolved": res.ambiguous_detections,
            "search_truncated": bool(truncated_by_type.get(ptype)),
            "false_positive_examples": fp_examples, "false_negative_examples": fn_examples,
        }
    p, r = m.ratio(tp_all, tp_all + fp_all), m.ratio(tp_all, tp_all + fn_all)
    return {"per_type": per_type,
            "overall_micro": {"true_positives": tp_all, "false_positives": fp_all, "false_negatives": fn_all,
                              "precision": m.as_json(p, (tp_all, tp_all + fp_all)), "recall": m.as_json(r, (tp_all, tp_all + fn_all)),
                              "f1": m.as_json(m.f1(p, r))},
            "matching": {"min_gt_coverage_percent": ev.min_gt_coverage_percent,
                         "min_detection_purity_percent": ev.min_detection_purity_percent,
                         "assignment": "greedy one-to-one, best overlap first, deterministic tie-breaks"}}


def run_evaluation(conn: FalkorDBConnection, config: DetectionConfig, namespace: str, ground_truth_path) -> Dict[str, Any]:
    # 1. detection first, from graph data and detection rules only
    engine = InvestigationEngine(conn, config)
    result = engine.investigate(InvestigationRequest("dataset", namespace=namespace))
    report: Dict[str, Any] = {
        "namespace": namespace, "rule_fingerprint": result.rule_fingerprint,
        "detection": {"status": result.status, "complete": result.complete, "duration_ms": result.duration_ms,
                      "detectors": result.detector_reports, "evidence_failures": result.evidence_failures,
                      "total_detections": len(result.detections)},
        "evaluation": None,
        "limitations": [
            "Synthetic data with deliberately constructed patterns: results do not measure real-world accuracy.",
            "Random ordinary activity can resemble a pattern; such detections are counted as false positives.",
            "Detector thresholds are configuration; metrics are specific to the configuration used."],
    }
    if result.status in ("anchor_not_found", "database_error"):
        return report
    # 2. only now are the labels read
    truth = load_ground_truth(ground_truth_path, namespace)
    ids = sorted({t for g in truth.patterns for t in g.transaction_ids})
    present = set()
    store = GraphStore(conn)
    for i in range(0, len(ids), 1000):
        present |= set(store.fetch_nodes("Transaction", ids[i:i + 1000]))
    truncated = {n: bool(r.get("truncated")) for n, r in result.detector_reports.items() if not n.startswith("_")}
    report["evaluation"] = evaluate_detections(result.detections, truth, config, truncated, present)
    report["evaluation"]["truncated_searches"] = sum(truncated.values())
    return report


def render_text(r: Dict[str, Any]) -> str:
    d = r["detection"]
    L = [f"Evaluation of namespace '{r['namespace']}' (rule fingerprint {r['rule_fingerprint']})",
         f"  detection: {d['status']}, complete={d['complete']}, {d['total_detections']} detection(s), {d['duration_ms']} ms"]
    if r["evaluation"] is None:
        L.append("  evaluation not performed")
        return "\n".join(L)
    fmt = lambda x: "undefined" if x is None else f"{x['value']:.4f} ({x['fraction']})"
    L.append(f"  matching: coverage >= {r['evaluation']['matching']['min_gt_coverage_percent']}% of the instance, "
             f"purity >= {r['evaluation']['matching']['min_detection_purity_percent']}% of the detection")
    for t, x in r["evaluation"]["per_type"].items():
        if not x["evaluated"]:
            L.append(f"  {t:<13} not evaluated ({x['reason']})")
            continue
        L.append(f"  {t:<13} labelled {x['ground_truth_instances']}, detected {x['detected_instances']}, TP {x['true_positives']}, "
                 f"FP {x['false_positives']}, FN {x['false_negatives']}, excluded {x['excluded_instances']}, truncated {x['search_truncated']}")
        L.append(f"  {'':<13} precision {fmt(x['precision'])}, recall {fmt(x['recall'])}, F1 {fmt(x['f1'])}")
        L.append(f"  {'':<13} account coverage {fmt(x['account_coverage'])}, transaction coverage {fmt(x['transaction_coverage'])}")
        for e in x["false_positive_examples"]:
            L.append(f"  {'':<13} FP {e['detection_id']}: {e['transactions']} tx, {e['classification']}")
        for e in x["false_negative_examples"]:
            L.append(f"  {'':<13} FN {e['pattern_id']}: {e['transactions']} tx, best overlap {e['best_overlap_transactions']}")
    o = r["evaluation"]["overall_micro"]
    L.append(f"  overall (micro): TP {o['true_positives']}, FP {o['false_positives']}, FN {o['false_negatives']}, "
             f"precision {fmt(o['precision'])}, recall {fmt(o['recall'])}, F1 {fmt(o['f1'])}")
    L.append(f"  truncated searches: {r['evaluation']['truncated_searches']}")
    for lim in r["limitations"]:
        L.append(f"  note: {lim}")
    return "\n".join(L)
