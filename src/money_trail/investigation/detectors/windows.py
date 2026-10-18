"""Sliding-window logic shared by fan-in and fan-out (pure functions, no database access).

Window definition (exact): for transactions sorted by (timestamp, id), the window starting at
transaction i contains every later-or-equal transaction j with timestamp[j] - timestamp[i] <= window_ms
(the end point is inclusive). Only maximal windows are examined: a window is skipped when it adds
nothing to the previous window's end (its content is a subset). A maximal window qualifies when it
holds at least `min_transactions` transfers from at least `min_counterparties` distinct counterparty
accounts (and, if configured, a total amount of at least `min_total_minor` in the configured currency).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from money_trail.investigation.detectors.models import TxRecord


@dataclass(frozen=True)
class WindowHit:
    start: int      # index into the filtered, sorted list
    end: int        # inclusive
    txs: Tuple[TxRecord, ...]
    counterparties: Tuple[str, ...]
    total_minor: Optional[int]


def find_windows(txs: Sequence[TxRecord], *, focus: str, role: str, window_ms: int, min_counterparties: int,
                 min_transactions: int, amount_currency: Optional[str] = None,
                 min_total_minor: Optional[int] = None) -> Tuple[List[WindowHit], Dict[str, int]]:
    """Return qualifying maximal windows around `focus`.

    role "destination": focus receives; the counterparty of each transfer is its source (fan-in).
    role "source": focus sends; the counterparty is its destination (fan-out).
    Transfers where the counterparty equals the focus account are excluded. When an amount rule is
    configured only transfers in its currency participate; the others are counted, never converted.
    """
    excluded = {"self_transfers": 0, "other_currency": 0}
    rows: List[Tuple[TxRecord, str]] = []
    for t in sorted(txs, key=lambda t: t.sort_key):
        cp = t.source_account if role == "destination" else t.destination_account
        own = t.destination_account if role == "destination" else t.source_account
        if own != focus:
            continue
        if cp == focus:
            excluded["self_transfers"] += 1
            continue
        if amount_currency is not None and t.currency != amount_currency:
            excluded["other_currency"] += 1
            continue
        rows.append((t, cp))

    hits: List[WindowHit] = []
    counts: Counter = Counter()
    total = 0
    j = -1                      # inclusive right edge currently inside the counters
    prev_end = -1
    for i in range(len(rows)):
        while j + 1 < len(rows) and rows[j + 1][0].timestamp - rows[i][0].timestamp <= window_ms:
            j += 1
            counts[rows[j][1]] += 1
            total += rows[j][0].amount
        if j > prev_end:                                  # maximal: reaches further than the previous window
            n = j - i + 1
            ok = len(counts) >= min_counterparties and n >= min_transactions
            if ok and min_total_minor is not None:
                ok = total >= min_total_minor
            if ok:
                members = tuple(r[0] for r in rows[i:j + 1])
                hits.append(WindowHit(i, j, members, tuple(sorted(counts)),
                                      total if min_total_minor is not None else None))
        prev_end = max(prev_end, j)
        # slide: drop transaction i from the counters
        counts[rows[i][1]] -= 1
        if counts[rows[i][1]] == 0:
            del counts[rows[i][1]]
        total -= rows[i][0].amount
    return hits, excluded
