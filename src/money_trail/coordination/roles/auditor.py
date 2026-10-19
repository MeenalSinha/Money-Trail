"""Evidence Auditor agent role.

Independently verifies investigation findings and supporting evidence records
against real graph data before results can be accepted or promoted to persistent memory.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from money_trail.coordination.models import AgentRole, AgentTaskRecord
from money_trail.coordination.roles.base import BaseAgentRole
from money_trail.database import FalkorDBConnection, GraphStore

log = logging.getLogger(__name__)


class EvidenceAuditorRole(BaseAgentRole):
    def __init__(self, conn: FalkorDBConnection) -> None:
        super().__init__(AgentRole.AUDITOR, conn)
        self.store = GraphStore(conn)

    def audit_finding(self, finding_id: str) -> Dict[str, Any]:
        """Deterministically audit a single finding and its evidence chain in FalkorDB."""
        f_node = self.store.get_node("Finding", finding_id)
        if not f_node:
            return {"finding_id": finding_id, "verified": False, "reason": "Finding node not found in graph."}

        # 1. Fetch supporting evidence
        ev_rows = self._conn.graph.ro_query(
            "MATCH (f:Finding {finding_id: $fid})-[:SUPPORTED_BY]->(e:Evidence) "
            "RETURN e.evidence_id, e.produced_by_tool",
            {"fid": finding_id},
        ).result_set

        if not ev_rows:
            return {
                "finding_id": finding_id,
                "verified": False,
                "reason": "Finding has no supporting evidence relationships in graph.",
            }

        evidence_audits = []
        for eid, tool in ev_rows:
            # 2. Check evidence node and referenced transactions
            tx_rows = self._conn.graph.ro_query(
                "MATCH (e:Evidence {evidence_id: $eid})-[:REFERENCES]->(t:Transaction) "
                "RETURN t.transaction_id, t.amount, t.currency, t.timestamp",
                {"eid": eid},
            ).result_set

            if not tx_rows:
                evidence_audits.append({"evidence_id": eid, "valid": False, "reason": "Evidence references no transactions."})
                continue

            # 3. Check transaction connections to accounts
            connected_ok = True
            for tx_id, amt, curr, ts in tx_rows:
                acc_check = self._conn.graph.ro_query(
                    "MATCH (src:Account)-[:SENT]->(t:Transaction {transaction_id: $tid})-[:RECEIVED_BY]->(dst:Account) "
                    "RETURN count(src), count(dst)",
                    {"tid": tx_id},
                ).result_set[0]
                if acc_check[0] == 0 or acc_check[1] == 0:
                    connected_ok = False
                    break

            if not connected_ok:
                evidence_audits.append({
                    "evidence_id": eid,
                    "valid": False,
                    "reason": "Referenced transactions lack valid account endpoints.",
                })
            else:
                evidence_audits.append({
                    "evidence_id": eid,
                    "valid": True,
                    "transaction_count": len(tx_rows),
                    "tool": tool,
                })

        all_valid = all(a["valid"] for a in evidence_audits)
        return {
            "finding_id": finding_id,
            "kind": f_node.get("kind"),
            "statement": f_node.get("statement"),
            "verified": all_valid,
            "evidence_audits": evidence_audits,
            "reason": "Evidence chain verified against real transactions." if all_valid else "Broken evidence references.",
        }

    def execute_task(self, task: AgentTaskRecord, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute evidence audit over finding IDs or investigation context."""
        finding_ids = context.get("finding_ids", [])
        if not finding_ids:
            # Look up findings belonging to this investigation in graph
            inv_id = task.investigation_id
            rows = self._conn.graph.ro_query(
                "MATCH (i:Investigation {investigation_id: $inv_id})-[:HAS_FINDING]->(f:Finding) "
                "RETURN f.finding_id",
                {"inv_id": inv_id},
            ).result_set
            finding_ids = [r[0] for r in rows]

        audits = [self.audit_finding(fid) for fid in finding_ids]
        verified_count = sum(1 for a in audits if a["verified"])
        rejected_count = len(audits) - verified_count

        return {
            "status": "completed",
            "total_findings": len(audits),
            "verified_count": verified_count,
            "rejected_count": rejected_count,
            "verified_finding_ids": [a["finding_id"] for a in audits if a["verified"]],
            "rejected_finding_ids": [a["finding_id"] for a in audits if not a["verified"]],
            "audits": audits,
            "summary": f"Audited {len(audits)} findings: {verified_count} verified, {rejected_count} rejected.",
        }
