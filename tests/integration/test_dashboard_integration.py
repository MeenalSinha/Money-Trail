"""Real FalkorDB integration tests for Phase 6 Dashboard Service and Workspaces.

Validates that all dashboard views, graph visualizations, metrics,
and multi-agent investigations operate strictly against real FalkorDB state,
preserving dataset scoping and reporting real errors for invalid inputs.
"""
from __future__ import annotations

import time
import pytest

from money_trail.coordination.models import AgentRole, HandoffStatus, SessionStatus, TaskStatus
from money_trail.coordination.store import CoordinationStore
from money_trail.dashboard.service import DashboardService
from money_trail.database import GraphStore
from money_trail.database.migration import migrate_schema
from money_trail.exceptions import ValidationError
from money_trail.memory.models import MemoryScope, MemoryStatus, MemoryType
from money_trail.memory.store import MemoryStore


def _seed_test_financial_graph(store: GraphStore, dataset: str = "ds_alpha"):
    """Helper to seed connected accounts and transactions."""
    store.upsert_node("Account", {"account_id": f"{dataset}:acc_1", "dataset": dataset})
    store.upsert_node("Account", {"account_id": f"{dataset}:acc_2", "dataset": dataset})
    store.upsert_node("Account", {"account_id": f"{dataset}:acc_3", "dataset": dataset})

    store.upsert_node("Transaction", {
        "transaction_id": f"{dataset}:tx_1",
        "amount": 25000,
        "currency": "USD",
        "timestamp": 1700000000000,
        "dataset": dataset,
    })
    store.upsert_node("Transaction", {
        "transaction_id": f"{dataset}:tx_2",
        "amount": 24800,
        "currency": "USD",
        "timestamp": 1700001000000,
        "dataset": dataset,
    })

    store.link("SENT", f"{dataset}:acc_1", f"{dataset}:tx_1")
    store.link("RECEIVED_BY", f"{dataset}:tx_1", f"{dataset}:acc_2")

    store.link("SENT", f"{dataset}:acc_2", f"{dataset}:tx_2")
    store.link("RECEIVED_BY", f"{dataset}:tx_2", f"{dataset}:acc_3")


def test_dashboard_empty_graph_state(ready):
    """Scenario F: Empty graph renders without exceptions or invented metrics."""
    migrate_schema(ready)
    svc = DashboardService(ready)

    metrics = svc.get_overview_metrics()
    assert metrics["transactions"] == 0
    assert metrics["accounts"] == 0
    assert metrics["investigations"]["total"] == 0
    assert metrics["findings"]["total"] == 0
    assert metrics["tasks"]["total"] == 0
    assert metrics["memories"] == 0
    assert metrics["sessions"] == 0

    assert svc.list_datasets() == []
    assert svc.list_investigations() == []
    assert svc.search_transactions() == []


def test_dashboard_system_health(ready):
    """Test System Status check against live FalkorDB instance."""
    migrate_schema(ready)
    svc = DashboardService(ready)
    health = svc.get_system_health()

    assert health["database_connected"] is True
    assert health["ping_latency_ms"] is not None
    assert health["ping_latency_ms"] >= 0
    assert health["active_schema_version"] == 2
    assert health["applied_migrations_count"] >= 1
    assert not health["errors"]


def test_dashboard_dataset_scoping_and_search(ready):
    """Scenario E & Dataset scoping: verify isolation and transaction search."""
    store = GraphStore(ready)
    _seed_test_financial_graph(store, "ds_alpha")
    _seed_test_financial_graph(store, "ds_beta")

    svc = DashboardService(ready)
    datasets = svc.list_datasets()
    assert "ds_alpha" in datasets
    assert "ds_beta" in datasets

    # Scoped search
    txs_alpha = svc.search_transactions(dataset="ds_alpha")
    assert len(txs_alpha) == 2
    assert all(t["dataset"] == "ds_alpha" for t in txs_alpha)

    # Filtered search
    filtered = svc.search_transactions(dataset="ds_alpha", query="tx_1")
    assert len(filtered) == 1
    assert filtered[0]["transaction_id"] == "ds_alpha:tx_1"
    assert filtered[0]["source_account"] == "ds_alpha:acc_1"
    assert filtered[0]["target_account"] == "ds_alpha:acc_2"


def test_dashboard_graph_network_traversal(ready):
    """Test interactive graph traversal with directed edges and connected accounts."""
    store = GraphStore(ready)
    _seed_test_financial_graph(store, "ds_alpha")

    svc = DashboardService(ready)
    graph = svc.get_transaction_graph_network("ds_alpha:tx_1", max_hops=2)

    assert graph["center_id"] == "ds_alpha:tx_1"
    node_ids = {n["id"] for n in graph["nodes"]}
    assert "ds_alpha:tx_1" in node_ids
    assert "ds_alpha:acc_1" in node_ids
    assert "ds_alpha:acc_2" in node_ids
    assert "ds_alpha:tx_2" in node_ids
    assert "ds_alpha:acc_3" in node_ids

    # Verify directed edges
    edge_pairs = {(e["from"], e["to"], e["label"]) for e in graph["edges"]}
    assert ("ds_alpha:acc_1", "ds_alpha:tx_1", "SENT") in edge_pairs
    assert ("ds_alpha:tx_1", "ds_alpha:acc_2", "RECEIVED_BY") in edge_pairs
    assert ("ds_alpha:acc_2", "ds_alpha:tx_2", "SENT") in edge_pairs
    assert ("ds_alpha:tx_2", "ds_alpha:acc_3", "RECEIVED_BY") in edge_pairs


def test_dashboard_investigation_workspace_retrieval(ready):
    """Scenario A & B: Retrieve complete investigation workspace with tasks and findings."""
    migrate_schema(ready)
    store = GraphStore(ready)
    _seed_test_financial_graph(store, "ds_alpha")

    # Create investigation, finding, evidence
    store.upsert_node("Investigation", {
        "investigation_id": "inv_test_001",
        "status": "in_progress",
        "title": "Pass-through laundering review",
        "created_at": 1700000000000,
        "updated_at": 1700001000000,
    })
    store.link("STARTED_FROM", "inv_test_001", "ds_alpha:tx_1")

    store.upsert_node("Finding", {
        "finding_id": "fnd_001",
        "kind": "observation",
        "statement": "Rapid pass-through transfer observed from acc_1 to acc_3 via acc_2",
        "created_at": 1700000500000,
    })
    store.link("HAS_FINDING", "inv_test_001", "fnd_001")

    store.upsert_node("Evidence", {
        "evidence_id": "ev_001",
        "created_at": 1700000600000,
        "produced_by_tool": "pass_through",
    })
    store.link("SUPPORTED_BY", "fnd_001", "ev_001")
    store.link("REFERENCES", "ev_001", "ds_alpha:tx_1")
    store.link("REFERENCES", "ev_001", "ds_alpha:tx_2")

    # Add agent task and handoff
    coord = CoordinationStore(ready)
    t = coord.create_task(
        investigation_id="inv_test_001",
        task_type="trace_transactions",
        assigned_role=AgentRole.INVESTIGATOR.value,
    )
    coord.claim_task(t.task_id, agent_id="agent_inv_1", role=AgentRole.INVESTIGATOR.value)
    coord.complete_task(t.task_id, result_summary="Found 2 connected transfers", finding_ids=["fnd_001"])

    coord.create_handoff(
        investigation_id="inv_test_001",
        task_id=t.task_id,
        source_agent_id="agent_inv_1",
        target_role=AgentRole.AUDITOR.value,
        reason="Verify pass-through evidence grounding",
    )

    svc = DashboardService(ready)
    workspace = svc.get_investigation_workspace("inv_test_001")

    assert workspace is not None
    assert workspace["investigation_id"] == "inv_test_001"
    assert workspace["status"] in ("open", "in_progress")
    assert workspace["anchor"]["transaction_id"] == "ds_alpha:tx_1"
    assert len(workspace["findings"]) == 1
    assert len(workspace["tasks"]) == 1
    assert workspace["tasks"][0]["task_id"] == t.task_id
    assert len(workspace["handoffs"]) == 1
    assert workspace["handoffs"][0]["reason"] == "Verify pass-through evidence grounding"

    # Verify pattern subgraph retrieval
    subgraph = svc.get_pattern_subgraph("fnd_001")
    assert subgraph is not None
    assert subgraph["finding_id"] == "fnd_001"
    assert len(subgraph["transactions"]) == 2


def test_dashboard_memory_provenance_retrieval(ready):
    """Scenario D: Memory provenance retrieval from FalkorDB."""
    migrate_schema(ready)
    store = GraphStore(ready)
    _seed_test_financial_graph(store, "ds_alpha")

    # Create investigation and verified finding
    store.upsert_node("Investigation", {"investigation_id": "inv_mem_001", "status": "closed", "created_at": 1700000000000})
    store.upsert_node("Finding", {
        "finding_id": "fnd_mem_001",
        "kind": "observation",
        "statement": "Persistent memory ground finding",
        "created_at": 1700000000000,
    })
    store.upsert_node("Evidence", {
        "evidence_id": "ev_mem_001",
        "produced_by_tool": "trace",
        "created_at": 1700000000000,
    })
    store.link("HAS_FINDING", "inv_mem_001", "fnd_mem_001")
    store.link("SUPPORTED_BY", "fnd_mem_001", "ev_mem_001")
    store.link("REFERENCES", "ev_mem_001", "ds_alpha:tx_1")

    # Add persistent memory grounded in evidence
    coord = CoordinationStore(ready)
    sess = coord.create_session("inv_mem_001")

    mem_store = MemoryStore(ready)
    mem = mem_store.record_episodic(
        session_id=sess.session_id,
        statement="Subject acc_1 engaged in rapid outbound transfer.",
        scope=MemoryScope.INVESTIGATION.value,
        investigation_id="inv_mem_001",
        evidence_ids=["ev_mem_001"],
    )

    m = mem_store.get_memory(mem.memory_id)
    assert m is not None
    assert "ev_mem_001" in m.evidence_ids

    # Query provenance chain: Memory -> Evidence -> Transaction
    prov_rows = ready.graph.ro_query(
        "MATCH (m:Memory {memory_id: $mid})-[:GROUNDED_BY]->(e:Evidence)-[:REFERENCES]->(t:Transaction) "
        "RETURN e.evidence_id, t.transaction_id",
        {"mid": mem.memory_id},
    ).result_set
    assert len(prov_rows) >= 1
    assert prov_rows[0][0] == "ev_mem_001"
    assert prov_rows[0][1] == "ds_alpha:tx_1"


def test_dashboard_invalid_inputs(ready):
    """Scenario E: Nonexistent transactions and invalid inputs are safely rejected."""
    migrate_schema(ready)
    svc = DashboardService(ready)

    # Nonexistent transaction raises ValidationError
    with pytest.raises(ValidationError):
        svc.get_transaction_graph_network("non_existent_tx_99999")

    # Starting investigation on nonexistent transaction raises ValidationError
    with pytest.raises(ValidationError):
        svc.start_investigation("non_existent_tx_99999")

    # Nonexistent investigation workspace returns None
    assert svc.get_investigation_workspace("inv_missing") is None

    # Nonexistent pattern subgraph returns None
    assert svc.get_pattern_subgraph("fnd_missing") is None
