"""Structured investigation request and its validation.

Outcomes (`RequestStatus`) are distinct: invalid request, missing anchor, unknown anchor, valid, and valid with an
unsupported intent. Nothing in the question is treated as a verified fact.

Precedence when inputs overlap (documented in docs/agent.md):
  1. The structured anchor (type + id) always decides what is investigated; the question never changes it.
  2. If `namespace` is given and the anchor id does not start with "<namespace>:", the canonical id is
     "<namespace>:<anchor_id>" (the ingestion rule). A namespace alone is not an anchor.
  3. Canonical-looking identifiers in the question that differ from the anchor are reported as warnings; they are
     not investigated.
  4. A structured `intent` overrides anything the question suggests. An unsupported structured intent is refused
     deterministically. Free-text intent is assessed by the model and recorded; it cannot widen what tools can do.
  5. Explicit operational limits may only lower the configured limits.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from money_trail.agent.config import LoopConfig
from money_trail.agent.tools.schema import ID_PATTERN
from money_trail.database import graph_schema as gs
from money_trail.exceptions import ValidationError

ANCHOR_TYPES = ("transaction", "account")
_ANCHOR_NODES = {"transaction": next(n for n in gs.NODES if n.label == "Transaction"),
                "account": next(n for n in gs.NODES if n.label == "Account")}
SUPPORTED_INTENTS = ("pattern_review", "evidence_verification")
DEFAULT_INTENT = "pattern_review"
LIMIT_KEYS = ("max_steps", "max_tool_calls", "max_duration_seconds")
MAX_QUESTION_CHARS = 2000
_KEYS = ("anchor_type", "anchor_id", "question", "intent", "detection_config", "namespace", "limits", "request_id")
_NS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_REQ_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_QID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}:[A-Za-z0-9][A-Za-z0-9_.-]*[A-Za-z0-9]")


class RequestStatus:
    VALID = "valid"
    INVALID = "invalid_request"
    MISSING_ANCHOR = "missing_anchor"
    UNKNOWN_ANCHOR = "unknown_anchor"
    UNSUPPORTED_INTENT = "valid_unsupported_intent"


@dataclass(frozen=True)
class AgentRequest:
    anchor_type: str
    anchor_id: str                              # canonical (namespaced) identifier
    question: Optional[str] = None
    intent: str = DEFAULT_INTENT
    detection_config: Optional[str] = None
    namespace: Optional[str] = None
    limits: Mapping[str, float] = field(default_factory=dict)
    request_id: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"anchor_type": self.anchor_type, "anchor_id": self.anchor_id, "question": self.question,
                "intent": self.intent, "detection_config": self.detection_config, "namespace": self.namespace,
                "limits": dict(self.limits), "request_id": self.request_id}

    def effective_loop(self, loop: LoopConfig) -> LoopConfig:
        """The configured loop limits, lowered by the request's explicit limits. Raising a limit is an error."""
        from dataclasses import replace
        over = {}
        for k, v in self.limits.items():
            cap = getattr(loop, k)
            if v > cap:
                raise ValidationError(f"request limit {k}={v} exceeds the configured maximum {cap}.")
            over[k] = v
        return replace(loop, **over)


@dataclass
class RequestValidation:
    status: str
    problems: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    request: Optional[AgentRequest] = None

    @property
    def runnable(self) -> bool:
        return self.status in (RequestStatus.VALID,) and self.request is not None


def _invalid(*problems: str) -> RequestValidation:
    return RequestValidation(RequestStatus.INVALID, list(problems))


def validate_request(raw: Any, conn=None) -> RequestValidation:
    """Validate a request mapping. With `conn`, also checks that the anchor exists (read-only lookup)."""
    if not isinstance(raw, Mapping):
        return _invalid("the request must be an object")
    extra = sorted(set(raw) - set(_KEYS))
    if extra:
        return _invalid(f"unknown request fields: {', '.join(extra)}")

    atype, aid = raw.get("anchor_type"), raw.get("anchor_id")
    if aid is None or (isinstance(aid, str) and not aid.strip()):
        return RequestValidation(RequestStatus.MISSING_ANCHOR,
                                 ["an anchor is required: anchor_type and anchor_id (a transaction or an account)"])
    if atype not in ANCHOR_TYPES:
        return _invalid(f"anchor_type must be one of: {', '.join(ANCHOR_TYPES)}")
    if not isinstance(aid, str) or not ID_PATTERN.match(aid):
        return _invalid("anchor_id must be a well-formed identifier string")

    problems: List[str] = []
    ns = raw.get("namespace")
    if ns is not None and (not isinstance(ns, str) or not _NS_RE.match(ns)):
        problems.append("namespace must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
    q = raw.get("question")
    if q is not None and (not isinstance(q, str) or len(q) > MAX_QUESTION_CHARS or
                          any(ord(c) < 32 and c not in "\n\t" for c in q) or not q.strip()):
        problems.append(f"question must be non-empty text of at most {MAX_QUESTION_CHARS} characters without control characters")
    intent = raw.get("intent", DEFAULT_INTENT)
    if not isinstance(intent, str) or not intent.strip() or len(intent) > 64:
        problems.append("intent must be a short string")
    cfg = raw.get("detection_config")
    if cfg is not None and (not isinstance(cfg, str) or not cfg.strip()):
        problems.append("detection_config must be a path string")
    rid = raw.get("request_id")
    if rid is not None and (not isinstance(rid, str) or not _REQ_ID_RE.match(rid)):
        problems.append("request_id must match [A-Za-z0-9_.:-]{1,64}")
    limits = raw.get("limits") or {}
    clean_limits: Dict[str, float] = {}
    if not isinstance(limits, Mapping):
        problems.append("limits must be an object")
    else:
        for k, v in limits.items():
            if k not in LIMIT_KEYS:
                problems.append(f"limits.{k} is not a permitted limit ({', '.join(LIMIT_KEYS)})")
            elif isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0 or \
                    (k != "max_duration_seconds" and not isinstance(v, int)):
                problems.append(f"limits.{k} must be a positive {'number' if k == 'max_duration_seconds' else 'integer'}")
            else:
                clean_limits[k] = v
    if problems:
        return _invalid(*problems)

    canonical = aid if (ns is None or aid.startswith(ns + ":")) else f"{ns}:{aid}"
    warnings = []
    mentioned = sorted({m for m in _QID_RE.findall(q or "") if m != canonical and m != aid})
    for m in mentioned[:5]:
        warnings.append(f"the question mentions '{m}', which differs from the anchor; the anchor "
                        f"'{canonical}' takes precedence and '{m}' was not investigated")
    req = AgentRequest(atype, canonical, q, intent, cfg, ns, clean_limits, rid)

    if intent not in SUPPORTED_INTENTS:
        return RequestValidation(RequestStatus.UNSUPPORTED_INTENT,
                                 [f"intent '{intent}' is not supported; supported intents: {', '.join(SUPPORTED_INTENTS)}"],
                                 warnings, req)
    if conn is not None:
        spec = _ANCHOR_NODES[atype]                 # label and id property come from the centralized schema registry
        if not conn.graph.ro_query(f"MATCH (n:{spec.label} {{{spec.id_property}: $id}}) RETURN 1 LIMIT 1", {"id": canonical}).result_set:
            return RequestValidation(RequestStatus.UNKNOWN_ANCHOR, [f"{atype} '{canonical}' does not exist in the graph"],
                                     warnings, req)
    return RequestValidation(RequestStatus.VALID, [], warnings, req)
