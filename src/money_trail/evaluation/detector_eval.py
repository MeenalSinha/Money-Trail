"""Comprehensive Detector Evaluation and Error Analysis for Phase 7.

Validates all four graph-based detectors:
1. Circular transfer
2. Rapid pass-through
3. Fan-in
4. Fan-out

Enforces strict ground-truth isolation: runtime detectors execute against
the live FalkorDB graph without accessing ground-truth labels. Label matching
and error analysis take place strictly in the post-detection evaluation phase.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Tuple

from money_trail.database import FalkorDBConnection, GraphStore
from money_trail.evaluation.ground_truth import load_ground_truth
from money_trail.evaluation.matching import DetItem, TruthItem, match_type
from money_trail.evaluation.qa_config import DatasetProfile, QAConfig
from money_trail.evaluation.report import evaluate_detections, run_evaluation
from money_trail.generation import generate, load_generator_config, write_dataset
from money_trail.ingestion import ingest_dataset, load_dataset_config
from money_trail.investigation.config import DetectionConfig, load_detection_config
from money_trail.investigation.detectors.models import PATTERN_ORDER
from money_trail.investigation.engine import InvestigationEngine, InvestigationRequest

log = logging.getLogger(__name__)


def build_generator_dict(profile: DatasetProfile, output_dir: Path, ingest_output_dir: Path) -> Dict[str, Any]:
    """Build a valid generator configuration dict from a DatasetProfile."""
    return {
        "namespace": profile.namespace,
        "seed": profile.seed,
        "output_dir": str(output_dir),
        "ingest_output_dir": str(ingest_output_dir),
        "currency": {"code": "USD", "exponent": 2},
        "amount_format": "major",
        "time_window": {"start": "2026-01-01T00:00:00Z", "days": 30},
        "ordinary_activity": {
            "accounts": profile.accounts,
            "transactions": profile.transactions,
            "amount_minor": {"min": profile.amount_min_minor, "max": profile.amount_max_minor},
            "channels": ["transfer", "wire", "card"],
        },
        "id_prefixes": {"account": "ACC", "transaction": "TXN", "alert": "ALT"},
        "patterns": [
            {
                "type": "circular",
                "count": profile.patterns_per_type,
                "cycle_length": {"min": 3, "max": 5},
                "amount_minor": {"min": 100000, "max": 900000},
                "retention_bps": 9900,
                "delay_seconds": {"min": 60, "max": 7200},
                "shared_account_fraction_percent": 10,
            },
            {
                "type": "pass_through",
                "count": profile.patterns_per_type,
                "hops": {"min": 2, "max": 4},
                "amount_minor": {"min": 100000, "max": 900000},
                "retention_bps": 9800,
                "delay_seconds": {"min": 30, "max": 3600},
                "shared_account_fraction_percent": 10,
            },
            {
                "type": "fan_in",
                "count": profile.patterns_per_type,
                "sources": {"min": 5, "max": 8},
                "amount_minor": {"min": 20000, "max": 90000},
                "delay_seconds": {"min": 0, "max": 86400},
                "shared_account_fraction_percent": 10,
            },
            {
                "type": "fan_out",
                "count": profile.patterns_per_type,
                "recipients": {"min": 5, "max": 8},
                "amount_minor": {"min": 20000, "max": 90000},
                "delay_seconds": {"min": 0, "max": 86400},
                "shared_account_fraction_percent": 10,
            },
        ],
        "seed_alerts": {
            "on_pattern_transactions": max(1, profile.patterns_per_type * 2),
            "on_ordinary_transactions": max(1, profile.patterns_per_type * 2),
            "alert_type": "seed_signal",
            "severity": "unspecified",
        },
    }


def prepare_synthetic_dataset(profile: DatasetProfile, base_dir: Path) -> Tuple[Path, Path]:
    """Generate and write synthetic dataset to disk.
    
    Returns:
        (dataset_dir, ground_truth_path)
    """
    raw_cfg = build_generator_dict(profile, base_dir / "gen", base_dir / "ingest_out")
    gen_file = base_dir / f"gen_{profile.namespace}.json"
    gen_file.parent.mkdir(parents=True, exist_ok=True)
    gen_file.write_text(json.dumps(raw_cfg, indent=2), encoding="utf-8")

    cfg = load_generator_config(gen_file)
    manifest = write_dataset(cfg, generate(cfg), overwrite=True)
    ds_dir = cfg.output_dir / profile.namespace
    gt_path = ds_dir / "ground_truth.json"
    return ds_dir, gt_path


def ingest_dataset_into_graph(conn: FalkorDBConnection, ds_dir: Path) -> Dict[str, Any]:
    """Ingest a prepared dataset into FalkorDB."""
    ingest_cfg_path = ds_dir / "ingest_config.json"
    dataset_cfg = load_dataset_config(ingest_cfg_path)
    report = ingest_dataset(conn, dataset_cfg)
    return report


def analyze_detector_errors(
    detections: List[Any],
    truth: Any,
    config: DetectionConfig,
    present_tx: set,
) -> Dict[str, Any]:
    """Perform in-depth error analysis on false positives and false negatives."""
    error_analysis: Dict[str, Any] = {}
    ev = config.evaluation

    for ptype in PATTERN_ORDER:
        gts_all = [g for g in truth.patterns if g.pattern_type == ptype]
        excluded = [g for g in gts_all if not set(g.transaction_ids) <= present_tx]
        gts = [TruthItem(g.pattern_id, g.pattern_type, frozenset(g.transaction_ids), frozenset(g.account_ids))
               for g in gts_all if g not in excluded]
        dets = [DetItem(d.detection_id, d.pattern_type, frozenset(d.transaction_ids), frozenset(d.account_ids))
                for d in detections if d.pattern_type == ptype]

        res = match_type(dets, gts, ev.min_gt_coverage_percent, ev.min_detection_purity_percent)

        detailed_fps = []
        for d in res.unmatched_detections:
            matched_gts = [g for g in gts if (d.transaction_ids & g.transaction_ids)]
            overlap_counts = {g.pattern_id: len(d.transaction_ids & g.transaction_ids) for g in matched_gts}
            detailed_fps.append({
                "detection_id": d.detection_id,
                "pattern_type": ptype,
                "transactions_count": len(d.transaction_ids),
                "accounts_count": len(d.account_ids),
                "sample_transactions": sorted(list(d.transaction_ids))[:5],
                "sample_accounts": sorted(list(d.account_ids))[:5],
                "overlap_with_known_patterns": overlap_counts,
                "root_cause": (
                    "insufficient_coverage_or_purity"
                    if matched_gts
                    else "spurious_ordinary_network_structural_match"
                ),
            })

        detailed_fns = []
        for g in res.unmatched_truths:
            overlapping_dets = [d for d in dets if (d.transaction_ids & g.transaction_ids)]
            best_det = max(
                overlapping_dets,
                key=lambda d: len(d.transaction_ids & g.transaction_ids),
                default=None,
            )
            overlap_len = len(best_det.transaction_ids & g.transaction_ids) if best_det else 0
            detailed_fns.append({
                "pattern_id": g.pattern_id,
                "pattern_type": ptype,
                "expected_transactions_count": len(g.transaction_ids),
                "expected_accounts_count": len(g.account_ids),
                "sample_expected_transactions": sorted(list(g.transaction_ids))[:5],
                "best_candidate_detection": best_det.detection_id if best_det else None,
                "best_overlap_count": overlap_len,
                "root_cause": (
                    "partial_subgraph_detection_failed_threshold"
                    if best_det
                    else "traversal_boundary_or_parameter_cutoff"
                ),
            })

        error_analysis[ptype] = {
            "total_ground_truth": len(gts),
            "total_detected": len(dets),
            "true_positives": len(res.pairs),
            "false_positives_count": len(detailed_fps),
            "false_negatives_count": len(detailed_fns),
            "false_positives": detailed_fps,
            "false_negatives": detailed_fns,
        }

    return error_analysis


def evaluate_detector_suite(
    conn: FalkorDBConnection,
    det_config: DetectionConfig,
    qa_cfg: QAConfig,
    working_dir: Path,
) -> Dict[str, Any]:
    """Execute end-to-end detector evaluation on Dev, Held-out Eval, and Scaling datasets."""
    results: Dict[str, Any] = {
        "execution_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "qa_config_version": qa_cfg.version,
        "dev_evaluation": None,
        "eval_evaluation": None,
        "error_analysis": None,
        "scaling_benchmark": [],
    }

    # 1. Dev/Validation Dataset Run
    log.info("Generating and ingesting dev dataset: %s", qa_cfg.dev_dataset.namespace)
    dev_dir, dev_gt = prepare_synthetic_dataset(qa_cfg.dev_dataset, working_dir / "dev")
    ingest_dataset_into_graph(conn, dev_dir)
    dev_eval = run_evaluation(conn, det_config, qa_cfg.dev_dataset.namespace, dev_gt)
    results["dev_evaluation"] = dev_eval
    results["dev_gt_path"] = str(dev_gt)

    # 2. Held-Out Evaluation Dataset Run (Strict Ground-Truth Isolation)
    log.info("Generating and ingesting held-out evaluation dataset: %s", qa_cfg.eval_dataset.namespace)
    eval_dir, eval_gt = prepare_synthetic_dataset(qa_cfg.eval_dataset, working_dir / "eval")
    ingest_dataset_into_graph(conn, eval_dir)

    # Runtime detection executes against graph first without ground-truth labels
    engine = InvestigationEngine(conn, det_config)
    eval_req = InvestigationRequest("dataset", namespace=qa_cfg.eval_dataset.namespace)
    t0 = time.perf_counter()
    eval_det_res = engine.investigate(eval_req)
    detection_wall_ms = int((time.perf_counter() - t0) * 1000)

    # Post-detection evaluation matching with ground truth
    truth = load_ground_truth(eval_gt, qa_cfg.eval_dataset.namespace)
    store = GraphStore(conn)
    all_truth_tx = sorted({t for g in truth.patterns for t in g.transaction_ids})
    present_tx = set()
    for i in range(0, len(all_truth_tx), 1000):
        present_tx |= set(store.fetch_nodes("Transaction", all_truth_tx[i:i + 1000]))

    truncated_by_type = {n: bool(r.get("truncated")) for n, r in eval_det_res.detector_reports.items() if not n.startswith("_")}
    eval_metrics = evaluate_detections(eval_det_res.detections, truth, det_config, truncated_by_type, present_tx)

    results["eval_evaluation"] = {
        "namespace": qa_cfg.eval_dataset.namespace,
        "detection_wall_ms": detection_wall_ms,
        "detections_count": len(eval_det_res.detections),
        "status": eval_det_res.status,
        "complete": eval_det_res.complete,
        "evaluation": eval_metrics,
        "detector_reports": eval_det_res.detector_reports,
    }

    # Detailed Error Analysis on Held-out Dataset
    results["error_analysis"] = analyze_detector_errors(
        eval_det_res.detections, truth, det_config, present_tx
    )

    # 3. Scaling Benchmark
    for scale_prof in qa_cfg.scaling_datasets:
        log.info("Evaluating scaling on profile: %s (%d tx)", scale_prof.namespace, scale_prof.transactions)
        scale_dir, scale_gt = prepare_synthetic_dataset(scale_prof, working_dir / f"scale_{scale_prof.transactions}")
        ingest_res = ingest_dataset_into_graph(conn, scale_dir)

        s_t0 = time.perf_counter()
        s_res = engine.investigate(InvestigationRequest("dataset", namespace=scale_prof.namespace))
        s_dur = int((time.perf_counter() - s_t0) * 1000)

        results["scaling_benchmark"].append({
            "namespace": scale_prof.namespace,
            "transactions": scale_prof.transactions,
            "accounts": scale_prof.accounts,
            "patterns_per_type": scale_prof.patterns_per_type,
            "total_detections": len(s_res.detections),
            "duration_ms": s_dur,
            "status": s_res.status,
            "complete": s_res.complete,
            "detector_reports": s_res.detector_reports,
        })

    return results
