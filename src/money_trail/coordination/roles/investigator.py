"""Graph Investigator agent role.

Reuses the Phase 4 investigation agent and registered graph tools to examine
connected transactions, accounts, and configured detection patterns.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from money_trail.agent.agent import InvestigationAgent
from money_trail.coordination.models import AgentRole, AgentTaskRecord
from money_trail.coordination.roles.base import BaseAgentRole
from money_trail.database import FalkorDBConnection
from money_trail.investigation.config import DetectionConfig

log = logging.getLogger(__name__)


class GraphInvestigatorRole(BaseAgentRole):
    def __init__(
        self,
        conn: FalkorDBConnection,
        agent: Optional[InvestigationAgent] = None,
        detection_config: Optional[DetectionConfig] = None,
    ) -> None:
        super().__init__(AgentRole.INVESTIGATOR, conn)
        self.agent = agent
        self.detection_config = detection_config

    def execute_task(self, task: AgentTaskRecord, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute transaction / account graph investigation."""
        anchor_id = context.get("anchor_id")
        anchor_type = context.get("anchor_type", "transaction")
        question = context.get("question", "Trace connected transfer patterns.")
        persist = context.get("persist", False)

        if not self.agent:
            # Fall back to deterministic engine investigation if agent instance is not provided
            from money_trail.investigation.engine import InvestigationEngine, InvestigationRequest
            from money_trail.investigation.findings import persist_investigation

            dc = self.detection_config
            if not dc:
                from money_trail.investigation.config import load_detection_config
                ex_path = Path("examples/detection.example.json")
                if ex_path.exists():
                    dc = load_detection_config(ex_path)
                else:
                    from support import detection_dict
                    from money_trail.investigation.config import parse_detection_config
                    dc = parse_detection_config(detection_dict())

            eng = InvestigationEngine(self._conn, dc)
            req = InvestigationRequest(
                anchor_type=anchor_type,
                anchor_id=anchor_id,
                namespace=context.get("namespace"),
            )
            result = eng.investigate(req)
            import time
            from money_trail.database import GraphStore
            from money_trail.investigation.detectors.models import evidence_id_for

            finding_ids = []
            evidence_ids = [evidence_id_for(d.detection_id) for d in result.detections]
            if persist and anchor_type == "transaction":
                store = GraphStore(self._conn)
                now_ms = int(time.time() * 1000)
                ns = context.get("namespace") or (anchor_id.split(":")[0] if ":" in anchor_id else "default")
                p_res = persist_investigation(
                    store,
                    config=dc,
                    namespace=ns,
                    anchor_tx=anchor_id,
                    detections=result.detections,
                    now_ms=now_ms,
                )
                finding_ids = p_res.get("finding_ids", [])
                evidence_ids = p_res.get("evidence_ids", evidence_ids)

            return {
                "status": "completed",
                "detections": [d.to_dict() for d in result.detections],
                "finding_ids": finding_ids,
                "evidence_ids": evidence_ids,
                "summary": f"Detected {len(result.detections)} patterns via graph investigation.",
            }

        # Use full InvestigationAgent
        report = self.agent.investigate(
            raw_request={
                "anchor": {"type": anchor_type, "id": anchor_id},
                "question": question,
                "dataset": context.get("namespace"),
            },
            persist=persist,
        )

        detections = report.get("detections", [])
        evidence_ids = []
        for d in detections:
            evidence_ids.extend(d.get("evidence_ids", []))

        finding_ids = []
        persisted = report.get("persistence")
        if persisted and isinstance(persisted, dict):
            findings_meta = persisted.get("findings", {})
            if isinstance(findings_meta, dict):
                finding_ids = [f["finding_id"] for f in findings_meta.get("records", [])]

        return {
            "status": "completed" if report.get("status") in ("completed", "patterns_detected", "no_patterns_detected") else "failed",
            "report": report,
            "detections": detections,
            "finding_ids": finding_ids,
            "evidence_ids": evidence_ids,
            "summary": report.get("summary", {}).get("headline", "Investigation completed"),
        }
