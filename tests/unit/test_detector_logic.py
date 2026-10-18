"""Pure detector logic (normalization, exact arithmetic, windows, ids). No database involved:
these tests do not claim anything about FalkorDB integration."""
import itertools

import pytest
from redis import exceptions as redis_exc

from money_trail.investigation.detectors import base as b
from money_trail.investigation.detectors.circular import CircularSettings, cycles_from_rows
from money_trail.investigation.detectors.models import (
    TxRecord, canonical_rotation, detection_key, evidence_id_for, make_detection_id, stable_id,
)
from money_trail.investigation.detectors.pass_through import PassThroughSettings, chains_from_rows, maximal_only
from money_trail.investigation.detectors.windows import find_windows


def T(i, src, dst, ts, amount=100, ccy="USD"):
    return TxRecord(i, src, dst, amount, ccy, ts)


# ---- exact amount arithmetic -----------------------------------------------------------------
@pytest.mark.parametrize("prev,nxt,lo,hi,ok", [
    (10000, 9000, 9000, 10000, True),      # exactly the lower bound
    (10000, 8999, 9000, 10000, False),     # one minor unit below
    (10000, 10000, 9000, 10000, True),     # exactly the upper bound
    (10000, 10001, 9000, 10000, False),
    (3, 3, 10000, 10000, True),
    (3, 2, 6667, 10000, False),            # 2/3 = 6666.67 bps: rounding up must not make it pass
    (3, 2, 6666, 10000, True),
    (0, 0, 0, 10000, False),               # zero previous amount: ratio undefined -> not satisfied
    (0, 5, 0, 1000000, False),
    (10**15, 9 * 10**14, 9000, 10000, True),   # large values stay exact (no float, no overflow)
])
def test_retention_is_exact_integer_arithmetic(prev, nxt, lo, hi, ok):
    assert b.retention_ok(prev, nxt, lo, hi) is ok


def test_chain_amount_check_requires_same_currency_before_comparing():
    rule = {"min_bps": 9000, "max_bps": 10000}
    assert b.chain_amount_check([T("a", "A", "B", 1, 100), T("b", "B", "C", 2, 95)], rule) == "ok"
    assert b.chain_amount_check([T("a", "A", "B", 1, 100, "USD"), T("b", "B", "C", 2, 95, "EUR")], rule) == "currency_mismatch"
    assert b.chain_amount_check([T("a", "A", "B", 1, 100), T("b", "B", "C", 2, 50)], rule) == "retention_failed"
    assert b.chain_amount_check([T("a", "A", "B", 1, 100, "USD"), T("b", "B", "C", 2, 5, "EUR")], None) == "ok"   # no amount rule


# ---- canonical rotation -----------------------------------------------------------------------
def cycle():
    return [T("t1", "A", "B", 10), T("t2", "B", "C", 20), T("t3", "C", "A", 30)]


def test_rotations_of_one_cycle_are_canonicalized_to_the_same_sequence():
    base = cycle()
    results = {tuple(t.transaction_id for t in canonical_rotation(base[i:] + base[:i])) for i in range(3)}
    assert results == {("t1", "t2", "t3")}


def test_reversal_is_never_applied():
    rev = list(reversed(cycle()))                      # a different (reversed) sequence of the same transactions
    assert [t.transaction_id for t in canonical_rotation(rev)] != ["t1", "t2", "t3"]
    assert sorted(t.transaction_id for t in canonical_rotation(rev)) == ["t1", "t2", "t3"]


def test_rotation_tie_break_is_by_id():
    tied = [T("b", "A", "B", 5), T("a", "B", "C", 5), T("c", "C", "A", 5)]
    assert [t.transaction_id for t in canonical_rotation(tied)] == ["a", "c", "b"]


# ---- cycle normalization ------------------------------------------------------------------------
S = CircularSettings(3, 5, 1000, 5000, False, {"min_bps": 9000, "max_bps": 10000})


def row(start, ids_amounts, accs, ccy="USD", ts0=10):
    return (start, [[i, a, ccy, ts0 + 10 * k] for k, (i, a) in enumerate(ids_amounts)], accs)


def test_cycle_rows_are_normalized_and_validated():
    good = row("A", [("t1", 100), ("t2", 95), ("t3", 92)], ["B", "C"])
    cycles, skipped = cycles_from_rows([good], S)
    assert [t.transaction_id for t in cycles[0]] == ["t1", "t2", "t3"]
    assert [(t.source_account, t.destination_account) for t in cycles[0]] == [("A", "B"), ("B", "C"), ("C", "A")]
    assert sum(skipped.values()) == 0


def test_cycle_rows_rejected_for_repeated_accounts_incomplete_data_and_amounts():
    repeated = row("A", [("t1", 100), ("t2", 95), ("t3", 92)], ["B", "B"])
    incomplete = ("A", [["t1", 100, "USD", 1], [None, 1, "USD", 2], ["t3", 1, "USD", 3]], ["B", "C"])
    wrong_ccy = ("A", [["t1", 100, "USD", 1], ["t2", 95, "EUR", 2], ["t3", 92, "EUR", 3]], ["B", "C"])
    lossy = row("A", [("t1", 100), ("t2", 50), ("t3", 49)], ["B", "C"])
    missing_acc = row("A", [("t1", 100), ("t2", 95), ("t3", 92)], ["B"])
    cycles, sk = cycles_from_rows([repeated, incomplete, wrong_ccy, lossy, missing_acc], S)
    assert cycles == []
    assert sk == {"incomplete_data": 2, "currency_mismatch": 1, "retention_failed": 1, "repeated_accounts": 1}


def test_repeated_accounts_allowed_only_when_configured():
    s2 = CircularSettings(3, 5, 1000, 5000, True, None)
    cycles, _ = cycles_from_rows([row("A", [("t1", 100), ("t2", 95), ("t3", 92)], ["B", "B"])], s2)
    assert len(cycles) == 1


def test_amount_rule_off_means_no_amount_or_currency_checks():
    s2 = CircularSettings(3, 5, 1000, 5000, False, None)
    cycles, _ = cycles_from_rows([("A", [["t1", 100, "USD", 1], ["t2", 1, "EUR", 2], ["t3", 9999, "JPY", 3]], ["B", "C"])], s2)
    assert len(cycles) == 1


# ---- pass-through chains -------------------------------------------------------------------------
P = PassThroughSettings(2, 4, 1000, 5000, False, {"min_bps": 9000, "max_bps": 10000})


def test_chain_rows_and_maximality():
    chain3 = ("A", [["t1", 100, "USD", 1], ["t2", 95, "USD", 2], ["t3", 91, "USD", 3]], ["B", "C"], "D")
    sub = ("A", [["t1", 100, "USD", 1], ["t2", 95, "USD", 2]], ["B"], "C")
    sub2 = ("B", [["t2", 95, "USD", 2], ["t3", 91, "USD", 3]], ["C"], "D")
    other = ("X", [["u1", 10, "USD", 5], ["u2", 10, "USD", 6]], ["Y"], "Z")
    chains, skipped = chains_from_rows([chain3, sub, sub2, other], P)
    kept = maximal_only(chains)
    assert sorted(tuple(t.transaction_id for t in c) for c in kept) == [("t1", "t2", "t3"), ("u1", "u2")]
    assert sum(skipped.values()) == 0


def test_chain_rows_reject_cycles_when_repeats_are_disallowed():
    loop = ("A", [["t1", 100, "USD", 1], ["t2", 95, "USD", 2]], ["B"], "A")
    chains, sk = chains_from_rows([loop], P)
    assert chains == [] and sk["repeated_accounts"] == 1


def test_maximal_only_is_deterministic_and_dedupes_identical_chains():
    c = [T("a", "A", "B", 1), T("b", "B", "C", 2)]
    assert len(maximal_only([c, list(c)])) == 1


# ---- sliding windows -------------------------------------------------------------------------------
def inflows(spec, focus="Z"):
    return [T(f"t{i}", src, focus, ts, amt, ccy) for i, (src, ts, amt, ccy) in enumerate(spec)]


def hits(spec, **kw):
    args = dict(focus="Z", role="destination", window_ms=1000, min_counterparties=3, min_transactions=3)
    args.update(kw)
    return find_windows(inflows(spec), **args)


def test_window_end_is_inclusive_and_one_millisecond_over_fails():
    inside = [("A", 0, 1, "USD"), ("B", 500, 1, "USD"), ("C", 1000, 1, "USD")]
    over = [("A", 0, 1, "USD"), ("B", 500, 1, "USD"), ("C", 1001, 1, "USD")]
    assert len(hits(inside)[0]) == 1
    assert hits(over)[0] == []


def test_repeated_transfers_from_one_source_are_not_independent_sources():
    spec = [("A", 0, 1, "USD"), ("A", 10, 1, "USD"), ("A", 20, 1, "USD"), ("B", 30, 1, "USD")]
    assert hits(spec)[0] == []                                 # 4 transfers, only 2 distinct sources
    assert len(hits(spec, min_counterparties=2, min_transactions=4)[0]) == 1


def test_min_transaction_count_is_separate_from_distinct_count():
    spec = [("A", 0, 1, "USD"), ("B", 10, 1, "USD"), ("C", 20, 1, "USD")]
    assert len(hits(spec)[0]) == 1
    assert hits(spec, min_transactions=4)[0] == []


def test_self_transfers_are_excluded_and_counted():
    spec = [("A", 0, 1, "USD"), ("B", 10, 1, "USD"), ("Z", 20, 1, "USD"), ("C", 30, 1, "USD")]
    h, ex = hits(spec)
    assert ex["self_transfers"] == 1 and len(h) == 1 and "Z" not in h[0].counterparties and len(h[0].txs) == 3


def test_amount_rule_uses_only_its_currency_and_never_converts():
    spec = [("A", 0, 100, "USD"), ("B", 10, 100, "USD"), ("C", 20, 100, "USD"), ("D", 30, 100000, "EUR")]
    h, ex = hits(spec, amount_currency="USD", min_total_minor=300)
    assert len(h) == 1 and h[0].total_minor == 300 and ex["other_currency"] == 1
    assert hits(spec, amount_currency="USD", min_total_minor=301)[0] == []          # EUR does not top it up


def test_only_maximal_windows_and_separate_bursts():
    burst1 = [("A", 0, 1, "USD"), ("B", 100, 1, "USD"), ("C", 200, 1, "USD")]
    burst2 = [("D", 10_000, 1, "USD"), ("E", 10_100, 1, "USD"), ("F", 10_200, 1, "USD")]
    h, _ = hits(burst1 + burst2)
    assert [tuple(sorted(x.counterparties)) for x in h] == [("A", "B", "C"), ("D", "E", "F")]


def test_sliding_windows_report_distinct_sets_but_not_subsets():
    # five sources spread so that windows [0..1000] and [500..1500] are different maximal windows
    spec = [("A", 0, 1, "USD"), ("B", 400, 1, "USD"), ("C", 800, 1, "USD"), ("D", 1200, 1, "USD"), ("E", 1600, 1, "USD")]
    h, _ = hits(spec)
    sets = [tuple(sorted(x.counterparties)) for x in h]
    assert sets == [("A", "B", "C"), ("B", "C", "D"), ("C", "D", "E")]


def test_fan_out_role_uses_destinations_as_counterparties():
    outs = [T(f"o{i}", "Z", d, i * 10) for i, d in enumerate("ABCD")] + [T("x", "Z", "A", 99)]
    h, _ = find_windows(outs, focus="Z", role="source", window_ms=1000, min_counterparties=4, min_transactions=4)
    assert len(h) == 1 and h[0].counterparties == ("A", "B", "C", "D") and len(h[0].txs) == 5


def test_window_input_order_does_not_matter():
    spec = [("A", 0, 1, "USD"), ("B", 100, 1, "USD"), ("C", 200, 1, "USD")]
    base = [tuple(x.counterparties) for x in hits(spec)[0]]
    for perm in itertools.permutations(spec):
        assert [tuple(x.counterparties) for x in hits(list(perm))[0]] == base


# ---- identifiers -------------------------------------------------------------------------------------
def test_ids_are_deterministic_and_sensitive_to_every_input():
    k = detection_key("circular", canonical_rotation(cycle()), None)
    base = make_detection_id("ns", "circular", "rv1", k)
    assert base == make_detection_id("ns", "circular", "rv1", k) and base.startswith("det:")
    assert len({base, make_detection_id("ns2", "circular", "rv1", k), make_detection_id("ns", "circular", "rv2", k),
                make_detection_id("ns", "circular", "rv1", k + " ")}) == 4
    assert evidence_id_for(base) == evidence_id_for(base) != evidence_id_for(base + "x")
    assert stable_id("a", "x", "y") != stable_id("a", "xy")          # parts are delimited


def test_fan_key_ignores_transaction_order_but_not_membership():
    a, c = [T("t1", "A", "Z", 1), T("t2", "B", "Z", 2)], [T("t2", "B", "Z", 2), T("t1", "A", "Z", 1)]
    assert detection_key("fan_in", a, "Z") == detection_key("fan_in", c, "Z")
    assert detection_key("fan_in", a, "Z") != detection_key("fan_in", a[:1], "Z")
    assert detection_key("fan_in", a, "Z") != detection_key("fan_in", a, "Y")


# ---- path-bound safety ----------------------------------------------------------------------------------
def test_path_bounds_are_validated_before_entering_query_text():
    q = b.forward_query(3, anchored=False, closing=True, distinct_accounts=True)
    assert q.count("]->(a") == 2 and "$ns" in q and "LIMIT $cap" in q               # two staged hops for three transactions
    for bad in (0, -1, 11, True, "3", "3; MATCH (n) DETACH DELETE n", 2.5):
        with pytest.raises(Exception):
            b.forward_query(bad, anchored=True, closing=False, distinct_accounts=False)
        with pytest.raises(Exception):
            b.backward_start_query(bad)


def test_staged_queries_filter_at_every_hop_and_use_only_registry_names():
    q = b.forward_query(4, anchored=True, closing=True, distinct_accounts=True)
    assert q.count("WHERE") == 3 and q.count("$gap") == 3 and q.count("$window") == 3        # one filter set per hop
    assert "*" not in q.replace("RETURN", "")                                                # no variable-length pattern
    chain = b.forward_query(2, anchored=False, closing=False, distinct_accounts=False)
    assert "OPTIONAL MATCH" in chain and "a1.account_id <> " not in chain
    assert "DETACH" not in q and "CREATE" not in q and "SET " not in q and "DELETE" not in q   # read-only
    bq = b.backward_start_query(3)
    assert bq.count("UNION") == 2 and bq.count("$aid") == 3 and "UNWIND" not in bq        # parameter-bound anchor, one branch per depth


def test_schema_names_come_from_the_registry():
    assert (b.TX_LABEL, b.ACC_LABEL, b.SENT, b.RECV, b.P_TS) == ("Transaction", "Account", "SENT", "RECEIVED_BY", "timestamp")


# ---- timeout translation (error-path isolation only) ---------------------------------------------------
class _FakeGraph:
    def __init__(self, exc):
        self.exc = exc

    def ro_query(self, q, params, timeout=None):
        raise self.exc


class _FakeConn:
    def __init__(self, exc):
        self.graph = _FakeGraph(exc)


def test_server_timeout_becomes_search_timeout_and_other_errors_propagate():
    with pytest.raises(b.SearchTimeout):
        b.GraphReader(_FakeConn(redis_exc.ResponseError("Query timed out")), 5).run("RETURN 1", {})
    with pytest.raises(redis_exc.ResponseError):
        b.GraphReader(_FakeConn(redis_exc.ResponseError("Invalid input")), 5).run("RETURN 1", {})
