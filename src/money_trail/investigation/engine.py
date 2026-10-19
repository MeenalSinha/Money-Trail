"""Investigation engine: anchor -> scope -> enabled detectors -> dedupe -> evidence check -> persist.

The engine works only from graph records and detection rules; it never imports the evaluation package. Outcomes are explicit
and distinct: patterns_detected, no_pattern_found, incomplete (search limits hit, nothing found),
anchor_not_found, evidence_validation_failed, database_error. Configuration problems are raised
by the loader before an engine exists.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from redis import exceptions as redis_exc

from money_trail.database import FalkorDBConnection, GraphStore
from money_trail.exceptions import ConfigurationError, MoneyTrailError, ValidationError
from money_trail.investigation.config import DetectionConfig
from money_trail.investigation.detectors import base as b
from money_trail.investigation.detectors.models import Detection, PATTERN_ORDER, Scope
from money_trail.investigation.detectors.registry import DETECTORS
from money_trail.investigation.evidence import validate_detections
from money_trail.investigation.findings import persist_investigation

ANCHOR_TYPES = ("transaction", "account", "dataset")
DB_ERRORS = (MoneyTrailError, redis_exc.RedisError)


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class InvestigationRequest:
    anchor_type: str
    anchor_id: Optional[str] = None      # transaction or account id (canonical, namespaced)
    namespace: Optional[str] = None      # required for dataset scope

    def __post_init__(self):
        if self.anchor_type not in ANCHOR_TYPES:
            raise ValidationError(f"anchor_type must be one of: {', '.join(ANCHOR_TYPES)}.")
        if self.anchor_type == "dataset":
            if not self.namespace:
                raise ValidationError("a dataset-wide investigation needs a namespace.")
        elif not self.anchor_id:
            raise ValidationError(f"a {self.anchor_type} investigation needs an anchor id.")


@dataclass
class InvestigationResult:
    status: str
    request: InvestigationRequest
    namespace: Optional[str] = None
    complete: bool = True
    detections: List[Detection] = field(default_factory=list)
    detector_reports: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    evidence_failures: List[Dict[str, Any]] = field(default_factory=list)
    persistence: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    rule_fingerprint: str = ""
    duration_ms: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status, "complete": self.complete,
            "anchor": {"type": self.request.anchor_type, "id": self.request.anchor_id,
                       "namespace": self.namespace or self.request.namespace},
            "rule_fingerprint": self.rule_fingerprint,
            "detections": [d.to_dict() for d in self.detections],
            "detectors": self.detector_reports, "evidence_failures": self.evidence_failures,
            "persistence": self.persistence, "error": self.error, "duration_ms": self.duration_ms,
        }


class InvestigationEngine:
    def __init__(self, conn: FalkorDBConnection, config: DetectionConfig,
                 clock: Callable[[], int] = now_ms) -> None:
        self.conn, self.config, self.clock = conn, config, clock
        self.store = GraphStore(conn)
        timeout = config.limits.query_timeout_ms
        sock = conn.settings.socket_timeout
        if timeout is not None and timeout >= sock * 1000:
            raise ConfigurationError(
                f"limits.query_timeout_ms ({timeout}) must be smaller than FALKORDB_SOCKET_TIMEOUT ({sock:g} s): "
                "otherwise a slow search ends as a client socket error instead of an incomplete-search report.")

    # ---- scope -----------------------------------------------------------------------
    def _resolve_scope(self, req: InvestigationRequest) -> Optional[Scope]:
        lim = self.config.limits
        if req.anchor_type == "dataset":
            if self.store.count_matching("Transaction", "dataset", req.namespace) == 0:
                return None
            return Scope("dataset", None, req.namespace)
        if req.anchor_type == "transaction":
            node = self.store.fetch_nodes("Transaction", [req.anchor_id]).get(req.anchor_id)
            if node is None:
                return None
            senders, receivers = self.store.fetch_transaction_endpoints([req.anchor_id]).get(req.anchor_id, (set(), set()))
            return Scope("transaction", req.anchor_id, node.get("dataset"), (req.anchor_id,),
                         next(iter(senders)) if len(senders) == 1 else None,
                         next(iter(receivers)) if len(receivers) == 1 else None)
        node = self.store.fetch_nodes("Account", [req.anchor_id]).get(req.anchor_id)
        if node is None:
            return None
        rows = self.conn.graph.ro_query(
            "MATCH (a:Account {account_id: $a})-[:SENT]->(t:Transaction) RETURN t.transaction_id "
            "UNION MATCH (t:Transaction)-[:RECEIVED_BY]->(a:Account {account_id: $a}) RETURN t.transaction_id",
            {"a": req.anchor_id}).result_set
        ids = sorted({r[0] for r in rows})
        truncated = len(ids) > lim.max_anchor_transactions
        return Scope("account", req.anchor_id, node.get("dataset"), tuple(ids[:lim.max_anchor_transactions]),
                     anchor_truncated=truncated)

    # ---- run -------------------------------------------------------------------------
    def _select_detectors(self, detectors: Optional[Sequence[str]], persist: bool) -> Tuple[str, ...]:
        """All enabled detectors by default; an explicit subset must name enabled detectors (no free names)."""
        enabled = self.config.enabled_names()
        if detectors is None:
            return enabled
        chosen = tuple(dict.fromkeys(detectors))
        if not chosen:
            raise ValidationError("detectors must name at least one detector when given.")
        bad = [n for n in chosen if n not in enabled]
        if bad:
            raise ValidationError(f"unsupported or disabled detector(s): {', '.join(map(str, bad))}. "
                                  f"Enabled: {', '.join(enabled)}.")
        if persist and set(chosen) != set(enabled):
            raise ValidationError("persisting requires every enabled detector to run: the investigation identity "
                                  "covers the whole enabled rule set.")
        return tuple(n for n in enabled if n in chosen)

    def investigate(self, req: InvestigationRequest, *, persist: bool = False,
                    detectors: Optional[Sequence[str]] = None) -> InvestigationResult:
        names = self._select_detectors(detectors, persist)
        if persist and req.anchor_type != "transaction":
            raise ValidationError("Only transaction-anchored investigations can be persisted: the schema links an "
                                  "Investigation to its starting Transaction, and none is invented for accounts or datasets.")
        t0 = time.perf_counter()
        result = InvestigationResult("no_pattern_found", req, rule_fingerprint=self.config.fingerprint())
        try:
            scope = self._resolve_scope(req)
            if scope is None:
                result.status, result.complete = "anchor_not_found", False
                return self._done(result, t0)
            result.namespace = scope.namespace
            scope_warnings = []
            if scope.namespace is None:
                scope_warnings.append("anchor record has no dataset property")
            if self.config.limits.query_timeout_ms is None:
                scope_warnings.append("no query_timeout_ms configured: a very slow search will surface as a socket error")
            if scope_warnings:
                result.detector_reports["_scope"] = {"warnings": scope_warnings}

            reader = b.GraphReader(self.conn, self.config.limits.query_timeout_ms)
            collected: Dict[str, Detection] = {}
            truncated_any = scope.anchor_truncated
            for name in names:
                det = DETECTORS[name](self.config.settings[name], self.config.limits, reader,
                                      self.config.rule_version(name), self.clock)
                before = reader.snapshot()
                out = det.detect(scope)
                after = reader.snapshot()
                truncated_any = truncated_any or out.truncated
                result.detector_reports[name] = {
                    "rule_version": self.config.rule_version(name), "detections": len(out.detections),
                    "truncated": out.truncated, "warnings": out.warnings,
                    "queries": after["queries"] - before["queries"],
                    "rows_read": after["rows_read"] - before["rows_read"],
                    "query_seconds": round(after["query_seconds"] - before["query_seconds"], 4)}
                for d in out.detections:
                    collected.setdefault(d.detection_id, d)

            ordered = sorted(collected.values(), key=lambda d: (PATTERN_ORDER.index(d.pattern_type), d.detection_id))
            valid, failures = validate_detections(self.store, ordered)
            result.detections, result.evidence_failures = valid, failures
            result.complete = not truncated_any

            if persist and valid:
                result.persistence = persist_investigation(
                    self.store, config=self.config, namespace=scope.namespace or "", anchor_tx=req.anchor_id,
                    detections=valid, now_ms=self.clock())
            if failures:
                result.status = "evidence_validation_failed"
            elif valid:
                result.status = "patterns_detected"
            elif truncated_any:
                result.status = "incomplete"
            else:
                result.status = "no_pattern_found"
        except DB_ERRORS as exc:
            result.status, result.complete = "database_error", False
            result.error = f"{type(exc).__name__}: {str(exc)[:200]}"
        return self._done(result, t0)

    @staticmethod
    def _done(result: InvestigationResult, t0: float) -> InvestigationResult:
        result.duration_ms = int((time.perf_counter() - t0) * 1000)
        return result
