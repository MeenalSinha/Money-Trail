"""Phase 7 Integration Tests: Memory & Multi-Agent Reliability and Interruption Recovery.

Verifies graph-backed memory persistence, scope isolation, multi-agent coordination,
and recovery across 6 distinct interruption points on real FalkorDB.
"""
import pytest

from money_trail.database.migration import migrate_schema
from money_trail.evaluation.memory_coordination_eval import (
    evaluate_coordination_reliability,
    evaluate_interruption_recovery,
    evaluate_persistent_memory,
)


def test_persistent_memory_evaluation(ready):
    """Test all persistent memory invariants, provenance chains, and isolation."""
    migrate_schema(ready)
    mem_eval = evaluate_persistent_memory(ready, test_prefix="test_mem_rel")
    assert mem_eval["passed"] is True, f"Memory evaluation failed: {mem_eval}"
    for check_name, c in mem_eval["checks"].items():
        assert c["passed"] is True, f"Check failed: {check_name}"


def test_coordination_reliability(ready):
    """Test multi-agent state machines, cycle detection, dependencies, and handoff idempotency."""
    migrate_schema(ready)
    coord_eval = evaluate_coordination_reliability(ready, test_prefix="test_coord_rel")
    assert coord_eval["passed"] is True, f"Coordination evaluation failed: {coord_eval}"
    for check_name, c in coord_eval["checks"].items():
        assert c["passed"] is True, f"Check failed: {check_name}"


def test_interruption_recovery_across_6_points(ready):
    """Test application state recovery across all 6 distinct interruption points."""
    migrate_schema(ready)
    rec_eval = evaluate_interruption_recovery(ready, test_prefix="test_rec_rel")
    assert rec_eval["passed"] is True, f"Recovery evaluation failed: {rec_eval}"
    assert len(rec_eval["scenarios"]) == 6
    for s in rec_eval["scenarios"]:
        assert s["passed"] is True, f"Recovery scenario failed at point {s['point']}"
