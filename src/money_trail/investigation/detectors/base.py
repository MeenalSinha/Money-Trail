"""Detector interface, bounded graph reader and shared Cypher fragments.

Every label, relationship type and property name used in detector Cypher comes from the schema
registry. Values are always query parameters. The only values placed in query text are integer
path bounds, validated against the safety ceiling before use.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from redis import exceptions as redis_exc

from money_trail.config.settings import QUERY_CEILINGS
from money_trail.database import FalkorDBConnection
from money_trail.database import graph_schema as gs
from money_trail.exceptions import DatasetConfigError, MoneyTrailError
from money_trail.investigation.detectors.models import (
    Detection, DetectorOutput, Scope, TxRecord, canonical_rotation, detection_key, evidence_id_for,
    make_detection_id,
)

# --- schema-derived names (single source of truth: graph_schema) ---------------------------
TX = gs.get_node_spec("Transaction")
ACC = gs.get_node_spec("Account")
SENT = gs.get_rel_spec("SENT").type
RECV = gs.get_rel_spec("RECEIVED_BY").type
TX_LABEL, ACC_LABEL = TX.label, ACC.label
TX_ID, ACC_ID = TX.id_property, ACC.id_property
P_TS = TX.prop("timestamp").name
P_AMOUNT = TX.prop("amount").name
P_CCY = TX.prop("currency").name
P_DATASET = TX.prop("dataset").name

TX_FIELDS = f"[x IN txs | [x.{TX_ID}, x.{P_AMOUNT}, x.{P_CCY}, x.{P_TS}]]"


class SearchTimeout(MoneyTrailError):
    """The database stopped a query because it exceeded query_timeout_ms (results are incomplete)."""


def bound(n: int, what: str) -> int:
    """Validate an integer destined for query text (path bounds cannot be parameterized)."""
    if isinstance(n, bool) or not isinstance(n, int) or not (1 <= n <= QUERY_CEILINGS["hops"]):
        raise DatasetConfigError(f"{what} must be an integer between 1 and {QUERY_CEILINGS['hops']}.")
    return n


def _tx_cols(i: int) -> str:
    return f"[t{i}.{TX_ID}, t{i}.{P_AMOUNT}, t{i}.{P_CCY}, t{i}.{P_TS}]"


def forward_query(n: int, *, anchored: bool, closing: bool, distinct_accounts: bool) -> str:
    """Cypher for chains of exactly `n` transactions, built as n-1 explicit stages.

    Every stage is its own MATCH ... WHERE, so chronology (non-decreasing timestamps, ties allowed), the
    maximum gap, the maximum total window and account distinctness prune candidates at each hop instead
    of after full path enumeration (variable-length patterns cannot prune by time while expanding; on a
    dense graph that difference is orders of magnitude). `n` is validated and is the only value placed in
    the text besides schema names. closing=True requires the last transaction to return to the account
    that sent the first (circular); otherwise the origin and final accounts are read with OPTIONAL MATCH
    so incomplete graph records can be reported instead of silently dropped.
    """
    n = bound(n, "hops")
    if anchored:
        q = f"UNWIND $starts AS sid MATCH (t0:{TX_LABEL} {{{TX_ID}: sid}}) "
    else:
        q = f"MATCH (t0:{TX_LABEL}) WHERE t0.{P_DATASET} = $ns "
    if closing:
        q += f"MATCH (s:{ACC_LABEL})-[:{SENT}]->(t0) "
    for i in range(1, n):
        conds = [f"t{i}.{P_TS} >= t{i-1}.{P_TS}", f"t{i}.{P_TS} - t{i-1}.{P_TS} <= $gap",
                 f"t{i}.{P_TS} - t0.{P_TS} <= $window"]
        conds += [f"t{i}.{TX_ID} <> t{j}.{TX_ID}" for j in range(i)]            # a transaction is never revisited
        if distinct_accounts:
            conds += [f"a{i}.{ACC_ID} <> a{j}.{ACC_ID}" for j in range(1, i)]
            if closing:
                conds.append(f"a{i}.{ACC_ID} <> s.{ACC_ID}")
        q += (f"MATCH (t{i-1})-[:{RECV}]->(a{i}:{ACC_LABEL})-[:{SENT}]->(t{i}:{TX_LABEL}) WHERE " + " AND ".join(conds) + " ")
    txs = "[" + ", ".join(_tx_cols(i) for i in range(n)) + "]"
    accs = "[" + ", ".join(f"a{i}.{ACC_ID}" for i in range(1, n)) + "]"
    if closing:
        return q + f"MATCH (t{n-1})-[:{RECV}]->(s) RETURN s.{ACC_ID}, {txs}, {accs} LIMIT $cap"
    return (q + f"OPTIONAL MATCH (o:{ACC_LABEL})-[:{SENT}]->(t0) OPTIONAL MATCH (t{n-1})-[:{RECV}]->(d:{ACC_LABEL}) "
            f"RETURN o.{ACC_ID}, {txs}, {accs}, d.{ACC_ID} LIMIT $cap")


def backward_start_query(max_depth: int) -> str:
    """Transactions that could precede one anchor transaction in a chronological chain, up to `max_depth` steps.

    The anchor is bound with the query parameter $aid inside the same pattern as the traversal. That is what
    makes the planner start from an index lookup of the anchor and walk outwards; binding it through UNWIND
    makes it scan every transaction first (measured: about 1 ms versus about 750 ms on 60,000 transactions).
    One UNION branch per depth; each hop is filtered for chronology, gap and total window.
    """
    max_depth = bound(max_depth, "depth")
    branches = []
    for d in range(1, max_depth + 1):
        pat, conds = f"(c0:{TX_LABEL} {{{TX_ID}: $aid}})", []
        for i in range(1, d + 1):
            later = "c0" if i == 1 else f"p{i-1}"
            pat += f"<-[:{SENT}]-(b{i}:{ACC_LABEL})<-[:{RECV}]-(p{i}:{TX_LABEL})"
            conds += [f"p{i}.{P_TS} <= {later}.{P_TS}", f"{later}.{P_TS} - p{i}.{P_TS} <= $gap",
                      f"c0.{P_TS} - p{i}.{P_TS} <= $window"]
        branches.append(f"MATCH {pat} WHERE {' AND '.join(conds)} RETURN DISTINCT p{d}.{TX_ID} AS id LIMIT $cap")
    return " UNION ".join(branches)


@dataclass
class Limits:
    max_candidate_paths: int
    max_detections_per_detector: int
    max_anchor_transactions: int
    max_evidence_transactions: int
    query_timeout_ms: Optional[int]


class GraphReader:
    """Read-only access with an optional server-side timeout and simple accounting."""

    def __init__(self, conn: FalkorDBConnection, timeout_ms: Optional[int]) -> None:
        self._conn, self._timeout = conn, timeout_ms
        self.queries = 0
        self.rows = 0
        self.seconds = 0.0

    def run(self, query: str, params: Dict[str, Any]) -> List[list]:
        t0 = time.perf_counter()
        try:
            res = self._conn.graph.ro_query(query, params, timeout=self._timeout)
        except redis_exc.ResponseError as exc:
            if "timed out" in str(exc).lower() or "timeout" in str(exc).lower():
                raise SearchTimeout(f"query exceeded query_timeout_ms={self._timeout}") from None
            raise
        finally:
            self.seconds += time.perf_counter() - t0
            self.queries += 1
        self.rows += len(res.result_set)
        return res.result_set

    def snapshot(self) -> Dict[str, Any]:
        return {"queries": self.queries, "rows_read": self.rows, "query_seconds": round(self.seconds, 4)}


class Detector(ABC):
    """Common interface. Concrete detectors parse their own settings via `parse_settings`."""

    name: str = ""

    def __init__(self, settings: Any, limits: Limits, reader: GraphReader, rule_version: str,
                 clock: Callable[[], int]) -> None:
        self.settings, self.limits, self.reader = settings, limits, reader
        self.rule_version, self.clock = rule_version, clock

    @staticmethod
    @abstractmethod
    def parse_settings(raw: Dict[str, Any], where: str) -> Any: ...

    @abstractmethod
    def detect(self, scope: Scope) -> DetectorOutput: ...

    # --- shared helpers ----------------------------------------------------------------
    def _anchored_starts(self, scope: Scope, out: DetectorOutput) -> List[str]:
        """Anchor transactions plus every transaction that could precede one inside the chronology limits."""
        s, lim = self.settings, self.limits
        starts = list(scope.anchor_tx_ids)
        if s.max_hops > 1:
            query = backward_start_query(s.max_hops - 1)
            for aid in scope.anchor_tx_ids:               # one indexed query per anchor
                rows = self.reader.run(query, {"aid": aid, "gap": s.max_gap_ms, "window": s.max_total_window_ms,
                                               "cap": lim.max_candidate_paths + 1})
                if len(rows) > lim.max_candidate_paths:
                    out.truncated = True
                    out.warnings.append("candidate start transactions exceeded max_candidate_paths; search is incomplete")
                starts += [r[0] for r in rows[:lim.max_candidate_paths]]
        starts = list(dict.fromkeys(starts))
        if len(starts) > lim.max_candidate_paths:
            starts = starts[:lim.max_candidate_paths]
            out.truncated = True
            out.warnings.append("candidate start transactions exceeded max_candidate_paths; search is incomplete")
        return starts

    def _collect_rows(self, scope: Scope, out: DetectorOutput, closing: bool) -> List[list]:
        """Run one staged query per chain length; cap and flag truncation per query."""
        s, lim = self.settings, self.limits
        anchored = scope.anchor_type != "dataset"
        params: Dict[str, Any] = {"gap": s.max_gap_ms, "window": s.max_total_window_ms,
                                  "cap": lim.max_candidate_paths + 1, "ns": scope.namespace}
        if anchored:
            params["starts"] = self._anchored_starts(scope, out)
            if not params["starts"]:
                return []
        rows: List[list] = []
        for n in range(s.min_hops, s.max_hops + 1):
            got = self.reader.run(forward_query(n, anchored=anchored, closing=closing,
                                                distinct_accounts=not s.allow_repeated_accounts), params)
            if len(got) > lim.max_candidate_paths:
                got = got[:lim.max_candidate_paths]
                out.truncated = True
                out.warnings.append(f"candidate paths of {n} transactions exceeded max_candidate_paths; "
                                    "search is incomplete")
            rows += got
        return rows

    def _finish(self, scope: Scope, found: Dict[str, Detection], out: DetectorOutput) -> DetectorOutput:
        ordered = sorted(found.values(), key=lambda d: d.detection_id)
        cap = self.limits.max_detections_per_detector
        if len(ordered) > cap:
            ordered = ordered[:cap]
            out.truncated = True
            out.warnings.append(f"more than max_detections_per_detector={cap} detections; list truncated")
        out.detections = ordered
        out.stats.update(self.reader.snapshot())
        return out

    def _build(self, scope: Scope, ordered: Sequence[TxRecord], focus: Optional[str], criteria: Dict[str, Any],
               accounts: Sequence[str], roles: Dict[str, Any], warnings: Sequence[str] = (),
               truncated: bool = False) -> Detection:
        key = detection_key(self.name, ordered, focus)
        did = make_detection_id(scope.namespace, self.name, self.rule_version, key)
        return Detection(
            detection_id=did, pattern_type=self.name, anchor_id=scope.anchor_id,
            dataset_namespace=scope.namespace, account_ids=tuple(sorted(set(accounts))),
            transaction_ids=tuple(t.transaction_id for t in ordered), ordered_path=tuple(ordered),
            criteria=criteria, evidence_refs=(evidence_id_for(did),), truncated=truncated,
            warnings=tuple(warnings), rule_version=self.rule_version, created_at=self.clock(), roles=roles)


# --- exact amount criteria (shared by circular and pass-through) -------------------------------
def retention_ok(prev_amount: int, next_amount: int, min_bps: int, max_bps: int) -> bool:
    """True when next/prev lies in [min_bps, max_bps] basis points, using exact integer arithmetic.

    Cross-multiplication avoids division and rounding. A zero previous amount makes the ratio
    undefined, so the criterion is not satisfied.
    """
    if prev_amount <= 0:
        return False
    return min_bps * prev_amount <= next_amount * 10000 <= max_bps * prev_amount


def chain_amount_check(txs: Sequence[TxRecord], rule: Optional[Dict[str, int]]) -> str:
    """'ok', 'currency_mismatch' or 'retention_failed' for consecutive transfers under `rule`."""
    if rule is None:
        return "ok"
    for a, b in zip(txs, txs[1:]):
        if a.currency != b.currency:
            return "currency_mismatch"
        if not retention_ok(a.amount, b.amount, rule["min_bps"], rule["max_bps"]):
            return "retention_failed"
    return "ok"


def parse_rows_to_txs(tx_rows: Sequence[Sequence[Any]], accounts: Sequence[str]) -> Optional[List[TxRecord]]:
    """Build TxRecords from `[id, amount, currency, timestamp]` rows and the ordered account sequence
    (len(tx_rows)+1 accounts: sender of the first transfer, then each receiver). None if data is incomplete."""
    if len(accounts) != len(tx_rows) + 1 or any(a is None for a in accounts):
        return None
    out = []
    for i, r in enumerate(tx_rows):
        tid, amount, ccy, ts = r
        if tid is None or amount is None or ccy is None or ts is None:
            return None
        out.append(TxRecord(tid, accounts[i], accounts[i + 1], int(amount), ccy, int(ts)))
    return out
