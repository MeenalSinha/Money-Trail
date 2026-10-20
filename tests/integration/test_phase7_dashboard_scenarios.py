"""Integration tests for Phase 7 Task 5: Dashboard and User-Workflow Quality Assurance (Scenarios A through J).

Tests all 10 dashboard user workflow scenarios through the real DashboardService layer against FalkorDB.
"""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from money_trail.config import Settings
from money_trail.dashboard.service import DashboardService
from money_trail.database.falkordb_client import FalkorDBConnection
from money_trail.database.migration import migrate_schema
from money_trail.evaluation.detector_eval import (
    ingest_dataset_into_graph,
    prepare_synthetic_dataset,
)
from money_trail.evaluation.qa_config import DatasetProfile
from money_trail.memory.models import MemoryScope
from money_trail.memory.store import MemoryStore

pytestmark = pytest.mark.integration


def test_dashboard_scenarios_a_through_j(ready, tmp_path):
    """Execute Scenarios A through J against real FalkorDB test graph."""
    migrate_schema(ready)
    svc = DashboardService(ready)

    # -------------------------------------------------------------------------
    # Scenario A: Fresh graph
    # -------------------------------------------------------------------------
    overview_fresh = svc.get_overview_metrics()
    assert overview_fresh["transactions"] == 0
    assert overview_fresh["accounts"] == 0
    assert overview_fresh["investigations"]["total"] == 0
    assert overview_fresh["tasks"]["total"] == 0
    assert overview_fresh["memories"] == 0
    assert svc.list_investigations() == []
    assert svc.list_datasets() == []

    health_fresh = svc.get_system_health()
    assert health_fresh["database_connected"] is True
    assert health_fresh["active_schema_version"] is not None

    # -------------------------------------------------------------------------
    # Scenario B: Populated graph
    # -------------------------------------------------------------------------
    ns = "dash_scen_ns"
    profile = DatasetProfile(
        namespace=ns,
        seed=101,
        accounts=20,
        transactions=60,
        patterns_per_type=1,
    )
    ds_dir, _ = prepare_synthetic_dataset(profile, tmp_path)
    ingest_dataset_into_graph(ready, ds_dir)

    overview_pop = svc.get_overview_metrics(dataset=ns)
    real_tx_count = ready.graph.ro_query("MATCH (t:Transaction {dataset: $ds}) RETURN count(t)", {"ds": ns}).result_set[0][0]
    assert overview_pop["transactions"] == real_tx_count
    assert overview_pop["transactions"] > 0
    assert ns in svc.list_datasets()

    tx_list = svc.search_transactions(dataset=ns, limit=10)
    assert len(tx_list) > 0
    anchor_tx = tx_list[0]["transaction_id"]

    # -------------------------------------------------------------------------
    # Scenario C: New investigation
    # -------------------------------------------------------------------------
    inv_id = f"dash_test_inv_{ns}"
    inv_res = svc.start_investigation(
        anchor_id=anchor_tx,
        dataset=ns,
        question="Investigate suspicious network connectivity",
        investigation_id=inv_id,
        persist=True,
    )
    assert inv_res["investigation_id"] == inv_id
    workspace = svc.get_investigation_workspace(inv_id)
    assert workspace is not None
    assert workspace["investigation_id"] == inv_id
    assert workspace["anchor"]["transaction_id"] == anchor_tx
    assert len(workspace["sessions"]) >= 1

    # -------------------------------------------------------------------------
    # Scenario D: Graph exploration
    # -------------------------------------------------------------------------
    graph_net = svc.get_transaction_graph_network(anchor_tx, max_hops=2)
    assert graph_net["center_id"] == anchor_tx
    assert len(graph_net["nodes"]) >= 1
    center_nodes = [n for n in graph_net["nodes"] if n["id"] == anchor_tx]
    assert len(center_nodes) == 1
    assert center_nodes[0]["is_center"] is True

    # -------------------------------------------------------------------------
    # Scenario E: Evidence inspection
    # -------------------------------------------------------------------------
    findings = workspace.get("findings", [])
    if findings:
        f_id = findings[0]["finding_id"]
        subgraph = svc.get_pattern_subgraph(f_id)
        if subgraph:
            assert subgraph["finding_id"] == f_id
            assert len(subgraph["nodes"]) > 0
            assert len(subgraph["edges"]) > 0

    # -------------------------------------------------------------------------
    # Scenario F: Memory inspection
    # -------------------------------------------------------------------------
    mem_store = MemoryStore(ready)
    mem = mem_store.record_episodic(
        session_id=workspace["sessions"][0]["session_id"],
        statement="Dashboard observed suspicious fan-in transfer cluster.",
        scope=MemoryScope.INVESTIGATION.value,
        investigation_id=inv_id,
    )
    ws_after_mem = svc.get_investigation_workspace(inv_id)
    assert ws_after_mem is not None
    stored_mem_ids = [m["memory_id"] for m in ws_after_mem["memories"]]
    assert mem.memory_id in stored_mem_ids

    # -------------------------------------------------------------------------
    # Scenario G: Task and handoff inspection
    # -------------------------------------------------------------------------
    assert len(ws_after_mem["tasks"]) >= 1
    task_types = [t["task_type"] for t in ws_after_mem["tasks"]]
    assert len(task_types) > 0

    # -------------------------------------------------------------------------
    # Scenario H: Investigation resumption
    # -------------------------------------------------------------------------
    from money_trail.coordination.models import SessionStatus
    from money_trail.coordination.store import CoordinationStore

    coord_store = CoordinationStore(ready)
    inv_res_id = f"inv_resume_{ns}"
    sess_res = coord_store.create_session(inv_res_id)
    # Transition ACTIVE -> INTERRUPTED (valid transition)
    coord_store.update_session_status(sess_res.session_id, SessionStatus.INTERRUPTED.value)
    resumed = svc.resume_investigation(sess_res.session_id)
    assert resumed is not None
    assert "status" in resumed or "session_id" in resumed

    # -------------------------------------------------------------------------
    # Scenario I: Backend failure handling
    # -------------------------------------------------------------------------
    import dataclasses
    bad_settings = dataclasses.replace(
        ready.settings,
        port=59999,  # Unreachable port
        connect_timeout=0.2,
        socket_timeout=0.2,
    )
    bad_conn = FalkorDBConnection(bad_settings)
    disconnected_svc = DashboardService(bad_conn)
    fail_health = disconnected_svc.get_system_health()
    assert fail_health["database_connected"] is False
    assert len(fail_health["errors"]) > 0

    # -------------------------------------------------------------------------
    # Scenario J: Evaluation display
    # -------------------------------------------------------------------------
    inv_list = svc.list_investigations(limit=10)
    assert len(inv_list) >= 1
    assert any(i["investigation_id"] == inv_id for i in inv_list)
