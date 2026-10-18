"""Shared implementation of fan-in and fan-out detection (direction is the only difference)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from money_trail.investigation.detectors import base as b
from money_trail.investigation.detectors.models import DetectorOutput, Scope, TxRecord
from money_trail.investigation.detectors.settings_util import MAX_MS, no_extra, req_int
from money_trail.investigation.detectors.windows import find_windows
from money_trail.exceptions import DatasetConfigError


@dataclass(frozen=True)
class FanSettings:
    min_distinct_counterparties: int
    min_transactions: int
    window_ms: int
    min_total_amount: Optional[Dict[str, Any]]    # {"currency": "USD", "minor": N} or None

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def parse_fan_settings(raw: Dict[str, Any], where: str) -> FanSettings:
    no_extra(raw, ("min_distinct_counterparties", "min_transactions", "window_ms", "min_total_amount"), where)
    cps = req_int(raw, "min_distinct_counterparties", where, 2, 1_000_000)
    n = req_int(raw, "min_transactions", where, 2, 10_000_000)
    if n < cps:
        raise DatasetConfigError(f"{where}.min_transactions must be at least min_distinct_counterparties.")
    window = req_int(raw, "window_ms", where, 0, MAX_MS)
    if "min_total_amount" not in raw:
        raise DatasetConfigError(f"{where}.min_total_amount is required (use null for no amount criterion).")
    rule = raw["min_total_amount"]
    if rule is not None:
        if not isinstance(rule, dict):
            raise DatasetConfigError(f"{where}.min_total_amount must be null or {{currency, minor}}.")
        no_extra(rule, ("currency", "minor"), f"{where}.min_total_amount")
        cur = rule.get("currency")
        if not (isinstance(cur, str) and len(cur) == 3 and cur.isupper() and cur.isalpha()):
            raise DatasetConfigError(f"{where}.min_total_amount.currency must be a 3-letter upper-case code.")
        rule = {"currency": cur, "minor": req_int(rule, "minor", f"{where}.min_total_amount", 0, 2**62)}
    return FanSettings(cps, n, window, rule)


class FanDetector(b.Detector):
    role = ""            # "destination" (fan-in) or "source" (fan-out)
    focus_role_name = ""  # "collector" / "distributor"
    counterparty_name = ""

    # ---- graph queries ------------------------------------------------------------------
    def _focus_edge(self) -> str:
        """Pattern matching a focus account and the transfers it is the destination/source of."""
        if self.role == "destination":
            return (f"(c:{b.ACC_LABEL} {{{b.ACC_ID}: $acc}})<-[:{b.RECV}]-(t:{b.TX_LABEL})<-[:{b.SENT}]-(o:{b.ACC_LABEL})")
        return (f"(c:{b.ACC_LABEL} {{{b.ACC_ID}: $acc}})-[:{b.SENT}]->(t:{b.TX_LABEL})-[:{b.RECV}]->(o:{b.ACC_LABEL})")

    def _fetch(self, account: str) -> List[TxRecord]:
        rows = self.reader.run(
            f"MATCH {self._focus_edge()} RETURN t.{b.TX_ID}, o.{b.ACC_ID}, t.{b.P_AMOUNT}, t.{b.P_CCY}, t.{b.P_TS} "
            f"ORDER BY t.{b.P_TS}, t.{b.TX_ID} LIMIT $cap", {"acc": account, "cap": self.limits.max_candidate_paths + 1})
        out = []
        for tid, other, amount, ccy, ts in rows:
            if None in (tid, other, amount, ccy, ts):
                continue
            src, dst = (other, account) if self.role == "destination" else (account, other)
            out.append(TxRecord(tid, src, dst, int(amount), ccy, int(ts)))
        return out

    def _candidate_accounts(self, namespace: str, out: DetectorOutput) -> List[str]:
        s = self.settings
        if self.role == "destination":
            match = f"(o:{b.ACC_LABEL})-[:{b.SENT}]->(t:{b.TX_LABEL})-[:{b.RECV}]->(c:{b.ACC_LABEL})"
        else:
            match = f"(c:{b.ACC_LABEL})-[:{b.SENT}]->(t:{b.TX_LABEL})-[:{b.RECV}]->(o:{b.ACC_LABEL})"
        rows = self.reader.run(
            f"MATCH {match} WHERE t.{b.P_DATASET} = $ns AND o.{b.ACC_ID} <> c.{b.ACC_ID} "
            f"WITH c, count(DISTINCT o) AS cps, count(t) AS n WHERE cps >= $mc AND n >= $mt "
            f"RETURN c.{b.ACC_ID} ORDER BY n DESC, c.{b.ACC_ID} LIMIT $cap",
            {"ns": namespace, "mc": s.min_distinct_counterparties, "mt": s.min_transactions,
             "cap": self.limits.max_candidate_paths + 1})
        accounts = [r[0] for r in rows]
        if len(accounts) > self.limits.max_candidate_paths:
            accounts = accounts[:self.limits.max_candidate_paths]
            out.truncated = True
            out.warnings.append("candidate accounts exceeded max_candidate_paths; search is incomplete")
        return accounts

    # ---- detection ----------------------------------------------------------------------
    def detect(self, scope: Scope) -> DetectorOutput:
        s, lim, out = self.settings, self.limits, DetectorOutput()
        found: Dict[str, Any] = {}
        try:
            if scope.anchor_type == "dataset":
                focus_accounts = self._candidate_accounts(scope.namespace, out)
            elif scope.anchor_type == "account":
                focus_accounts = [scope.anchor_id]
            else:
                focus = scope.tx_destination if self.role == "destination" else scope.tx_source
                if focus is None:
                    out.warnings.append("anchor transaction has no unique "
                                        f"{'destination' if self.role == 'destination' else 'source'} account; "
                                        "this detector was not run")
                    return self._finish(scope, {}, out)
                focus_accounts = [focus]
            for acc in focus_accounts:
                self._scan(acc, scope, out, found)
        except b.SearchTimeout as exc:
            out.truncated = True
            out.warnings.append(f"{exc}; results are incomplete")
        return self._finish(scope, found, out)

    def _scan(self, account: str, scope: Scope, out: DetectorOutput, found: Dict[str, Any]) -> None:
        s, lim = self.settings, self.limits
        txs = self._fetch(account)
        if len(txs) > lim.max_candidate_paths:
            txs = txs[:lim.max_candidate_paths]
            out.truncated = True
            out.warnings.append("transfers of one account exceeded max_candidate_paths; search is incomplete")
        rule = s.min_total_amount
        hits, excluded = find_windows(
            txs, focus=account, role=self.role, window_ms=s.window_ms,
            min_counterparties=s.min_distinct_counterparties, min_transactions=s.min_transactions,
            amount_currency=rule["currency"] if rule else None, min_total_minor=rule["minor"] if rule else None)
        if excluded["other_currency"]:
            out.warnings.append(f"{excluded['other_currency']} transfer(s) in other currencies ignored "
                                "(amounts are never converted)")
        anchors = set(scope.anchor_tx_ids) if scope.anchor_type == "transaction" else set()
        for h in hits:
            if anchors and not anchors & {t.transaction_id for t in h.txs}:
                continue
            members = list(h.txs)
            warn, trunc = [], False
            if len(members) > lim.max_evidence_transactions:
                members = members[:lim.max_evidence_transactions]
                warn.append(f"evidence truncated to max_evidence_transactions={lim.max_evidence_transactions}")
                trunc = True
                out.truncated = True
            criteria = {"configured": s.as_dict(),
                        "observed": {"distinct_counterparties": len(h.counterparties),
                                     "transactions": len(h.txs),
                                     "window_span_ms": h.txs[-1].timestamp - h.txs[0].timestamp,
                                     **({"total_minor": h.total_minor, "currency": rule["currency"]} if rule else {})}}
            accounts = [account] + list(h.counterparties)
            d = self._build(scope, members, account, criteria, accounts,
                            {self.focus_role_name: account, self.counterparty_name: list(h.counterparties)},
                            warn, trunc)
            found[d.detection_id] = d
