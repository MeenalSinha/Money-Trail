"""Persistent FalkorDB graph memory store.

Provides durable, provenance-aware read and write operations for episodic
and semantic memory. Enforces scope isolation, temporal validity checks,
anti-poisoning evidence validation, supersession, and contradiction tracking.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from money_trail.database import FalkorDBConnection, GraphStore
from money_trail.database import graph_schema as gs
from money_trail.exceptions import ValidationError
from money_trail.memory.models import (
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    _now_ms,
    stable_memory_id,
)

log = logging.getLogger(__name__)


class MemoryStore:
    def __init__(self, conn: FalkorDBConnection) -> None:
        self._conn = conn
        self._store = GraphStore(conn)

    @property
    def conn(self) -> FalkorDBConnection:
        return self._conn

    # ---- Writing memories ---------------------------------------------------

    def record_episodic(
        self,
        session_id: str,
        statement: str,
        scope: str = MemoryScope.INVESTIGATION.value,
        dataset: Optional[str] = None,
        investigation_id: Optional[str] = None,
        provenance_tool: Optional[str] = None,
        evidence_ids: Optional[Sequence[str]] = None,
        confidence: Optional[float] = None,
        created_at: Optional[int] = None,
    ) -> MemoryRecord:
        """Record an episodic memory node linked to the creating session.

        Episodic memories capture specific events, actions, or intermediate
        findings that occurred during an investigation session.
        """
        scope_id = investigation_id if scope == "investigation" else (dataset or "global")
        if not scope_id:
            raise ValidationError(
                f"Scope '{scope}' requires investigation_id or dataset to be specified."
            )

        ts = created_at or _now_ms()
        mem_id = stable_memory_id(scope, scope_id, MemoryType.EPISODIC.value, statement, provenance_tool)

        record = MemoryRecord(
            memory_id=mem_id,
            memory_type=MemoryType.EPISODIC.value,
            scope=scope,
            statement=statement,
            status=MemoryStatus.ACTIVE.value,
            created_at=ts,
            dataset=dataset,
            investigation_id=investigation_id,
            provenance_tool=provenance_tool,
            confidence=confidence,
            evidence_ids=list(evidence_ids or []),
        )

        # 1. Upsert Memory node
        self._store.upsert_node("Memory", record.as_graph_props())

        # 2. Link Session -[:RECORDED]-> Memory if session exists
        if session_id:
            try:
                self._store.link("RECORDED", session_id, mem_id)
            except Exception as exc:
                log.warning("Could not link session %s to memory %s: %s", session_id, mem_id, exc)

        # 3. Link Evidence -[:GROUNDED_BY]-> Evidence
        if evidence_ids:
            for eid in evidence_ids:
                try:
                    self._store.link("GROUNDED_BY", mem_id, eid)
                except Exception as exc:
                    log.warning("Could not link memory %s to evidence %s: %s", mem_id, eid, exc)

        return record

    def curate_semantic(
        self,
        statement: str,
        scope: str = MemoryScope.INVESTIGATION.value,
        dataset: Optional[str] = None,
        investigation_id: Optional[str] = None,
        provenance_tool: Optional[str] = None,
        rule_fingerprint: Optional[str] = None,
        evidence_ids: Optional[Sequence[str]] = None,
        confidence: Optional[float] = None,
        valid_from: Optional[int] = None,
        valid_to: Optional[int] = None,
        require_evidence: bool = True,
        created_at: Optional[int] = None,
    ) -> MemoryRecord:
        """Promote or record a verified semantic memory grounded in graph evidence.

        ANTI-POISONING GUARANTEE:
        Semantic memories asserting factual claims MUST be grounded by real
        verified evidence records present in FalkorDB. Unsupported LLM commentary
        cannot be silently promoted to semantic memory.
        """
        scope_id = investigation_id if scope == "investigation" else (dataset or "global")
        if not scope_id:
            raise ValidationError(
                f"Scope '{scope}' requires investigation_id or dataset to be specified."
            )

        e_ids = list(evidence_ids or [])
        if require_evidence:
            if not e_ids:
                raise ValidationError(
                    "Semantic factual memory must be grounded by at least one verified evidence ID."
                )
            # Verify evidence items exist in graph and reference valid transactions
            for eid in e_ids:
                ev_node = self._store.get_node("Evidence", eid)
                if not ev_node:
                    raise ValidationError(
                        f"Evidence '{eid}' does not exist in graph; cannot ground semantic memory."
                    )
                # Confirm evidence references at least one transaction
                tx_check = self._conn.graph.ro_query(
                    "MATCH (e:Evidence {evidence_id: $eid})-[:REFERENCES]->(t:Transaction) "
                    "RETURN count(t)",
                    {"eid": eid},
                ).result_set[0][0]
                if tx_check == 0:
                    raise ValidationError(
                        f"Evidence '{eid}' references no transaction; cannot ground factual claim."
                    )

        ts = created_at or _now_ms()
        mem_id = stable_memory_id(scope, scope_id, MemoryType.SEMANTIC.value, statement, provenance_tool)

        record = MemoryRecord(
            memory_id=mem_id,
            memory_type=MemoryType.SEMANTIC.value,
            scope=scope,
            statement=statement,
            status=MemoryStatus.ACTIVE.value,
            created_at=ts,
            dataset=dataset,
            investigation_id=investigation_id,
            provenance_tool=provenance_tool,
            rule_fingerprint=rule_fingerprint,
            confidence=confidence,
            valid_from=valid_from,
            valid_to=valid_to,
            evidence_ids=e_ids,
        )

        # 1. Upsert node
        self._store.upsert_node("Memory", record.as_graph_props())

        # 2. Link Evidence
        for eid in e_ids:
            try:
                self._store.link("GROUNDED_BY", mem_id, eid)
            except Exception as exc:
                log.warning("Could not link memory %s to evidence %s: %s", mem_id, eid, exc)

        return record

    # ---- Reading and Querying -----------------------------------------------

    def get_memory(self, memory_id: str) -> Optional[MemoryRecord]:
        """Fetch a memory record by ID with its supporting evidence, supersession, and contradictions."""
        node = self._store.get_node("Memory", memory_id)
        if not node:
            return None

        # Fetch evidence references
        ev_rows = self._conn.graph.ro_query(
            "MATCH (m:Memory {memory_id: $id})-[:GROUNDED_BY]->(e:Evidence) "
            "RETURN e.evidence_id",
            {"id": memory_id},
        ).result_set
        evidence_ids = [r[0] for r in ev_rows]

        # Fetch supersedes and superseded_by
        older_rows = self._conn.graph.ro_query(
            "MATCH (m:Memory {memory_id: $id})-[:SUPERSEDES]->(older:Memory) "
            "RETURN older.memory_id",
            {"id": memory_id},
        ).result_set
        supersedes_id = older_rows[0][0] if older_rows else None

        newer_rows = self._conn.graph.ro_query(
            "MATCH (m:Memory {memory_id: $id})<-[:SUPERSEDES]-(newer:Memory) "
            "RETURN newer.memory_id",
            {"id": memory_id},
        ).result_set
        superseded_by = newer_rows[0][0] if newer_rows else None

        # Fetch contradictions
        contra_rows = self._conn.graph.ro_query(
            "MATCH (m:Memory {memory_id: $id})-[:CONTRADICTS]-(c:Memory) "
            "RETURN c.memory_id",
            {"id": memory_id},
        ).result_set
        contradictions = sorted({r[0] for r in contra_rows})

        return MemoryRecord(
            memory_id=node["memory_id"],
            memory_type=node["memory_type"],
            scope=node["scope"],
            statement=node["statement"],
            status=node["status"],
            created_at=int(node["created_at"]),
            dataset=node.get("dataset"),
            investigation_id=node.get("investigation_id"),
            updated_at=int(node["updated_at"]) if node.get("updated_at") is not None else None,
            valid_from=int(node["valid_from"]) if node.get("valid_from") is not None else None,
            valid_to=int(node["valid_to"]) if node.get("valid_to") is not None else None,
            provenance_tool=node.get("provenance_tool"),
            rule_fingerprint=node.get("rule_fingerprint"),
            confidence=float(node["confidence"]) if node.get("confidence") is not None else None,
            evidence_ids=evidence_ids,
            superseded_by=superseded_by,
            supersedes_id=supersedes_id,
            contradicting_memory_ids=contradictions,
        )

    def query_memories(
        self,
        scope: Optional[str] = None,
        dataset: Optional[str] = None,
        investigation_id: Optional[str] = None,
        memory_type: Optional[str] = None,
        status: Optional[str] = MemoryStatus.ACTIVE.value,
        valid_at: Optional[int] = None,
        include_evidence: bool = True,
        limit: int = 50,
    ) -> List[MemoryRecord]:
        """Scope-isolated memory retrieval from FalkorDB.

        Guarantees:
        1. When scope='investigation', NEVER returns memories belonging to other investigations.
        2. When scope='dataset', NEVER leaks memories across dataset boundaries.
        3. Excludes superseded / invalidated memories by default unless requested.
        4. Enforces temporal validity windows when valid_at is provided.
        5. Results are bounded (limit <= 1000).
        """
        if not scope:
            if investigation_id:
                scope = MemoryScope.INVESTIGATION.value
            elif dataset:
                scope = MemoryScope.DATASET.value
            else:
                scope = MemoryScope.INVESTIGATION.value

        if scope not in gs.MEMORY_SCOPES:
            raise ValidationError(f"Invalid scope '{scope}'; allowed: {gs.MEMORY_SCOPES}")

        effective_limit = min(max(1, limit), 1000)
        clauses = ["MATCH (m:Memory)"]
        where: List[str] = ["m.scope = $scope"]
        params: Dict[str, Any] = {"scope": scope}

        if scope == MemoryScope.INVESTIGATION.value:
            if not investigation_id:
                raise ValidationError("Querying investigation scope requires investigation_id.")
            where.append("m.investigation_id = $inv_id")
            params["inv_id"] = investigation_id
        elif scope == MemoryScope.DATASET.value:
            if not dataset:
                raise ValidationError("Querying dataset scope requires dataset.")
            where.append("m.dataset = $dataset")
            params["dataset"] = dataset

        if memory_type:
            if memory_type not in gs.MEMORY_TYPES:
                raise ValidationError(f"Invalid memory_type '{memory_type}'.")
            where.append("m.memory_type = $mem_type")
            params["mem_type"] = memory_type

        if status:
            where.append("m.status = $status")
            params["status"] = status

        if valid_at is not None:
            where.append("(m.valid_from IS NULL OR m.valid_from <= $valid_at)")
            where.append("(m.valid_to IS NULL OR m.valid_to >= $valid_at)")
            params["valid_at"] = valid_at

        clauses.append("WHERE " + " AND ".join(where))
        clauses.append("RETURN m.memory_id ORDER BY m.created_at DESC")
        clauses.append(f"LIMIT {effective_limit}")

        cypher = "\n".join(clauses)
        rows = self._conn.graph.ro_query(cypher, params).result_set

        results = []
        for (mid,) in rows:
            rec = self.get_memory(mid)
            if rec:
                results.append(rec)
        return results

    # Alias for convenient retrieval
    retrieve_memories = query_memories

    # ---- Updates, Supersession, Contradictions, Retractions -----------------

    def supersede(
        self,
        old_memory_id: str,
        new_memory_id: str,
        reason: Optional[str] = None,
    ) -> None:
        """Supersede an older memory with a newer or corrected memory.

        Preserves the historical record:
        - Creates (new_memory)-[:SUPERSEDES]->(old_memory)
        - Sets old_memory.status = 'superseded' and records update timestamp.
        """
        old = self.get_memory(old_memory_id)
        new = self.get_memory(new_memory_id)
        if not old:
            raise ValidationError(f"Target memory '{old_memory_id}' to supersede not found.")
        if not new:
            raise ValidationError(f"Replacement memory '{new_memory_id}' not found.")

        ts = _now_ms()
        # 1. Link (new)-[:SUPERSEDES]->(old)
        self._store.link("SUPERSEDES", new_memory_id, old_memory_id)

        # 2. Update status of old memory
        self._conn.graph.query(
            "MATCH (m:Memory {memory_id: $id}) "
            "SET m.status = $status, m.updated_at = $ts "
            "RETURN count(m)",
            {"id": old_memory_id, "status": MemoryStatus.SUPERSEDED.value, "ts": ts},
        )
        log.info(
            "Memory %s superseded by %s (reason: %s)", old_memory_id, new_memory_id, reason or "unspecified"
        )

    def mark_contradiction(
        self,
        memory_id_a: str,
        memory_id_b: str,
        reason: Optional[str] = None,
    ) -> None:
        """Record a bidirectional contradiction link between two conflicting claims."""
        if memory_id_a == memory_id_b:
            raise ValidationError("A memory cannot contradict itself.")
        a = self.get_memory(memory_id_a)
        b = self.get_memory(memory_id_b)
        if not a or not b:
            raise ValidationError("Both memories must exist to record a contradiction.")

        self._store.link("CONTRADICTS", memory_id_a, memory_id_b)
        self._store.link("CONTRADICTS", memory_id_b, memory_id_a)
        log.info("Recorded contradiction between %s and %s", memory_id_a, memory_id_b)

    def invalidate(self, memory_id: str, reason: Optional[str] = None) -> None:
        """Retract or invalidate a memory that was found incorrect or refuted."""
        m = self.get_memory(memory_id)
        if not m:
            raise ValidationError(f"Memory '{memory_id}' not found.")
        ts = _now_ms()
        self._conn.graph.query(
            "MATCH (m:Memory {memory_id: $id}) "
            "SET m.status = $status, m.updated_at = $ts "
            "RETURN count(m)",
            {"id": memory_id, "status": MemoryStatus.INVALIDATED.value, "ts": ts},
        )
        log.info("Invalidated memory %s (reason: %s)", memory_id, reason or "unspecified")

    def revalidate_memories_for_transactions(self, transaction_ids: Sequence[str]) -> List[str]:
        """Mark memories whose grounding evidence depends on modified transactions.

        When transaction data changes, any semantic memory grounded by evidence
        referencing those transactions must be marked as 'revalidation_needed'.
        """
        if not transaction_ids:
            return []
        rows = self._conn.graph.query(
            "MATCH (m:Memory)-[:GROUNDED_BY]->(e:Evidence)-[:REFERENCES]->(t:Transaction) "
            "WHERE t.transaction_id IN $tx_ids AND m.status = 'active' "
            "SET m.status = 'revalidation_needed', m.updated_at = $ts "
            "RETURN DISTINCT m.memory_id",
            {"tx_ids": list(transaction_ids), "ts": _now_ms()},
        ).result_set
        flagged = [r[0] for r in rows]
        log.info("Flagged %d memories for revalidation", len(flagged))
        return flagged
