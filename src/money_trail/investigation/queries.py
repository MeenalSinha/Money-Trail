"""Read-only foundational investigation queries.

All traversal runs inside FalkorDB (ro_query). Labels and relationship types come
from the schema registry; every value is a query parameter. The only values placed
in query text are integers that were validated and bounded (variable-length path
bounds cannot be parameterized in Cypher). These queries describe connectivity;
they never classify anything as suspicious.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from money_trail.config import Settings
from money_trail.config.settings import QUERY_CEILINGS
from money_trail.database import FalkorDBConnection
from money_trail.database import graph_schema as gs
from money_trail.exceptions import QueryError

DIRECTIONS = ("in", "out", "both")
TRACE_DIRECTIONS = ("forward", "backward")


@dataclass(frozen=True)
class QueryLimits:
    """Explicit, validated bounds. None means 'not provided'; queries that need one fail clearly."""
    max_results: Optional[int] = None
    max_hops: Optional[int] = None
    max_paths: Optional[int] = None
    max_gap_ms: Optional[int] = None

    @staticmethod
    def from_settings(s: Settings) -> "QueryLimits":
        return QueryLimits(s.query_max_results, s.query_max_hops, s.query_max_paths, s.query_max_gap_ms)

    def override(self, **kw) -> "QueryLimits":
        return QueryLimits(**{**self.__dict__, **{k: v for k, v in kw.items() if v is not None}})

    def require(self, name: str, ceiling_key: Optional[str]) -> int:
        v = getattr(self, name)
        env = "MONEY_TRAIL_QUERY_" + name.upper()
        if v is None:
            raise QueryError(f"{name} is required: pass it explicitly or set {env}.")
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise QueryError(f"{name} must be a positive integer.")
        if ceiling_key and v > QUERY_CEILINGS[ceiling_key]:
            raise QueryError(f"{name} must not exceed the safety ceiling of {QUERY_CEILINGS[ceiling_key]}.")
        return v


@dataclass
class QueryOutcome:
    """status: 'ok' (found, possibly empty results) or 'not_found' (the anchor record does not exist)."""
    status: str
    query: str
    data: Dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"status": self.status, "query": self.query, "truncated": self.truncated,
                "warnings": self.warnings, "data": self.data}


def _props(node) -> Dict[str, Any]:
    return dict(node.properties)


def _tx_key(tx: Dict[str, Any]):
    return (tx["timestamp"], tx["transaction_id"])


class InvestigationQueries:
    def __init__(self, conn: FalkorDBConnection, limits: QueryLimits) -> None:
        self._conn = conn
        self.limits = limits
        self._N = {n.label: n for n in gs.NODES}

    def _ro(self, query: str, params: Dict[str, Any]):
        return self._conn.graph.ro_query(query, params).result_set

    # --- Query 1 ------------------------------------------------------------
    def transaction_context(self, transaction_id: str) -> QueryOutcome:
        rows = self._ro(
            "MATCH (t:Transaction {transaction_id: $id}) "
            "OPTIONAL MATCH (s:Account)-[:SENT]->(t) "
            "OPTIONAL MATCH (t)-[:RECEIVED_BY]->(d:Account) "
            "OPTIONAL MATCH (al:Alert)-[:FLAGS]->(t) "
            "RETURN t, collect(DISTINCT s), collect(DISTINCT d), collect(DISTINCT al)",
            {"id": transaction_id})
        if not rows:
            return QueryOutcome("not_found", "transaction_context", {"transaction_id": transaction_id})
        t, senders, receivers, alerts = rows[0]
        warnings = []
        if len(senders) != 1:
            warnings.append(f"expected exactly one source account, found {len(senders)}")
        if len(receivers) != 1:
            warnings.append(f"expected exactly one destination account, found {len(receivers)}")
        return QueryOutcome("ok", "transaction_context", {
            "transaction": _props(t),
            "source_accounts": sorted((_props(a) for a in senders), key=lambda a: a["account_id"]),
            "destination_accounts": sorted((_props(a) for a in receivers), key=lambda a: a["account_id"]),
            "alerts": sorted((_props(a) for a in alerts), key=lambda a: a["alert_id"]),
        }, warnings=warnings)

    # --- Query 2 ------------------------------------------------------------
    def account_activity(self, account_id: str, direction: str = "both", start_ms: Optional[int] = None,
                         end_ms: Optional[int] = None) -> QueryOutcome:
        if direction not in DIRECTIONS:
            raise QueryError(f"direction must be one of: {', '.join(DIRECTIONS)}.")
        limit = self.limits.require("max_results", "results")
        for name, v in (("start_ms", start_ms), ("end_ms", end_ms)):
            if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
                raise QueryError(f"{name} must be a non-negative integer (epoch milliseconds).")
        if start_ms is not None and end_ms is not None and start_ms > end_ms:
            raise QueryError("start_ms must not exceed end_ms.")
        if not self._ro("MATCH (a:Account {account_id: $id}) RETURN a.account_id", {"id": account_id}):
            return QueryOutcome("not_found", "account_activity", {"account_id": account_id})

        window = ""
        if start_ms is not None:
            window += " AND t.timestamp >= $start"
        if end_ms is not None:
            window += " AND t.timestamp <= $end"
        params = {"id": account_id, "start": start_ms, "end": end_ms, "cap": limit + 1}
        items: List[Dict[str, Any]] = []
        for want, pattern in (("out", "(a)-[:SENT]->(t)-[:RECEIVED_BY]->(c:Account)"),
                              ("in", "(c:Account)-[:SENT]->(t)-[:RECEIVED_BY]->(a)")):
            if direction not in (want, "both"):
                continue
            rows = self._ro(
                f"MATCH (a:Account {{account_id: $id}}) MATCH {pattern} WHERE true{window} "
                "RETURN t, c.account_id ORDER BY t.timestamp, t.transaction_id LIMIT $cap", params)
            items += [{"direction": want, "counterparty": c, "transaction": _props(t)} for t, c in rows]
        items.sort(key=lambda i: (_tx_key(i["transaction"]), i["direction"]))
        truncated = len(items) > limit
        return QueryOutcome("ok", "account_activity", {
            "account_id": account_id, "direction": direction, "start_ms": start_ms, "end_ms": end_ms,
            "limit": limit, "activity": items[:limit]}, truncated=truncated)

    # --- Query 3 ------------------------------------------------------------
    def trace_network(self, transaction_id: str, direction: str = "forward",
                      max_hops: Optional[int] = None, max_paths: Optional[int] = None,
                      max_gap_ms: Optional[int] = None) -> QueryOutcome:
        """Time-respecting transaction paths. One hop = one further transaction.

        forward: follow funds downstream (destination account then its later outgoing transactions).
        backward: follow funds upstream (source account then its earlier incoming transactions).
        max_gap_ms optionally bounds the time between consecutive transactions.
        """
        if direction not in TRACE_DIRECTIONS:
            raise QueryError(f"direction must be one of: {', '.join(TRACE_DIRECTIONS)}.")
        lim = self.limits.override(max_hops=max_hops, max_paths=max_paths, max_gap_ms=max_gap_ms)
        hops = lim.require("max_hops", "hops")
        paths_cap = lim.require("max_paths", "paths")
        gap = lim.max_gap_ms
        if gap is not None:
            gap = lim.require("max_gap_ms", None)
        if not self._ro("MATCH (t:Transaction {transaction_id: $id}) RETURN 1", {"id": transaction_id}):
            return QueryOutcome("not_found", "trace_network", {"transaction_id": transaction_id})

        # hops is a validated int in [1, ceiling]; it is the only interpolated value.
        arrow_l, arrow_r = ("", "->") if direction == "forward" else ("<", "-")
        order_ok = ("txs[i].timestamp <= txs[i+1].timestamp" if direction == "forward"
                    else "txs[i].timestamp >= txs[i+1].timestamp")
        gap_ok = "" if gap is None else " AND abs(txs[i+1].timestamp - txs[i].timestamp) <= $gap"
        query = (
            f"MATCH p = (t0:Transaction {{transaction_id: $id}}){arrow_l}-[:RECEIVED_BY|SENT*2..{2 * int(hops)}]{arrow_r}"
            "(tn:Transaction) "
            "WITH p, [n IN nodes(p) WHERE labels(n)[0] = 'Transaction'] AS txs "
            f"WHERE all(i IN range(0, size(txs) - 2) WHERE {order_ok}{gap_ok}) "
            "RETURN nodes(p) AS ns LIMIT $cap")
        rows = self._ro(query, {"id": transaction_id, "gap": gap, "cap": paths_cap + 1})
        truncated = len(rows) > paths_cap
        paths, accounts, txs_seen = [], {}, {}
        for (nodes,) in rows[:paths_cap]:
            steps = []
            for n in nodes:
                p = _props(n)
                if "transaction_id" in p:
                    txs_seen[p["transaction_id"]] = p
                    steps.append({"transaction": p})
                else:
                    accounts[p["account_id"]] = p
                    steps.append({"account": p["account_id"]})
            paths.append(steps)
        paths.sort(key=lambda s: [x["transaction"]["transaction_id"] if "transaction" in x else x["account"] for x in s])
        # Path nodes alternate transaction/account, so the first sender and last receivers are not on the
        # path itself. Fetch every path transaction's endpoints so each step can be explained.
        endpoints: Dict[str, Dict[str, str]] = {}
        if txs_seen:
            for tid, src, dst in self._ro(
                    "MATCH (a:Account)-[:SENT]->(t:Transaction)-[:RECEIVED_BY]->(b:Account) "
                    "WHERE t.transaction_id IN $ids RETURN t.transaction_id, a.account_id, b.account_id",
                    {"ids": sorted(txs_seen)}):
                endpoints[tid] = {"source": src, "destination": dst}
                accounts[src] = accounts.get(src, {"account_id": src})
                accounts[dst] = accounts.get(dst, {"account_id": dst})
        return QueryOutcome("ok", "trace_network", {
            "start_transaction": transaction_id, "direction": direction, "max_hops": hops, "max_paths": paths_cap,
            "max_gap_ms": gap, "paths": paths,
            "transactions": sorted(txs_seen.values(), key=_tx_key),
            "transaction_endpoints": endpoints,
            "accounts": sorted(accounts)}, truncated=truncated,
            warnings=["truncated: more paths exist than max_paths; the returned set is not exhaustive"]
            if truncated else [])

    # --- Query 4 ------------------------------------------------------------
    def evidence_paths(self, investigation_id: str) -> QueryOutcome:
        limit = self.limits.require("max_results", "results")
        if not self._ro("MATCH (i:Investigation {investigation_id: $id}) RETURN 1", {"id": investigation_id}):
            return QueryOutcome("not_found", "evidence_paths", {"investigation_id": investigation_id})
        rows = self._ro(
            "MATCH (i:Investigation {investigation_id: $id})-[:HAS_FINDING]->(f:Finding)"
            "-[:SUPPORTED_BY]->(e:Evidence)-[:REFERENCES]->(t:Transaction) "
            "RETURN i, f, e, t ORDER BY f.finding_id, e.evidence_id, t.transaction_id LIMIT $cap",
            {"id": investigation_id, "cap": limit + 1})
        out = [{"finding": _props(f), "evidence": _props(e), "transaction": _props(t)} for _, f, e, t in rows[:limit]]
        return QueryOutcome("ok", "evidence_paths", {"investigation_id": investigation_id, "limit": limit,
                                                    "paths": out}, truncated=len(rows) > limit)
