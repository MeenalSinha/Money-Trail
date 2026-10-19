"""Graph-backed coordination store in FalkorDB.

Manages persistent agent sessions, DAG-based task scheduling,
atomic task claiming, and auditable agent handoffs using native graph relationships.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from money_trail.coordination.models import (
    AgentHandoffRecord,
    AgentRole,
    AgentSessionRecord,
    AgentTaskRecord,
    HandoffStatus,
    SessionStatus,
    TaskStatus,
    _now_ms,
    stable_handoff_id,
    stable_session_id,
    stable_task_id,
)
from money_trail.coordination.state_machine import (
    detect_dependency_cycle,
    validate_handoff_transition,
    validate_session_transition,
    validate_task_transition,
)
from money_trail.database import FalkorDBConnection, GraphStore
from money_trail.database import graph_schema as gs
from money_trail.exceptions import ValidationError

log = logging.getLogger(__name__)


class CoordinationStore:
    def __init__(self, conn: FalkorDBConnection) -> None:
        self._conn = conn
        self._store = GraphStore(conn)

    @property
    def conn(self) -> FalkorDBConnection:
        return self._conn

    # =========================================================================
    # Session Management
    # =========================================================================

    def create_session(
        self,
        investigation_id: str,
        dataset: Optional[str] = None,
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> AgentSessionRecord:
        """Create and persist an AgentSession linked to the investigation."""
        sid = session_id or stable_session_id(investigation_id)
        now = _now_ms()
        rec = AgentSessionRecord(
            session_id=sid,
            investigation_id=investigation_id,
            status=SessionStatus.ACTIVE.value,
            started_at=now,
            dataset=dataset,
            updated_at=now,
            metadata_json=json.dumps(metadata) if metadata else None,
        )
        self._store.upsert_node(
            "Investigation",
            {"investigation_id": investigation_id, "status": "open", "created_at": now},
        )
        self._store.upsert_node("AgentSession", rec.as_graph_props())
        try:
            self._store.link("HAS_SESSION", investigation_id, sid)
        except Exception as exc:
            log.warning("Could not link investigation %s to session %s: %s", investigation_id, sid, exc)
        return rec

    def get_session(self, session_id: str) -> Optional[AgentSessionRecord]:
        """Fetch session by ID."""
        node = self._store.get_node("AgentSession", session_id)
        if not node:
            return None
        return AgentSessionRecord(
            session_id=node["session_id"],
            investigation_id=node["investigation_id"],
            status=node["status"],
            started_at=int(node["started_at"]),
            dataset=node.get("dataset"),
            updated_at=int(node["updated_at"]) if node.get("updated_at") is not None else None,
            completed_at=int(node["completed_at"]) if node.get("completed_at") is not None else None,
            metadata_json=node.get("metadata_json"),
        )

    def update_session_status(self, session_id: str, new_status: str) -> AgentSessionRecord:
        """Transition session status with state machine validation."""
        rec = self.get_session(session_id)
        if not rec:
            raise ValidationError(f"Session '{session_id}' not found.")
        validate_session_transition(rec.status, new_status)
        now = _now_ms()
        completed_at = now if new_status in (SessionStatus.COMPLETED.value, SessionStatus.FAILED.value) else None
        self._conn.graph.query(
            "MATCH (s:AgentSession {session_id: $sid}) "
            "SET s.status = $status, s.updated_at = $ts, "
            "s.completed_at = CASE WHEN $comp IS NOT NULL THEN $comp ELSE s.completed_at END "
            "RETURN count(s)",
            {"sid": session_id, "status": new_status, "ts": now, "comp": completed_at},
        )
        rec.status = new_status
        rec.updated_at = now
        rec.completed_at = completed_at or rec.completed_at
        return rec

    def recover_session(self, session_id: str) -> Dict[str, Any]:
        """Reconstruct the entire investigation and coordination state from FalkorDB.

        Works across separate processes: retrieves the session, its investigation,
        tasks with statuses and dependencies, unblocked tasks, recorded memories,
        and completed handoffs.
        """
        sess = self.get_session(session_id)
        if not sess:
            raise ValidationError(f"Cannot recover non-existent session '{session_id}'.")

        inv_id = sess.investigation_id
        tasks = self.list_tasks(inv_id)
        ready_tasks = self.get_ready_tasks(inv_id)
        unresolved_tasks = [
            t for t in tasks if t.status in (TaskStatus.PENDING.value, TaskStatus.READY.value, TaskStatus.RUNNING.value, TaskStatus.BLOCKED.value)
        ]
        handoffs = self.list_handoffs(inv_id)

        # Retrieve memories associated with this session or investigation
        mem_rows = self._conn.graph.ro_query(
            "MATCH (m:Memory) WHERE m.investigation_id = $inv_id RETURN m.memory_id",
            {"inv_id": inv_id},
        ).result_set
        memory_ids = [r[0] for r in mem_rows]

        # Retrieve findings
        fnd_rows = self._conn.graph.ro_query(
            "MATCH (i:Investigation {investigation_id: $inv_id})-[:HAS_FINDING]->(f:Finding) "
            "RETURN f.finding_id, f.kind, f.statement",
            {"inv_id": inv_id},
        ).result_set
        findings = [{"finding_id": r[0], "kind": r[1], "statement": r[2]} for r in fnd_rows]

        return {
            "session": sess,
            "investigation_id": inv_id,
            "status": sess.status,
            "tasks": tasks,
            "ready_tasks": ready_tasks,
            "unresolved_tasks": unresolved_tasks,
            "handoffs": handoffs,
            "memory_ids": memory_ids,
            "findings": findings,
        }

    # =========================================================================
    # Task Management
    # =========================================================================

    def create_task(
        self,
        investigation_id: str,
        task_type: str,
        session_id: Optional[str] = None,
        assigned_role: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        dependencies: Optional[Sequence[str]] = None,
    ) -> AgentTaskRecord:
        """Create a new task with dependencies and DAG cycle detection.

        Idempotent: if a task with the same idempotency_key already exists
        for this investigation, returns the existing task record without modification.
        """
        # Idempotency check
        if idempotency_key:
            existing = self._conn.graph.ro_query(
                "MATCH (t:AgentTask {investigation_id: $inv_id, idempotency_key: $key}) "
                "RETURN t.task_id",
                {"inv_id": investigation_id, "key": idempotency_key},
            ).result_set
            if existing:
                task_rec = self.get_task(existing[0][0])
                if task_rec:
                    return task_rec

        deps = list(dependencies or [])
        # Verify dependency tasks belong to the same investigation
        if deps:
            for dep_id in deps:
                dep_node = self.get_task(dep_id)
                if not dep_node or dep_node.investigation_id != investigation_id:
                    raise ValidationError(
                        f"Dependency task '{dep_id}' does not exist in investigation '{investigation_id}'."
                    )

        # Generate task ID
        tkey = idempotency_key or f"{task_type}:{len(deps)}:{_now_ms()}"
        tid = stable_task_id(investigation_id, task_type, tkey)

        # Verify DAG acyclicity before creating
        existing_tasks = self.list_tasks(investigation_id)
        dep_graph: Dict[str, List[str]] = {t.task_id: list(t.dependencies) for t in existing_tasks}
        dep_graph[tid] = deps
        cycle = detect_dependency_cycle(dep_graph)
        if cycle:
            raise ValidationError(
                f"Cannot create task '{task_type}': introduces dependency cycle: {' -> '.join(cycle)}"
            )

        # Determine initial status: READY if no deps or all deps completed; else PENDING
        all_completed = True
        for dep_id in deps:
            d = self.get_task(dep_id)
            if not d or d.status != TaskStatus.COMPLETED.value:
                all_completed = False
                break
        initial_status = TaskStatus.READY.value if (not deps or all_completed) else TaskStatus.PENDING.value

        now = _now_ms()
        task_rec = AgentTaskRecord(
            task_id=tid,
            task_type=task_type,
            status=initial_status,
            investigation_id=investigation_id,
            created_at=now,
            session_id=session_id,
            assigned_role=assigned_role,
            idempotency_key=idempotency_key,
            dependencies=deps,
        )

        # Ensure Investigation node exists in graph
        self._store.upsert_node(
            "Investigation",
            {"investigation_id": investigation_id, "status": "open", "created_at": now},
        )

        # Persist task node
        self._store.upsert_node("AgentTask", task_rec.as_graph_props())

        # Link BELONGS_TO investigation
        try:
            self._store.link("BELONGS_TO", tid, investigation_id)
        except Exception as exc:
            log.warning("Could not link task %s to investigation %s: %s", tid, investigation_id, exc)

        # Link DEPENDS_ON for each prerequisite
        for dep_id in deps:
            try:
                self._store.link("DEPENDS_ON", tid, dep_id)
            except Exception as exc:
                log.warning("Could not link task %s DEPENDS_ON %s: %s", tid, dep_id, exc)

        return task_rec

    def get_task(self, task_id: str) -> Optional[AgentTaskRecord]:
        """Fetch task by ID with its dependencies and assigned details."""
        node = self._store.get_node("AgentTask", task_id)
        if not node:
            return None
        dep_rows = self._conn.graph.ro_query(
            "MATCH (t:AgentTask {task_id: $tid})-[:DEPENDS_ON]->(d:AgentTask) "
            "RETURN d.task_id",
            {"tid": task_id},
        ).result_set
        deps = [r[0] for r in dep_rows]

        return AgentTaskRecord(
            task_id=node["task_id"],
            task_type=node["task_type"],
            status=node["status"],
            investigation_id=node["investigation_id"],
            created_at=int(node["created_at"]),
            session_id=node.get("session_id"),
            assigned_role=node.get("assigned_role"),
            assigned_agent_id=node.get("assigned_agent_id"),
            attempt_count=int(node.get("attempt_count") or 0),
            idempotency_key=node.get("idempotency_key"),
            updated_at=int(node["updated_at"]) if node.get("updated_at") is not None else None,
            completed_at=int(node["completed_at"]) if node.get("completed_at") is not None else None,
            result_summary=node.get("result_summary"),
            error_detail=node.get("error_detail"),
            dependencies=deps,
        )

    def list_tasks(
        self,
        investigation_id: str,
        status: Optional[str] = None,
        role: Optional[str] = None,
    ) -> List[AgentTaskRecord]:
        """List tasks for an investigation with optional status/role filter."""
        clauses = ["MATCH (t:AgentTask {investigation_id: $inv_id})"]
        where = []
        params: Dict[str, Any] = {"inv_id": investigation_id}
        if status:
            where.append("t.status = $status")
            params["status"] = status
        if role:
            where.append("t.assigned_role = $role")
            params["role"] = role
        if where:
            clauses.append("WHERE " + " AND ".join(where))
        clauses.append("RETURN t.task_id ORDER BY t.created_at ASC")

        rows = self._conn.graph.ro_query("\n".join(clauses), params).result_set
        tasks = []
        for (tid,) in rows:
            t = self.get_task(tid)
            if t:
                tasks.append(t)
        return tasks

    def get_ready_tasks(self, investigation_id: str, role: Optional[str] = None) -> List[AgentTaskRecord]:
        """Query FalkorDB for tasks eligible to execute right now.

        A task is ready if:
        - It is explicitly in READY status, OR
        - It is PENDING and all tasks it DEPENDS_ON are COMPLETED.
        Any newly unblocked tasks are transitioned to READY in the graph.
        """
        all_tasks = self.list_tasks(investigation_id, role=role)
        ready_tasks: List[AgentTaskRecord] = []
        now = _now_ms()

        for t in all_tasks:
            if t.status == TaskStatus.READY.value:
                ready_tasks.append(t)
            elif t.status == TaskStatus.PENDING.value:
                # Check if all dependencies are completed
                deps_satisfied = True
                for dep_id in t.dependencies:
                    dep = self.get_task(dep_id)
                    if not dep or dep.status != TaskStatus.COMPLETED.value:
                        deps_satisfied = False
                        break
                if deps_satisfied:
                    # Unblock to READY
                    self._conn.graph.query(
                        "MATCH (t:AgentTask {task_id: $tid}) "
                        "SET t.status = $status, t.updated_at = $now "
                        "RETURN count(t)",
                        {"tid": t.task_id, "status": TaskStatus.READY.value, "now": now},
                    )
                    t.status = TaskStatus.READY.value
                    t.updated_at = now
                    ready_tasks.append(t)
        return ready_tasks

    def claim_task(self, task_id: str, agent_id: str, role: Optional[str] = None) -> AgentTaskRecord:
        """Atomically claim a READY task for execution by an agent."""
        task = self.get_task(task_id)
        if not task:
            raise ValidationError(f"Task '{task_id}' not found.")
        validate_task_transition(task.status, TaskStatus.RUNNING.value)

        now = _now_ms()
        new_attempts = task.attempt_count + 1
        # Atomic update in FalkorDB
        res = self._conn.graph.query(
            "MATCH (t:AgentTask {task_id: $tid}) "
            "WHERE t.status = 'ready' "
            "SET t.status = 'running', t.assigned_agent_id = $agent_id, "
            "t.attempt_count = $attempts, t.updated_at = $now "
            "RETURN count(t)",
            {"tid": task_id, "agent_id": agent_id, "attempts": new_attempts, "now": now},
        ).result_set[0][0]

        if res == 0:
            raise ValidationError(
                f"Task '{task_id}' could not be claimed (it may have been claimed by another worker)."
            )

        # Ensure Agent node exists and link OWNS_TASK
        try:
            r = role or task.assigned_role or "agent"
            self._store.upsert_node("Agent", {"agent_id": agent_id, "role": r})
            self._store.link("OWNS_TASK", agent_id, task_id)
        except Exception as exc:
            log.warning("Could not link agent %s OWNS_TASK %s: %s", agent_id, task_id, exc)

        task.status = TaskStatus.RUNNING.value
        task.assigned_agent_id = agent_id
        task.attempt_count = new_attempts
        task.updated_at = now
        return task

    def complete_task(
        self,
        task_id: str,
        result_summary: Optional[str] = None,
        finding_ids: Optional[Sequence[str]] = None,
    ) -> AgentTaskRecord:
        """Mark task completed, record findings, and unblock downstream tasks."""
        task = self.get_task(task_id)
        if not task:
            raise ValidationError(f"Task '{task_id}' not found.")
        validate_task_transition(task.status, TaskStatus.COMPLETED.value)

        now = _now_ms()
        self._conn.graph.query(
            "MATCH (t:AgentTask {task_id: $tid}) "
            "SET t.status = 'completed', t.completed_at = $now, t.updated_at = $now, "
            "t.result_summary = $res "
            "RETURN count(t)",
            {"tid": task_id, "now": now, "res": result_summary or ""},
        )

        # Link PRODUCED_FINDING for generated findings
        if finding_ids:
            for fid in finding_ids:
                try:
                    self._store.link("PRODUCED_FINDING", task_id, fid)
                except Exception as exc:
                    log.warning("Could not link task %s PRODUCED_FINDING %s: %s", task_id, fid, exc)

        task.status = TaskStatus.COMPLETED.value
        task.completed_at = now
        task.updated_at = now
        task.result_summary = result_summary

        # Unblock downstream tasks that depended on this task
        self._unblock_dependents(task.investigation_id, task_id)
        return task

    def _unblock_dependents(self, investigation_id: str, completed_task_id: str) -> None:
        """Evaluate pending tasks that depend on completed_task_id and transition to READY if unblocked."""
        dependents = self._conn.graph.ro_query(
            "MATCH (d:AgentTask {investigation_id: $inv_id})-[:DEPENDS_ON]->(c:AgentTask {task_id: $cid}) "
            "WHERE d.status = 'pending' "
            "RETURN d.task_id",
            {"inv_id": investigation_id, "cid": completed_task_id},
        ).result_set

        now = _now_ms()
        for (dep_task_id,) in dependents:
            t = self.get_task(dep_task_id)
            if not t:
                continue
            all_done = True
            for req_id in t.dependencies:
                dep_t = self.get_task(req_id)
                if not dep_t or dep_t.status != TaskStatus.COMPLETED.value:
                    all_done = False
                    break
            if all_done:
                self._conn.graph.query(
                    "MATCH (t:AgentTask {task_id: $tid}) "
                    "SET t.status = 'ready', t.updated_at = $now "
                    "RETURN count(t)",
                    {"tid": dep_task_id, "now": now},
                )
                log.info("Task %s unblocked to READY following completion of %s", dep_task_id, completed_task_id)

    def fail_task(self, task_id: str, error_detail: Optional[str] = None) -> AgentTaskRecord:
        """Mark task failed and block any downstream dependent tasks."""
        task = self.get_task(task_id)
        if not task:
            raise ValidationError(f"Task '{task_id}' not found.")
        validate_task_transition(task.status, TaskStatus.FAILED.value)

        now = _now_ms()
        self._conn.graph.query(
            "MATCH (t:AgentTask {task_id: $tid}) "
            "SET t.status = 'failed', t.updated_at = $now, t.error_detail = $err "
            "RETURN count(t)",
            {"tid": task_id, "now": now, "err": error_detail or "Execution failed"},
        )
        task.status = TaskStatus.FAILED.value
        task.error_detail = error_detail

        # Block downstream tasks
        self._conn.graph.query(
            "MATCH (d:AgentTask {investigation_id: $inv_id})-[:DEPENDS_ON]->(f:AgentTask {task_id: $fid}) "
            "WHERE d.status IN ['pending', 'ready'] "
            "SET d.status = 'blocked', d.updated_at = $now, "
            "d.error_detail = 'Blocked: prerequisite task failed' "
            "RETURN count(d)",
            {"inv_id": task.investigation_id, "fid": task_id, "now": now},
        )
        return task

    def block_task(self, task_id: str, reason: Optional[str] = None) -> AgentTaskRecord:
        """Explicitly block a task."""
        task = self.get_task(task_id)
        if not task:
            raise ValidationError(f"Task '{task_id}' not found.")
        validate_task_transition(task.status, TaskStatus.BLOCKED.value)

        now = _now_ms()
        self._conn.graph.query(
            "MATCH (t:AgentTask {task_id: $tid}) "
            "SET t.status = 'blocked', t.updated_at = $now, t.error_detail = $err "
            "RETURN count(t)",
            {"tid": task_id, "now": now, "err": reason or "Task blocked"},
        )
        task.status = TaskStatus.BLOCKED.value
        task.error_detail = reason
        return task

    # =========================================================================
    # Handoff Management
    # =========================================================================

    def create_handoff(
        self,
        investigation_id: str,
        task_id: str,
        source_agent_id: str,
        target_role: str,
        reason: Optional[str] = None,
        context_summary: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        evidence_refs: Optional[Sequence[str]] = None,
    ) -> AgentHandoffRecord:
        """Create a persistent, auditable handoff between agents."""
        if target_role not in gs.AGENT_ROLES:
            raise ValidationError(f"Invalid target_role '{target_role}'; allowed: {gs.AGENT_ROLES}")

        # Idempotency check
        if idempotency_key:
            existing = self._conn.graph.ro_query(
                "MATCH (h:AgentHandoff {investigation_id: $inv_id, idempotency_key: $key}) "
                "RETURN h.handoff_id",
                {"inv_id": investigation_id, "key": idempotency_key},
            ).result_set
            if existing:
                h_rec = self.get_handoff(existing[0][0])
                if h_rec:
                    return h_rec

        hkey = idempotency_key or f"{task_id}:{source_agent_id}:{target_role}:{_now_ms()}"
        hid = stable_handoff_id(investigation_id, task_id, source_agent_id, target_role, hkey)
        now = _now_ms()

        rec = AgentHandoffRecord(
            handoff_id=hid,
            investigation_id=investigation_id,
            task_id=task_id,
            source_agent_id=source_agent_id,
            target_role=target_role,
            status=HandoffStatus.INITIATED.value,
            created_at=now,
            reason=reason,
            context_summary=context_summary,
            idempotency_key=idempotency_key,
            evidence_refs=list(evidence_refs or []),
        )

        self._store.upsert_node(
            "Investigation",
            {"investigation_id": investigation_id, "status": "open", "created_at": now},
        )
        self._store.upsert_node("AgentHandoff", rec.as_graph_props())

        # Link relationships
        try:
            self._store.upsert_node("Agent", {"agent_id": source_agent_id, "role": "agent"})
            self._store.link("INITIATED_HANDOFF", source_agent_id, hid)
            self._store.link("TRANSFERS_TASK", hid, task_id)
            self._store.link("HANDOFF_IN", hid, investigation_id)
        except Exception as exc:
            log.warning("Could not link handoff relationships for %s: %s", hid, exc)

        return rec

    def get_handoff(self, handoff_id: str) -> Optional[AgentHandoffRecord]:
        """Fetch handoff by ID."""
        node = self._store.get_node("AgentHandoff", handoff_id)
        if not node:
            return None
        return AgentHandoffRecord(
            handoff_id=node["handoff_id"],
            investigation_id=node["investigation_id"],
            task_id=node["task_id"],
            source_agent_id=node["source_agent_id"],
            target_role=node["target_role"],
            status=node["status"],
            created_at=int(node["created_at"]),
            target_agent_id=node.get("target_agent_id"),
            reason=node.get("reason"),
            context_summary=node.get("context_summary"),
            idempotency_key=node.get("idempotency_key"),
            updated_at=int(node["updated_at"]) if node.get("updated_at") is not None else None,
            completed_at=int(node["completed_at"]) if node.get("completed_at") is not None else None,
        )

    def list_handoffs(self, investigation_id: str) -> List[AgentHandoffRecord]:
        """List all handoffs for an investigation."""
        rows = self._conn.graph.ro_query(
            "MATCH (h:AgentHandoff {investigation_id: $inv_id}) "
            "RETURN h.handoff_id ORDER BY h.created_at ASC",
            {"inv_id": investigation_id},
        ).result_set
        handoffs = []
        for (hid,) in rows:
            h = self.get_handoff(hid)
            if h:
                handoffs.append(h)
        return handoffs

    def update_handoff_status(
        self,
        handoff_id: str,
        new_status: str,
        target_agent_id: Optional[str] = None,
    ) -> AgentHandoffRecord:
        """Update handoff status with transition validation."""
        h = self.get_handoff(handoff_id)
        if not h:
            raise ValidationError(f"Handoff '{handoff_id}' not found.")
        validate_handoff_transition(h.status, new_status)

        now = _now_ms()
        completed_at = now if new_status in (HandoffStatus.COMPLETED.value, HandoffStatus.FAILED.value, HandoffStatus.REJECTED.value) else None

        self._conn.graph.query(
            "MATCH (h:AgentHandoff {handoff_id: $hid}) "
            "SET h.status = $status, h.updated_at = $now, "
            "h.completed_at = CASE WHEN $comp IS NOT NULL THEN $comp ELSE h.completed_at END, "
            "h.target_agent_id = CASE WHEN $tagent IS NOT NULL THEN $tagent ELSE h.target_agent_id END "
            "RETURN count(h)",
            {"hid": handoff_id, "status": new_status, "now": now, "comp": completed_at, "tagent": target_agent_id},
        )
        h.status = new_status
        h.updated_at = now
        h.completed_at = completed_at or h.completed_at
        if target_agent_id:
            h.target_agent_id = target_agent_id
        return h
