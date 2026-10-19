"""Coordination models, role definitions, and state representations.

Defines the explicit state machines, agent roles, session records, task nodes,
and handoff models for graph-based multi-agent coordination.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Set

from money_trail.database import graph_schema as gs
from money_trail.exceptions import ValidationError


class TaskStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentRole(str, Enum):
    COORDINATOR = "coordinator"
    INVESTIGATOR = "investigator"
    AUDITOR = "auditor"
    MEMORY_CURATOR = "memory_curator"


class SessionStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class HandoffStatus(str, Enum):
    INITIATED = "initiated"
    TRANSFERRED = "transferred"
    ACCEPTED = "accepted"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"


def _now_ms() -> int:
    return int(time.time() * 1000)


def stable_session_id(investigation_id: str, seed: Optional[str] = None) -> str:
    salt = seed or str(_now_ms())
    h = hashlib.sha256(f"sess:{investigation_id}:{salt}".encode("utf-8")).hexdigest()[:16]
    return f"sess_{h}"


def stable_task_id(investigation_id: str, task_type: str, idempotency_key: str) -> str:
    h = hashlib.sha256(f"task:{investigation_id}:{task_type}:{idempotency_key}".encode("utf-8")).hexdigest()[:20]
    return f"task_{h}"


def stable_handoff_id(investigation_id: str, task_id: str, source_role: str, target_role: str, idempotency_key: str) -> str:
    h = hashlib.sha256(f"hnd:{investigation_id}:{task_id}:{source_role}:{target_role}:{idempotency_key}".encode("utf-8")).hexdigest()[:20]
    return f"hnd_{h}"


@dataclass
class AgentSessionRecord:
    session_id: str
    investigation_id: str
    status: str
    started_at: int
    dataset: Optional[str] = None
    updated_at: Optional[int] = None
    completed_at: Optional[int] = None
    metadata_json: Optional[str] = None

    def validate(self) -> None:
        if self.status not in gs.SESSION_STATUSES:
            raise ValidationError(f"Invalid session status '{self.status}'; allowed: {gs.SESSION_STATUSES}")
        if not self.session_id or not self.session_id.strip():
            raise ValidationError("Session ID must not be empty.")
        if not self.investigation_id or not self.investigation_id.strip():
            raise ValidationError("Investigation ID must not be empty.")

    def as_graph_props(self) -> Dict[str, Any]:
        self.validate()
        p: Dict[str, Any] = {
            "session_id": self.session_id,
            "investigation_id": self.investigation_id,
            "status": self.status,
            "started_at": self.started_at,
        }
        if self.dataset:
            p["dataset"] = self.dataset
        if self.updated_at is not None:
            p["updated_at"] = self.updated_at
        if self.completed_at is not None:
            p["completed_at"] = self.completed_at
        if self.metadata_json:
            p["metadata_json"] = self.metadata_json
        return p


@dataclass
class AgentTaskRecord:
    task_id: str
    task_type: str
    status: str
    investigation_id: str
    created_at: int
    session_id: Optional[str] = None
    assigned_role: Optional[str] = None
    assigned_agent_id: Optional[str] = None
    attempt_count: int = 0
    idempotency_key: Optional[str] = None
    updated_at: Optional[int] = None
    completed_at: Optional[int] = None
    result_summary: Optional[str] = None
    error_detail: Optional[str] = None
    dependencies: List[str] = field(default_factory=list)

    def validate(self) -> None:
        if self.status not in gs.TASK_STATUSES:
            raise ValidationError(f"Invalid task status '{self.status}'; allowed: {gs.TASK_STATUSES}")
        if self.assigned_role and self.assigned_role not in gs.AGENT_ROLES:
            raise ValidationError(f"Invalid assigned_role '{self.assigned_role}'; allowed: {gs.AGENT_ROLES}")
        if not self.task_id or not self.task_id.strip():
            raise ValidationError("Task ID must not be empty.")
        if not self.investigation_id or not self.investigation_id.strip():
            raise ValidationError("Investigation ID must not be empty.")
        if not self.task_type or not self.task_type.strip():
            raise ValidationError("Task type must not be empty.")

    def as_graph_props(self) -> Dict[str, Any]:
        self.validate()
        p: Dict[str, Any] = {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "status": self.status,
            "investigation_id": self.investigation_id,
            "created_at": self.created_at,
        }
        if self.session_id:
            p["session_id"] = self.session_id
        if self.assigned_role:
            p["assigned_role"] = self.assigned_role
        if self.assigned_agent_id:
            p["assigned_agent_id"] = self.assigned_agent_id
        if self.attempt_count is not None:
            p["attempt_count"] = int(self.attempt_count)
        if self.idempotency_key:
            p["idempotency_key"] = self.idempotency_key
        if self.updated_at is not None:
            p["updated_at"] = self.updated_at
        if self.completed_at is not None:
            p["completed_at"] = self.completed_at
        if self.result_summary:
            p["result_summary"] = self.result_summary
        if self.error_detail:
            p["error_detail"] = self.error_detail
        return p


@dataclass
class AgentHandoffRecord:
    handoff_id: str
    investigation_id: str
    task_id: str
    source_agent_id: str
    target_role: str
    status: str
    created_at: int
    target_agent_id: Optional[str] = None
    reason: Optional[str] = None
    context_summary: Optional[str] = None
    idempotency_key: Optional[str] = None
    updated_at: Optional[int] = None
    completed_at: Optional[int] = None
    evidence_refs: List[str] = field(default_factory=list)

    def validate(self) -> None:
        if self.status not in gs.HANDOFF_STATUSES:
            raise ValidationError(f"Invalid handoff status '{self.status}'; allowed: {gs.HANDOFF_STATUSES}")
        if self.target_role not in gs.AGENT_ROLES:
            raise ValidationError(f"Invalid target_role '{self.target_role}'; allowed: {gs.AGENT_ROLES}")
        if not self.handoff_id or not self.handoff_id.strip():
            raise ValidationError("Handoff ID must not be empty.")
        if not self.investigation_id or not self.investigation_id.strip():
            raise ValidationError("Investigation ID must not be empty.")
        if not self.task_id or not self.task_id.strip():
            raise ValidationError("Task ID must not be empty.")
        if not self.source_agent_id or not self.source_agent_id.strip():
            raise ValidationError("Source agent ID must not be empty.")

    def as_graph_props(self) -> Dict[str, Any]:
        self.validate()
        p: Dict[str, Any] = {
            "handoff_id": self.handoff_id,
            "investigation_id": self.investigation_id,
            "task_id": self.task_id,
            "source_agent_id": self.source_agent_id,
            "target_role": self.target_role,
            "status": self.status,
            "created_at": self.created_at,
        }
        if self.target_agent_id:
            p["target_agent_id"] = self.target_agent_id
        if self.reason:
            p["reason"] = self.reason
        if self.context_summary:
            p["context_summary"] = self.context_summary
        if self.idempotency_key:
            p["idempotency_key"] = self.idempotency_key
        if self.updated_at is not None:
            p["updated_at"] = self.updated_at
        if self.completed_at is not None:
            p["completed_at"] = self.completed_at
        return p
