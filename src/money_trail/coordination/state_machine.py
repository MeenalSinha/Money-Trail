"""State machine validations and dependency graph cycle checks.

Enforces deterministic transitions for agent tasks, handoffs, and sessions,
and ensures task dependency graphs remain acyclic directed graphs (DAGs).
"""
from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Sequence, Set

from money_trail.coordination.models import HandoffStatus, SessionStatus, TaskStatus
from money_trail.exceptions import ValidationError

# Allowed state transitions for AgentTask
ALLOWED_TASK_TRANSITIONS: Mapping[TaskStatus, Set[TaskStatus]] = {
    TaskStatus.PENDING: {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.CANCELLED},
    TaskStatus.READY: {TaskStatus.RUNNING, TaskStatus.BLOCKED, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED, TaskStatus.CANCELLED},
    TaskStatus.BLOCKED: {TaskStatus.READY, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.FAILED: {TaskStatus.READY, TaskStatus.CANCELLED},  # Retry path
    TaskStatus.COMPLETED: set(),  # Terminal state
    TaskStatus.CANCELLED: set(),  # Terminal state
}

# Allowed state transitions for AgentHandoff
ALLOWED_HANDOFF_TRANSITIONS: Mapping[HandoffStatus, Set[HandoffStatus]] = {
    HandoffStatus.INITIATED: {HandoffStatus.TRANSFERRED, HandoffStatus.REJECTED, HandoffStatus.FAILED},
    HandoffStatus.TRANSFERRED: {HandoffStatus.ACCEPTED, HandoffStatus.REJECTED, HandoffStatus.FAILED},
    HandoffStatus.ACCEPTED: {HandoffStatus.COMPLETED, HandoffStatus.FAILED},
    HandoffStatus.COMPLETED: set(),
    HandoffStatus.FAILED: set(),
    HandoffStatus.REJECTED: set(),
}

# Allowed state transitions for AgentSession
ALLOWED_SESSION_TRANSITIONS: Mapping[SessionStatus, Set[SessionStatus]] = {
    SessionStatus.ACTIVE: {SessionStatus.COMPLETED, SessionStatus.INTERRUPTED, SessionStatus.FAILED},
    SessionStatus.INTERRUPTED: {SessionStatus.ACTIVE, SessionStatus.FAILED, SessionStatus.COMPLETED},
    SessionStatus.COMPLETED: set(),
    SessionStatus.FAILED: set(),
}


def validate_task_transition(current: str, target: str) -> None:
    """Validate that moving a task from `current` to `target` is permitted."""
    try:
        cur_status = TaskStatus(current)
        tgt_status = TaskStatus(target)
    except ValueError as exc:
        raise ValidationError(f"Unknown task status: {exc}") from None

    if cur_status == tgt_status:
        return  # Idempotent no-op

    allowed = ALLOWED_TASK_TRANSITIONS.get(cur_status, set())
    if tgt_status not in allowed:
        raise ValidationError(
            f"Invalid task transition from '{cur_status.value}' to '{tgt_status.value}'; "
            f"allowed targets: {sorted(s.value for s in allowed)}"
        )


def validate_handoff_transition(current: str, target: str) -> None:
    """Validate that moving a handoff from `current` to `target` is permitted."""
    try:
        cur_status = HandoffStatus(current)
        tgt_status = HandoffStatus(target)
    except ValueError as exc:
        raise ValidationError(f"Unknown handoff status: {exc}") from None

    if cur_status == tgt_status:
        return

    allowed = ALLOWED_HANDOFF_TRANSITIONS.get(cur_status, set())
    if tgt_status not in allowed:
        raise ValidationError(
            f"Invalid handoff transition from '{cur_status.value}' to '{tgt_status.value}'; "
            f"allowed targets: {sorted(s.value for s in allowed)}"
        )


def validate_session_transition(current: str, target: str) -> None:
    """Validate that moving a session from `current` to `target` is permitted."""
    try:
        cur_status = SessionStatus(current)
        tgt_status = SessionStatus(target)
    except ValueError as exc:
        raise ValidationError(f"Unknown session status: {exc}") from None

    if cur_status == tgt_status:
        return

    allowed = ALLOWED_SESSION_TRANSITIONS.get(cur_status, set())
    if tgt_status not in allowed:
        raise ValidationError(
            f"Invalid session transition from '{cur_status.value}' to '{tgt_status.value}'; "
            f"allowed targets: {sorted(s.value for s in allowed)}"
        )


def detect_dependency_cycle(
    task_dependencies: Mapping[str, Sequence[str]]
) -> Optional[List[str]]:
    """Detect if a task dependency graph contains cycles.

    Returns the cycle list [task_a, task_b, ..., task_a] if detected,
    or None if the graph is a valid DAG.
    """
    # 1. Quick check for direct self-dependency
    for task_id, deps in task_dependencies.items():
        if task_id in deps:
            return [task_id, task_id]

    visited: Set[str] = set()
    rec_stack: List[str] = []

    def dfs(node: str) -> Optional[List[str]]:
        visited.add(node)
        rec_stack.append(node)

        for neighbor in task_dependencies.get(node, []):
            if neighbor not in visited:
                cycle = dfs(neighbor)
                if cycle:
                    return cycle
            elif neighbor in rec_stack:
                # Cycle found
                idx = rec_stack.index(neighbor)
                return rec_stack[idx:] + [neighbor]

        rec_stack.pop()
        return None

    for node in task_dependencies:
        if node not in visited:
            cycle = dfs(node)
            if cycle:
                return cycle
    return None
