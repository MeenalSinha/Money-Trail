"""Deterministic grounding of model-written text.

The model's prose is never trusted. Each sentence is kept only if
  1. every identifier-like token in it is an identifier the model was actually shown (the ledger),
  2. every number in it is a value that appears in the ledger, the active configuration or the run's own counts,
  3. every timestamp in it matches a ledger timestamp, and
  4. any term from `grounding.forbidden_conclusion_terms` appears only in hedged or negated wording.
Rejected sentences are dropped and recorded with their reasons. This is a defence-in-depth filter with known limits
(docs/agent.md): it cannot judge meaning. The primary protection is structural: findings, evidence, amounts and
classifications are produced by deterministic code and cannot be created or upgraded by model text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Set

from money_trail.agent.config import GroundingConfig
from money_trail.agent.ledger import Ledger
from money_trail.ingestion.contract import format_epoch_ms_iso

_ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?Z?)?\b")
_QID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+@-]*:[A-Za-z0-9_.+@-]*[A-Za-z0-9]")
_BARE = re.compile(r"\b(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{6,}\b")
_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?!\w)")
_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")      # over-splitting is safe: each fragment is checked on its own
_DIVISORS = (1000, 60_000, 3_600_000, 86_400_000)


def _ints(obj: Any) -> Iterable[int]:
    if isinstance(obj, bool):
        return
    if isinstance(obj, int):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _ints(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _ints(v)


def allowed_numbers(ledger: Ledger, extra: Iterable[int] = ()) -> Set[str]:
    vals: Set[int] = set(extra) | set(ledger.counts().values()) | set(ledger.payload_numbers)
    for t in ledger.transactions.values():
        vals |= {v for v in (t.get("amount"), t.get("timestamp")) if isinstance(v, int)}
    for ld in ledger.detections.values():
        d = ld.detection
        vals |= {len(d.transaction_ids), len(d.account_ids), len(ledger.detections)}
        vals |= set(_ints(d.criteria)) | set(_ints(d.roles))
    out: Set[str] = set()
    for v in vals:
        out.add(str(v))
        for dv in _DIVISORS:
            if abs(v) >= dv and v % dv == 0:
                out.add(str(v // dv))
    return out


def allowed_iso(ledger: Ledger) -> Set[str]:
    out = set()
    for t in ledger.transactions.values():
        if isinstance(t.get("timestamp"), int):
            full = format_epoch_ms_iso(t["timestamp"]).rstrip("Z")
            out |= {full, full.rstrip("0").rstrip("."), full[:19], full[:16], full[:10]}
    return out


def _norm_iso(tok: str) -> str:
    return tok.replace(" ", "T").rstrip("Z")


@dataclass
class GroundedText:
    text: str
    sentences: int = 0
    factual: int = 0                    # sentences that contain an identifier, number or timestamp
    factual_grounded: int = 0
    rejected: List[Dict[str, Any]] = field(default_factory=list)


class GroundingChecker:
    def __init__(self, cfg: GroundingConfig, ledger: Ledger, extra_numbers: Iterable[int] = ()) -> None:
        self.cfg = cfg
        self.ids = ledger.identifiers()
        self.locals = {i.split(":", 1)[1] for i in self.ids if ":" in i}
        self.numbers = allowed_numbers(ledger, extra_numbers)
        self.iso = allowed_iso(ledger)
        self._term_res = [(t, re.compile(r"(?<![a-z])" + re.escape(t))) for t in cfg.forbidden_conclusion_terms]
        self._hedges = [re.compile(r"(?<![a-z])" + re.escape(h) + r"(?![a-z])") for h in cfg.hedging_markers]

    def _sentence_problems(self, s: str) -> (List[str], bool):
        reasons: List[str] = []
        factual = False
        low = s.lower()
        rest = s
        for m in _ISO.findall(s):
            factual = True
            if _norm_iso(m) not in self.iso:
                reasons.append(f"timestamp not in retrieved records: {m}")
            rest = rest.replace(m, " ")
        for tok in _QID.findall(rest):
            tok = tok.rstrip(".")
            if not any(c.isalpha() for c in tok) or tok.replace(":", "").isdigit():
                continue
            factual = True
            if tok not in self.ids:
                reasons.append(f"identifier not in retrieved records: {tok}")
            rest = rest.replace(tok, " ")
        for tok in _BARE.findall(rest):
            factual = True
            if tok not in self.ids and tok not in self.locals:
                reasons.append(f"identifier-like token not in retrieved records: {tok}")
            rest = rest.replace(tok, " ")
        for n in _NUM.findall(rest):
            factual = True
            if n.replace(",", "") not in self.numbers:
                reasons.append(f"number not in retrieved records or configuration: {n}")
        hedged = any(h.search(low) for h in self._hedges)
        for term, rx in self._term_res:
            if rx.search(low) and not hedged:
                reasons.append(f"unhedged conclusion term: {term}")
        return reasons, factual

    def check(self, text: str) -> GroundedText:
        text = (text or "")[: self.cfg.max_text_chars]
        out = GroundedText("")
        kept: List[str] = []
        for s in (x.strip() for x in _SPLIT.split(text)):
            if not s:
                continue
            out.sentences += 1
            reasons, factual = self._sentence_problems(s)
            out.factual += factual
            if reasons:
                out.rejected.append({"sentence": s[:300], "reasons": reasons})
            else:
                out.factual_grounded += factual
                kept.append(s)
        out.text = " ".join(kept)
        return out
