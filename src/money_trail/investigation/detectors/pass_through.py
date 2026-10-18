"""Rapid pass-through detector: maximal chronologically ordered transfer chains.

FalkorDB finds the candidate chains with staged queries (one MATCH ... WHERE per hop, so chronology, gap
and window filters prune at every hop). Python verifies the exact amount-retention rule, requires currency
compatibility before comparing amounts, and keeps only maximal chains so a longer qualifying chain
does not also yield each of its sub-chains.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from money_trail.investigation.detectors import base as b
from money_trail.investigation.detectors.models import DetectorOutput, Scope, TxRecord
from money_trail.investigation.detectors.settings_util import (
    MAX_MS, hops_range, no_extra, nullable_retention, req_bool, req_int,
)


@dataclass(frozen=True)
class PassThroughSettings:
    min_hops: int
    max_hops: int
    max_gap_ms: int
    max_total_window_ms: int
    allow_repeated_accounts: bool
    retention: Optional[Dict[str, int]]

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def maximal_only(chains: Sequence[Sequence[TxRecord]]) -> List[Sequence[TxRecord]]:
    """Drop any chain that appears as a contiguous run inside a longer qualifying chain."""
    keyed = sorted({tuple(t.transaction_id for t in c): c for c in chains}.items(), key=lambda kv: (-len(kv[0]), kv[0]))
    kept: List[Tuple[str, ...]] = []
    result = []
    for ids, chain in keyed:
        n = len(ids)
        if any(len(k) > n and any(k[i:i + n] == ids for i in range(len(k) - n + 1)) for k in kept):
            continue
        kept.append(ids)
        result.append(chain)
    return result


def chains_from_rows(rows: Sequence[Sequence[Any]], s: PassThroughSettings) -> Tuple[List[List[TxRecord]], Dict[str, int]]:
    """Normalize raw rows `(origin, tx_rows, interior_accounts, final_account)`; returns qualifying chains."""
    skipped = {"incomplete_data": 0, "currency_mismatch": 0, "retention_failed": 0, "repeated_accounts": 0}
    out: List[List[TxRecord]] = []
    for origin, tx_rows, accs, final in rows:
        seq = [origin] + list(accs) + [final]
        txs = b.parse_rows_to_txs(tx_rows, seq)
        if txs is None:
            skipped["incomplete_data"] += 1
            continue
        if not s.allow_repeated_accounts and len(set(seq)) != len(seq):
            skipped["repeated_accounts"] += 1
            continue
        verdict = b.chain_amount_check(txs, s.retention)
        if verdict != "ok":
            skipped[verdict] += 1
            continue
        out.append(txs)
    return out, skipped


class PassThroughDetector(b.Detector):
    name = "pass_through"

    @staticmethod
    def parse_settings(raw: Dict[str, Any], where: str) -> PassThroughSettings:
        no_extra(raw, ("min_hops", "max_hops", "max_gap_ms", "max_total_window_ms",
                       "allow_repeated_accounts", "retention"), where)
        lo, hi = hops_range(raw, where, 2)
        return PassThroughSettings(
            lo, hi, req_int(raw, "max_gap_ms", where, 0, MAX_MS),
            req_int(raw, "max_total_window_ms", where, 0, MAX_MS),
            req_bool(raw, "allow_repeated_accounts", where),
            nullable_retention(raw, "retention", where))

    def detect(self, scope: Scope) -> DetectorOutput:
        s, lim, out = self.settings, self.limits, DetectorOutput()
        try:
            rows = self._collect_rows(scope, out, closing=False)
        except b.SearchTimeout as exc:
            out.truncated = True
            out.warnings.append(f"{exc}; results are incomplete")
            return self._finish(scope, {}, out)
        if scope.anchor_truncated:
            out.truncated = True
            out.warnings.append("account anchor has more incident transactions than max_anchor_transactions")

        chains, skipped = chains_from_rows(rows, s)
        for k, n in skipped.items():
            if n:
                out.warnings.append(f"{n} candidate chain(s) skipped: {k.replace('_', ' ')}")
        anchors = set(scope.anchor_tx_ids)
        found = {}
        for chain in maximal_only(chains):
            if anchors and not anchors & {t.transaction_id for t in chain}:
                continue
            gaps = [b2.timestamp - a.timestamp for a, b2 in zip(chain, chain[1:])]
            criteria = {"configured": s.as_dict(),
                        "observed": {"transactions": len(chain), "accounts": len(chain) + 1,
                                     "total_window_ms": chain[-1].timestamp - chain[0].timestamp,
                                     "max_gap_ms": max(gaps) if gaps else 0, "maximal": True}}
            accounts = [chain[0].source_account] + [t.destination_account for t in chain]
            warn, trunc = [], False
            if len(chain) > lim.max_evidence_transactions:
                warn.append("evidence list exceeds max_evidence_transactions")
                trunc = True
            d = self._build(scope, chain, None, criteria, accounts,
                            {"origin": accounts[0], "destination": accounts[-1], "intermediaries": accounts[1:-1]},
                            warn, trunc)
            found[d.detection_id] = d
        return self._finish(scope, found, out)
