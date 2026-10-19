"""Registered tools against a REAL FalkorDB (spec tests A-F, H). Expected values are read with independent raw Cypher."""
import json
from collections import Counter

import pytest
from redis import exceptions as redis_exc

from money_trail.exceptions import QueryTimeoutError, ValidationError
from money_trail.investigation.engine import InvestigationEngine, InvestigationRequest
from money_trail.investigation.queries import InvestigationQueries, QueryLimits
from support import detection_dict

NS = "agt-ns"


@pytest.fixture
def loaded(ready, run_ingest, make_dataset):
    ds = make_dataset(NS, seed=3)
    assert run_ingest(ds)["status"] == "succeeded"
    return ds


def patterns(ds, ptype):
    return [p for p in json.loads((ds.dir / "ground_truth.json").read_text())["patterns"] if p["type"] == ptype]


def raw_tx(conn, tid):
    r = conn.graph.ro_query("MATCH (a:Account)-[:SENT]->(t:Transaction {transaction_id:$i})-[:RECEIVED_BY]->(b:Account) "
                            "RETURN a.account_id, b.account_id, t.amount, t.currency, t.timestamp", {"i": tid}).result_set
    return r[0] if r else None


def data(e):
    assert e.executed, e.message
    return e.result.data


# ---- A: transaction context ------------------------------------------------------------------------
def test_a_transaction_context_matches_independent_graph_read(loaded, ready, tool_env):
    env = tool_env()
    for p in patterns(loaded, "circular")[:2]:
        tid = p["transaction_ids"][0]
        e = env.call("get_transaction_context", transaction_id=tid)
        d = data(e)
        src, dst, amt, cur, ts = raw_tx(ready, tid)
        assert e.result.status == "ok"
        assert d["transaction"]["amount"] == amt and d["transaction"]["currency"] == cur and d["transaction"]["timestamp"] == ts
        assert d["source_accounts"] == [src] and d["destination_accounts"] == [dst]
        assert env.ledger.transactions[tid] == {"transaction_id": tid, "source_account": src, "destination_account": dst,
                                                "amount": amt, "currency": cur, "timestamp": ts}
    alerted = ready.graph.ro_query("MATCH (t:Transaction)<-[]-(a:Alert) RETURN t.transaction_id, count(a) ORDER BY t.transaction_id LIMIT 1").result_set
    assert alerted, "the generated dataset is expected to contain alerts"
    tid2, expected = alerted[0]
    assert len(data(env.call("get_transaction_context", transaction_id=tid2))["alerts"]) == expected >= 1


# ---- B: tracing -------------------------------------------------------------------------------------
def test_b_trace_returns_real_time_respecting_paths_within_bounds(loaded, ready, tool_env):
    env = tool_env({"tool_limits": {"max_hops": 3, "max_paths": 10}})
    tid = patterns(loaded, "circular")[0]["transaction_ids"][0]
    e = env.call("trace_transaction_network", anchor_type="transaction", anchor_id=tid, direction="forward")
    d = data(e)
    assert d["path_count"] >= 1 and d["max_hops"] == 3 and d["path_count"] <= 10
    for path in d["paths"]:
        assert path[0] == tid and 2 <= len(path) <= 4          # start transaction + at most max_hops further
        recs = [raw_tx(ready, t) for t in path]
        assert all(recs)
        for (s1, d1, *_r1, ts1), (s2, d2, *_r2, ts2) in zip(recs, recs[1:]):
            assert d1 == s2 and ts2 >= ts1                      # contiguous in the graph and ordered in time
    assert set(d["evidence_references"]["transaction_ids"]) == {t for p in d["paths"] for t in p}


def test_b_trace_from_an_account_and_both_directions(loaded, ready, tool_env):
    env = tool_env()
    first_tx = patterns(loaded, "circular")[0]["transaction_ids"][0]
    acct = raw_tx(ready, first_tx)[0]          # first sender: has a forward AND a backward time-respecting path in a cycle
    f = data(env.call("trace_transaction_network", anchor_type="account", anchor_id=acct, direction="forward", max_hops=3))
    b = data(env.call("trace_transaction_network", anchor_type="account", anchor_id=acct, direction="backward", max_hops=3))
    assert f["path_count"] >= 1 and b["path_count"] >= 1
    for p in f["paths"]:
        assert raw_tx(ready, p[0])[0] == acct                  # forward paths start with a transaction SENT by the account
    for p in b["paths"]:
        assert raw_tx(ready, p[0])[1] == acct                  # backward paths start with a transaction received by it


def test_b_trace_bounds_are_enforced_and_truncation_is_flagged(loaded, tool_env):
    env = tool_env({"tool_limits": {"max_hops": 4, "max_paths": 100}})
    tid = patterns(loaded, "circular")[0]["transaction_ids"][0]
    e = env.call("trace_transaction_network", anchor_type="transaction", anchor_id=tid, max_hops=4, max_paths=1)
    assert e.result.status == "incomplete" and e.result.truncated and data(e)["path_count"] == 1
    assert any("truncated" in w for w in e.result.warnings)
    for bad in ({"max_hops": 5}, {"max_paths": 101}, {"max_hops": 0}):
        r = env.call("trace_transaction_network", anchor_type="transaction", anchor_id=tid, **bad)
        assert not r.executed and r.result.status == "invalid_arguments"
    r = env.call("trace_transaction_network", anchor_type="account", anchor_id="x:y", max_hops=1)
    assert r.result.status in ("invalid_arguments", "not_found", "error")        # account trace needs at least 2 hops


def test_account_trace_query_rejects_a_single_hop(ready, loaded):
    q = InvestigationQueries(ready, QueryLimits(max_hops=4, max_paths=10))
    from money_trail.exceptions import QueryError
    with pytest.raises(QueryError, match="at least 2"):
        q.trace_network_from_account("x", max_hops=1)


def test_query_timeout_is_reported_as_incomplete_not_as_empty(loaded, ready, tool_env, spy_graph):
    log, add_hook = spy_graph
    env = tool_env()
    tid = patterns(loaded, "circular")[0]["transaction_ids"][0]
    ok = env.call("trace_transaction_network", anchor_type="transaction", anchor_id=tid)
    assert ok.result.status == "ok" and any("RECEIVED_BY|SENT" in q for _, q in log)       # positive control: the spy sees real queries

    def timeout(q):
        if "RECEIVED_BY|SENT" in q:
            raise redis_exc.ResponseError("Query timed out")
    add_hook(timeout)
    env2 = tool_env()
    e = env2.call("trace_transaction_network", anchor_type="transaction", anchor_id=tid)
    assert e.result.status == "incomplete" and e.result.truncated and "does not mean nothing exists" in e.result.warnings[0]
    assert env2.ledger.counts()["transactions"] == 0                 # nothing was delivered, so nothing is citable


# ---- C: detector tools ------------------------------------------------------------------------------
@pytest.mark.parametrize("ptype", ["circular", "pass_through", "fan_in", "fan_out"])
def test_c_detector_tool_matches_phase3_engine_and_finds_the_generated_pattern(loaded, ready, tool_env, det_config, ptype):
    env = tool_env()
    gt = patterns(loaded, ptype)[0]
    anchor = gt["transaction_ids"][0]
    e = env.call("run_pattern_detector", detector=ptype, anchor_type="transaction", anchor_id=anchor)
    got = {d["detection_id"]: d for d in data(e)["detections"]}
    full = InvestigationEngine(ready, det_config).investigate(InvestigationRequest("transaction", anchor))
    want = {d.detection_id for d in full.detections if d.pattern_type == ptype}
    assert set(got) == want and want                                  # same ids as the full Phase 3 run, restricted to this detector
    assert all(d["pattern_type"] == ptype for d in got.values())
    assert any(set(gt["transaction_ids"]) <= set(d["transaction_ids"]) for d in got.values())
    for d in got.values():                                            # every returned amount/timestamp equals the graph
        for t in d["transactions"]:
            src, dst, amt, cur, ts = raw_tx(ready, t["transaction_id"])
            assert (t["source_account"], t["destination_account"], t["amount"], t["currency"], t["timestamp"]) == (src, dst, amt, cur, ts)


def test_c_run_investigation_equals_union_of_detectors(loaded, ready, tool_env, det_config):
    env = tool_env()
    anchor = patterns(loaded, "circular")[0]["transaction_ids"][0]
    broad = {d["detection_id"] for d in data(env.call("run_investigation", anchor_type="transaction", anchor_id=anchor))["detections"]}
    narrow = set()
    for name in det_config.enabled_names():
        narrow |= {d["detection_id"] for d in data(env.call("run_pattern_detector", detector=name, anchor_type="transaction", anchor_id=anchor))["detections"]}
    assert broad == narrow and broad


def test_c_detector_name_cannot_be_arbitrary_or_disabled(loaded, tool_env, det_config):
    env = tool_env()
    for name in ("MATCH (n) RETURN n", "os.system", "circular; DROP", ""):
        e = env.call("run_pattern_detector", detector=name, anchor_type="transaction", anchor_id="x:y")
        assert not e.executed and "must be one of" in e.message
    raw = detection_dict()
    raw["detectors"]["fan_in"]["enabled"] = False
    from money_trail.investigation.config import parse_detection_config
    with pytest.raises(ValidationError, match="unsupported or disabled"):
        InvestigationEngine(env.ctx.conn, parse_detection_config(raw)).investigate(InvestigationRequest("transaction", "x:y"), detectors=["fan_in"])


def test_engine_subset_cannot_be_persisted(loaded, ready, det_config):
    anchor = patterns(loaded, "circular")[0]["transaction_ids"][0]
    with pytest.raises(ValidationError, match="every enabled detector"):
        InvestigationEngine(ready, det_config).investigate(InvestigationRequest("transaction", anchor), persist=True, detectors=["circular"])
    with pytest.raises(ValidationError, match="at least one"):
        InvestigationEngine(ready, det_config).investigate(InvestigationRequest("transaction", anchor), detectors=[])


# ---- D: stored findings and evidence ---------------------------------------------------------------
def test_d_finding_and_evidence_tools_read_persisted_records(loaded, ready, tool_env, det_config):
    anchor = patterns(loaded, "circular")[0]["transaction_ids"][0]
    res = InvestigationEngine(ready, det_config).investigate(InvestigationRequest("transaction", anchor), persist=True)
    assert res.persistence
    env = tool_env()
    ctx = data(env.call("get_transaction_context", transaction_id=anchor))
    stored = ctx["stored_findings"]
    assert stored and all(f["finding_id"].startswith("fnd:") for f in stored)
    circ = next(d for d in res.detections if d.pattern_type == "circular")
    fid = stored[0]["finding_id"]
    fd = data(env.call("get_finding_details", finding_id=fid))
    assert fd["references_valid"] is True and fd["finding"]["finding_id"] == fid and fd["investigation_ids"]
    ev = fd["evidence"][0]
    eid = ev["evidence"]["evidence_id"]
    ed = data(env.call("get_evidence_details", evidence_id=eid))
    assert ed["references_valid"] and fid in ed["supports_findings"]
    assert ed["evidence"]["query_ref"] and ed["evidence"]["produced_by_tool"]
    if circ.evidence_refs[0] == eid:
        assert set(ed["transaction_ids"]) == set(circ.transaction_ids)
    for tid, t in ed["transactions"].items():                         # referenced transactions equal the graph
        src, dst, amt, cur, ts = raw_tx(ready, tid)
        assert t["amount"] == amt and t["source_accounts"] == [src] and t["destination_accounts"] == [dst]
    assert eid in env.ledger.evidence and fid in env.ledger.findings


def test_d_missing_and_incomplete_evidence_is_reported_explicitly(loaded, ready, tool_env, det_config):
    anchor = patterns(loaded, "circular")[0]["transaction_ids"][0]
    InvestigationEngine(ready, det_config).investigate(InvestigationRequest("transaction", anchor), persist=True)
    env = tool_env()
    fid = data(env.call("get_transaction_context", transaction_id=anchor))["stored_findings"][0]["finding_id"]
    eid = data(env.call("get_finding_details", finding_id=fid))["evidence"][0]["evidence"]["evidence_id"]
    victim = data(env.call("get_evidence_details", evidence_id=eid))["transaction_ids"][0]
    ready.graph.query("MATCH (t:Transaction {transaction_id:$i}) DETACH DELETE t", {"i": victim})
    e = env.call("get_evidence_details", evidence_id=eid)
    assert e.result.status == "incomplete" and data(e)["references_valid"] is False and e.result.warnings
    f = env.call("get_finding_details", finding_id=fid)
    assert f.result.status == "incomplete" and data(f)["references_valid"] is False
    ready.graph.query("MATCH (e:Evidence {evidence_id:$i}) DETACH DELETE e", {"i": eid})
    assert env.call("get_evidence_details", evidence_id=eid).result.status == "not_found"
    assert env.call("get_finding_details", finding_id=fid).result.status == "incomplete"      # finding now has no evidence


# ---- E: unknown anchors -----------------------------------------------------------------------------
def test_e_unknown_identifiers_are_not_found_everywhere(loaded, tool_env):
    env = tool_env()
    ghost_t, ghost_a = f"{NS}:TXN_DOES_NOT_EXIST", f"{NS}:ACC_DOES_NOT_EXIST"
    cases = [("get_transaction_context", dict(transaction_id=ghost_t)), ("get_account_activity", dict(account_id=ghost_a)),
             ("trace_transaction_network", dict(anchor_type="transaction", anchor_id=ghost_t)),
             ("trace_transaction_network", dict(anchor_type="account", anchor_id=ghost_a, max_hops=3)),
             ("run_pattern_detector", dict(detector="circular", anchor_type="transaction", anchor_id=ghost_t)),
             ("run_pattern_detector", dict(detector="fan_in", anchor_type="account", anchor_id=ghost_a)),
             ("run_investigation", dict(anchor_type="transaction", anchor_id=ghost_t)),
             ("get_finding_details", dict(finding_id="fnd:" + "0" * 32)), ("get_evidence_details", dict(evidence_id="evd:" + "0" * 32))]
    for name, args in cases:
        e = env.call(name, **args)
        assert e.executed and e.result.status == "not_found", (name, e.result.status)
        assert not (e.result.data.get("detections"))
    assert env.ledger.counts()["transactions"] == 0 and not env.ledger.detections          # nothing fabricated for the model to cite


# ---- F: invalid arguments never reach the database -------------------------------------------------
def test_f_invalid_calls_are_rejected_before_any_query(loaded, ready, tool_env, spy_graph):
    log, _ = spy_graph
    env = tool_env()
    assert env.call("get_transaction_context", transaction_id="agt-ns:T000000001").executed
    assert log and all(kind == "read" for kind, _ in log)             # positive control: valid calls are visible and read-only
    log.clear()
    bad = [("get_transaction_context", {"transaction_id": "x'; MATCH (n) DETACH DELETE n //"}),
           ("get_transaction_context", {"transaction_id": "a", "cypher": "MATCH (n) DETACH DELETE n"}),
           ("get_account_activity", {"account_id": "a", "limit": 10_000}), ("get_account_activity", {"account_id": "a", "direction": "sideways"}),
           ("get_account_activity", {"account_id": "a", "start_ms": -5}), ("run_pattern_detector", {"detector": "x", "anchor_type": "transaction", "anchor_id": "a"}),
           ("run_pattern_detector", {"detector": "circular", "anchor_type": "dataset", "anchor_id": "a"}),
           ("trace_transaction_network", {"anchor_type": "transaction", "anchor_id": "a", "max_hops": 1000}),
           ("get_finding_details", {}), ("get_evidence_details", {"evidence_id": 7}), ("drop_graph", {}), ("run_investigation", {"anchor_type": "transaction"})]
    for name, args in bad:
        e = env.call(name, **args)
        assert not e.executed and e.result.status == "invalid_arguments", (name, args)
    assert log == []                                                  # not a single query, read or write


def test_f_tools_never_write_even_on_success(loaded, ready, tool_env, spy_graph):
    log, _ = spy_graph
    env = tool_env()
    anchor = patterns(loaded, "circular")[0]["transaction_ids"][0]
    for name, args in [("get_transaction_context", dict(transaction_id=anchor)), ("run_investigation", dict(anchor_type="transaction", anchor_id=anchor)),
                       ("trace_transaction_network", dict(anchor_type="transaction", anchor_id=anchor)),
                       ("get_account_activity", dict(account_id=raw_tx(ready, anchor)[0]))]:
        assert env.call(name, **args).executed
    assert log and {k for k, _ in log} == {"read"}


# ---- H: the graph is authoritative -----------------------------------------------------------------
def test_h_tool_results_follow_the_graph(loaded, ready, tool_env):
    env = tool_env()
    cyc = patterns(loaded, "circular")[0]
    anchor, victim = cyc["transaction_ids"][0], cyc["transaction_ids"][1]
    victim_src = raw_tx(ready, victim)[0]
    before = data(env.call("run_pattern_detector", detector="circular", anchor_type="transaction", anchor_id=anchor))["detections"]
    n0 = len(data(env.call("get_account_activity", account_id=victim_src, direction="out", limit=50))["activity"])
    assert before and any(set(cyc["transaction_ids"]) <= set(d["transaction_ids"]) for d in before)
    ready.graph.query("MATCH (t:Transaction {transaction_id:$i}) DETACH DELETE t", {"i": victim})
    env2 = tool_env()
    after = data(env2.call("run_pattern_detector", detector="circular", anchor_type="transaction", anchor_id=anchor))["detections"]
    assert not any(set(cyc["transaction_ids"]) <= set(d["transaction_ids"]) for d in after)         # the cycle no longer exists
    assert env2.call("get_transaction_context", transaction_id=victim).result.status == "not_found"
    n1 = len(data(env2.call("get_account_activity", account_id=victim_src, direction="out", limit=50))["activity"])
    assert n1 == n0 - 1


def test_account_activity_limit_is_enforced_and_flagged(loaded, ready, tool_env):
    env = tool_env()
    busy = ready.graph.ro_query("MATCH (a:Account)-[:SENT]->(t:Transaction) RETURN a.account_id, count(t) AS n ORDER BY n DESC LIMIT 1").result_set[0]
    acct, n = busy
    assert n >= 3
    e = env.call("get_account_activity", account_id=acct, direction="out", limit=2)
    assert len(data(e)["activity"]) == 2 and e.result.truncated and e.result.status == "incomplete"
    e = env.call("get_account_activity", account_id=acct, direction="out", limit=50)
    assert len(data(e)["activity"]) == n and not e.result.truncated
