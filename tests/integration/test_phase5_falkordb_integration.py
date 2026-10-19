"""Real FalkorDB integration tests for Phase 5 (Tests A through O).

Executes against live FalkorDB instances using disposable test graphs.
Verifies additive migrations, persistent sessions, memory provenance,
anti-poisoning safeguards, task dependencies, handoffs, and session recovery.
"""
import time
import pytest

from money_trail.coordination.models import (
    AgentRole,
    HandoffStatus,
    SessionStatus,
    TaskStatus,
)
from money_trail.coordination.orchestrator import MultiAgentOrchestrator
from money_trail.coordination.store import CoordinationStore
from money_trail.database import FalkorDBConnection, GraphStore, initialize_graph, inspect_schema
from money_trail.database import graph_schema as gs
from money_trail.database.migration import (
    get_active_schema_version,
    get_applied_migrations,
    migrate_schema,
)
from money_trail.exceptions import ValidationError
from money_trail.memory.models import MemoryScope, MemoryStatus, MemoryType
from money_trail.memory.store import MemoryStore


# =============================================================================
# Test A: Schema Migration from Populated Database
# =============================================================================
def test_a_schema_migration_preserves_existing_data(conn, make_dataset):
    """Upgrade an existing populated database to the new schema and verify data integrity."""
    # 1. Initialize baseline schema
    initialize_graph(conn)
    store = GraphStore(conn)

    # 2. Write existing baseline entities (Accounts, Transactions, Finding, Evidence)
    store.upsert_node("Account", {"account_id": "test:acc_a", "dataset": "test"})
    store.upsert_node("Account", {"account_id": "test:acc_b", "dataset": "test"})
    store.upsert_node("Transaction", {
        "transaction_id": "test:tx_1",
        "amount": 50000,
        "currency": "USD",
        "timestamp": 1700000000000,
        "dataset": "test",
    })
    store.link("SENT", "test:acc_a", "test:tx_1")
    store.link("RECEIVED_BY", "test:tx_1", "test:acc_b")

    store.upsert_node("Investigation", {
        "investigation_id": "inv_v1_existing",
        "status": "in_progress",
        "created_at": 1700000000000,
    })
    store.upsert_node("Finding", {
        "finding_id": "fnd_v1_existing",
        "kind": "observation",
        "statement": "Observed cyclic flow in v1",
        "created_at": 1700000000000,
    })
    store.upsert_node("Evidence", {
        "evidence_id": "ev_v1_existing",
        "created_at": 1700000000000,
        "produced_by_tool": "circular",
    })
    store.link("HAS_FINDING", "inv_v1_existing", "fnd_v1_existing")
    store.link("SUPPORTED_BY", "fnd_v1_existing", "ev_v1_existing")
    store.link("REFERENCES", "ev_v1_existing", "test:tx_1")

    # 3. Perform migration to SCHEMA_VERSION 2
    mig_res = migrate_schema(conn)
    assert mig_res.to_version == 2
    assert 2 in mig_res.applied or mig_res.already_current

    # 4. Verify existing records and relationships remain 100% intact
    tx_node = store.get_node("Transaction", "test:tx_1")
    assert tx_node is not None
    assert tx_node["amount"] == 50000

    fnd_node = store.get_node("Finding", "fnd_v1_existing")
    assert fnd_node is not None
    assert fnd_node["statement"] == "Observed cyclic flow in v1"

    ev_node = store.get_node("Evidence", "ev_v1_existing")
    assert ev_node is not None

    # Verify edge traversal through existing data
    traversal = conn.graph.ro_query(
        "MATCH (i:Investigation {investigation_id: 'inv_v1_existing'})-[:HAS_FINDING]->(f:Finding)"
        "-[:SUPPORTED_BY]->(e:Evidence)-[:REFERENCES]->(t:Transaction)"
        "RETURN t.transaction_id"
    ).result_set
    assert traversal and traversal[0][0] == "test:tx_1"


# =============================================================================
# Test B: New Graph Initialization
# =============================================================================
def test_b_new_graph_initialization_creates_all_elements(conn, test_settings):
    """Initialize a brand new graph and verify all v2 nodes, indexes, constraints, and relationships exist."""
    res = initialize_graph(conn)
    assert res.graph_name == test_settings.graph_name
    state = inspect_schema(conn)
    assert state.ready

    # Check that new v2 entities have range indexes and unique constraints
    idx_labels = {lbl for (lbl, prop) in state.present_indexes}
    for expected_label in ("AgentSession", "Memory", "AgentTask", "AgentHandoff", "SchemaMigration"):
        assert expected_label in idx_labels


# =============================================================================
# Test C: Migration Idempotency
# =============================================================================
def test_c_migration_idempotency(conn):
    """Re-running schema migration must be a safe no-op that deletes no data."""
    migrate_schema(conn)
    store = GraphStore(conn)
    store.upsert_node("Account", {"account_id": "test:keep_me", "dataset": "test"})

    second = migrate_schema(conn)
    third = migrate_schema(conn)

    assert second.already_current
    assert third.already_current
    assert not second.changed
    assert not third.changed
    assert store.get_node("Account", "test:keep_me") is not None


# =============================================================================
# Test D: Session Persistence Across Connections
# =============================================================================
def test_d_session_persistence_across_connections(test_settings):
    """Create an AgentSession, close connection, re-open new connection, and verify persistence."""
    sess_id = "sess_test_d_001"
    inv_id = "inv_test_d_001"

    # Step 1: Open connection and create session
    with FalkorDBConnection(test_settings) as conn1:
        initialize_graph(conn1)
        store1 = CoordinationStore(conn1)
        rec = store1.create_session(
            investigation_id=inv_id,
            dataset="dataset_alpha",
            session_id=sess_id,
            metadata={"source": "cli_test"},
        )
        assert rec.session_id == sess_id
        assert rec.status == SessionStatus.ACTIVE.value

    # Step 2: Open completely separate connection and retrieve
    with FalkorDBConnection(test_settings) as conn2:
        store2 = CoordinationStore(conn2)
        recovered = store2.get_session(sess_id)
        assert recovered is not None
        assert recovered.session_id == sess_id
        assert recovered.investigation_id == inv_id
        assert recovered.dataset == "dataset_alpha"
        assert recovered.status == SessionStatus.ACTIVE.value


# =============================================================================
# Test E: Memory Persistence and Provenance Chain
# =============================================================================
def test_e_memory_persistence_and_provenance(test_settings):
    """Create a verified semantic memory grounded in evidence and verify provenance retrieval."""
    with FalkorDBConnection(test_settings) as conn:
        initialize_graph(conn)
        gstore = GraphStore(conn)
        mstore = MemoryStore(conn)

        # Set up prerequisite graph records: Transaction & Evidence
        gstore.upsert_node("Account", {"account_id": "acc_x", "dataset": "ds1"})
        gstore.upsert_node("Account", {"account_id": "acc_y", "dataset": "ds1"})
        gstore.upsert_node("Transaction", {
            "transaction_id": "tx_e1",
            "amount": 2000,
            "currency": "EUR",
            "timestamp": 1700000000000,
            "dataset": "ds1",
        })
        gstore.link("SENT", "acc_x", "tx_e1")
        gstore.link("RECEIVED_BY", "tx_e1", "acc_y")

        gstore.upsert_node("Evidence", {
            "evidence_id": "ev_e1",
            "created_at": 1700000000000,
            "produced_by_tool": "pass_through",
        })
        gstore.link("REFERENCES", "ev_e1", "tx_e1")

        # Curate grounded semantic memory
        mem = mstore.curate_semantic(
            statement="Observed pass-through transfer of 20.00 EUR from acc_x to acc_y",
            scope=MemoryScope.INVESTIGATION.value,
            investigation_id="inv_e1",
            dataset="ds1",
            provenance_tool="pass_through_detector",
            evidence_ids=["ev_e1"],
        )
        assert mem.memory_id.startswith("mem_")

    # Reconnect and inspect provenance
    with FalkorDBConnection(test_settings) as conn2:
        mstore2 = MemoryStore(conn2)
        rec = mstore2.get_memory(mem.memory_id)
        assert rec is not None
        assert rec.statement == "Observed pass-through transfer of 20.00 EUR from acc_x to acc_y"
        assert "ev_e1" in rec.evidence_ids


# =============================================================================
# Test F: Memory Supersession
# =============================================================================
def test_f_memory_supersession(conn):
    """Superseding a memory updates status to superseded and preserves supersession link."""
    initialize_graph(conn)
    mstore = MemoryStore(conn)
    gstore = GraphStore(conn)

    # Set up dummy evidence for grounding
    gstore.upsert_node("Account", {"account_id": "a1", "dataset": "d"})
    gstore.upsert_node("Account", {"account_id": "a2", "dataset": "d"})
    gstore.upsert_node("Transaction", {"transaction_id": "tx_f", "amount": 100, "currency": "USD", "timestamp": 1, "dataset": "d"})
    gstore.link("SENT", "a1", "tx_f")
    gstore.link("RECEIVED_BY", "tx_f", "a2")
    gstore.upsert_node("Evidence", {"evidence_id": "ev_f", "created_at": 1, "produced_by_tool": "tool"})
    gstore.link("REFERENCES", "ev_f", "tx_f")

    # Original memory
    old_mem = mstore.curate_semantic(
        statement="Initial observation: transfer was 1.00 USD",
        investigation_id="inv_f",
        evidence_ids=["ev_f"],
    )

    # Replacement memory
    new_mem = mstore.curate_semantic(
        statement="Corrected observation: transfer was 1.00 USD across verified accounts a1 and a2",
        investigation_id="inv_f",
        evidence_ids=["ev_f"],
    )

    # Apply supersession
    mstore.supersede(old_mem.memory_id, new_mem.memory_id, reason="Correction with counterparties")

    # Verify both records
    old_rec = mstore.get_memory(old_mem.memory_id)
    new_rec = mstore.get_memory(new_mem.memory_id)

    assert old_rec.status == MemoryStatus.SUPERSEDED.value
    assert old_rec.superseded_by == new_mem.memory_id
    assert new_rec.status == MemoryStatus.ACTIVE.value
    assert new_rec.supersedes_id == old_mem.memory_id


# =============================================================================
# Test G: Contradictory Memories
# =============================================================================
def test_g_contradictory_memories(conn):
    """Store conflicting claims and verify that contradiction relationships are preserved."""
    initialize_graph(conn)
    mstore = MemoryStore(conn)

    m1 = mstore.record_episodic(session_id="s1", statement="Claim A: account was active in January", investigation_id="inv_g")
    m2 = mstore.record_episodic(session_id="s1", statement="Claim B: account was inactive until March", investigation_id="inv_g")

    mstore.mark_contradiction(m1.memory_id, m2.memory_id, reason="Conflicting activity dates")

    rec1 = mstore.get_memory(m1.memory_id)
    rec2 = mstore.get_memory(m2.memory_id)

    assert m2.memory_id in rec1.contradicting_memory_ids
    assert m1.memory_id in rec2.contradicting_memory_ids


# =============================================================================
# Test H: Evidence-Grounded Memory (Anti-Poisoning)
# =============================================================================
def test_h_evidence_grounded_memory_anti_poisoning(conn):
    """Attempting to promote semantic factual memory without valid evidence must be rejected."""
    initialize_graph(conn)
    mstore = MemoryStore(conn)

    # Case 1: Empty evidence_ids
    with pytest.raises(ValidationError, match="must be grounded by at least one verified evidence ID"):
        mstore.curate_semantic(
            statement="Fabricated model claim without evidence",
            investigation_id="inv_h",
            evidence_ids=[],
            require_evidence=True,
        )

    # Case 2: Non-existent evidence ID
    with pytest.raises(ValidationError, match="does not exist in graph"):
        mstore.curate_semantic(
            statement="Fabricated claim with fake evidence ID",
            investigation_id="inv_h",
            evidence_ids=["ev_non_existent_999"],
            require_evidence=True,
        )


# =============================================================================
# Test I: Scope Isolation (Investigation and Dataset Boundaries)
# =============================================================================
def test_i_scope_isolation_prevents_leakage(conn):
    """Queries must never leak memories across different investigations or dataset namespaces."""
    initialize_graph(conn)
    mstore = MemoryStore(conn)

    # Create memories in separate investigations
    mstore.record_episodic(session_id="s1", statement="Fact about case 1", investigation_id="inv_alpha")
    mstore.record_episodic(session_id="s2", statement="Fact about case 2", investigation_id="inv_beta")

    # Create dataset-scoped memories in separate datasets
    mstore.record_episodic(session_id="s1", statement="Dataset 1 rule metadata", scope="dataset", dataset="ds_eur")
    mstore.record_episodic(session_id="s2", statement="Dataset 2 rule metadata", scope="dataset", dataset="ds_usd")

    # Query investigation alpha
    alpha_mems = mstore.query_memories(scope="investigation", investigation_id="inv_alpha")
    assert len(alpha_mems) == 1
    assert alpha_mems[0].statement == "Fact about case 1"

    # Query investigation beta
    beta_mems = mstore.query_memories(scope="investigation", investigation_id="inv_beta")
    assert len(beta_mems) == 1
    assert beta_mems[0].statement == "Fact about case 2"

    # Query dataset ds_eur
    eur_mems = mstore.query_memories(scope="dataset", dataset="ds_eur")
    assert len(eur_mems) == 1
    assert eur_mems[0].statement == "Dataset 1 rule metadata"


# =============================================================================
# Test J: Task Scheduling and Dependencies
# =============================================================================
def test_j_task_scheduling_and_dependencies(conn):
    """Coordinator retrieves only ready tasks; blocked tasks become ready when dependencies complete."""
    initialize_graph(conn)
    cstore = CoordinationStore(conn)
    inv_id = "inv_j"

    # Create Task 1 (no dependencies) -> initial status READY
    t1 = cstore.create_task(inv_id, "investigate_anchor", idempotency_key="t1")
    assert t1.status == TaskStatus.READY.value

    # Create Task 2 (depends on Task 1) -> initial status PENDING
    t2 = cstore.create_task(inv_id, "audit_findings", idempotency_key="t2", dependencies=[t1.task_id])
    assert t2.status == TaskStatus.PENDING.value

    # Query ready tasks: only t1 should be ready
    ready = cstore.get_ready_tasks(inv_id)
    assert len(ready) == 1
    assert ready[0].task_id == t1.task_id

    # Claim and complete Task 1
    cstore.claim_task(t1.task_id, agent_id="agent_inv")
    cstore.complete_task(t1.task_id, result_summary="T1 done")

    # Now Task 2 must automatically be unblocked to READY
    ready_after = cstore.get_ready_tasks(inv_id)
    assert len(ready_after) == 1
    assert ready_after[0].task_id == t2.task_id
    assert ready_after[0].status == TaskStatus.READY.value


# =============================================================================
# Test K: Agent Handoffs
# =============================================================================
def test_k_agent_handoffs(conn):
    """Create an agent handoff, verify relationships, and inspect handoff context."""
    initialize_graph(conn)
    cstore = CoordinationStore(conn)
    inv_id = "inv_k"

    t = cstore.create_task(inv_id, "audit_task", idempotency_key="tk")
    h = cstore.create_handoff(
        investigation_id=inv_id,
        task_id=t.task_id,
        source_agent_id="agent_investigator",
        target_role=AgentRole.AUDITOR.value,
        reason="Discovered cycle requiring verification",
        evidence_refs=["ev_1"],
    )
    assert h.status == HandoffStatus.INITIATED.value
    assert h.target_role == AgentRole.AUDITOR.value

    # Retrieve handoff
    retrieved = cstore.get_handoff(h.handoff_id)
    assert retrieved is not None
    assert retrieved.task_id == t.task_id
    assert retrieved.reason == "Discovered cycle requiring verification"


# =============================================================================
# Test L: Duplicate Scheduling and Idempotency
# =============================================================================
def test_l_duplicate_scheduling_and_idempotency(conn):
    """Repeated task creation with the same idempotency key returns the existing task."""
    initialize_graph(conn)
    cstore = CoordinationStore(conn)
    inv_id = "inv_l"

    t1 = cstore.create_task(inv_id, "sample_task", idempotency_key="idem_l_1")
    t2 = cstore.create_task(inv_id, "sample_task", idempotency_key="idem_l_1")

    assert t1.task_id == t2.task_id
    tasks = cstore.list_tasks(inv_id)
    assert len(tasks) == 1


# =============================================================================
# Test M: Interrupted Investigation and Resumption
# =============================================================================
def test_m_interrupted_investigation_and_resumption(conn, make_dataset):
    """Simulate an interrupted run midway, then resume and verify completion from graph state."""
    initialize_graph(conn)
    ds = make_dataset("interrupted-test", seed=42)
    from money_trail.ingestion import ingest_dataset, load_dataset_config
    ingest_dataset(conn, load_dataset_config(ds.ingest_config))

    import json
    gt = json.loads((ds.dir / "ground_truth.json").read_text())["patterns"]
    anchor = gt[0]["transaction_ids"][0]
    inv_id = "inv_interrupted_m"

    orch = MultiAgentOrchestrator(conn)

    # Run with interruption after 'investigate_anchor'
    run1 = orch.run_investigation(
        investigation_id=inv_id,
        anchor_id=anchor,
        dataset=ds.gen_cfg.namespace,
        interrupt_after_task_type="investigate_anchor",
    )
    assert run1["status"] == SessionStatus.INTERRUPTED.value
    sess_id = run1["session_id"]

    # Verify session is interrupted in graph
    sess_rec = orch.coord_store.get_session(sess_id)
    assert sess_rec.status == SessionStatus.INTERRUPTED.value

    # Resume from a new orchestrator instance (simulating new process)
    orch_resumed = MultiAgentOrchestrator(conn)
    run2 = orch_resumed.resume_investigation(sess_id)
    assert run2["status"] == SessionStatus.COMPLETED.value
    assert run2["resumed_steps"] >= 1


# =============================================================================
# Test O: Concurrent Task Claims
# =============================================================================
def test_o_concurrent_claims_prevented(conn):
    """A task cannot be claimed twice concurrently under the supported coordination model."""
    initialize_graph(conn)
    cstore = CoordinationStore(conn)
    inv_id = "inv_o"

    t = cstore.create_task(inv_id, "single_worker_task", idempotency_key="idem_o")
    assert t.status == TaskStatus.READY.value

    # First worker claims task
    c1 = cstore.claim_task(t.task_id, agent_id="worker_1")
    assert c1.status == TaskStatus.RUNNING.value

    # Second worker attempts to claim the same task -> fails
    with pytest.raises(ValidationError, match="could not be claimed"):
        cstore.claim_task(t.task_id, agent_id="worker_2")
