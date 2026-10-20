"""Phase 7 Integration Tests: Comprehensive AI Agent Evaluation & Failure Scenarios.

Tests the AI investigation agent orchestration, failure scenarios, and safety
safeguards against real FalkorDB.
"""
from pathlib import Path
import pytest

from money_trail.agent.config import load_agent_config
from money_trail.database.migration import migrate_schema
from money_trail.evaluation.agent_harness import (
    evaluate_agent_failure_scenarios,
    evaluate_scripted_agent_suite,
    verify_live_provider_status,
)
from money_trail.evaluation.detector_eval import (
    ingest_dataset_into_graph,
    prepare_synthetic_dataset,
)
from money_trail.evaluation.qa_config import DatasetProfile
from money_trail.investigation.config import load_detection_config


def test_agent_provider_status_without_key():
    agent_cfg = load_agent_config(Path("examples/agent.example.json"))
    status = verify_live_provider_status(agent_cfg, env_file=None)
    # In this environment without ANTHROPIC_API_KEY, must classify as BLOCKED / UNVERIFIED
    assert status["verified"] is False
    assert status["status"] == "BLOCKED"
    assert "No credential" in status["reason"]


def test_agent_all_15_failure_scenarios(ready):
    """Execute all 15 agent failure scenarios against real FalkorDB."""
    migrate_schema(ready)
    det_cfg = load_detection_config(Path("examples/detection.example.json"))
    agent_cfg = load_agent_config(Path("examples/agent.example.json"))

    scenarios = evaluate_agent_failure_scenarios(ready, det_cfg, agent_cfg, test_namespace="fail_test_ns")
    assert len(scenarios) == 15

    for sc in scenarios:
        assert sc["passed"] is True, f"Scenario failed: {sc['scenario_id']} - {sc}"
        assert sc["error_handled_safely"] is True


def test_agent_scripted_evaluation_suite(ready, tmp_path):
    """Execute scripted agent evaluation on synthetic dataset."""
    migrate_schema(ready)
    det_cfg = load_detection_config(Path("examples/detection.example.json"))
    agent_cfg = load_agent_config(Path("examples/agent.example.json"))

    prof = DatasetProfile(
        namespace="agent-eval-test",
        seed=101,
        accounts=20,
        transactions=50,
        patterns_per_type=1,
    )
    ds_dir, gt_path = prepare_synthetic_dataset(prof, tmp_path)
    ingest_rep = ingest_dataset_into_graph(ready, ds_dir)
    assert ingest_rep["status"] == "succeeded"

    eval_result = evaluate_scripted_agent_suite(ready, det_cfg, agent_cfg, gt_path)
    assert eval_result["scenarios"] >= 4
    assert eval_result["tool_selection_accuracy"] is not None
    assert eval_result["investigation_completion_rate"] is not None
    assert eval_result["unsupported_claim_rate"] is not None
    assert len(eval_result["post_filter_unresolved_ids"]) == 0
