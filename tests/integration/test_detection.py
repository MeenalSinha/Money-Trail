"""Phase 3 detectors and engine against a real FalkorDB (tests A-L). Each test has its own disposable graph."""
import copy
import json

import pytest

from money_trail.database import GraphStore
from money_trail.exceptions import ValidationError
from money_trail.investigation.config import parse_detection_config
from money_trail.investigation.detectors import base as b
from money_trail.investigation.engine import InvestigationEngine, InvestigationRequest
from money_trail.investigation.evidence import validate_detections
from money_trail.investigation.findings import get_investigation, list_findings
from support import detection_dict

NS = "det-ns"


def cfg(**mut):
    raw = detection_dict()
    for k, fn in mut.items():
        fn(raw)
    return parse_detection_config(raw)


def only(*names):
    """Config mutator: enable just the named detectors (isolates one detector's behaviour)."""
    def mut(raw):
        for k in raw["detectors"]:
            raw["detectors"][k]["enabled"] = k in names
    return mut


def engine(conn, config=None):
    return InvestigationEngine(conn, config or cfg())


def gt(ds):
    return json.loads((ds.dir / "ground_truth.json").read_text())["patterns"]


def ns_ids(ds, ids):
    return ids


@pytest.fixture
def loaded(ready, run_ingest, make_dataset):
    ds = make_dataset(NS, seed=3)
    assert run_ingest(ds)["status"] == "succeeded"
    return ds


def dataset_run(conn, config=None):
    return engine(conn, config).investigate(InvestigationRequest("dataset", namespace=NS))


def by_type(res, t):
    return [d for d in res.detections if d.pattern_type == t]


def find_exact(res, pattern):
    want = set(pattern["transaction_ids"])
    return [d for d in by_type(res, pattern["type"]) if set(d.transaction_ids) == want]


# ---- Tests A-D: the four detectors find the constructed patterns ----------------------------------
@pytest.mark.parametrize("ptype", ["circular", "pass_through", "fan_in", "fan_out"])
def test_a_to_d_dataset_scope_finds_every_constructed_pattern(ready, loaded, ptype):
    res = dataset_run(ready)
    assert res.status == "patterns_detected" and res.complete and not res.evidence_failures
    patterns = [p for p in gt(loaded) if p["type"] == ptype]
    assert patterns
    for p in patterns:
        hits = find_exact(res, p)
        assert len(hits) == 1, (ptype, p["pattern_id"])
        d = hits[0]
        assert list(d.account_ids) == sorted(p["account_ids"])
        assert d.rule_version.startswith(f"{ptype}:test-rules@1+") and d.dataset_namespace == NS
        if ptype in ("circular", "pass_through"):
            assert list(d.transaction_ids) == p["transaction_ids"]           # exact chronological order
            assert all(a.destination_account == c.source_account for a, c in zip(d.ordered_path, d.ordered_path[1:]))
        if ptype == "circular":
            assert d.ordered_path[-1].destination_account == d.ordered_path[0].source_account
            assert d.roles["start_account"] == d.ordered_path[0].source_account
        if ptype == "pass_through":
            assert d.roles["origin"] == p["roles"]["origin"] and d.roles["destination"] == p["roles"]["destination"]
            assert sorted(d.roles["intermediaries"]) == sorted(p["roles"]["mule_accounts"])
        if ptype == "fan_in":
            assert d.roles["collector"] == p["roles"]["collector"] and sorted(d.roles["sources"]) == sorted(p["roles"]["sources"])
            assert len({t.source_account for t in d.ordered_path}) == len(p["roles"]["sources"])
        if ptype == "fan_out":
            assert d.roles["distributor"] == p["roles"]["distributor"] and sorted(d.roles["recipients"]) == sorted(p["roles"]["recipients"])
        for t in d.ordered_path:
            assert t.amount > 0 and t.currency == "USD" and t.timestamp > 0


@pytest.mark.parametrize("ptype", ["circular", "pass_through", "fan_in", "fan_out"])
def test_every_transaction_and_account_anchor_reaches_its_pattern(ready, loaded, ptype):
    e = engine(ready)
    for p in [p for p in gt(loaded) if p["type"] == ptype]:
        for tx in p["transaction_ids"]:                       # first, middle and last transactions alike
            res = e.investigate(InvestigationRequest("transaction", tx))
            assert res.namespace == NS and find_exact(res, p), (ptype, tx)
        res = e.investigate(InvestigationRequest("account", p["account_ids"][0]))
        assert find_exact(res, p) or ptype in ("fan_in", "fan_out"), (ptype, "account anchor")


def test_account_anchor_finds_fan_patterns_at_the_collector_and_distributor(ready, loaded):
    e = engine(ready)
    for p in gt(loaded):
        if p["type"] == "fan_in":
            assert find_exact(e.investigate(InvestigationRequest("account", p["roles"]["collector"])), p)
        if p["type"] == "fan_out":
            assert find_exact(e.investigate(InvestigationRequest("account", p["roles"]["distributor"])), p)


def test_criteria_are_applied_not_just_structure(ready, loaded):
    pt = [p for p in gt(loaded) if p["type"] == "pass_through"][0]
    assert find_exact(dataset_run(ready), pt)
    strict_retention = cfg(x=lambda r: r["detectors"]["pass_through"].update(retention={"min_bps": 9800, "max_bps": 10000}))
    res = dataset_run(ready, strict_retention)                         # generated chain retains 9500 bps per hop
    assert not find_exact(res, pt)
    assert any("retention failed" in w for w in res.detector_reports["pass_through"]["warnings"])
    assert not find_exact(dataset_run(ready, cfg(x=lambda r: r["detectors"]["pass_through"].update(max_gap_ms=1))), pt)
    assert find_exact(dataset_run(ready, cfg(x=lambda r: r["detectors"]["pass_through"].update(retention=None))), pt)
    cyc = [p for p in gt(loaded) if p["type"] == "circular"][0]
    assert not find_exact(dataset_run(ready, cfg(x=lambda r: r["detectors"]["circular"].update(max_total_window_ms=1))), cyc)
    assert not find_exact(dataset_run(ready, cfg(x=lambda r: r["detectors"]["circular"].update(min_hops=5, max_hops=5))), cyc) \
        or len(cyc["transaction_ids"]) == 5
    fin = [p for p in gt(loaded) if p["type"] == "fan_in"][0]
    assert not find_exact(dataset_run(ready, cfg(x=lambda r: r["detectors"]["fan_in"].update(min_distinct_counterparties=50, min_transactions=50))), fin)
    assert not find_exact(dataset_run(ready, cfg(x=lambda r: r["detectors"]["fan_in"].update(window_ms=1))), fin)


def test_results_respect_configured_bounds(ready, loaded):
    c = cfg()
    res = dataset_run(ready, c)
    for d in res.detections:
        o = d.criteria["observed"]
        if d.pattern_type == "circular":
            s = c.settings["circular"]
            assert s.min_hops <= len(d.transaction_ids) <= s.max_hops and o["max_gap_ms"] <= s.max_gap_ms
            assert o["total_window_ms"] <= s.max_total_window_ms
        if d.pattern_type == "pass_through":
            s = c.settings["pass_through"]
            assert s.min_hops <= len(d.transaction_ids) <= s.max_hops and o["max_gap_ms"] <= s.max_gap_ms
            assert all(a.timestamp <= b2.timestamp for a, b2 in zip(d.ordered_path, d.ordered_path[1:]))
        if d.pattern_type in ("fan_in", "fan_out"):
            s = c.settings[d.pattern_type]
            assert o["distinct_counterparties"] >= s.min_distinct_counterparties and o["window_span_ms"] <= s.window_ms


# ---- Test E: negative examples in a controlled graph ----------------------------------------------------
CTL = "ctl"
H = 3_600_000


def build(conn, txs, ns=CTL):
    """txs: (id, src, dst, ts[, amount[, currency]]). Creates accounts, transactions and edges via the schema layer."""
    st = GraphStore(conn)
    for a in sorted({x for t in txs for x in t[1:3]}):
        st.upsert_node("Account", {"account_id": a, "dataset": ns})
    for t in txs:
        tid, s, d, ts = t[:4]
        amount = t[4] if len(t) > 4 else 100
        ccy = t[5] if len(t) > 5 else "USD"
        st.upsert_node("Transaction", {"transaction_id": tid, "amount": amount, "currency": ccy, "timestamp": ts, "dataset": ns})
        st.link("SENT", s, tid)
        st.link("RECEIVED_BY", tid, d)


def run_ctl(conn, config=None):
    return engine(conn, config).investigate(InvestigationRequest("dataset", namespace=CTL))


def ids_of(res, t=None):
    return sorted(tx for d in res.detections if t in (None, d.pattern_type) for tx in d.transaction_ids)


def test_e_circular_near_misses_are_not_reported(ready):
    build(ready, [
        ("g1", "A", "B", 0), ("g2", "B", "C", H * 2), ("g3", "C", "A", H * 2 + 10),      # gap > max_gap_ms (1h)
        ("o1", "X", "Y", 1000), ("o2", "Y", "Z", 500), ("o3", "Z", "X", 2000),           # not chronological
        ("c1", "P", "Q", 0, 100, "USD"), ("c2", "Q", "R", 10, 99, "EUR"), ("c3", "R", "P", 20, 98, "EUR"),   # currency mismatch
        ("s1", "M", "N", 0), ("s2", "N", "M", 10),                                       # 2-cycle, below min_hops 3
        ("l1", "K", "L", 0, 100), ("l2", "L", "J", 10, 50), ("l3", "J", "K", 20, 49),    # retention 50%
        ("r1", "U", "V", 0), ("r2", "V", "W", 10), ("r3", "W", "U", 20),                 # a real cycle: positive control
    ])
    res = run_ctl(ready)
    assert ids_of(res, "circular") == ["r1", "r2", "r3"]
    # the same near-miss structures become cycles once the specific rule is relaxed (the rule, not the shape, excluded them)
    relaxed = run_ctl(ready, cfg(x=lambda r: r["detectors"]["circular"].update(max_gap_ms=3 * H, amount_retention=None)))
    got = ids_of(relaxed, "circular")
    assert {"g1", "g2", "g3", "c1", "c2", "c3", "l1", "l2", "l3", "r1"} <= set(got)
    assert not {"o1", "o2", "o3", "s1", "s2"} & set(got)                                   # wrong order / too short stay excluded


def test_e_cycle_is_not_reported_in_reverse(ready):
    build(ready, [("f1", "A", "B", 0), ("f2", "B", "C", 10), ("f3", "C", "A", 20)])
    res = run_ctl(ready)
    (d,) = by_type(res, "circular")
    assert [(t.source_account, t.destination_account) for t in d.ordered_path] == [("A", "B"), ("B", "C"), ("C", "A")]


def test_e_fan_in_and_fan_out_near_misses(ready):
    build(ready, [
        ("a1", "S", "D1", 0), ("a2", "S", "D1", 10), ("a3", "S", "D1", 20), ("a4", "S", "D1", 30),          # one repeated source
        ("b1", "S1", "D2", 0), ("b2", "S2", "D2", 4 * H), ("b3", "S3", "D2", 8 * H),                        # spread beyond window
        ("c1", "T1", "D3", 0), ("c2", "T2", "D3", 10), ("c3", "T3", "D3", 20),                              # positive control
        ("e1", "E1", "D4", 0), ("e2", "E2", "D4", H), ("e3", "E3", "D4", 2 * H),                            # exactly window (2h): in
        ("f1", "F1", "D5", 0), ("f2", "F2", "D5", H), ("f3", "F3", "D5", 2 * H + 1),                        # 1 ms over: out
        ("g1", "G", "H1", 0), ("g2", "G", "H1", 10), ("g3", "G", "H2", 20), ("g4", "G", "H2", 30),          # only two recipients
    ])
    res = run_ctl(ready)
    fi = by_type(res, "fan_in")
    assert sorted(tuple(sorted(d.transaction_ids)) for d in fi) == [("c1", "c2", "c3"), ("e1", "e2", "e3")]
    assert not by_type(res, "fan_out")


def test_e_fan_in_counts_only_the_configured_currency_for_amount_rules(ready):
    build(ready, [("u1", "A", "D", 0, 100), ("u2", "B", "D", 10, 100), ("u3", "C", "D", 20, 100, "EUR"),
                  ("v1", "A2", "D2", 0, 100), ("v2", "B2", "D2", 10, 100), ("v3", "C2", "D2", 20, 100)])
    rule = lambda r: r["detectors"]["fan_in"].update(min_total_amount={"currency": "USD", "minor": 300})
    res = run_ctl(ready, cfg(x=rule))
    assert [tuple(sorted(d.transaction_ids)) for d in by_type(res, "fan_in")] == [("v1", "v2", "v3")]   # EUR never converted
    assert any("other currencies" in w for w in res.detector_reports["fan_in"]["warnings"])


def test_e_pass_through_requires_retention_and_compatible_currency(ready):
    build(ready, [("p1", "A", "B", 0, 100), ("p2", "B", "C", 10, 95),                       # positive control (A>B>C)
                  ("q1", "D", "E", 0, 100), ("q2", "E", "F", 10, 50),                       # keeps only 50%
                  ("r1", "G", "H", 0, 100, "USD"), ("r2", "H", "I", 10, 95, "EUR"),        # different currency
                  ("s1", "J", "K", 0, 100), ("s2", "K", "L", 4 * H, 95),                    # gap too large
                  ("z1", "M", "N", 0, 0), ("z2", "N", "O", 10, 0)])                         # zero amounts: ratio undefined
    res = run_ctl(ready)
    assert ids_of(res, "pass_through") == ["p1", "p2"]


# ---- Test F: duplicates -------------------------------------------------------------------------------------
def test_f_one_cycle_is_one_finding_whatever_the_starting_point(ready):
    build(ready, [("t1", "A", "B", 0), ("t2", "B", "C", 10), ("t3", "C", "A", 20)])
    e = engine(ready)
    seen = set()
    for req in [InvestigationRequest("transaction", t) for t in ("t1", "t2", "t3")] + \
               [InvestigationRequest("account", a) for a in "ABC"] + [InvestigationRequest("dataset", namespace=CTL)]:
        res = e.investigate(req)
        cyc = by_type(res, "circular")
        assert len(cyc) == 1, req
        seen.add(cyc[0].detection_id)
    assert len(seen) == 1
    # a tie (identical timestamps) must not create rotated duplicates either
    build(ready, [("w1", "P", "Q", 5), ("w2", "Q", "R", 5), ("w3", "R", "P", 5)])
    res = e.investigate(InvestigationRequest("dataset", namespace=CTL))
    tied = [d for d in by_type(res, "circular") if "w1" in d.transaction_ids]
    assert len(tied) == 1 and list(tied[0].transaction_ids) == ["w1", "w2", "w3"]


def test_f_overlapping_pass_through_chains_report_only_the_maximal_chain(ready):
    build(ready, [("a", "A", "B", 0), ("b", "B", "C", 10), ("c", "C", "D", 20), ("d", "D", "E", 30)])
    res = run_ctl(ready)
    assert [list(d.transaction_ids) for d in by_type(res, "pass_through")] == [["a", "b", "c", "d"]]


# ---- Test G: evidence completeness -----------------------------------------------------------------------------
def test_g_every_detection_is_backed_by_real_transactions(ready, loaded):
    from money_trail.investigation import InvestigationQueries, QueryLimits
    res = dataset_run(ready)
    assert res.detections and not res.evidence_failures
    q = InvestigationQueries(ready, QueryLimits(max_results=10))
    for d in res.detections:
        for t in d.ordered_path:
            ctx = q.transaction_context(t.transaction_id)
            assert ctx.status == "ok" and ctx.warnings == []
            assert ctx.data["transaction"]["amount"] == t.amount and ctx.data["transaction"]["timestamp"] == t.timestamp
            assert [a["account_id"] for a in ctx.data["source_accounts"]] == [t.source_account]
            assert [a["account_id"] for a in ctx.data["destination_accounts"]] == [t.destination_account]
        assert set(d.transaction_ids) == {t.transaction_id for t in d.ordered_path}
        assert d.evidence_refs and d.evidence_refs[0].startswith("evd:")


def test_g_tampered_evidence_is_caught_by_validation(ready):
    build(ready, [("t1", "A", "B", 0), ("t2", "B", "C", 10), ("t3", "C", "A", 20)])
    res = run_ctl(ready)
    ds = list(res.detections)
    store = GraphStore(ready)
    ok, bad = validate_detections(store, ds)
    assert len(ok) == len(ds) and not bad
    ready.graph.query("MATCH (t:Transaction {transaction_id: 't2'}) SET t.amount = 5")
    ok, bad = validate_detections(store, ds)
    assert not ok and "differs" in bad[0]["reason"]
    ready.graph.query("MATCH (t:Transaction {transaction_id: 't2'}) SET t.amount = 100")
    ready.graph.query("MATCH (:Account {account_id: 'B'})-[r:SENT]->(:Transaction {transaction_id: 't2'}) DELETE r")
    ok, bad = validate_detections(store, ds)
    assert not ok and "endpoints" in bad[0]["reason"]
    GraphStore(ready).link("SENT", "B", "t2")                                   # repair, then test a vanished transaction
    ok, bad = validate_detections(store, ds)
    assert len(ok) == len(ds) and not bad
    ready.graph.query("MATCH (t:Transaction {transaction_id: 't3'}) DETACH DELETE t")
    ok, bad = validate_detections(store, ds)
    assert "no longer exists" in bad[0]["reason"]


def test_g_incomplete_graph_records_are_skipped_not_invented(ready):
    build(ready, [("t1", "A", "B", 0), ("t2", "B", "C", 10), ("t3", "C", "A", 20)])
    ready.graph.query("MATCH (:Account {account_id: 'C'})-[r:SENT]->(:Transaction {transaction_id: 't3'}) DELETE r")
    res = run_ctl(ready)
    assert not by_type(res, "circular")                      # no source for t3, so no closed path; nothing fabricated
    build(ready, [("m1", "X", "Y", 0), ("m2", "Y", "Z", 10)])
    ready.graph.query("MATCH (:Transaction {transaction_id: 'm2'})-[r:RECEIVED_BY]->(:Account {account_id: 'Z'}) DELETE r")
    res = run_ctl(ready)
    assert not [d for d in by_type(res, "pass_through") if "m2" in d.transaction_ids]
    assert any("incomplete data" in w for w in res.detector_reports["pass_through"]["warnings"])


# ---- Tests H and I: persistence and idempotency ----------------------------------------------------------------------
def counts(conn):
    g = conn.graph
    return {lab: g.ro_query(f"MATCH (n:{lab}) RETURN count(n)").result_set[0][0]
            for lab in ("Investigation", "Finding", "Evidence", "Transaction")} | {
        rel: g.ro_query(f"MATCH ()-[r:{rel}]->() RETURN count(r)").result_set[0][0]
        for rel in ("STARTED_FROM", "HAS_FINDING", "SUPPORTED_BY", "REFERENCES")}


def test_h_findings_and_evidence_persist_and_trace_back_to_transactions(ready, loaded):
    cyc = [p for p in gt(loaded) if p["type"] == "circular"][0]
    anchor = cyc["transaction_ids"][1]
    e = engine(ready)
    res = e.investigate(InvestigationRequest("transaction", anchor), persist=True)
    assert res.status == "patterns_detected" and res.persistence
    pr = res.persistence
    inv = pr["investigation_id"]
    assert inv.startswith("inv:") and pr["investigation"] == {"created": 1, "existing": 0}
    n_det = len(res.detections)
    assert pr["evidence"]["created"] == n_det and pr["findings"]["created"] == 2 * n_det        # observation + hypothesis each
    # read back through the persisted graph only
    full = get_investigation(ready, inv, limit=500)
    assert full["investigation"]["status"] == "open" and full["started_from"] == [anchor]
    kinds = sorted(f["kind"] for f in full["findings"])
    assert kinds == ["hypothesis"] * n_det + ["observation"] * n_det
    persisted_tx = {t for f in full["findings"] for ev in f["evidence"] for t in ev["transaction_ids"]}
    assert persisted_tx == {t for d in res.detections for t in d.transaction_ids}
    assert set(cyc["transaction_ids"]) <= persisted_tx
    for f in full["findings"]:
        assert f["evidence"] and "does not establish wrongdoing" in f["statement"] or f["kind"] == "hypothesis"
        for ev in f["evidence"]:
            assert ev["produced_by_tool"].startswith("money_trail.detector.") and ev["query_ref"].split(":")[0] in (
                "circular", "pass_through", "fan_in", "fan_out")
    for tx, info in full["transactions"].items():
        assert info["source_accounts"] and info["destination_accounts"] and info["amount"] > 0
    # the complete path, straight from Cypher over the Phase 1 relationships
    rows = ready.graph.ro_query(
        "MATCH (i:Investigation {investigation_id: $i})-[:STARTED_FROM]->(a:Transaction), "
        "(i)-[:HAS_FINDING]->(f:Finding)-[:SUPPORTED_BY]->(e:Evidence)-[:REFERENCES]->(t:Transaction) "
        "RETURN DISTINCT t.transaction_id", {"i": inv}).result_set
    assert {r[0] for r in rows} == persisted_tx
    # existing Phase 2 query layer can read it too
    from money_trail.investigation import InvestigationQueries, QueryLimits
    paths = InvestigationQueries(ready, QueryLimits(max_results=500)).evidence_paths(inv)
    assert paths.status == "ok" and {p["transaction"]["transaction_id"] for p in paths.data["paths"]} == persisted_tx
    # persistence survives a brand-new connection
    from money_trail.database import FalkorDBConnection
    with FalkorDBConnection(ready.settings) as other:
        again = list_findings(other, investigation_id=inv, limit=500)
        assert len(again["findings"]) == 2 * n_det and not again["truncated"]
        assert list_findings(other, transaction_id=anchor, limit=500)["findings"]
    # no hidden labels or extra node kinds were introduced
    assert {r[0] for r in ready.graph.ro_query("MATCH (n) RETURN DISTINCT labels(n)[0]").result_set} <= {
        "Account", "Transaction", "Alert", "Investigation", "Finding", "Evidence", "__schema_bootstrap__"}


def test_h_hypothesis_findings_follow_configuration(ready):
    build(ready, [("t1", "A", "B", 0), ("t2", "B", "C", 10), ("t3", "C", "A", 20)])
    c = cfg(x=lambda r: r["persistence"].update(hypothesis_findings=False))
    res = engine(ready, c).investigate(InvestigationRequest("transaction", "t1"), persist=True)
    assert counts(ready)["Finding"] == len(res.detections) and res.persistence["findings"]["created"] == len(res.detections)
    assert {r[0] for r in ready.graph.ro_query("MATCH (f:Finding) RETURN f.kind").result_set} == {"observation"}


def test_i_rerunning_an_investigation_creates_nothing_new(ready, loaded):
    anchor = [p for p in gt(loaded) if p["type"] == "fan_in"][0]["transaction_ids"][0]
    e = engine(ready)
    first = e.investigate(InvestigationRequest("transaction", anchor), persist=True)
    before = counts(ready)
    snap = {r[0]: r[1] for r in ready.graph.ro_query("MATCH (f:Finding) RETURN f.finding_id, f.created_at").result_set}
    second = e.investigate(InvestigationRequest("transaction", anchor), persist=True)
    assert counts(ready) == before
    assert second.persistence["investigation_id"] == first.persistence["investigation_id"]
    p = second.persistence
    assert p["investigation"] == {"created": 0, "existing": 1} and p["findings"]["created"] == 0 and p["evidence"]["created"] == 0
    assert all(v["created"] == 0 for v in p["relationships"].values())
    assert snap == {r[0]: r[1] for r in ready.graph.ro_query("MATCH (f:Finding) RETURN f.finding_id, f.created_at").result_set}


def test_i_two_anchors_in_one_pattern_share_findings_but_not_investigations(ready):
    build(ready, [("t1", "A", "B", 0), ("t2", "B", "C", 10), ("t3", "C", "A", 20)])
    e = engine(ready, cfg(x=only("circular")))      # other detectors legitimately see different chains per anchor
    e.investigate(InvestigationRequest("transaction", "t1"), persist=True)
    mid = counts(ready)
    e.investigate(InvestigationRequest("transaction", "t3"), persist=True)
    after = counts(ready)
    assert after["Investigation"] == mid["Investigation"] + 1
    assert after["Finding"] == mid["Finding"] and after["Evidence"] == mid["Evidence"]
    assert after["HAS_FINDING"] == mid["HAS_FINDING"] + mid["Finding"]            # new investigation links the shared findings


def test_i_only_transaction_anchors_can_be_persisted(ready):
    build(ready, [("t1", "A", "B", 0)])
    for req in (InvestigationRequest("account", "A"), InvestigationRequest("dataset", namespace=CTL)):
        with pytest.raises(ValidationError, match="Only transaction-anchored"):
            engine(ready).investigate(req, persist=True)
    assert counts(ready)["Investigation"] == 0


# ---- Test J: configuration changes ------------------------------------------------------------------------------------
def test_j_changed_rules_change_behaviour_and_create_distinguishable_findings(ready):
    build(ready, [("t1", "A", "B", 0), ("t2", "B", "C", 10), ("t3", "C", "A", 20)])
    base = cfg(x=only("circular"))
    changed = cfg(x=lambda r: (only("circular")(r), r["detectors"]["circular"].update(max_gap_ms=H + 1)))
    r1 = engine(ready, base).investigate(InvestigationRequest("transaction", "t1"), persist=True)
    mid = counts(ready)
    r2 = engine(ready, changed).investigate(InvestigationRequest("transaction", "t1"), persist=True)
    d1, d2 = by_type(r1, "circular")[0], by_type(r2, "circular")[0]
    assert d1.transaction_ids == d2.transaction_ids and d1.rule_version != d2.rule_version
    assert d1.detection_id != d2.detection_id
    assert r1.persistence["investigation_id"] != r2.persistence["investigation_id"]
    after = counts(ready)
    assert after["Investigation"] == mid["Investigation"] + 1 and after["Finding"] > mid["Finding"]     # old findings are kept
    assert {f["finding_id"] for f in list_findings(ready, investigation_id=r1.persistence["investigation_id"], limit=100)["findings"]} \
        .isdisjoint({f["finding_id"] for f in list_findings(ready, investigation_id=r2.persistence["investigation_id"], limit=100)["findings"]})
    # a threshold change that the data cannot satisfy changes the outcome
    strict = cfg(x=lambda r: r["detectors"]["circular"].update(max_gap_ms=1))
    assert not by_type(engine(ready, strict).investigate(InvestigationRequest("transaction", "t1")), "circular")


# ---- Test K: bounds, truncation, outcomes -------------------------------------------------------------------------------
def test_k_search_limits_are_reported_as_incomplete(ready, loaded):
    capped = cfg(x=lambda r: r["limits"].update(max_candidate_paths=1))
    res = dataset_run(ready, capped)
    assert not res.complete and any(r["truncated"] for r in res.detector_reports.values())
    assert any("incomplete" in w for r in res.detector_reports.values() for w in r["warnings"])
    # nothing found + truncated search => 'incomplete', never 'no_pattern_found'
    nothing = cfg(x=lambda r: (r["limits"].update(max_candidate_paths=1),
                               r["detectors"]["pass_through"].update(retention={"min_bps": 10000, "max_bps": 10000}),
                               [r["detectors"][k].update(enabled=False) for k in ("circular", "fan_in", "fan_out")]))
    res = dataset_run(ready, nothing)
    assert res.status == "incomplete" and not res.complete and not res.detections


def test_k_complete_empty_search_is_a_definite_negative(ready):
    build(ready, [("t1", "A", "B", 0), ("t2", "C", "D", 5)])
    res = run_ctl(ready)
    assert res.status == "no_pattern_found" and res.complete and not res.detections


def test_k_detection_cap_flags_truncation(ready, loaded):
    res = dataset_run(ready, cfg(x=lambda r: r["limits"].update(max_detections_per_detector=1)))
    assert all(len(by_type(res, t)) <= 1 for t in ("circular", "pass_through", "fan_in", "fan_out"))
    assert not res.complete and any("max_detections_per_detector" in w for r in res.detector_reports.values() for w in r["warnings"])


def test_k_path_depth_is_enforced(ready):
    build(ready, [("t1", "A", "B", 0), ("t2", "B", "C", 10), ("t3", "C", "A", 20)])
    shallow = cfg(x=lambda r: r["detectors"]["circular"].update(min_hops=2, max_hops=2))
    assert not by_type(run_ctl(ready, shallow), "circular")
    exact = cfg(x=lambda r: r["detectors"]["circular"].update(min_hops=3, max_hops=3))
    assert len(by_type(run_ctl(ready, exact), "circular")) == 1


def test_k_account_anchor_cap_is_reported(ready):
    build(ready, [("t1", "A", "B", 0), ("t2", "A", "C", 1), ("t3", "D", "A", 2)])
    res = engine(ready, cfg(x=lambda r: r["limits"].update(max_anchor_transactions=1))).investigate(InvestigationRequest("account", "A"))
    assert not res.complete


def test_k_missing_anchors_and_empty_namespaces(ready):
    e = engine(ready)
    for req in (InvestigationRequest("transaction", "nope"), InvestigationRequest("account", "nope"),
                InvestigationRequest("dataset", namespace="not-a-namespace")):
        res = e.investigate(req)
        assert res.status == "anchor_not_found" and not res.complete and not res.detections


def test_k_database_errors_are_reported_not_hidden(ready, monkeypatch):
    build(ready, [("t1", "A", "B", 0)])
    from redis import exceptions as redis_exc
    def boom(self, *a, **k):
        raise redis_exc.ConnectionError("down")
    monkeypatch.setattr(b.GraphReader, "run", boom)
    res = engine(ready).investigate(InvestigationRequest("transaction", "t1"))
    assert res.status == "database_error" and not res.complete and "ConnectionError" in res.error


def test_k_real_server_side_timeout_is_enforced(ready, loaded):
    reader = b.GraphReader(ready, timeout_ms=1)
    with pytest.raises(b.SearchTimeout):
        reader.run("MATCH (a:Account), (t:Transaction), (u:Transaction), (v:Transaction) RETURN count(*)", {})


def test_k_timeout_marks_the_search_incomplete(ready, loaded, monkeypatch):
    def slow(self, query, params):
        raise b.SearchTimeout("query exceeded query_timeout_ms=1")
    monkeypatch.setattr(b.GraphReader, "run", slow)
    res = dataset_run(ready)
    assert res.status == "incomplete" and not res.complete and not res.detections


# ---- Test L: ground-truth separation and identifier independence ------------------------------------------------------------
def test_l_detection_is_independent_of_namespace_and_generated_identifiers(ready, run_ingest, make_dataset):
    a, b2 = make_dataset("alpha", seed=41), make_dataset("beta", seed=41)         # identical data, different namespaces
    run_ingest(a), run_ingest(b2)
    e = engine(ready)
    ra = e.investigate(InvestigationRequest("dataset", namespace="alpha"))
    rb = e.investigate(InvestigationRequest("dataset", namespace="beta"))
    strip = lambda res, ns: sorted((d.pattern_type, tuple(t.replace(f"{ns}:", "", 1) for t in d.transaction_ids)) for d in res.detections)
    assert strip(ra, "alpha") == strip(rb, "beta") and ra.detections
    assert {d.detection_id for d in ra.detections}.isdisjoint({d.detection_id for d in rb.detections})   # ids are namespaced


def test_l_new_seeds_are_detected_without_any_coupling_to_earlier_data(ready, run_ingest, make_dataset):
    e = engine(ready)
    for seed in (101, 202):
        ds = make_dataset(f"seed{seed}", seed=seed)
        run_ingest(ds)
        res = e.investigate(InvestigationRequest("dataset", namespace=f"seed{seed}"))
        for p in gt(ds):
            assert find_exact(res, p), (seed, p["type"])


def test_l_no_label_reaches_the_graph_or_the_investigation_output(ready, run_ingest, make_dataset):
    ds = make_dataset("sep2", seed=55)
    run_ingest(ds)
    pats = gt(ds)
    anchor = [p for p in pats if p["type"] == "circular"][0]["transaction_ids"][0]
    e = engine(ready)
    res = e.investigate(InvestigationRequest("transaction", anchor), persist=True)
    full = e.investigate(InvestigationRequest("dataset", namespace="sep2"))
    blob = json.dumps(res.as_dict()) + json.dumps(full.as_dict())
    pattern_ids = [p["pattern_id"] for p in pats]
    assert not any(pid in blob for pid in pattern_ids)
    assert '"roles"' in blob and "ground" not in blob.lower()
    from money_trail.database import graph_schema as gs
    allowed = {n.label: {p.name for p in n.properties} for n in gs.NODES}
    strings = set()
    for label in allowed:
        for (n,) in ready.graph.ro_query(f"MATCH (n:{label}) RETURN n").result_set:
            assert set(n.properties) <= allowed[label]                     # no extra properties anywhere
            strings |= {v for v in n.properties.values() if isinstance(v, str)}
    assert not [s for s in strings if any(pid in s for pid in pattern_ids)]
    for label in ("Account", "Transaction", "Alert"):
        keys = set()
        for (n,) in ready.graph.ro_query(f"MATCH (n:{label}) RETURN n").result_set:
            keys |= set(n.properties)
        assert not keys & {"pattern_type", "pattern_id", "pattern", "ground_truth", "label", "role"}


def test_k_anchored_discovery_starts_from_an_index_lookup_not_a_label_scan(ready, loaded):
    """Regression guard for the measured planner behaviour: anchoring through a parameter makes the planner start
    from the indexed anchor; anchoring through UNWIND made it scan every transaction."""
    plan = " | ".join(x.strip() for x in str(ready.graph.explain(
        b.backward_start_query(2), {"aid": "x", "gap": 1, "window": 1, "cap": 1})).splitlines())
    assert "Node By Index Scan | (c0:Transaction)" in plan and "Node By Label Scan" not in plan
    fwd = " | ".join(x.strip() for x in str(ready.graph.explain(
        b.forward_query(3, anchored=True, closing=True, distinct_accounts=True),
        {"starts": ["x"], "gap": 1, "window": 1, "cap": 1})).splitlines())
    assert "Node By Index Scan" in fwd
