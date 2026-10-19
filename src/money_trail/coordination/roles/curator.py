"""Memory Curator agent role.

Maintains persistent graph memory using audited findings and provenance rules.
Promotes only verified, evidence-grounded facts to semantic memory and records
episodic memories for session actions.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from money_trail.coordination.models import AgentRole, AgentTaskRecord
from money_trail.coordination.roles.base import BaseAgentRole
from money_trail.database import FalkorDBConnection
from money_trail.memory.models import MemoryScope
from money_trail.memory.store import MemoryStore

log = logging.getLogger(__name__)


class MemoryCuratorRole(BaseAgentRole):
    def __init__(self, conn: FalkorDBConnection) -> None:
        super().__init__(AgentRole.MEMORY_CURATOR, conn)
        self.memory_store = MemoryStore(conn)

    def execute_task(self, task: AgentTaskRecord, context: Dict[str, Any]) -> Dict[str, Any]:
        """Curate persistent memory from audited findings and investigation events."""
        inv_id = task.investigation_id
        session_id = task.session_id or context.get("session_id")
        dataset = context.get("dataset")
        verified_finding_ids = context.get("verified_finding_ids", [])

        # 1. Record episodic memory of the investigation execution
        episodic_stmt = context.get(
            "episodic_summary",
            f"Investigation session {session_id or inv_id} executed with {len(verified_finding_ids)} verified findings.",
        )
        ep_rec = self.memory_store.record_episodic(
            session_id=session_id or f"sess_{inv_id}",
            statement=episodic_stmt,
            scope=MemoryScope.INVESTIGATION.value,
            dataset=dataset,
            investigation_id=inv_id,
            provenance_tool="coordinator_orchestrator",
        )

        # 2. Promote verified findings to grounded semantic memory
        curated_semantic_ids = []
        rejected_promotion_ids = []

        for fid in verified_finding_ids:
            # Query finding statement and evidence IDs from graph
            f_rows = self._conn.graph.ro_query(
                "MATCH (f:Finding {finding_id: $fid}) "
                "OPTIONAL MATCH (f)-[:SUPPORTED_BY]->(e:Evidence) "
                "RETURN f.statement, f.kind, collect(e.evidence_id)",
                {"fid": fid},
            ).result_set

            if not f_rows or not f_rows[0][0]:
                rejected_promotion_ids.append(fid)
                continue

            statement, kind, evidence_ids = f_rows[0][0], f_rows[0][1], f_rows[0][2]
            if not evidence_ids:
                # Anti-poisoning safeguard: Cannot promote factual memory without evidence
                log.warning("Rejecting semantic promotion for finding %s: no evidence IDs", fid)
                rejected_promotion_ids.append(fid)
                continue

            try:
                sem_rec = self.memory_store.curate_semantic(
                    statement=statement,
                    scope=MemoryScope.INVESTIGATION.value,
                    dataset=dataset,
                    investigation_id=inv_id,
                    provenance_tool=f"auditor_verified_{kind}",
                    evidence_ids=evidence_ids,
                    require_evidence=True,
                )
                curated_semantic_ids.append(sem_rec.memory_id)
            except Exception as exc:
                log.warning("Could not curate semantic memory for %s: %s", fid, exc)
                rejected_promotion_ids.append(fid)

        return {
            "status": "completed",
            "episodic_memory_id": ep_rec.memory_id,
            "curated_semantic_ids": curated_semantic_ids,
            "rejected_promotions": rejected_promotion_ids,
            "summary": (
                f"Recorded 1 episodic memory and curated {len(curated_semantic_ids)} "
                f"grounded semantic memories (rejected: {len(rejected_promotion_ids)})."
            ),
        }
