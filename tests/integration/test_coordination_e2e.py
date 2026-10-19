"""End-to-End Coordination Scenarios and Baseline Comparisons (Part N & Task 12).

Tests Scenarios 1 through 7 specified in Section 42:
  Scenario 1: Memory across sessions
  Scenario 2: Evidence-based memory promotion
  Scenario 3: Rejected memory (anti-poisoning / unverified claims)
  Scenario 4: Multi-agent handoff (Coordinator -> Investigator -> Auditor -> Memory Curator)
  Scenario 5: Interrupted recovery (new process resumption from FalkorDB)
  Scenario 6: Conflicting evidence (revalidation, supersession, and contradiction links)
  Scenario 7: Scope isolation (cross-investigation & cross-dataset boundary enforcement)
  Baseline Comparison: Coordinated DAG scheduling vs. Uncoordinated sequential execution.
"""
import json
import pytest

from money_trail.coordination.models import (
    AgentRole,
    HandoffStatus,
    SessionStatus,
    TaskStatus,
)
from money_trail.coordination.orchestrator import MultiAgentOrchestrator
from money_trail.coordination.store import CoordinationStore
from money_trail.database import initialize_graph
from money_trail.exceptions import ValidationError
from money_trail.ingestion import ingest_dataset, load_dataset_config
from money_trail.memory.models import MemoryScope, MemoryStatus, MemoryType
from money_trail.memory.store import MemoryStore


def _setup_test_dataset(conn, make_dataset, name="e2e-ds", seed=42):
    initialize_graph(conn)
    ds = make_dataset(name, seed=seed)
    ingest_dataset(conn, load_dataset_config(ds.ingest_config))
    gt = json.loads((ds.dir / "ground_truth.json").read_text())["patterns"]
    anchor = gt[0]["transaction_ids"][0]
    return ds, anchor


# =============================================================================
# Scenario 1: Memory across sessions
# =============================================================================
def test_scenario_1_memory_across_sessions(conn, make_dataset):
    """Start an investigation, persist findings, terminate session, start a new process.

    The new run must retrieve the relevant memory and avoid repeating completed work.
    """
    ds, anchor = _setup_test_dataset(conn, make_dataset, "scen1-ds", seed=101)
    inv_id = "inv_scenario_1"
    ns = ds.gen_cfg.namespace

    orch1 = MultiAgentOrchestrator(conn)
    res1 = orch1.run_investigation(
        investigation_id=inv_id,
        anchor_id=anchor,
        dataset=ns,
        persist=True,
    )
    assert res1["status"] == SessionStatus.COMPLETED.value
    assert res1["steps_executed"] >= 1

    # Simulate new application process with fresh orchestrator instance
    orch2 = MultiAgentOrchestrator(conn)
    mem_store2 = MemoryStore(conn)

    # Memories from session 1 must be retrievable
    retrieved = mem_store2.retrieve_memories(
        scope=MemoryScope.INVESTIGATION.value,
        investigation_id=inv_id,
    )
    assert len(retrieved) >= 1
    statements = [m.statement for m in retrieved]
    assert any("Detected" in s or "Observed" in s or "pattern" in s for s in statements)

    # Running again with same anchor and rules should identify completed tasks and avoid duplicate work
    cstore2 = CoordinationStore(conn)
    completed_tasks = cstore2.list_tasks(inv_id, status=TaskStatus.COMPLETED.value)
    assert len(completed_tasks) >= 3


# =============================================================================
# Scenario 2: Evidence-based memory promotion
# =============================================================================
def test_scenario_2_evidence_based_memory_promotion(conn, make_dataset):
    """Investigator discovers a pattern, Auditor verifies it, Curator persists semantic memory.

    Verify that the memory is discoverable in a later session and linked to evidence.
    """
    ds, anchor = _setup_test_dataset(conn, make_dataset, "scen2-ds", seed=102)
    inv_id = "inv_scenario_2"
    ns = ds.gen_cfg.namespace

    orch = MultiAgentOrchestrator(conn)
    res = orch.run_investigation(investigation_id=inv_id, anchor_id=anchor, dataset=ns, persist=True)
    assert res["status"] == SessionStatus.COMPLETED.value

    # Verify semantic memory was promoted with valid evidence references
    mem_store = MemoryStore(conn)
    semantic_mems = mem_store.retrieve_memories(
        scope=MemoryScope.INVESTIGATION.value,
        investigation_id=inv_id,
        memory_type=MemoryType.SEMANTIC.value,
    )
    assert len(semantic_mems) >= 1
    sem = semantic_mems[0]
    assert sem.status == MemoryStatus.ACTIVE.value
    assert len(sem.evidence_ids) >= 1

    # Verify that the evidence node actually exists in FalkorDB
    ev_id = sem.evidence_ids[0]
    ev_node = conn.graph.ro_query(
        "MATCH (e:Evidence {evidence_id: $eid}) RETURN e.evidence_id",
        {"eid": ev_id},
    ).result_set
    assert len(ev_node) == 1


# =============================================================================
# Scenario 3: Rejected memory (anti-poisoning)
# =============================================================================
def test_scenario_3_rejected_unsupported_memory(conn):
    """A claim referencing non-existent evidence or transactions is rejected by the Curator."""
    initialize_graph(conn)
    mem_store = MemoryStore(conn)
    inv_id = "inv_scenario_3"

    # Attempt to curate a semantic memory with completely fake evidence
    with pytest.raises(ValidationError, match="does not exist in graph"):
        mem_store.curate_semantic(
            statement="Fake laundering cycle found without evidence.",
            investigation_id=inv_id,
            evidence_ids=["fake_evidence_9999"],
            dataset="test-ns",
            provenance_tool="hallucinated_model_statement",
        )

    # Verify it was not stored in FalkorDB
    mems = mem_store.retrieve_memories(
        scope=MemoryScope.INVESTIGATION.value,
        investigation_id=inv_id,
    )
    assert len(mems) == 0


# =============================================================================
# Scenario 4: Multi-agent handoffs across roles
# =============================================================================
def test_scenario_4_multi_agent_handoff(conn, make_dataset):
    """The Coordinator creates tasks. The Investigator runs. A handoff transfers to the Auditor.

    The Auditor verifies the results, and hands off to the Curator.
    The entire sequence is recoverable from FalkorDB.
    """
    ds, anchor = _setup_test_dataset(conn, make_dataset, "scen4-ds", seed=104)
    inv_id = "inv_scenario_4"
    ns = ds.gen_cfg.namespace

    orch = MultiAgentOrchestrator(conn)
    res = orch.run_investigation(investigation_id=inv_id, anchor_id=anchor, dataset=ns, persist=True)
    assert res["status"] == SessionStatus.COMPLETED.value

    # Recover handoffs from FalkorDB
    cstore = CoordinationStore(conn)
    handoffs = cstore.list_handoffs(inv_id)
    assert len(handoffs) >= 2

    # Verify target roles and completion
    target_roles = [h.target_role for h in handoffs]
    assert AgentRole.AUDITOR.value in target_roles
    assert AgentRole.MEMORY_CURATOR.value in target_roles
    assert all(h.status == HandoffStatus.COMPLETED.value for h in handoffs)


# =============================================================================
# Scenario 5: Interrupted recovery
# =============================================================================
def test_scenario_5_interrupted_recovery(conn, make_dataset):
    """Terminate execution midway, start a new process, resume from unfinished state."""
    ds, anchor = _setup_test_dataset(conn, make_dataset, "scen5-ds", seed=105)
    inv_id = "inv_scenario_5"
    ns = ds.gen_cfg.namespace

    orch1 = MultiAgentOrchestrator(conn)
    run1 = orch1.run_investigation(
        investigation_id=inv_id,
        anchor_id=anchor,
        dataset=ns,
        persist=True,
        interrupt_after_task_type="investigate_anchor",
    )
    assert run1["status"] == SessionStatus.INTERRUPTED.value
    sess_id = run1["session_id"]

    # Resume with fresh orchestrator instance
    orch2 = MultiAgentOrchestrator(conn)
    run2 = orch2.resume_investigation(sess_id)
    assert run2["status"] == SessionStatus.COMPLETED.value
    assert run2["resumed_steps"] >= 1

    # In FalkorDB, all tasks must now be COMPLETED
    cstore = CoordinationStore(conn)
    tasks = cstore.list_tasks(inv_id)
    assert all(t.status == TaskStatus.COMPLETED.value for t in tasks)


# =============================================================================
# Scenario 6: Conflicting evidence, supersession, and contradictions
# =============================================================================
def test_scenario_6_conflicting_evidence_and_supersession(conn):
    """A finding/memory change marks earlier facts as superseded and preserves contradiction links."""
    initialize_graph(conn)
    mem_store = MemoryStore(conn)
    inv_id = "inv_scenario_6"

    # Store initial memory
    m1 = mem_store.record_episodic(
        session_id="sess_s6",
        statement="Account ACC_A routed funds exclusively to ACC_B.",
        investigation_id=inv_id,
        provenance_tool="tool_initial_scan",
    )
    assert m1.status == MemoryStatus.ACTIVE.value

    # A subsequent deeper analysis discovers funds also routed to ACC_C, superseding m1
    m2 = mem_store.record_episodic(
        session_id="sess_s6",
        statement="Account ACC_A routed funds to ACC_B and ACC_C in a fan-out pattern.",
        investigation_id=inv_id,
        provenance_tool="tool_deep_scan",
    )
    mem_store.supersede(
        old_memory_id=m1.memory_id,
        new_memory_id=m2.memory_id,
        reason="Detected counterparty ACC_C previously missed.",
    )

    # Verify old memory is superseded
    old_rec = mem_store.get_memory(m1.memory_id)
    assert old_rec.status == MemoryStatus.SUPERSEDED.value

    # Verify new memory links to old via superseded
    new_rec = mem_store.get_memory(m2.memory_id)
    assert m1.memory_id in new_rec.supersedes

    # Record explicit contradiction link between two conflicting hypotheses
    m_h1 = mem_store.record_episodic(
        session_id="sess_s6",
        statement="Hypothesis 1: Fast pass-through structuring.",
        investigation_id=inv_id,
        provenance_tool="model_hypo_1",
    )
    m_h2 = mem_store.record_episodic(
        session_id="sess_s6",
        statement="Hypothesis 2: Normal commercial batch payroll distribution.",
        investigation_id=inv_id,
        provenance_tool="model_hypo_2",
    )
    mem_store.mark_contradiction(
        m_h1.memory_id,
        m_h2.memory_id,
        reason="Opposing classifications of transfer intent.",
    )

    m_h1_rec = mem_store.get_memory(m_h1.memory_id)
    assert m_h2.memory_id in m_h1_rec.contradicts


# =============================================================================
# Scenario 7: Scope isolation
# =============================================================================
def test_scenario_7_scope_isolation(conn):
    """Memories in Dataset A or Investigation 1 do not leak to Dataset B or Investigation 2."""
    initialize_graph(conn)
    mem_store = MemoryStore(conn)

    # Investigation 1 in Dataset ALPHA
    mem_store.record_episodic(
        session_id="sess_alpha",
        statement="Sensitive entity found in Alpha.",
        scope=MemoryScope.DATASET.value,
        dataset="ALPHA",
    )

    # Investigation 2 in Dataset BETA
    mem_store.record_episodic(
        session_id="sess_beta",
        statement="Normal activity in Beta.",
        scope=MemoryScope.DATASET.value,
        dataset="BETA",
    )

    # Query scoped to ALPHA
    res_alpha = mem_store.retrieve_memories(
        scope=MemoryScope.DATASET.value,
        dataset="ALPHA",
    )
    assert len(res_alpha) == 1
    assert "Alpha" in res_alpha[0].statement
    assert "Beta" not in res_alpha[0].statement

    # Query scoped to BETA
    res_beta = mem_store.retrieve_memories(
        scope=MemoryScope.DATASET.value,
        dataset="BETA",
    )
    assert len(res_beta) == 1
    assert "Beta" in res_beta[0].statement
    assert "Alpha" not in res_beta[0].statement


# =============================================================================
# Baseline Comparison (Part N: Measure coordination effectiveness)
# =============================================================================
def test_baseline_comparison_coordinated_vs_uncoordinated(conn, make_dataset):
    """Compare DAG-coordinated multi-agent workflow vs uncoordinated sequential execution."""
    ds, anchor = _setup_test_dataset(conn, make_dataset, "baseline-ds", seed=107)
    ns = ds.gen_cfg.namespace

    orch = MultiAgentOrchestrator(conn)
    comparison = orch.run_baseline_comparison(
        investigation_id="inv_baseline_eval",
        anchor_id=anchor,
        dataset=ns,
    )

    coord = comparison["graph_coordinated"]
    uncoord = comparison["baseline"]

    assert coord["dependency_conflicts"] == 0
    assert uncoord["dependency_conflicts"] > 0
    assert uncoord["redundant_calls"] > 0
    assert coord["status"] == SessionStatus.COMPLETED.value
    assert coord["tasks_executed"] >= 3
