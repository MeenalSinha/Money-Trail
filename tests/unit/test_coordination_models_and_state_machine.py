"""Unit tests for coordination state machines, DAG cycle detection, and role permissions."""
import pytest

from money_trail.coordination.models import (
    AgentHandoffRecord,
    AgentRole,
    AgentSessionRecord,
    AgentTaskRecord,
    HandoffStatus,
    SessionStatus,
    TaskStatus,
    stable_handoff_id,
    stable_session_id,
    stable_task_id,
)
from money_trail.coordination.roles.base import ROLE_REGISTRY
from money_trail.coordination.state_machine import (
    detect_dependency_cycle,
    validate_handoff_transition,
    validate_session_transition,
    validate_task_transition,
)
from money_trail.exceptions import ValidationError


def test_stable_ids_deterministic():
    s1 = stable_session_id("inv_1", "fixed_seed")
    s2 = stable_session_id("inv_1", "fixed_seed")
    assert s1 == s2
    assert s1.startswith("sess_")

    t1 = stable_task_id("inv_1", "trace", "idem_key_1")
    t2 = stable_task_id("inv_1", "trace", "idem_key_1")
    assert t1 == t2
    assert t1.startswith("task_")

    h1 = stable_handoff_id("inv_1", "task_1", "investigator", "auditor", "idem_1")
    h2 = stable_handoff_id("inv_1", "task_1", "investigator", "auditor", "idem_1")
    assert h1 == h2
    assert h1.startswith("hnd_")


def test_task_state_machine_valid_transitions():
    # pending -> ready
    validate_task_transition("pending", "ready")
    # ready -> running
    validate_task_transition("ready", "running")
    # running -> completed
    validate_task_transition("running", "completed")
    # running -> failed
    validate_task_transition("running", "failed")
    # failed -> ready (retry)
    validate_task_transition("failed", "ready")
    # running -> blocked
    validate_task_transition("running", "blocked")
    # blocked -> ready
    validate_task_transition("blocked", "ready")
    # idempotent same-state transition
    validate_task_transition("ready", "ready")


def test_task_state_machine_invalid_transitions():
    # completed is terminal: cannot go anywhere
    with pytest.raises(ValidationError, match="Invalid task transition"):
        validate_task_transition("completed", "running")
    with pytest.raises(ValidationError, match="Invalid task transition"):
        validate_task_transition("completed", "ready")

    # pending cannot directly jump to completed without running
    with pytest.raises(ValidationError, match="Invalid task transition"):
        validate_task_transition("pending", "completed")

    # unknown status
    with pytest.raises(ValidationError, match="Unknown task status"):
        validate_task_transition("pending", "magical_status")


def test_handoff_state_machine_transitions():
    validate_handoff_transition("initiated", "transferred")
    validate_handoff_transition("transferred", "accepted")
    validate_handoff_transition("accepted", "completed")

    with pytest.raises(ValidationError, match="Invalid handoff transition"):
        validate_handoff_transition("completed", "initiated")


def test_session_state_machine_transitions():
    validate_session_transition("active", "interrupted")
    validate_session_transition("interrupted", "active")
    validate_session_transition("active", "completed")

    with pytest.raises(ValidationError, match="Invalid session transition"):
        validate_session_transition("completed", "active")


def test_dag_cycle_detection():
    # 1. Acyclic linear: A -> B -> C (A depends on B, B depends on C)
    graph_ok = {
        "A": ["B"],
        "B": ["C"],
        "C": [],
    }
    assert detect_dependency_cycle(graph_ok) is None

    # 2. Direct self-dependency
    graph_self = {"A": ["A"]}
    cycle = detect_dependency_cycle(graph_self)
    assert cycle == ["A", "A"]

    # 3. Two-node cycle: A -> B and B -> A
    graph_2cycle = {
        "A": ["B"],
        "B": ["A"],
    }
    cycle2 = detect_dependency_cycle(graph_2cycle)
    assert cycle2 is not None
    assert set(cycle2) == {"A", "B"}

    # 4. Three-node cycle: A -> B -> C -> A
    graph_3cycle = {
        "A": ["B"],
        "B": ["C"],
        "C": ["A"],
    }
    cycle3 = detect_dependency_cycle(graph_3cycle)
    assert cycle3 is not None
    assert len(cycle3) == 4
    assert cycle3[0] == cycle3[-1]


def test_role_registry_specifications():
    assert len(ROLE_REGISTRY) == 4
    assert AgentRole.COORDINATOR in ROLE_REGISTRY
    assert AgentRole.INVESTIGATOR in ROLE_REGISTRY
    assert AgentRole.AUDITOR in ROLE_REGISTRY
    assert AgentRole.MEMORY_CURATOR in ROLE_REGISTRY

    # Investigator cannot write graph directly
    assert not ROLE_REGISTRY[AgentRole.INVESTIGATOR].can_write_graph
    # Auditor has audit permissions
    assert ROLE_REGISTRY[AgentRole.AUDITOR].can_audit_evidence
    # Memory Curator has curate permissions
    assert ROLE_REGISTRY[AgentRole.MEMORY_CURATOR].can_curate_memory
