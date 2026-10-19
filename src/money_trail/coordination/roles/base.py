"""Base interface and permission registries for specialized agent roles.

Defines role responsibilities, permitted toolsets, input/output requirements,
and execution interfaces.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set

from money_trail.coordination.models import AgentRole, AgentTaskRecord
from money_trail.database import FalkorDBConnection
from money_trail.exceptions import ValidationError


@dataclass
class RoleSpec:
    role: AgentRole
    description: str
    permitted_tools: Set[str]
    can_write_graph: bool = False
    can_curate_memory: bool = False
    can_audit_evidence: bool = False


ROLE_REGISTRY: Dict[AgentRole, RoleSpec] = {
    AgentRole.COORDINATOR: RoleSpec(
        role=AgentRole.COORDINATOR,
        description="Creates, schedules, and manages task graphs and handoffs across agents.",
        permitted_tools={"query_tasks", "schedule_task", "claim_task", "complete_task", "query_memory"},
        can_write_graph=True,
    ),
    AgentRole.INVESTIGATOR: RoleSpec(
        role=AgentRole.INVESTIGATOR,
        description="Uses graph tools and Phase 4 agent loop to trace paths and detect patterns.",
        permitted_tools={
            "get_transaction_context",
            "get_account_activity",
            "trace_paths",
            "run_investigation",
            "run_detector",
            "get_finding",
            "get_evidence",
        },
        can_write_graph=False,
    ),
    AgentRole.AUDITOR: RoleSpec(
        role=AgentRole.AUDITOR,
        description="Independently verifies findings and supporting evidence against the graph.",
        permitted_tools={"get_finding", "get_evidence", "get_transaction_context"},
        can_write_graph=False,
        can_audit_evidence=True,
    ),
    AgentRole.MEMORY_CURATOR: RoleSpec(
        role=AgentRole.MEMORY_CURATOR,
        description="Maintains persistent graph memory using audited results and provenance rules.",
        permitted_tools={"query_memory", "record_episodic", "curate_semantic", "supersede_memory"},
        can_write_graph=True,
        can_curate_memory=True,
    ),
}


class BaseAgentRole(ABC):
    def __init__(self, role: AgentRole, conn: FalkorDBConnection) -> None:
        self._role = role
        self._conn = conn
        self._spec = ROLE_REGISTRY[role]

    @property
    def role(self) -> AgentRole:
        return self._role

    @property
    def spec(self) -> RoleSpec:
        return self._spec

    @abstractmethod
    def execute_task(self, task: AgentTaskRecord, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the assigned task within permitted role capabilities."""
        pass
