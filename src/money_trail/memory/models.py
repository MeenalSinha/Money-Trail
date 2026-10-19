"""Persistent graph memory models and identities.

Defines memory types (episodic, semantic), scopes (investigation, dataset),
lifecycle statuses, and deterministic identity policies.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

from money_trail.database import graph_schema as gs
from money_trail.exceptions import ValidationError


class MemoryType(str, Enum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"


class MemoryScope(str, Enum):
    INVESTIGATION = "investigation"
    DATASET = "dataset"


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    INVALIDATED = "invalidated"
    REVALIDATION_NEEDED = "revalidation_needed"


def _now_ms() -> int:
    return int(time.time() * 1000)


def stable_memory_id(
    scope: str,
    scope_id: str,
    memory_type: str,
    statement: str,
    provenance_tool: Optional[str] = None,
) -> str:
    """Generate a stable, deterministic memory identity from canonical inputs.

    Never generates random hashes; identical claims in the same scope
    derive the same stable memory id.
    """
    clean_scope = str(scope).strip().lower()
    clean_scope_id = str(scope_id).strip()
    clean_type = str(memory_type).strip().lower()
    clean_stmt = " ".join(statement.strip().split())
    clean_prov = str(provenance_tool or "").strip()

    raw = f"{clean_scope}:{clean_scope_id}:{clean_type}:{clean_stmt}:{clean_prov}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"mem_{digest}"


@dataclass
class MemoryRecord:
    memory_id: str
    memory_type: str
    scope: str
    statement: str
    status: str
    created_at: int
    dataset: Optional[str] = None
    investigation_id: Optional[str] = None
    updated_at: Optional[int] = None
    valid_from: Optional[int] = None
    valid_to: Optional[int] = None
    provenance_tool: Optional[str] = None
    rule_fingerprint: Optional[str] = None
    confidence: Optional[float] = None
    evidence_ids: List[str] = field(default_factory=list)
    superseded_by: Optional[str] = None
    supersedes_id: Optional[str] = None
    contradicting_memory_ids: List[str] = field(default_factory=list)

    @property
    def supersedes(self) -> List[str]:
        return [self.supersedes_id] if self.supersedes_id else []

    @property
    def contradicts(self) -> List[str]:
        return self.contradicting_memory_ids

    def validate(self) -> None:
        if self.memory_type not in gs.MEMORY_TYPES:
            raise ValidationError(
                f"Invalid memory_type '{self.memory_type}'; allowed: {gs.MEMORY_TYPES}"
            )
        if self.scope not in gs.MEMORY_SCOPES:
            raise ValidationError(
                f"Invalid memory scope '{self.scope}'; allowed: {gs.MEMORY_SCOPES}"
            )
        if self.status not in gs.MEMORY_STATUSES:
            raise ValidationError(
                f"Invalid memory status '{self.status}'; allowed: {gs.MEMORY_STATUSES}"
            )
        if not self.statement or not self.statement.strip():
            raise ValidationError("Memory statement must not be empty.")
        if len(self.statement) > 5000:
            raise ValidationError("Memory statement exceeds maximum allowed length of 5000 chars.")
        if self.scope == "dataset" and not self.dataset:
            raise ValidationError("Dataset-scoped memory requires a non-empty dataset identifier.")
        if self.scope == "investigation" and not self.investigation_id:
            raise ValidationError("Investigation-scoped memory requires a non-empty investigation_id.")
        if self.confidence is not None and not (0.0 <= self.confidence <= 1.0):
            raise ValidationError("Memory confidence must be between 0.0 and 1.0.")

    def as_graph_props(self) -> Dict[str, Any]:
        """Return valid node properties for GraphStore."""
        self.validate()
        props: Dict[str, Any] = {
            "memory_id": self.memory_id,
            "memory_type": self.memory_type,
            "scope": self.scope,
            "statement": self.statement,
            "status": self.status,
            "created_at": self.created_at,
        }
        if self.dataset:
            props["dataset"] = self.dataset
        if self.investigation_id:
            props["investigation_id"] = self.investigation_id
        if self.updated_at is not None:
            props["updated_at"] = self.updated_at
        if self.valid_from is not None:
            props["valid_from"] = self.valid_from
        if self.valid_to is not None:
            props["valid_to"] = self.valid_to
        if self.provenance_tool:
            props["provenance_tool"] = self.provenance_tool
        if self.rule_fingerprint:
            props["rule_fingerprint"] = self.rule_fingerprint
        if self.confidence is not None:
            props["confidence"] = float(self.confidence)
        return props

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        return d
