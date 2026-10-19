"""Unit tests for Phase 6 Dashboard Service, Visualizations, and Components."""
from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from money_trail.dashboard.components.styles import status_badge
from money_trail.dashboard.components.graph_viz import render_interactive_graph
from money_trail.dashboard.service import DashboardService
from money_trail.cli import main


def test_status_badge_rendering():
    """Verify status_badge renders proper HTML and badge classes."""
    b_completed = status_badge("completed")
    assert "completed" in b_completed
    assert "mt-badge-completed" in b_completed

    b_verified = status_badge("verified")
    assert "verified" in b_verified
    assert "mt-badge-verified" in b_verified

    b_failed = status_badge("failed")
    assert "failed" in b_failed
    assert "mt-badge-failed" in b_failed

    b_blocked = status_badge("blocked")
    assert "blocked" in b_blocked
    assert "mt-badge-blocked" in b_blocked

    b_custom = status_badge("unknown_custom_status")
    assert "unknown_custom_status" in b_custom
    assert "mt-badge" in b_custom


def test_graph_viz_empty_state():
    """Verify render_interactive_graph renders gracefully with empty nodes/edges."""
    html = render_interactive_graph([], [])
    assert "No graph nodes to display in the current scope" in html


def test_graph_viz_with_data():
    """Verify render_interactive_graph produces vis-network container when provided data."""
    nodes = [
        {"id": "A1", "label": "Account 1", "type": "Account"},
        {"id": "T1", "label": "Tx 1", "type": "Transaction"},
    ]
    edges = [
        {"from": "A1", "to": "T1", "label": "$100.00"},
    ]
    html = render_interactive_graph(nodes, edges, height="500px")
    assert "vis-network" in html
    assert "vis.Network" in html
    assert "A1" in html
    assert "T1" in html
    assert "$100.00" in html


def test_dashboard_service_overview_mock():
    """Verify DashboardService.get_overview_metrics with mock connection."""
    mock_conn = MagicMock()
    # Mock result sets for: tx_count, acc_count, alert_count, inv_rows, finding_rows, task_rows, memory_count, session_count
    mock_conn.graph.ro_query.side_effect = [
        MagicMock(result_set=[[50]]),  # tx_count
        MagicMock(result_set=[[10]]),  # acc_count
        MagicMock(result_set=[[5]]),   # alert_count
        MagicMock(result_set=[["open", 2], ["closed", 1]]),  # inv_rows
        MagicMock(result_set=[["observation", 3], ["hypothesis", 1]]),  # finding_rows
        MagicMock(result_set=[["completed", 4], ["ready", 2]]),  # task_rows
        MagicMock(result_set=[[7]]),   # memory_count
        MagicMock(result_set=[[3]]),   # session_count
    ]

    svc = DashboardService(mock_conn)
    metrics = svc.get_overview_metrics()

    assert metrics["transactions"] == 50
    assert metrics["accounts"] == 10
    assert metrics["alerts"] == 5
    assert metrics["investigations"]["total"] == 3
    assert metrics["investigations"]["open"] == 2
    assert metrics["findings"]["total"] == 4
    assert metrics["tasks"]["total"] == 6
    assert metrics["memories"] == 7
    assert metrics["sessions"] == 3


def test_dashboard_service_list_datasets_mock():
    """Verify DashboardService.list_datasets with mock connection."""
    mock_conn = MagicMock()
    mock_conn.graph.ro_query.return_value.result_set = [["ds-alpha"], ["ds-beta"]]

    svc = DashboardService(mock_conn)
    datasets = svc.list_datasets()
    assert datasets == ["ds-alpha", "ds-beta"]


def test_dashboard_service_search_transactions_mock():
    """Verify DashboardService.search_transactions query structure."""
    mock_conn = MagicMock()
    mock_conn.graph.ro_query.return_value.result_set = [
        ["tx-100", 2500.0, "USD", 1700000000000, "ds-alpha", "ACC-1", "ACC-2"]
    ]

    svc = DashboardService(mock_conn)
    results = svc.search_transactions(dataset="ds-alpha", query="tx-1", limit=10)
    assert len(results) == 1
    assert results[0]["transaction_id"] == "tx-100"
    assert results[0]["amount"] == 2500.0
    assert results[0]["source_account"] == "ACC-1"
    assert results[0]["target_account"] == "ACC-2"


def test_dashboard_service_get_transaction_graph_network_mock():
    """Verify graph payload formatting from mock data."""
    mock_conn = MagicMock()
    # Mock store get_node returning the center transaction node
    mock_tx_node = {"transaction_id": "tx-100", "amount": 5000.0, "currency": "USD", "dataset": "ds-1"}

    # Mock accounts and connected tx
    mock_src = MagicMock(properties={"account_id": "ACC-SRC"})
    mock_dst = MagicMock(properties={"account_id": "ACC-DST"})
    mock_t2 = MagicMock(properties={"transaction_id": "tx-102"})
    mock_a3 = MagicMock(properties={"account_id": "ACC-3"})

    # start, src, dst, t2, a3
    mock_conn.graph.ro_query.return_value.result_set = [
        [MagicMock(), mock_src, mock_dst, mock_t2, mock_a3]
    ]

    svc = DashboardService(mock_conn)
    svc._store.get_node = MagicMock(return_value=mock_tx_node)

    graph = svc.get_transaction_graph_network("tx-100", max_hops=2)
    assert graph["center_id"] == "tx-100"
    assert len(graph["nodes"]) >= 3
    assert len(graph["edges"]) >= 2
    node_ids = {n["id"] for n in graph["nodes"]}
    assert "tx-100" in node_ids
    assert "ACC-SRC" in node_ids
    assert "ACC-DST" in node_ids


def test_dashboard_service_get_system_health_mock():
    """Verify get_system_health compiles healthy status when DB responds."""
    mock_conn = MagicMock()
    mock_conn.ping.return_value = True
    mock_conn.graph_exists.return_value = True
    mock_conn.settings.graph_name = "test_graph"
    mock_conn.settings.host = "localhost"
    mock_conn.settings.port = 6379

    # Migration queries: get_applied_migrations returns 5 columns with status='completed'
    mock_conn.graph.ro_query.return_value.result_set = [
        ["mig_1", 1, 1700000000000, "phase1", "completed"],
        ["mig_2", 2, 1700001000000, "phase5", "completed"],
    ]

    svc = DashboardService(mock_conn)
    health = svc.get_system_health()

    assert health["database_connected"] is True
    assert health["graph_name"] == "test_graph"
    assert health["active_schema_version"] == 2
    assert health["applied_migrations_count"] == 2


def test_cli_dashboard_parser():
    """Verify CLI accepts dashboard command and arguments."""
    with pytest.raises(SystemExit) as exc:
        main(["dashboard", "--help"])
    assert exc.value.code == 0
