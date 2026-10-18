"""Phase 2 ingestion against a real FalkorDB. Every test uses its own disposable graph."""
import json
from decimal import Decimal

import pytest

from money_trail.database import GraphStore, check_health
from money_trail.database import graph_schema as gs
from money_trail.ingestion import ingest_dataset, load_dataset_config
from money_trail.ingestion import pipeline as pl
from money_trail.ingestion.contract import TimestampParser, namespaced_id

NS = "test-ns"


def tx_params(ds):
    """Expected canonical transaction values computed independently from the CSV (exact decimals)."""
    p, out = TimestampParser("iso8601"), []
    for t in ds.rows("transactions.csv"):
        out.append({"id": namespaced_id(t["txn_id"], NS, "x"), "src": namespaced_id(t["from_account"], NS, "x"),
                    "dst": namespaced_id(t["to_account"], NS, "x"),
                    "amount": int(Decimal(t["amount"]) * 100), "ts": p.to_epoch_ms(t["booked_at"])})
    return out


# ---- Test A: end-to-end import ------------------------------------------------
def test_a_end_to_end_import(ready, run_ingest, make_dataset, counts):
    ds = make_dataset(NS)
    r = run_ingest(ds)
    assert r["status"] == "succeeded" and r["failed_stage"] is None and r["writes_occurred"]
    n = ds.manifest["record_counts"]
    c = counts()
    assert (c["Account"], c["Transaction"], c["Alert"]) == (n["accounts"], n["transactions"], n["alerts"])
    assert c["SENT"] == c["RECEIVED_BY"] == n["transactions"] and c["FLAGS"] == n["alerts"]
    assert r["graph"]["accounts"]["new"] == n["accounts"] and r["graph"]["transactions"]["new"] == n["transactions"]
    assert r["verification"]["passed"] and r["batches"]["failed"] == 0
    assert r["graph_totals"]["before"] == {"Account": 0, "Transaction": 0, "Alert": 0}
    assert r["graph_totals"]["after"] == {"Account": n["accounts"], "Transaction": n["transactions"], "Alert": n["alerts"]}
    assert all(f["sha256"] for f in r["files"].values())
    cfg = load_dataset_config(ds.ingest_config)
    saved = json.loads((cfg.report_dir / f"{r['run_id']}.json").read_text())
    assert saved["status"] == "succeeded" and "password" not in json.dumps(saved).lower()
    # accounts carry dataset provenance
    assert ready.graph.ro_query("MATCH (a:Account) WHERE a.dataset <> $ns RETURN count(a)", {"ns": NS}).result_set == [[0]]
    # schema stays healthy and the Phase 1 health check agrees
    assert check_health(ready).healthy


# ---- Test B: relationships, directions, values ----------------------------------
def test_b_transaction_relationships_and_values(ready, run_ingest, make_dataset):
    ds = make_dataset(NS)
    run_ingest(ds)
    rows = tx_params(ds)
    g = ready.graph
    ok = g.ro_query(
        "UNWIND $rows AS r MATCH (a:Account {account_id: r.src})-[:SENT]->(t:Transaction {transaction_id: r.id})"
        "-[:RECEIVED_BY]->(b:Account {account_id: r.dst}) "
        "WHERE t.amount = r.amount AND t.timestamp = r.ts AND t.currency = 'USD' RETURN count(t)", {"rows": rows}).result_set
    assert ok == [[len(rows)]]
    # nothing runs the other way
    assert g.ro_query("MATCH (:Transaction)-[r:SENT]->() RETURN count(r)").result_set == [[0]]
    assert g.ro_query("MATCH ()-[r:RECEIVED_BY]->(:Transaction) RETURN count(r)").result_set == [[0]]
    assert g.ro_query("MATCH (:Transaction)-[r:FLAGS]->() RETURN count(r)").result_set == [[0]]
    # each transaction has exactly one sender and one receiver
    assert g.ro_query("MATCH (t:Transaction) OPTIONAL MATCH (a:Account)-[:SENT]->(t) WITH t, count(a) AS c "
                      "WHERE c <> 1 RETURN count(t)").result_set == [[0]]
    # alerts flag existing transactions and nothing else was invented
    assert g.ro_query("MATCH (n) WHERE n:Investigation OR n:Finding OR n:Evidence OR n:Agent RETURN count(n)").result_set == [[0]]


# ---- Test C: referential integrity ----------------------------------------------
def _add_orphan(ds):
    ds.rewrite_csv("transactions.csv", lambda rows: rows + [["TORPHAN", "A00000001", "ZZ_MISSING", "5.00", "USD",
                                                             "2026-01-02T00:00:00.000Z", "x"]])


def test_c_strict_mode_rejects_missing_account_and_writes_nothing(ready, run_ingest, make_dataset, counts):
    ds = make_dataset(NS)
    _add_orphan(ds)
    r = run_ingest(ds)
    assert r["status"] == "failed_validation" and r["failed_stage"] == "validation" and not r["writes_occurred"]
    assert {"entity": "transactions", "code": "UNKNOWN_ACCOUNT", "count": 1} in r["validation_errors"]["by_code"]
    assert all(v == 0 for v in counts().values())


def test_c_quarantine_mode_separates_the_bad_record(ready, run_ingest, make_dataset, counts):
    ds = make_dataset(NS)
    _add_orphan(ds)
    r = run_ingest(ds, invalid_policy="quarantine")
    assert r["status"] == "succeeded_with_rejections" and r["records"]["transactions"]["rejected"] == 1
    n = ds.manifest["record_counts"]
    assert counts()["Transaction"] == n["transactions"]
    assert ready.graph.ro_query("MATCH (t:Transaction {transaction_id: $i}) RETURN count(t)",
                                {"i": "test-ns:TORPHAN"}).result_set == [[0]]
    qfile = load_dataset_config(ds.ingest_config).quarantine_dir / r["run_id"] / "transactions.jsonl"
    rec = json.loads(qfile.read_text().splitlines()[0])
    assert rec["errors"][0]["code"] == "UNKNOWN_ACCOUNT" and rec["raw"]["txn_id"] == "TORPHAN" and rec["line"] >= 2
    assert r["quarantine"]["files"] == {"transactions": "transactions.jsonl"}


def test_c_derived_accounts_only_when_explicitly_configured(ready, run_ingest, make_dataset, counts):
    ds = make_dataset(NS)
    raw = json.loads(ds.ingest_config.read_text())
    del raw["files"]["accounts"]
    raw["ingestion"]["accounts_policy"] = "derive_from_transactions"
    ds.ingest_config.write_text(json.dumps(raw))
    r = run_ingest(ds)
    # Pattern accounts all appear in transactions, but accounts without any transaction are not invented
    assert r["status"] == "succeeded" and r["accounts_derived"] == counts()["Account"] > 0
    assert r["accounts_derived"] <= ds.manifest["record_counts"]["accounts"]
    assert r["records"]["accounts"]["accepted"] == r["accounts_derived"]


# ---- Test D: idempotent re-import --------------------------------------------------
def test_d_idempotent_reimport(ready, run_ingest, make_dataset, counts):
    ds = make_dataset(NS)
    first = run_ingest(ds)
    before = counts()
    second = run_ingest(ds)
    assert counts() == before
    n = ds.manifest["record_counts"]
    assert second["status"] == "succeeded" and not second["writes_occurred"]
    for e, total in (("accounts", n["accounts"]), ("transactions", n["transactions"]), ("alerts", n["alerts"])):
        assert second["graph"][e] == {"new": 0, "unchanged": total, "updated": 0,
                                      "conflicting_rejected": 0, "conflicting_skipped": 0}
    assert all(v["created"] == 0 for v in second["relationships"].values())
    assert second["relationships"]["SENT"]["already_present"] == n["transactions"]
    assert second["graph_totals"]["before"] == second["graph_totals"]["after"] == first["graph_totals"]["after"]
    assert second["run_id"] != first["run_id"]


# ---- Test E: namespace isolation -------------------------------------------------
def test_e_cross_dataset_namespace_isolation(ready, run_ingest, make_dataset, counts):
    a, b = make_dataset("ns-alpha", seed=5), make_dataset("ns-beta", seed=5)   # identical source ids
    assert a.rows("transactions.csv")[0]["txn_id"] == b.rows("transactions.csv")[0]["txn_id"]
    ra, rb = run_ingest(a), run_ingest(b)
    assert ra["status"] == rb["status"] == "succeeded"
    n = a.manifest["record_counts"]
    c = counts()
    assert c["Account"] == 2 * n["accounts"] and c["Transaction"] == 2 * n["transactions"] and c["Alert"] == 2 * n["alerts"]
    assert rb["graph_totals"]["before"]["Transaction"] == 0          # beta's namespace was empty before its import
    first = a.rows("transactions.csv")[0]["txn_id"]
    store = GraphStore(ready)
    ta, tb = store.get_node("Transaction", f"ns-alpha:{first}"), store.get_node("Transaction", f"ns-beta:{first}")
    assert ta and tb and ta["dataset"] == "ns-alpha" and tb["dataset"] == "ns-beta"
    assert ready.graph.ro_query("MATCH (t:Transaction) RETURN count(DISTINCT t.transaction_id)").result_set == [[2 * n["transactions"]]]
    # each namespace's edges stay inside it
    assert ready.graph.ro_query("MATCH (a:Account)-[:SENT]->(t:Transaction) WHERE a.dataset <> t.dataset "
                                "RETURN count(t)").result_set == [[0]]


# ---- Test F: conflicting records and policies -------------------------------------
def _first_tx_row(ds):
    return ds.rows("transactions.csv")[0]


def _bump_amount(ds):
    def fn(rows):
        i = rows[0].index("amount")
        rows[1][i] = str(Decimal(rows[1][i]) + 1)
        return rows
    ds.rewrite_csv("transactions.csv", fn)


def _redirect_destination(ds):
    other = ds.rows("accounts.csv")[0]["account_id"]
    def fn(rows):
        i, j = rows[0].index("to_account"), rows[0].index("from_account")
        if rows[1][i] == other or rows[1][j] == other:
            other2 = ds.rows("accounts.csv")[1]["account_id"]
            rows[1][i] = other2 if rows[1][j] != other2 else ds.rows("accounts.csv")[2]["account_id"]
        else:
            rows[1][i] = other
        return rows
    ds.rewrite_csv("transactions.csv", fn)


def _amount_in_graph(conn, ds):
    tid = namespaced_id(_first_tx_row(ds)["txn_id"], NS, "x")
    return GraphStore(conn).get_node("Transaction", tid)["amount"]


@pytest.mark.parametrize("invalid,graph_policy,status,expect", [
    ("strict", "reject", "failed_conflict_check", {"amount_changed": False, "wrote": False}),
    ("quarantine", "reject", "succeeded_with_rejections", {"rejected": 1, "amount_changed": False}),
    ("strict", "skip", "succeeded_with_rejections", {"skipped": 1, "amount_changed": False}),
    ("strict", "update", "succeeded", {"updated": 1, "amount_changed": True}),
])
def test_f_graph_conflict_policies(ready, run_ingest, make_dataset, counts, invalid, graph_policy, status, expect):
    ds = make_dataset(NS)
    run_ingest(ds)
    original, before = _amount_in_graph(ready, ds), counts()
    _bump_amount(ds)
    r = run_ingest(ds, invalid_policy=invalid, graph_conflict_policy=graph_policy)
    assert r["status"] == status, r["error"]
    g = r["graph"]["transactions"]
    assert g["conflicting_rejected"] == expect.get("rejected", 0)
    assert g["conflicting_skipped"] == expect.get("skipped", 0)
    assert g["updated"] == expect.get("updated", 0)
    assert (_amount_in_graph(ready, ds) != original) == expect["amount_changed"]
    assert counts() == before                                   # never duplicates, whatever the policy
    if status == "failed_conflict_check":
        assert not r["writes_occurred"] and r["failed_stage"] == "plan"
    if invalid == "quarantine":
        qf = load_dataset_config(ds.ingest_config).quarantine_dir / r["run_id"] / "transactions.jsonl"
        assert json.loads(qf.read_text().splitlines()[0])["errors"][0]["code"] == "GRAPH_CONFLICT"


@pytest.mark.parametrize("graph_policy", ["reject", "skip", "update"])
def test_f_endpoint_conflicts_are_never_applied(ready, run_ingest, make_dataset, counts, graph_policy):
    ds = make_dataset(NS)
    run_ingest(ds)
    tid = namespaced_id(_first_tx_row(ds)["txn_id"], NS, "x")
    def edges():
        return ready.graph.ro_query(
            "MATCH (a:Account)-[:SENT]->(t:Transaction {transaction_id: $t})-[:RECEIVED_BY]->(b:Account) "
            "RETURN a.account_id, b.account_id", {"t": tid}).result_set
    before = edges()
    _redirect_destination(ds)
    r = run_ingest(ds, invalid_policy="quarantine", graph_conflict_policy=graph_policy)
    assert edges() == before and len(before) == 1
    g = r["graph"]["transactions"]
    assert g["conflicting_rejected"] + g["conflicting_skipped"] == 1 and g["updated"] == 0


def test_f_source_conflicts_follow_configuration(ready, run_ingest, make_dataset, counts):
    ds = make_dataset(NS)
    row = list(_first_tx_row(ds).values())
    dup = list(row); dup[3] = str(Decimal(dup[3]) + 7)
    ds.rewrite_csv("transactions.csv", lambda rows: rows + [dup])
    r = run_ingest(ds)                                            # default: reject
    assert r["status"] == "failed_validation" and not r["writes_occurred"]
    assert {"entity": "transactions", "code": "DUPLICATE_CONFLICT", "count": 1} in r["validation_errors"]["by_code"]
    assert all(v == 0 for v in counts().values())
    r = run_ingest(ds, source_conflict_policy="keep_first")
    assert r["status"] == "succeeded" and r["records"]["transactions"]["source_conflicts_skipped"] == 1
    assert _amount_in_graph(ready, ds) == int(Decimal(row[3]) * 100)       # first occurrence won


def test_f_identical_repeats_are_skipped_and_counted(ready, run_ingest, make_dataset, counts):
    ds = make_dataset(NS)
    row = list(_first_tx_row(ds).values())
    ds.rewrite_csv("transactions.csv", lambda rows: rows + [row, row])
    r = run_ingest(ds)
    assert r["status"] == "succeeded" and r["records"]["transactions"]["duplicates_identical"] == 2
    assert counts()["Transaction"] == ds.manifest["record_counts"]["transactions"]


# ---- Test G: invalid input end to end ------------------------------------------------
BAD_ROWS = [
    ["TBAD1", "A00000001", "A00000002", "-5", "USD", "2026-01-02T00:00:00Z", ""],       # amount
    ["TBAD2", "A00000001", "A00000002", "5", "ZZZ", "2026-01-02T00:00:00Z", ""],         # currency
    ["TBAD3", "A00000001", "A00000002", "5", "USD", "not-a-date", ""],                   # timestamp
    ["TBAD4", "A00000001", "A00000002", "5.001", "USD", "2026-01-02T00:00:00Z", ""],     # precision
    ["", "A00000001", "A00000002", "5", "USD", "2026-01-02T00:00:00Z", ""],              # identifier
    ["TBAD6", "A00000001", "A00000002", "5", "USD", "2026-01-02T00:00:00"],              # malformed row
]


def test_g_invalid_input_strict_then_quarantine(ready, run_ingest, make_dataset, counts):
    ds = make_dataset(NS)
    ds.rewrite_csv("transactions.csv", lambda rows: rows + BAD_ROWS)
    r = run_ingest(ds)
    assert r["status"] == "failed_validation" and not r["writes_occurred"]
    assert r["validation_errors"]["total"] == 6 and all(v == 0 for v in counts().values())
    codes = {x["code"] for x in r["validation_errors"]["by_code"]}
    assert codes == {"INVALID_AMOUNT", "UNSUPPORTED_CURRENCY", "INVALID_TIMESTAMP", "MISSING_VALUE", "MALFORMED_ROW"}
    r = run_ingest(ds, invalid_policy="quarantine")
    assert r["status"] == "succeeded_with_rejections" and r["records"]["transactions"]["rejected"] == 6
    assert counts()["Transaction"] == ds.manifest["record_counts"]["transactions"]
    lines = (load_dataset_config(ds.ingest_config).quarantine_dir / r["run_id"] / "transactions.jsonl").read_text().splitlines()
    assert len(lines) == 6


def test_g_missing_column_fails_clearly(ready, run_ingest, make_dataset, counts):
    ds = make_dataset(NS)
    p = ds.dir / "transactions.csv"
    p.write_text(p.read_text().replace("booked_at", "booked_when", 1))
    r = run_ingest(ds)
    assert r["status"] == "failed_validation" and "booked_at" in r["error"] and not r["writes_occurred"]
    assert all(v == 0 for v in counts().values())


# ---- Test J: partial failure and recovery -----------------------------------------------
def test_j_partial_failure_is_reported_accurately_and_rerun_recovers(ready, run_ingest, make_dataset, counts, monkeypatch):
    ds = make_dataset(NS)
    ds.edit_ingest(batch_size=10)
    rows = tx_params(ds)
    victim = rows[12]                                    # lives in the 2nd transaction batch
    channel = ds.rows("transactions.csv")[12]["channel"]
    # a pre-existing, identical transaction node without edges, as if an earlier run died after creating it
    GraphStore(ready).upsert_node("Transaction", {
        "transaction_id": victim["id"], "dataset": NS, "currency": "USD", "amount": victim["amount"],
        "timestamp": victim["ts"], "channel": channel})
    real, calls = pl.classify, {"n": 0}
    plan_calls = -(-len(rows) // 10)                     # strict mode first runs one read-only plan call per batch

    def flaky(store, cfg, entity, batch):
        out = real(store, cfg, entity, batch)
        if entity == "transactions":
            calls["n"] += 1
            if calls["n"] == plan_calls + 2:            # 2nd WRITE batch: claim everything is new -> real unique violation
                return {"new": list(batch), "unchanged": [], "changed": [], "conflict": []}
        return out

    monkeypatch.setattr(pl, "classify", flaky)
    r = run_ingest(ds)
    assert r["status"] == "failed_write" and r["failed_stage"] == "write" and r["writes_occurred"]
    assert r["batches"]["failed"] == 1 and r["batches"]["succeeded"] >= 2           # accounts + 1st tx batch committed
    d = r["failure_detail"]
    assert d["entity"] == "transactions" and d["records_in_batch"] == 10 and "unique constraint" in r["error"]
    n = ds.manifest["record_counts"]
    c = counts()
    assert c["Account"] == n["accounts"] and 10 <= c["Transaction"] < n["transactions"]   # partial progress is real
    assert c["Alert"] == 0                                                                 # later stages never started
    assert r["verification"] is None

    monkeypatch.setattr(pl, "classify", real)
    again = run_ingest(ds)
    assert again["status"] == "succeeded"
    c = counts()
    assert c["Transaction"] == n["transactions"] and c["SENT"] == c["RECEIVED_BY"] == n["transactions"]
    assert c["Alert"] == n["alerts"] and c["Account"] == n["accounts"]
    assert ready.graph.ro_query("MATCH (t:Transaction) RETURN count(DISTINCT t.transaction_id)").result_set == [[n["transactions"]]]
    assert again["graph"]["accounts"]["new"] == 0 and again["graph"]["transactions"]["unchanged"] >= 10


# ---- Test K: isolation and safety --------------------------------------------------------
def test_k_ingest_requires_initialized_schema_and_does_not_create_it(conn, make_dataset):
    ds = make_dataset(NS)
    r = ingest_dataset(conn, load_dataset_config(ds.ingest_config))
    assert r["status"] == "failed_preflight" and not r["writes_occurred"]
    assert not conn.graph_exists()                       # no graph, no schema side effects


def test_k_ingestion_only_touches_its_own_graph(ready, run_ingest, make_dataset, make_graph_name, base_settings):
    from money_trail.database import FalkorDBConnection
    other = FalkorDBConnection(base_settings.with_graph_name(make_graph_name()))
    try:
        other.graph.query("CREATE (:Account {account_id: 'other-graph-sentinel'})")
        r = run_ingest(make_dataset(NS))
        assert r["status"] == "succeeded"
        assert ready.settings.graph_name not in (other.settings.graph_name, base_settings.graph_name)
        assert GraphStore(other).count_nodes() == 1
        assert base_settings.graph_name not in {x for x in [ready.settings.graph_name]}
    finally:
        other.close()
