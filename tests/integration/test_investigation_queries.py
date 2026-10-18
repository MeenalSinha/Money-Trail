"""Foundational investigation queries against a real FalkorDB (tests H and I)."""
import json

import pytest

from money_trail.database import GraphStore
from money_trail.exceptions import QueryError
from money_trail.investigation import InvestigationQueries, QueryLimits


@pytest.fixture
def mini(ready):
    """A small, explicitly constructed graph with known timestamps (test data, not a dataset)."""
    st = GraphStore(ready)
    for a in "ABCDEF":
        st.upsert_node("Account", {"account_id": a})
    def tx(i, src, dst, ts, amount=100):
        st.upsert_node("Transaction", {"transaction_id": i, "amount": amount, "currency": "USD", "timestamp": ts})
        st.link("SENT", src, i); st.link("RECEIVED_BY", i, dst)
    tx("tx1", "A", "B", 1000); tx("tx2", "B", "C", 2000); tx("tx3", "C", "D", 3000)
    tx("tx4", "B", "E", 1500); tx("tx5", "C", "B", 500)
    st.upsert_node("Alert", {"alert_id": "al1", "created_at": 2100, "alert_type": "t"})
    st.link("FLAGS", "al1", "tx2")
    return ready


def q(conn, **limits):
    return InvestigationQueries(conn, QueryLimits(**limits))


def nodes_and_edges(conn):
    return conn.graph.ro_query("MATCH (n) OPTIONAL MATCH (n)-[r]->() RETURN count(DISTINCT n), count(r)").result_set[0]


# ---- Query 1
def test_h_transaction_context(mini):
    r = q(mini).transaction_context("tx2")
    assert r.status == "ok" and r.warnings == []
    d = r.data
    assert d["transaction"] == {"transaction_id": "tx2", "amount": 100, "currency": "USD", "timestamp": 2000}
    assert [a["account_id"] for a in d["source_accounts"]] == ["B"]
    assert [a["account_id"] for a in d["destination_accounts"]] == ["C"]
    assert [a["alert_id"] for a in d["alerts"]] == ["al1"]
    assert q(mini).transaction_context("tx1").data["alerts"] == []        # present but empty, not missing


def test_h_transaction_not_found_is_distinct_from_empty(mini):
    r = q(mini).transaction_context("nope")
    assert r.status == "not_found" and r.data == {"transaction_id": "nope"}


def test_h_transaction_with_broken_endpoints_is_flagged_not_hidden(mini):
    mini.graph.query("MATCH (t:Transaction {transaction_id: 'tx1'})-[r:RECEIVED_BY]->() DELETE r")
    r = q(mini).transaction_context("tx1")
    assert r.status == "ok" and any("destination" in w for w in r.warnings)


# ---- Query 2
def ids(r):
    return [(a["direction"], a["transaction"]["transaction_id"], a["counterparty"]) for a in r.data["activity"]]


def test_h_account_activity_direction_order_and_counterparty(mini):
    got = ids(q(mini, max_results=10).account_activity("B"))
    assert got == [("in", "tx5", "C"), ("in", "tx1", "A"), ("out", "tx4", "E"), ("out", "tx2", "C")]
    assert ids(q(mini, max_results=10).account_activity("B", "out")) == [("out", "tx4", "E"), ("out", "tx2", "C")]
    assert ids(q(mini, max_results=10).account_activity("B", "in")) == [("in", "tx5", "C"), ("in", "tx1", "A")]


def test_h_account_activity_time_window_and_limit(mini):
    r = q(mini, max_results=10).account_activity("B", "both", start_ms=1200, end_ms=1800)
    assert ids(r) == [("out", "tx4", "E")] and not r.truncated
    r = q(mini, max_results=2).account_activity("B")
    assert len(r.data["activity"]) == 2 and r.truncated and r.data["limit"] == 2
    assert ids(r) == [("in", "tx5", "C"), ("in", "tx1", "A")]               # earliest first, deterministic


def test_h_account_activity_empty_vs_not_found_vs_invalid(mini):
    r = q(mini, max_results=5).account_activity("F")
    assert r.status == "ok" and r.data["activity"] == [] and not r.truncated
    assert q(mini, max_results=5).account_activity("nope").status == "not_found"
    for bad in (dict(direction="sideways"), dict(start_ms=5, end_ms=1), dict(start_ms=-1)):
        with pytest.raises(QueryError):
            q(mini, max_results=5).account_activity("B", **bad)
    with pytest.raises(QueryError, match="max_results"):
        q(mini).account_activity("B")


# ---- Query 3
def tx_paths(r):
    return sorted([s["transaction"]["transaction_id"] for s in p if "transaction" in s] for p in r.data["paths"])


def test_h_trace_forward_is_time_respecting_and_bounded(mini):
    r = q(mini).trace_network("tx1", "forward", max_hops=3, max_paths=50)
    assert tx_paths(r) == [["tx1", "tx2"], ["tx1", "tx2", "tx3"], ["tx1", "tx4"]]   # tx5 is earlier, never follows
    assert [t["transaction_id"] for t in r.data["transactions"]] == ["tx1", "tx4", "tx2", "tx3"]
    assert r.data["accounts"] == ["A", "B", "C", "D", "E"] and not r.truncated      # incl. first sender, last receivers
    assert r.data["transaction_endpoints"]["tx1"] == {"source": "A", "destination": "B"}
    assert r.data["transaction_endpoints"]["tx3"] == {"source": "C", "destination": "D"}
    full = next(p for p in r.data["paths"] if len(p) == 5)
    assert [list(s)[0] for s in full] == ["transaction", "account", "transaction", "account", "transaction"]
    assert tx_paths(q(mini).trace_network("tx1", "forward", max_hops=1, max_paths=50)) == [["tx1", "tx2"], ["tx1", "tx4"]]


def test_h_trace_gap_limit_and_path_cap(mini):
    r = q(mini).trace_network("tx1", "forward", max_hops=3, max_paths=50, max_gap_ms=600)
    assert tx_paths(r) == [["tx1", "tx4"]]
    r = q(mini).trace_network("tx1", "forward", max_hops=3, max_paths=1)
    assert len(r.data["paths"]) == 1 and r.truncated and r.warnings


def test_h_trace_backward(mini):
    r = q(mini).trace_network("tx3", "backward", max_hops=3, max_paths=50)
    # backward paths start at the seed; tx5 (C->B at 500) legitimately precedes tx2 via account B
    assert tx_paths(r) == [["tx3", "tx2"], ["tx3", "tx2", "tx1"], ["tx3", "tx2", "tx5"]]
    assert r.data["direction"] == "backward"


def test_h_trace_not_found_empty_and_invalid(mini):
    assert q(mini).trace_network("nope", max_hops=2, max_paths=5).status == "not_found"
    r = q(mini).trace_network("tx3", "forward", max_hops=3, max_paths=5)       # D never sends
    assert r.status == "ok" and r.data["paths"] == []
    with pytest.raises(QueryError, match="max_hops"):
        q(mini).trace_network("tx1")
    with pytest.raises(QueryError, match="ceiling"):
        q(mini).trace_network("tx1", max_hops=999, max_paths=5)
    with pytest.raises(QueryError):
        q(mini).trace_network("tx1", "sideways", max_hops=2, max_paths=5)
    with pytest.raises(QueryError):
        q(mini).trace_network("tx1", max_hops="2; MATCH (n) DETACH DELETE n", max_paths=5)


def test_h_trace_uses_limits_from_settings_style_defaults(mini):
    r = InvestigationQueries(mini, QueryLimits(max_hops=2, max_paths=10, max_gap_ms=None)).trace_network("tx1")
    assert r.status == "ok" and r.data["max_hops"] == 2


# ---- Query 4
def test_h_evidence_path(mini):
    st = GraphStore(mini)
    st.upsert_node("Investigation", {"investigation_id": "inv1", "status": "open", "created_at": 1})
    st.upsert_node("Investigation", {"investigation_id": "inv2", "status": "open", "created_at": 1})
    st.upsert_node("Finding", {"finding_id": "f1", "kind": "observation", "statement": "s", "created_at": 1})
    st.upsert_node("Evidence", {"evidence_id": "e1", "created_at": 1, "produced_by_tool": "t", "query_ref": "q"})
    st.link("STARTED_FROM", "inv1", "tx1"); st.link("HAS_FINDING", "inv1", "f1")
    st.link("SUPPORTED_BY", "f1", "e1"); st.link("REFERENCES", "e1", "tx2")
    r = q(mini, max_results=10).evidence_paths("inv1")
    assert r.status == "ok" and len(r.data["paths"]) == 1
    p = r.data["paths"][0]
    assert (p["finding"]["finding_id"], p["evidence"]["evidence_id"], p["transaction"]["transaction_id"]) == ("f1", "e1", "tx2")
    assert p["transaction"]["amount"] == 100 and p["transaction"]["timestamp"] == 2000
    assert q(mini, max_results=10).evidence_paths("inv2").data["paths"] == []        # exists, nothing yet
    assert q(mini, max_results=10).evidence_paths("nope").status == "not_found"


# ---- read-only and injection safety
def test_h_queries_are_read_only_and_treat_input_as_data(mini):
    before = nodes_and_edges(mini)
    engine = q(mini, max_results=5, max_hops=3, max_paths=10)
    evil = "x'}) DETACH DELETE n //"
    for fn in (lambda: engine.transaction_context(evil), lambda: engine.account_activity(evil),
               lambda: engine.trace_network(evil), lambda: engine.evidence_paths(evil)):
        assert fn().status == "not_found"
    engine.transaction_context("tx1"); engine.account_activity("B"); engine.trace_network("tx1")
    assert nodes_and_edges(mini) == before


def test_h_all_queries_use_read_only_graph_calls(mini, monkeypatch):
    graph_cls = type(mini.graph)
    def forbid(self, *a, **k):
        raise AssertionError("investigation queries must not issue write-capable queries")
    monkeypatch.setattr(graph_cls, "query", forbid)
    engine = q(mini, max_results=5, max_hops=3, max_paths=10)
    engine.transaction_context("tx1"); engine.account_activity("B"); engine.trace_network("tx1"); engine.evidence_paths("x")


# ---- real generated data: the graph recovers constructed patterns
def _gt(ds):
    return json.loads((ds.dir / "ground_truth.json").read_text())


def test_h_trace_recovers_constructed_patterns_from_ingested_data(ready, run_ingest, make_dataset):
    ds = make_dataset("recover", seed=21)
    assert run_ingest(ds)["status"] == "succeeded"
    engine = q(ready, max_results=50, max_hops=6, max_paths=500)
    checked = {}
    for p in _gt(ds)["patterns"]:
        if p["type"] not in ("circular", "pass_through"):
            continue
        txs = p["transaction_ids"]
        r = engine.trace_network(txs[0], "forward", max_hops=len(txs) - 1)
        longest = max(([s["transaction"]["transaction_id"] for s in path if "transaction" in s] for path in r.data["paths"]),
                      key=len, default=[])
        assert txs[:len(longest)] == longest[:len(txs)] or set(txs) <= set(t["transaction_id"] for t in r.data["transactions"])
        assert set(txs) <= {t["transaction_id"] for t in r.data["transactions"]}, p["type"]   # whole chain found by the graph
        checked[p["type"]] = len(txs)
        back = engine.trace_network(txs[-1], "backward", max_hops=len(txs) - 1)
        assert set(txs) <= {t["transaction_id"] for t in back.data["transactions"]}
    assert set(checked) == {"circular", "pass_through"}


def test_h_fan_in_and_fan_out_are_visible_through_account_activity(ready, run_ingest, make_dataset):
    ds = make_dataset("fan", seed=22)
    run_ingest(ds)
    engine = q(ready, max_results=500)
    for p in _gt(ds)["patterns"]:
        if p["type"] == "fan_in":
            r = engine.account_activity(p["roles"]["collector"], "in")
            got = {a["transaction"]["transaction_id"] for a in r.data["activity"]}
            assert set(p["transaction_ids"]) <= got
        if p["type"] == "fan_out":
            r = engine.account_activity(p["roles"]["distributor"], "out")
            got = {a["transaction"]["transaction_id"] for a in r.data["activity"]}
            assert set(p["transaction_ids"]) <= got


# ---- Test I: ground-truth separation
def test_i_ground_truth_is_not_in_the_graph_or_reachable_through_queries(ready, run_ingest, make_dataset):
    ds = make_dataset("sep", seed=33)
    assert run_ingest(ds)["status"] == "succeeded"
    gt = _gt(ds)
    allowed = {n.label: {p.name for p in n.properties} for n in gs_nodes()}
    g = ready.graph
    string_values = set()
    for label in ("Account", "Transaction", "Alert"):
        for (n,) in g.ro_query(f"MATCH (n:{label}) RETURN n").result_set:
            assert set(n.properties) <= allowed[label]                      # only schema properties exist
            string_values |= {v for v in n.properties.values() if isinstance(v, str)}
    for p in gt["patterns"]:
        assert p["pattern_id"] not in string_values
    forbidden = ("circular", "fan_in", "fan_out", "pass_through", "pattern", "ground_truth")
    assert not [v for v in string_values if any(w in v.lower() for w in forbidden)]
    assert {r[0] for r in g.ro_query("CALL db.relationshipTypes()").result_set} <= {"SENT", "RECEIVED_BY", "FLAGS"}
    # labels refer to real ingested entities, so evaluation can join them to the graph later
    store = GraphStore(ready)
    for p in gt["patterns"]:
        assert all(store.get_node("Transaction", t) for t in p["transaction_ids"])
        assert all(store.get_node("Account", a) for a in p["account_ids"])
    # nothing the query layer returns for pattern members carries labels
    engine = q(ready, max_results=100, max_hops=4, max_paths=100)
    blob = ""
    for p in gt["patterns"]:
        blob += json.dumps(engine.transaction_context(p["transaction_ids"][0]).as_dict())
        blob += json.dumps(engine.trace_network(p["transaction_ids"][0]).as_dict())
        blob += json.dumps(engine.account_activity(p["account_ids"][0]).as_dict())
    assert not any(w in blob.lower() for w in forbidden)
    assert not any(p["pattern_id"] in blob for p in gt["patterns"])


def gs_nodes():
    from money_trail.database import graph_schema
    return graph_schema.NODES
