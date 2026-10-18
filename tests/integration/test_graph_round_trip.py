import time
import uuid

import pytest

from money_trail.database import GraphStore, initialize_graph
from money_trail.exceptions import ValidationError


@pytest.fixture
def store(conn):
    initialize_graph(conn)
    return GraphStore(conn)


def _ids():
    run = f"testrun-{uuid.uuid4().hex[:10]}"
    return {k: f"{run}:{k}" for k in
            ("acc_a", "acc_b", "tx", "alert", "inv", "finding", "evidence", "agent")}


def _build(store, ids, now):
    store.upsert_node("Account", {"account_id": ids["acc_a"], "dataset": "test-data"})
    store.upsert_node("Account", {"account_id": ids["acc_b"], "dataset": "test-data"})
    store.upsert_node("Transaction", {"transaction_id": ids["tx"], "amount": 12345, "currency": "USD",
                                      "timestamp": now, "channel": "test"})
    store.upsert_node("Alert", {"alert_id": ids["alert"], "created_at": now, "severity": "test"})
    store.upsert_node("Investigation", {"investigation_id": ids["inv"], "status": "open", "created_at": now})
    store.upsert_node("Finding", {"finding_id": ids["finding"], "kind": "observation",
                                  "statement": "test statement", "created_at": now, "confidence": 0.5})
    store.upsert_node("Evidence", {"evidence_id": ids["evidence"], "created_at": now,
                                   "produced_by_tool": "test-tool", "query_ref": "q-1"})
    store.upsert_node("Agent", {"agent_id": ids["agent"], "role": "test-role"})
    store.link("SENT", ids["acc_a"], ids["tx"])
    store.link("RECEIVED_BY", ids["tx"], ids["acc_b"])
    store.link("FLAGS", ids["alert"], ids["tx"])
    store.link("STARTED_FROM", ids["inv"], ids["tx"])
    store.link("HAS_FINDING", ids["inv"], ids["finding"])
    store.link("SUPPORTED_BY", ids["finding"], ids["evidence"])
    store.link("REFERENCES", ids["evidence"], ids["tx"])
    store.link("ASSIGNED_TO", ids["agent"], ids["inv"], {"assigned_at": now})
    store.link("AUTHORED", ids["agent"], ids["finding"], {"authored_at": now})


def test_d_round_trip_identities_properties_and_directions(conn, store):
    ids, now = _ids(), int(time.time() * 1000)
    _build(store, ids, now)

    tx = store.get_node("Transaction", ids["tx"])
    assert tx == {"transaction_id": ids["tx"], "amount": 12345, "currency": "USD",
                  "timestamp": now, "channel": "test"}
    assert store.get_node("Finding", ids["finding"])["confidence"] == 0.5
    assert store.get_node("Agent", ids["agent"]) == {"agent_id": ids["agent"], "role": "test-role"}
    assert store.get_node("Account", "does-not-exist") is None

    assert store.related("SENT", ids["acc_a"]) == [ids["tx"]]
    assert store.related("RECEIVED_BY", ids["tx"]) == [ids["acc_b"]]
    assert store.related("FLAGS", ids["alert"]) == [ids["tx"]]
    assert store.related("STARTED_FROM", ids["inv"]) == [ids["tx"]]
    assert store.related("HAS_FINDING", ids["inv"]) == [ids["finding"]]
    assert store.related("SUPPORTED_BY", ids["finding"]) == [ids["evidence"]]
    assert store.related("REFERENCES", ids["evidence"]) == [ids["tx"]]
    assert store.related("ASSIGNED_TO", ids["agent"]) == [ids["inv"]]
    assert store.related("AUTHORED", ids["agent"]) == [ids["finding"]]

    # direction check with raw Cypher: reverse traversal finds nothing
    rev = conn.graph.ro_query(
        "MATCH (:Transaction {transaction_id: $t})-[:SENT]->(:Account) RETURN count(*)", {"t": ids["tx"]}
    ).result_set
    assert rev == [[0]]

    # multi-hop evidence trace: investigation -> finding -> evidence -> transaction -> receiving account
    trace = conn.graph.ro_query(
        "MATCH (:Investigation {investigation_id: $i})-[:HAS_FINDING]->(:Finding)-[:SUPPORTED_BY]->"
        "(:Evidence)-[:REFERENCES]->(:Transaction)-[:RECEIVED_BY]->(a:Account) RETURN a.account_id",
        {"i": ids["inv"]},
    ).result_set
    assert trace == [[ids["acc_b"]]]

    assert store.count_nodes() == 8


def test_upsert_is_idempotent_and_updates(conn, store):
    ids, now = _ids(), int(time.time() * 1000)
    base = {"transaction_id": ids["tx"], "amount": 1, "currency": "USD", "timestamp": now}
    store.upsert_node("Transaction", base)
    store.upsert_node("Transaction", {**base, "amount": 2})
    assert store.count_nodes() == 1
    assert store.get_node("Transaction", ids["tx"])["amount"] == 2


def test_validation_blocks_bad_writes_before_database(conn, store):
    with pytest.raises(ValidationError):
        store.upsert_node("Transaction", {"transaction_id": "t", "amount": -5, "currency": "USD", "timestamp": 1})
    with pytest.raises(ValidationError):
        store.upsert_node("Account) DETACH DELETE (n", {"account_id": "x"})
    assert store.count_nodes() == 0


def test_link_to_missing_endpoint_fails(conn, store):
    with pytest.raises(ValidationError, match="does not exist"):
        store.link("SENT", "no-such-account", "no-such-tx")
    assert conn.graph.ro_query("MATCH ()-[r]->() RETURN count(r)").result_set == [[0]]


def test_database_enforces_constraints_independently_of_python_validation(conn, store):
    ids = _ids()
    store.upsert_node("Account", {"account_id": ids["acc_a"]})
    with pytest.raises(Exception):  # unique constraint, bypassing GraphStore on purpose
        conn.graph.query("CREATE (:Account {account_id: $id})", {"id": ids["acc_a"]})
    with pytest.raises(Exception):  # mandatory constraint
        conn.graph.query("CREATE (:Transaction {transaction_id: $id})", {"id": ids["tx"]})
    assert store.count_nodes() == 1


def test_injection_style_values_are_treated_as_data(conn, store):
    evil = "x'}) DETACH DELETE n //"
    store.upsert_node("Account", {"account_id": evil})
    store.upsert_node("Account", {"account_id": "innocent"})
    assert store.get_node("Account", evil) == {"account_id": evil}
    assert store.count_nodes() == 2


def test_f_cleanup_removes_only_the_test_graph(conn, store, base_settings):
    ids, now = _ids(), int(time.time() * 1000)
    _build(store, ids, now)
    assert conn.graph_exists()
    conn.graph.delete()
    assert not conn.graph_exists()
    assert conn.settings.graph_name != base_settings.graph_name
