"""Phase 7 Integration Tests: Comprehensive Detector Evaluation & Error Analysis.

Tests all four pattern detectors against real FalkorDB test graph using
synthetic datasets with strict ground-truth isolation.
"""
from pathlib import Path
import pytest

from money_trail.database.migration import migrate_schema
from money_trail.evaluation.detector_eval import (
    analyze_detector_errors,
    ingest_dataset_into_graph,
    prepare_synthetic_dataset,
)
from money_trail.evaluation.ground_truth import load_ground_truth
from money_trail.evaluation.qa_config import DatasetProfile, QAConfig
from money_trail.evaluation.report import evaluate_detections
from money_trail.investigation.config import load_detection_config
from money_trail.investigation.detectors.models import PATTERN_ORDER
from money_trail.investigation.engine import InvestigationEngine, InvestigationRequest


def test_detector_evaluation_and_error_analysis(ready, tmp_path):
    """Verify detector evaluation, metrics calculation, and error analysis against real FalkorDB."""
    migrate_schema(ready)

    det_cfg_path = Path("examples/detection.example.json")
    det_cfg = load_detection_config(det_cfg_path)

    prof = DatasetProfile(
        namespace="det-eval-test",
        seed=42,
        accounts=25,
        transactions=80,
        patterns_per_type=1,
        description="Integration test dataset for detector evaluation",
    )

    # 1. Prepare and ingest synthetic dataset
    ds_dir, gt_path = prepare_synthetic_dataset(prof, tmp_path)
    ingest_rep = ingest_dataset_into_graph(ready, ds_dir)
    assert ingest_rep["status"] == "succeeded"

    # 2. Run detectors against real FalkorDB without ground truth (Isolation)
    engine = InvestigationEngine(ready, det_cfg)
    req = InvestigationRequest("dataset", namespace=prof.namespace)
    det_res = engine.investigate(req)

    assert det_res.status in ("patterns_detected", "no_patterns_detected")
    assert det_res.complete is True
    assert len(det_res.detections) >= 4  # Should find at least one of each pattern

    # 3. Post-detection evaluation matching with ground truth
    truth = load_ground_truth(gt_path, prof.namespace)
    all_truth_tx = sorted({t for g in truth.patterns for t in g.transaction_ids})
    present_tx = set()
    from money_trail.database import GraphStore
    store = GraphStore(ready)
    for i in range(0, len(all_truth_tx), 500):
        present_tx |= set(store.fetch_nodes("Transaction", all_truth_tx[i:i + 500]))

    truncated_by_type = {n: bool(r.get("truncated")) for n, r in det_res.detector_reports.items() if not n.startswith("_")}
    eval_metrics = evaluate_detections(det_res.detections, truth, det_cfg, truncated_by_type, present_tx)

    # Check metrics structure
    assert "per_type" in eval_metrics
    assert "overall_micro" in eval_metrics

    for ptype in PATTERN_ORDER:
        m = eval_metrics["per_type"][ptype]
        assert m["evaluated"] is True
        assert m["ground_truth_instances"] == 1
        assert m["true_positives"] >= 1
        assert m["precision"] is not None
        assert m["recall"] is not None
        assert m["f1"] is not None

    # 4. Error analysis
    error_analysis = analyze_detector_errors(det_res.detections, truth, det_cfg, present_tx)
    assert len(error_analysis) == 4
    for ptype in PATTERN_ORDER:
        info = error_analysis[ptype]
        assert "true_positives" in info
        assert "false_positives" in info
        assert "false_negatives" in info
        assert info["true_positives"] >= 1
