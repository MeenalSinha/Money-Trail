"""Circular-transfer detector: bounded, time-respecting directed cycles of transactions.

Graph-native: FalkorDB finds every chain Transaction -> Account -> Transaction ... that returns to the account
which sent the first transaction, using staged queries (one MATCH ... WHERE per hop) so chronology, gap,
window and distinct-account filters prune at every hop. Python then verifies amount criteria exactly, canonicalizes rotations and deduplicates.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from money_trail.investigation.detectors import base as b
from money_trail.investigation.detectors.models import (
    DetectorOutput, Scope, TxRecord, canonical_rotation,
)
from money_trail.investigation.detectors.settings_util import (
    MAX_MS, hops_range, no_extra, nullable_retention, req_bool, req_int,
)


@dataclass(frozen=True)
class CircularSettings:
    min_hops: int
    max_hops: int
    max_gap_ms: int
    max_total_window_ms: int
    allow_repeated_accounts: bool
    amount_retention: Optional[Dict[str, int]]

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def cycles_from_rows(rows: Sequence[Sequence[Any]], s: CircularSettings) -> Tuple[List[List[TxRecord]], Dict[str, int]]:
    """Normalize raw graph rows `(start_account, tx_rows, interior_accounts)` into canonical cycles."""
    skipped = {"incomplete_data": 0, "currency_mismatch": 0, "retention_failed": 0, "repeated_accounts": 0}
    out: List[List[TxRecord]] = []
    for start, tx_rows, accs in rows:
        seq = [start] + list(accs) + [start]
        txs = b.parse_rows_to_txs(tx_rows, seq)
        if txs is None:
            skipped["incomplete_data"] += 1
            continue
        if not s.allow_repeated_accounts and len(set(seq[:-1])) != len(seq) - 1:
            skipped["repeated_accounts"] += 1
            continue
        verdict = b.chain_amount_check(txs, s.amount_retention)
        if verdict != "ok":
            skipped[verdict] += 1
            continue
        out.append(canonical_rotation(txs))
    return out, skipped


class CircularDetector(b.Detector):
    name = "circular"

    @staticmethod
    def parse_settings(raw: Dict[str, Any], where: str) -> CircularSettings:
        no_extra(raw, ("min_hops", "max_hops", "max_gap_ms", "max_total_window_ms",
                       "allow_repeated_accounts", "amount_retention"), where)
        lo, hi = hops_range(raw, where, 2)     # a cycle needs at least two transactions
        return CircularSettings(
            lo, hi, req_int(raw, "max_gap_ms", where, 0, MAX_MS),
            req_int(raw, "max_total_window_ms", where, 0, MAX_MS),
            req_bool(raw, "allow_repeated_accounts", where),
            nullable_retention(raw, "amount_retention", where))

    # ---- detection ---------------------------------------------------------------------
    def detect(self, scope: Scope) -> DetectorOutput:
        s, lim, out = self.settings, self.limits, DetectorOutput()
        try:
            rows = self._collect_rows(scope, out, closing=True)
        except b.SearchTimeout as exc:
            out.truncated = True
            out.warnings.append(f"{exc}; results are incomplete")
            return self._finish(scope, {}, out)
        if scope.anchor_truncated:
            out.truncated = True
            out.warnings.append("account anchor has more incident transactions than max_anchor_transactions")

        cycles, skipped = cycles_from_rows(rows, s)
        for k, n in skipped.items():
            if n:
                out.warnings.append(f"{n} candidate cycle(s) skipped: {k.replace('_', ' ')}")
        anchors = set(scope.anchor_tx_ids)
        found = {}
        for cyc in cycles:
            if anchors and not anchors & {t.transaction_id for t in cyc}:
                continue
            gaps = [b2.timestamp - a.timestamp for a, b2 in zip(cyc, cyc[1:])]
            criteria = {"configured": s.as_dict(),
                        "observed": {"transactions": len(cyc), "distinct_accounts": len({t.source_account for t in cyc}),
                                     "total_window_ms": cyc[-1].timestamp - cyc[0].timestamp,
                                     "max_gap_ms": max(gaps) if gaps else 0, "returns_to_start_account": True}}
            warn, trunc = [], False
            if len(cyc) > lim.max_evidence_transactions:
                warn.append("evidence list exceeds max_evidence_transactions")
                trunc = True
            d = self._build(scope, cyc, None, criteria, [t.source_account for t in cyc],
                            {"start_account": cyc[0].source_account}, warn, trunc)
            found[d.detection_id] = d
        return self._finish(scope, found, out)
