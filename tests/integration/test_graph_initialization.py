import pytest

from money_trail.database import (GraphStore, check_health, initialize_graph, inspect_schema)
from money_trail.database import graph_schema as gs
from money_trail.database import health as h
from money_trail.database.falkordb_client import FalkorDBConnection


def test_health_distinguishes_missing_graph(conn):
    report = check_health(conn)
    assert report.database_reachable and not report.graph_accessible
    assert report.status == h.GRAPH_MISSING
    # a read-only check must not create the graph
    assert not conn.graph_exists()


def test_b_initialization_creates_full_schema(conn, test_settings):
    result = initialize_graph(conn)
    assert result.graph_name == test_settings.graph_name and result.graph_created
    assert len(result.indexes_created) == len(gs.required_indexes())
    assert len(result.constraints_created) == len(gs.required_constraints())
    state = inspect_schema(conn)
    assert state.ready and not state.missing_indexes and not state.missing_constraints
    assert check_health(conn).healthy


def test_health_reports_schema_not_ready_without_modifying(conn):
    # graph exists with data but no schema elements
    conn.graph.query("CREATE (:Account {account_id: 'x'})")
    report = check_health(conn)
    assert report.status == h.SCHEMA_NOT_READY and report.graph_accessible and not report.schema_ready
    assert report.details["missing_indexes"] and report.details["missing_constraints"]
    assert inspect_schema(conn).missing_indexes  # still missing: health did not repair anything


def test_c_idempotent_and_non_destructive(conn):
    initialize_graph(conn)
    store = GraphStore(conn)
    store.upsert_node("Account", {"account_id": "keep-me"})
    before_constraints = conn.graph.list_constraints()
    before_indexes = conn.graph.ro_query("CALL db.indexes()").result_set

    second = initialize_graph(conn)
    third = initialize_graph(conn)

    assert not second.changed and not third.changed
    assert not second.graph_created
    assert second.already_present == len(gs.required_indexes()) + len(gs.required_constraints())
    assert store.get_node("Account", "keep-me") == {"account_id": "keep-me"}
    assert len(conn.graph.list_constraints()) == len(before_constraints)
    assert len(conn.graph.ro_query("CALL db.indexes()").result_set) == len(before_indexes)


def test_init_on_existing_graph_with_data_preserves_data(conn):
    conn.graph.query("UNWIND range(1, 5) AS i CREATE (:Account {account_id: 'pre-' + toString(i)})")
    result = initialize_graph(conn)
    assert not result.graph_created and result.changed
    assert GraphStore(conn).count_nodes() == 5


def test_init_refuses_to_report_success_when_data_violates_constraints(conn):
    from money_trail.exceptions import SchemaError
    conn.graph.query("CREATE (:Account {account_id: 'dup'}), (:Account {account_id: 'dup'})")
    with pytest.raises(SchemaError):
        initialize_graph(conn, wait_timeout=3)
    assert GraphStore(conn).count_nodes() == 2  # nothing deleted


def test_e_isolation_other_graph_untouched(conn, make_graph_name, base_settings):
    sentinel_name = make_graph_name()
    sentinel_conn = FalkorDBConnection(base_settings.with_graph_name(sentinel_name))
    try:
        sentinel_conn.graph.query("CREATE (:Account {account_id: 'sentinel'})")
        initialize_graph(conn)
        GraphStore(conn).upsert_node("Account", {"account_id": "other"})
        assert conn.settings.graph_name not in (sentinel_name, base_settings.graph_name)
        assert GraphStore(sentinel_conn).get_node("Account", "sentinel") == {"account_id": "sentinel"}
        assert GraphStore(sentinel_conn).count_nodes() == 1
    finally:
        sentinel_conn.close()
