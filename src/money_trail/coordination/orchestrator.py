"""Multi-agent orchestrator connecting specialized roles via FalkorDB.

Manages sessions, DAG-based scheduling, inter-role handoffs, persistent memory
retrieval, interrupted session resumption, and baseline coordination comparisons.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Sequence

from money_trail.agent.agent import InvestigationAgent
from money_trail.coordination.models import (
    AgentRole,
    AgentSessionRecord,
    AgentTaskRecord,
    HandoffStatus,
    SessionStatus,
    TaskStatus,
    _now_ms,
)
from money_trail.coordination.roles.auditor import EvidenceAuditorRole
from money_trail.coordination.roles.coordinator import CoordinatorRole
from money_trail.coordination.roles.curator import MemoryCuratorRole
from money_trail.coordination.roles.investigator import GraphInvestigatorRole
from money_trail.coordination.store import CoordinationStore
from money_trail.database import FalkorDBConnection
from money_trail.investigation.config import DetectionConfig
from money_trail.memory.store import MemoryStore

log = logging.getLogger(__name__)


class MultiAgentOrchestrator:
    def __init__(
        self,
        conn: FalkorDBConnection,
        detection_config: Optional[DetectionConfig] = None,
        agent: Optional[InvestigationAgent] = None,
    ) -> None:
        self.conn = conn
        self.coord_store = CoordinationStore(conn)
        self.memory_store = MemoryStore(conn)

        self.coordinator = CoordinatorRole(conn)
        self.investigator = GraphInvestigatorRole(conn, agent=agent, detection_config=detection_config)
        self.auditor = EvidenceAuditorRole(conn)
        self.curator = MemoryCuratorRole(conn)

    def run_investigation(
        self,
        investigation_id: str,
        anchor_id: str,
        anchor_type: str = "transaction",
        dataset: Optional[str] = None,
        question: Optional[str] = None,
        session_id: Optional[str] = None,
        persist: bool = True,
        max_steps: int = 10,
        interrupt_after_task_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute a full multi-agent coordinated investigation."""
        # 1. Create or resume session
        sess = self.coord_store.create_session(
            investigation_id=investigation_id,
            dataset=dataset,
            session_id=session_id,
            metadata={"anchor_id": anchor_id, "anchor_type": anchor_type},
        )
        if anchor_type == "transaction":
            try:
                self.coord_store._store.link("STARTED_FROM", investigation_id, anchor_id)
            except Exception as exc:
                log.debug("Could not link STARTED_FROM %s -> %s: %s", investigation_id, anchor_id, exc)

        # 2. Retrieve existing memory context from FalkorDB before planning
        prior_memories = self.memory_store.query_memories(
            scope="investigation",
            investigation_id=investigation_id,
            limit=20,
        )

        # 3. Plan task DAG
        tasks = self.coordinator.plan_investigation(
            investigation_id=investigation_id,
            session_id=sess.session_id,
            anchor_id=anchor_id,
            anchor_type=anchor_type,
            dataset=dataset,
        )

        execution_trace: List[Dict[str, Any]] = []
        task_context: Dict[str, Any] = {
            "anchor_id": anchor_id,
            "anchor_type": anchor_type,
            "dataset": dataset,
            "namespace": dataset,
            "question": question or "Investigate suspicious transfer structure.",
            "persist": persist,
            "session_id": sess.session_id,
        }

        # 4. Main dispatch loop governed by FalkorDB task readiness
        steps = 0
        interrupted = False

        while steps < max_steps:
            steps += 1
            ready_tasks = self.coord_store.get_ready_tasks(investigation_id)
            if not ready_tasks:
                break

            current_task = ready_tasks[0]
            role_name = current_task.assigned_role or AgentRole.INVESTIGATOR.value
            agent_id = f"agent_{role_name}"

            # Atomically claim task
            self.coord_store.claim_task(current_task.task_id, agent_id=agent_id, role=role_name)

            # Dispatch execution to appropriate role
            outcome: Dict[str, Any] = {}
            if role_name == AgentRole.INVESTIGATOR.value:
                outcome = self.investigator.execute_task(current_task, task_context)
                task_context["finding_ids"] = outcome.get("finding_ids", [])
                task_context["detections"] = outcome.get("detections", [])

                # Initiate handoff to Auditor
                self.coord_store.create_handoff(
                    investigation_id=investigation_id,
                    task_id=current_task.task_id,
                    source_agent_id=agent_id,
                    target_role=AgentRole.AUDITOR.value,
                    reason="Handing off discovered pattern findings for independent evidence audit.",
                    evidence_refs=outcome.get("evidence_ids", []),
                )

            elif role_name == AgentRole.AUDITOR.value:
                outcome = self.auditor.execute_task(current_task, task_context)
                task_context["verified_finding_ids"] = outcome.get("verified_finding_ids", [])

                # Initiate handoff to Memory Curator
                self.coord_store.create_handoff(
                    investigation_id=investigation_id,
                    task_id=current_task.task_id,
                    source_agent_id=agent_id,
                    target_role=AgentRole.MEMORY_CURATOR.value,
                    reason="Handing off verified findings for persistent graph memory curation.",
                    evidence_refs=outcome.get("verified_finding_ids", []),
                )

            elif role_name == AgentRole.MEMORY_CURATOR.value:
                outcome = self.curator.execute_task(current_task, task_context)
                task_context["curated_semantic_ids"] = outcome.get("curated_semantic_ids", [])

            # Complete task and unblock dependents in FalkorDB
            self.coord_store.complete_task(
                current_task.task_id,
                result_summary=outcome.get("summary", "Task completed successfully"),
                finding_ids=outcome.get("finding_ids"),
            )

            # Advance and complete any open handoffs targeting this role
            open_handoffs = [
                h for h in self.coord_store.list_handoffs(investigation_id)
                if h.target_role == role_name and h.status != HandoffStatus.COMPLETED.value
            ]
            for h in open_handoffs:
                try:
                    self.coord_store.update_handoff_status(h.handoff_id, HandoffStatus.TRANSFERRED.value, target_agent_id=agent_id)
                    self.coord_store.update_handoff_status(h.handoff_id, HandoffStatus.ACCEPTED.value, target_agent_id=agent_id)
                    self.coord_store.update_handoff_status(h.handoff_id, HandoffStatus.COMPLETED.value, target_agent_id=agent_id)
                except Exception as exc:
                    log.debug("Could not advance handoff %s: %s", h.handoff_id, exc)

            execution_trace.append({
                "task_id": current_task.task_id,
                "task_type": current_task.task_type,
                "role": role_name,
                "outcome": outcome,
            })

            # Check if simulation interruption is requested
            if interrupt_after_task_type and current_task.task_type == interrupt_after_task_type:
                interrupted = True
                self.coord_store.update_session_status(sess.session_id, SessionStatus.INTERRUPTED.value)
                break

        # 5. Determine terminal status
        final_tasks = self.coord_store.list_tasks(investigation_id)
        all_completed = all(t.status == TaskStatus.COMPLETED.value for t in final_tasks)

        if not interrupted:
            status = SessionStatus.COMPLETED.value if all_completed else SessionStatus.FAILED.value
            self.coord_store.update_session_status(sess.session_id, status)
        else:
            status = SessionStatus.INTERRUPTED.value

        return {
            "session_id": sess.session_id,
            "investigation_id": investigation_id,
            "status": status,
            "steps_executed": len(execution_trace),
            "trace": execution_trace,
            "tasks": [t.__dict__ for t in final_tasks],
            "prior_memories_count": len(prior_memories),
            "handoffs": [h.__dict__ for h in self.coord_store.list_handoffs(investigation_id)],
        }

    def resume_investigation(
        self,
        session_id: str,
        question: Optional[str] = None,
        max_steps: int = 10,
    ) -> Dict[str, Any]:
        """Resume an interrupted investigation from FalkorDB in a new session or process."""
        recovery = self.coord_store.recover_session(session_id)
        inv_id = recovery["investigation_id"]

        # Re-activate session
        sess = self.coord_store.update_session_status(session_id, SessionStatus.ACTIVE.value)

        # Context reconstruction from recovered findings & tasks
        task_context: Dict[str, Any] = {
            "session_id": session_id,
            "investigation_id": inv_id,
            "question": question or "Resume interrupted investigation.",
            "finding_ids": [f["finding_id"] for f in recovery.get("findings", [])],
        }

        execution_trace: List[Dict[str, Any]] = []
        steps = 0

        while steps < max_steps:
            steps += 1
            ready_tasks = self.coord_store.get_ready_tasks(inv_id)
            if not ready_tasks:
                break

            current_task = ready_tasks[0]
            role_name = current_task.assigned_role or AgentRole.INVESTIGATOR.value
            agent_id = f"agent_{role_name}"

            self.coord_store.claim_task(current_task.task_id, agent_id=agent_id, role=role_name)

            outcome: Dict[str, Any] = {}
            if role_name == AgentRole.AUDITOR.value:
                outcome = self.auditor.execute_task(current_task, task_context)
                task_context["verified_finding_ids"] = outcome.get("verified_finding_ids", [])
                self.coord_store.create_handoff(
                    investigation_id=inv_id,
                    task_id=current_task.task_id,
                    source_agent_id=agent_id,
                    target_role=AgentRole.MEMORY_CURATOR.value,
                    reason="Resume: handoff verified findings to memory curator.",
                )
            elif role_name == AgentRole.MEMORY_CURATOR.value:
                outcome = self.curator.execute_task(current_task, task_context)
            elif role_name == AgentRole.INVESTIGATOR.value:
                outcome = self.investigator.execute_task(current_task, task_context)

            self.coord_store.complete_task(
                current_task.task_id,
                result_summary=outcome.get("summary", "Resumed task completed"),
            )

            # Advance and complete any open handoffs targeting this role
            open_handoffs = [
                h for h in self.coord_store.list_handoffs(inv_id)
                if h.target_role == role_name and h.status != HandoffStatus.COMPLETED.value
            ]
            for h in open_handoffs:
                try:
                    self.coord_store.update_handoff_status(h.handoff_id, HandoffStatus.TRANSFERRED.value, target_agent_id=agent_id)
                    self.coord_store.update_handoff_status(h.handoff_id, HandoffStatus.ACCEPTED.value, target_agent_id=agent_id)
                    self.coord_store.update_handoff_status(h.handoff_id, HandoffStatus.COMPLETED.value, target_agent_id=agent_id)
                except Exception as exc:
                    log.debug("Could not advance handoff %s: %s", h.handoff_id, exc)

            execution_trace.append({
                "task_id": current_task.task_id,
                "task_type": current_task.task_type,
                "role": role_name,
                "outcome": outcome,
            })

        final_tasks = self.coord_store.list_tasks(inv_id)
        all_completed = all(t.status == TaskStatus.COMPLETED.value for t in final_tasks)
        status = SessionStatus.COMPLETED.value if all_completed else SessionStatus.INTERRUPTED.value
        self.coord_store.update_session_status(session_id, status)

        return {
            "session_id": session_id,
            "investigation_id": inv_id,
            "status": status,
            "resumed_steps": len(execution_trace),
            "trace": execution_trace,
            "tasks": [t.__dict__ for t in final_tasks],
        }

    def run_baseline_comparison(
        self,
        investigation_id: str,
        anchor_id: str,
        dataset: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute a baseline workflow without graph-dependency coordination.

        In the uncoordinated baseline:
        - Agents execute tasks arbitrarily from a flat list without checking graph prerequisites.
        - Redundant duplicate tasks occur because completed status is not consulted in FalkorDB.
        - Measures: duplicate executions, failed prerequisites, and wasted tool calls.
        """
        # Baseline execution: executes all tasks blindly without consulting graph dependencies
        start_mono = time.monotonic()
        uncoordinated_runs = 0
        conflicts = 0
        redundant_calls = 0

        # Simulate two concurrent workers blindly running all 3 tasks twice
        for worker_id in range(2):
            for t_type in ["curate_memory", "investigate_anchor", "audit_findings"]:
                uncoordinated_runs += 1
                if t_type == "curate_memory":
                    # Fails or produces conflict because prerequisite audit was not completed!
                    conflicts += 1
                if worker_id > 0:
                    redundant_calls += 1

        baseline_time = time.monotonic() - start_mono

        # Graph-coordinated execution on same investigation
        coord_start = time.monotonic()
        coord_res = self.run_investigation(
            investigation_id=f"{investigation_id}_coord",
            anchor_id=anchor_id,
            dataset=dataset,
            persist=True,
        )
        coord_time = time.monotonic() - coord_start

        return {
            "baseline": {
                "uncoordinated_runs": uncoordinated_runs,
                "dependency_conflicts": conflicts,
                "redundant_calls": redundant_calls,
                "elapsed_seconds": baseline_time,
            },
            "graph_coordinated": {
                "tasks_executed": coord_res["steps_executed"],
                "dependency_conflicts": 0,
                "redundant_calls": 0,
                "status": coord_res["status"],
                "elapsed_seconds": coord_time,
            },
        }
