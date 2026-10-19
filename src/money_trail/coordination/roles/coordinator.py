"""Coordinator / Planner agent role.

Manages investigation task graphs, dependency-aware scheduling,
task readiness queries from FalkorDB, and multi-agent coordination.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from money_trail.coordination.models import (
    AgentRole,
    AgentTaskRecord,
    TaskStatus,
)
from money_trail.coordination.roles.base import BaseAgentRole
from money_trail.coordination.store import CoordinationStore
from money_trail.database import FalkorDBConnection
from money_trail.memory.store import MemoryStore

log = logging.getLogger(__name__)


class CoordinatorRole(BaseAgentRole):
    def __init__(self, conn: FalkorDBConnection) -> None:
        super().__init__(AgentRole.COORDINATOR, conn)
        self.coord_store = CoordinationStore(conn)
        self.memory_store = MemoryStore(conn)

    def plan_investigation(
        self,
        investigation_id: str,
        session_id: str,
        anchor_id: str,
        anchor_type: str = "transaction",
        dataset: Optional[str] = None,
    ) -> List[AgentTaskRecord]:
        """Create a structured, bounded DAG of tasks for an investigation.

        Dependencies:
          Task 1: investigate_anchor (role: investigator)
          Task 2: audit_findings    (role: auditor, depends on Task 1)
          Task 3: curate_memory     (role: memory_curator, depends on Task 2)
        """
        # Task 1: Investigation
        t1 = self.coord_store.create_task(
            investigation_id=investigation_id,
            task_type="investigate_anchor",
            session_id=session_id,
            assigned_role=AgentRole.INVESTIGATOR.value,
            idempotency_key=f"inv:{investigation_id}:{anchor_id}",
            dependencies=[],
        )

        # Task 2: Audit (depends on Task 1)
        t2 = self.coord_store.create_task(
            investigation_id=investigation_id,
            task_type="audit_findings",
            session_id=session_id,
            assigned_role=AgentRole.AUDITOR.value,
            idempotency_key=f"audit:{investigation_id}:{anchor_id}",
            dependencies=[t1.task_id],
        )

        # Task 3: Memory curation (depends on Task 2)
        t3 = self.coord_store.create_task(
            investigation_id=investigation_id,
            task_type="curate_memory",
            session_id=session_id,
            assigned_role=AgentRole.MEMORY_CURATOR.value,
            idempotency_key=f"curate:{investigation_id}:{anchor_id}",
            dependencies=[t2.task_id],
        )

        return [t1, t2, t3]

    def get_next_ready_task(self, investigation_id: str) -> Optional[AgentTaskRecord]:
        """Query FalkorDB to find the next ready task to execute."""
        ready_tasks = self.coord_store.get_ready_tasks(investigation_id)
        if not ready_tasks:
            return None
        return ready_tasks[0]

    def execute_task(self, task: AgentTaskRecord, context: Dict[str, Any]) -> Dict[str, Any]:
        """Coordinator manages task orchestration and reviews overall progress."""
        all_tasks = self.coord_store.list_tasks(task.investigation_id)
        completed = [t for t in all_tasks if t.status == TaskStatus.COMPLETED.value]
        ready = [t for t in all_tasks if t.status == TaskStatus.READY.value]
        blocked = [t for t in all_tasks if t.status == TaskStatus.BLOCKED.value]

        return {
            "total_tasks": len(all_tasks),
            "completed_count": len(completed),
            "ready_count": len(ready),
            "blocked_count": len(blocked),
            "is_complete": len(completed) == len(all_tasks),
        }
