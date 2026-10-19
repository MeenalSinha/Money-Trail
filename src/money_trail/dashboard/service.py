"""Dashboard application service layer.

Provides clean, validated, and bounded data access for the interactive dashboard,
delegating strictly to existing Phase 1-5 backend engines, stores, and schema registries.
Does NOT duplicate business logic, Cypher rules, or detectors.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from money_trail.coordination.models import AgentRole, SessionStatus, TaskStatus
from money_trail.coordination.orchestrator import MultiAgentOrchestrator
from money_trail.coordination.store import CoordinationStore
from money_trail.database import FalkorDBConnection, GraphStore
from money_trail.database.migration import get_active_schema_version, get_applied_migrations
from money_trail.exceptions import ValidationError
from money_trail.investigation.findings import list_findings, get_finding, get_evidence
from money_trail.memory.models import MemoryScope, MemoryStatus, MemoryType
from money_trail.memory.store import MemoryStore

log = logging.getLogger(__name__)


class DashboardService:
    """Service facade providing data for dashboard views."""

    def __init__(self, conn: FalkorDBConnection):
        self._conn = conn
        self._store = GraphStore(conn)
        self._mem_store = MemoryStore(conn)
        self._coord_store = CoordinationStore(conn)
        self._orchestrator = MultiAgentOrchestrator(conn)

    @property
    def conn(self) -> FalkorDBConnection:
        return self._conn

    # -------------------------------------------------------------------------
    # 1. System & Database Health
    # -------------------------------------------------------------------------
    def get_system_health(self) -> Dict[str, Any]:
        """Perform non-destructive health checks across database and subsystems."""
        status = {
            "database_connected": False,
            "ping_latency_ms": None,
            "active_schema_version": None,
            "applied_migrations_count": 0,
            "graph_name": self._conn.settings.graph_name,
            "endpoint": f"{self._conn.settings.host}:{self._conn.settings.port}",
            "errors": [],
        }
        t0 = time.monotonic()
        try:
            self._conn.ping()
            status["ping_latency_ms"] = round((time.monotonic() - t0) * 1000, 2)
            status["database_connected"] = True
        except Exception as exc:
            status["errors"].append(f"Database connection failed: {exc}")
            return status

        try:
            status["active_schema_version"] = get_active_schema_version(self._conn)
            migrations = get_applied_migrations(self._conn)
            status["applied_migrations_count"] = len(migrations)
            status["migrations"] = migrations
        except Exception as exc:
            status["errors"].append(f"Schema check failed: {exc}")

        return status

    # -------------------------------------------------------------------------
    # 2. Overview & Dataset Metrics
    # -------------------------------------------------------------------------
    def get_overview_metrics(self, dataset: Optional[str] = None) -> Dict[str, Any]:
        """Calculate high-level operational metrics using bounded queries."""
        where_tx = " WHERE t.dataset = $ds" if dataset else ""
        where_acc = " WHERE a.dataset = $ds" if dataset else ""
        params = {"ds": dataset} if dataset else {}

        tx_count = self._conn.graph.ro_query(
            f"MATCH (t:Transaction){where_tx} RETURN count(t)", params
        ).result_set[0][0]

        acc_count = self._conn.graph.ro_query(
            f"MATCH (a:Account){where_acc} RETURN count(a)", params
        ).result_set[0][0]

        alert_count = self._conn.graph.ro_query(
            "MATCH (al:Alert) RETURN count(al)"
        ).result_set[0][0]

        inv_rows = self._conn.graph.ro_query(
            "MATCH (i:Investigation) RETURN i.status, count(i)"
        ).result_set
        inv_counts = {"total": 0, "open": 0, "in_progress": 0, "closed": 0}
        for st, cnt in inv_rows:
            inv_counts["total"] += cnt
            if st in inv_counts:
                inv_counts[st] = cnt

        finding_rows = self._conn.graph.ro_query(
            "MATCH (f:Finding) RETURN f.kind, count(f)"
        ).result_set
        finding_counts = {"total": 0, "observation": 0, "hypothesis": 0, "interpretation": 0}
        for kind, cnt in finding_rows:
            finding_counts["total"] += cnt
            if kind in finding_counts:
                finding_counts[kind] = cnt

        task_rows = self._conn.graph.ro_query(
            "MATCH (t:AgentTask) RETURN t.status, count(t)"
        ).result_set
        task_counts = {"total": 0, "pending": 0, "ready": 0, "running": 0, "completed": 0, "blocked": 0, "failed": 0}
        for st, cnt in task_rows:
            task_counts["total"] += cnt
            if st in task_counts:
                task_counts[st] = cnt

        memory_count = self._conn.graph.ro_query(
            "MATCH (m:Memory) RETURN count(m)"
        ).result_set[0][0]

        session_count = self._conn.graph.ro_query(
            "MATCH (s:AgentSession) RETURN count(s)"
        ).result_set[0][0]

        return {
            "transactions": tx_count,
            "accounts": acc_count,
            "alerts": alert_count,
            "investigations": inv_counts,
            "findings": finding_counts,
            "tasks": task_counts,
            "memories": memory_count,
            "sessions": session_count,
        }

    def list_datasets(self) -> List[str]:
        """Fetch list of distinct dataset namespaces currently present in graph."""
        rows = self._conn.graph.ro_query(
            "MATCH (t:Transaction) RETURN DISTINCT t.dataset ORDER BY t.dataset"
        ).result_set
        datasets = [r[0] for r in rows if r[0]]
        return datasets

    # -------------------------------------------------------------------------
    # 3. Investigation Workspace
    # -------------------------------------------------------------------------
    def list_investigations(
        self,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """List investigations with finding counts and anchors."""
        effective_limit = min(max(1, limit), 200)
        where = ["1=1"]
        params: Dict[str, Any] = {}
        if status:
            where.append("i.status = $status")
            params["status"] = status

        cypher = f"""
        MATCH (i:Investigation)
        WHERE {' AND '.join(where)}
        OPTIONAL MATCH (i)-[:STARTED_FROM]->(t:Transaction)
        OPTIONAL MATCH (i)-[:HAS_FINDING]->(f:Finding)
        RETURN i.investigation_id, i.status, i.title, i.created_at, t.transaction_id, count(f)
        ORDER BY i.created_at DESC
        LIMIT {effective_limit}
        """
        rows = self._conn.graph.ro_query(cypher, params).result_set
        results = []
        for inv_id, st, title, created_at, anchor_id, fnd_count in rows:
            results.append({
                "investigation_id": inv_id,
                "status": st,
                "title": title or "Investigation",
                "created_at": created_at,
                "anchor_id": anchor_id,
                "findings_count": fnd_count,
            })
        return results

    def get_investigation_workspace(self, investigation_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve complete investigation state including findings, tasks, handoffs, sessions."""
        inv_node = self._store.get_node("Investigation", investigation_id)
        if not inv_node:
            return None

        # Anchor transaction
        anchor_rows = self._conn.graph.ro_query(
            "MATCH (i:Investigation {investigation_id: $id})-[:STARTED_FROM]->(t:Transaction) "
            "RETURN t.transaction_id, t.amount, t.currency, t.timestamp, t.dataset",
            {"id": investigation_id},
        ).result_set
        anchor_info = None
        if anchor_rows:
            anchor_info = {
                "transaction_id": anchor_rows[0][0],
                "amount": anchor_rows[0][1],
                "currency": anchor_rows[0][2],
                "timestamp": anchor_rows[0][3],
                "dataset": anchor_rows[0][4],
            }

        # Findings and supporting evidence
        findings = list_findings(self._conn, investigation_id=investigation_id, limit=50)

        # Tasks
        tasks = [t.__dict__ for t in self._coord_store.list_tasks(investigation_id)]

        # Handoffs
        handoffs = [h.__dict__ for h in self._coord_store.list_handoffs(investigation_id)]

        # Sessions
        sess_rows = self._conn.graph.ro_query(
            "MATCH (i:Investigation {investigation_id: $id})-[:HAS_SESSION]->(s:AgentSession) "
            "RETURN s.session_id, s.status, s.started_at, s.completed_at, s.updated_at "
            "ORDER BY s.started_at DESC",
            {"id": investigation_id},
        ).result_set
        sessions = [
            {"session_id": r[0], "status": r[1], "started_at": r[2], "completed_at": r[3], "updated_at": r[4]}
            for r in sess_rows
        ]

        # Scoped Memories
        memories = [
            m.__dict__
            for m in self._mem_store.retrieve_memories(
                scope=MemoryScope.INVESTIGATION.value,
                investigation_id=investigation_id,
                limit=50,
            )
        ]

        return {
            "investigation_id": investigation_id,
            "status": inv_node.get("status"),
            "title": inv_node.get("title"),
            "created_at": inv_node.get("created_at"),
            "updated_at": inv_node.get("updated_at"),
            "anchor": anchor_info,
            "findings": findings.get("findings", []),
            "tasks": tasks,
            "handoffs": handoffs,
            "sessions": sessions,
            "memories": memories,
        }

    def start_investigation(
        self,
        anchor_id: str,
        dataset: Optional[str] = None,
        question: Optional[str] = None,
        investigation_id: Optional[str] = None,
        persist: bool = True,
    ) -> Dict[str, Any]:
        """Validate input anchor and start coordinated multi-agent investigation."""
        # Confirm transaction anchor exists
        tx_node = self._store.get_node("Transaction", anchor_id)
        if not tx_node:
            raise ValidationError(f"Anchor transaction '{anchor_id}' does not exist in FalkorDB.")

        inv_id = investigation_id or f"inv_{anchor_id.replace(':', '_')}_{int(time.time())}"
        ds = dataset or tx_node.get("dataset")

        res = self._orchestrator.run_investigation(
            investigation_id=inv_id,
            anchor_id=anchor_id,
            dataset=ds,
            question=question or "Trace connected suspicious transfer patterns.",
            persist=persist,
        )
        return res

    def resume_investigation(self, session_id: str) -> Dict[str, Any]:
        """Resume an interrupted investigation session from graph state."""
        return self._orchestrator.resume_investigation(session_id)

    # -------------------------------------------------------------------------
    # 4. Interactive Graph Explorer
    # -------------------------------------------------------------------------
    def get_transaction_graph_network(
        self,
        transaction_id: str,
        max_hops: int = 2,
        max_nodes: int = 100,
    ) -> Dict[str, Any]:
        """Retrieve bounded transaction-account subgraph for interactive visualization."""
        tx_node = self._store.get_node("Transaction", transaction_id)
        if not tx_node:
            raise ValidationError(f"Transaction '{transaction_id}' not found.")

        # Cypher traversal up to max_hops (alternating Transaction -> Account -> Transaction)
        cypher = """
        MATCH (start:Transaction {transaction_id: $tid})
        OPTIONAL MATCH (src:Account)-[:SENT]->(start)
        OPTIONAL MATCH (start)-[:RECEIVED_BY]->(dst:Account)
        OPTIONAL MATCH (dst)-[:SENT]->(t2:Transaction)-[:RECEIVED_BY]->(a3:Account)
        RETURN start, src, dst, t2, a3
        LIMIT $lim
        """
        rows = self._conn.graph.ro_query(cypher, {"tid": transaction_id, "lim": max_nodes}).result_set

        nodes: Dict[str, Dict[str, Any]] = {}
        edges: List[Dict[str, Any]] = []
        edge_keys: set = set()

        def add_node(id_val: str, label: str, props: Dict[str, Any], is_center: bool = False):
            if id_val not in nodes:
                nodes[id_val] = {
                    "id": id_val,
                    "label": f"{label}\n{id_val[-8:] if len(id_val) > 8 else id_val}",
                    "type": label,
                    "is_center": is_center,
                    "properties": props,
                }

        def add_edge(src_id: str, dst_id: str, rel_type: str):
            key = f"{src_id}->{dst_id}:{rel_type}"
            if key not in edge_keys:
                edge_keys.add(key)
                edges.append({"from": src_id, "to": dst_id, "label": rel_type})

        # Center node
        add_node(transaction_id, "Transaction", tx_node, is_center=True)

        for row in rows:
            st, src, dst, t2, a3 = row
            if src:
                src_id = src.properties.get("account_id")
                add_node(src_id, "Account", src.properties)
                add_edge(src_id, transaction_id, "SENT")
            if dst:
                dst_id = dst.properties.get("account_id")
                add_node(dst_id, "Account", dst.properties)
                add_edge(transaction_id, dst_id, "RECEIVED_BY")
            if t2 and dst:
                t2_id = t2.properties.get("transaction_id")
                add_node(t2_id, "Transaction", t2.properties)
                add_edge(dst.properties.get("account_id"), t2_id, "SENT")
                if a3:
                    a3_id = a3.properties.get("account_id")
                    add_node(a3_id, "Account", a3.properties)
                    add_edge(t2_id, a3_id, "RECEIVED_BY")

        return {
            "center_id": transaction_id,
            "nodes": list(nodes.values()),
            "edges": edges,
        }

    def get_pattern_subgraph(self, finding_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve the exact subgraph of transactions and accounts supporting a finding."""
        fnd = get_finding(self._conn, finding_id, limit=100)
        if not fnd:
            return None

        tx_ids = []
        for ev in fnd.get("evidence", []):
            if "transaction_ids" in ev:
                tx_ids.extend(ev["transaction_ids"])

        if not tx_ids:
            return None

        # Fetch accounts and transfers for these transactions
        cypher = """
        MATCH (src:Account)-[:SENT]->(t:Transaction)-[:RECEIVED_BY]->(dst:Account)
        WHERE t.transaction_id IN $tx_ids
        RETURN src.account_id, t.transaction_id, t.amount, t.currency, t.timestamp, dst.account_id
        ORDER BY t.timestamp ASC
        """
        rows = self._conn.graph.ro_query(cypher, {"tx_ids": tx_ids}).result_set

        nodes: Dict[str, Dict[str, Any]] = {}
        edges: List[Dict[str, Any]] = []

        def add_node(id_val: str, label: str, title: str):
            if id_val not in nodes:
                nodes[id_val] = {
                    "id": id_val,
                    "label": f"{label}\n{id_val[-8:] if len(id_val) > 8 else id_val}",
                    "type": label,
                    "title": title,
                }

        for src_acc, tid, amt, curr, ts, dst_acc in rows:
            add_node(src_acc, "Account", f"Account: {src_acc}")
            add_node(tid, "Transaction", f"Tx: {tid} ({amt} {curr})")
            add_node(dst_acc, "Account", f"Account: {dst_acc}")

            edges.append({"from": src_acc, "to": tid, "label": "SENT"})
            edges.append({"from": tid, "to": dst_acc, "label": f"RECEIVED_BY\n{amt} {curr}"})

        return {
            "finding_id": finding_id,
            "statement": fnd.get("statement"),
            "kind": fnd.get("kind"),
            "nodes": list(nodes.values()),
            "edges": edges,
            "transactions": [
                {"src": r[0], "tx": r[1], "amount": r[2], "currency": r[3], "timestamp": r[4], "dst": r[5]}
                for r in rows
            ],
        }

    # -------------------------------------------------------------------------
    # 5. Search & Filters
    # -------------------------------------------------------------------------
    def search_transactions(
        self,
        dataset: Optional[str] = None,
        query: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Search transactions by ID or dataset prefix with bounds."""
        effective_limit = min(max(1, limit), 100)
        where = ["1=1"]
        params: Dict[str, Any] = {}

        if dataset:
            where.append("t.dataset = $ds")
            params["ds"] = dataset
        if query and query.strip():
            where.append("t.transaction_id CONTAINS $q")
            params["q"] = query.strip()

        cypher = f"""
        MATCH (src:Account)-[:SENT]->(t:Transaction)-[:RECEIVED_BY]->(dst:Account)
        WHERE {' AND '.join(where)}
        RETURN t.transaction_id, t.amount, t.currency, t.timestamp, t.dataset, src.account_id, dst.account_id
        ORDER BY t.timestamp DESC
        LIMIT {effective_limit}
        """
        rows = self._conn.graph.ro_query(cypher, params).result_set
        return [
            {
                "transaction_id": r[0],
                "amount": r[1],
                "currency": r[2],
                "timestamp": r[3],
                "dataset": r[4],
                "source_account": r[5],
                "target_account": r[6],
            }
            for r in rows
        ]
