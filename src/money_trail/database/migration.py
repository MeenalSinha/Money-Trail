"""Durable schema versioning and additive migrations for FalkorDB.

Maintains an auditable, durable record of schema migrations in the graph
via (:SchemaMigration) nodes, ensuring that existing financial data, investigations,
findings, and evidence are never deleted or rebuilt during upgrades.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from money_trail.database import graph_schema as gs
from money_trail.database.falkordb_client import FalkorDBConnection
from money_trail.database.graph_store import GraphStore
from money_trail.database.initialize_graph import (
    InitResult,
    _wait_indexes,
    _wait_operational,
    inspect_schema,
)
from money_trail.exceptions import SchemaError

log = logging.getLogger(__name__)

MIGRATION_V1_DESC = "Phase 1-4 baseline schema (Account, Transaction, Alert, Investigation, Finding, Evidence, Agent)"
MIGRATION_V2_DESC = "Phase 5 persistent graph memory, agent sessions, tasks, handoffs, and coordination"


@dataclass
class MigrationResult:
    graph_name: str
    from_version: int
    to_version: int
    applied: List[int] = field(default_factory=list)
    indexes_created: List[str] = field(default_factory=list)
    constraints_created: List[str] = field(default_factory=list)
    already_current: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.applied or self.indexes_created or self.constraints_created)


def _now_ms() -> int:
    return int(time.time() * 1000)


def get_applied_migrations(conn: FalkorDBConnection) -> List[Dict[str, Any]]:
    """Return all recorded migrations sorted by version ascending."""
    if not conn.graph_exists():
        return []
    try:
        rows = conn.graph.ro_query(
            "MATCH (m:SchemaMigration) "
            "RETURN m.migration_id, m.version, m.applied_at, m.description, m.status "
            "ORDER BY m.version ASC"
        ).result_set
        return [
            {
                "migration_id": r[0],
                "version": int(r[1]),
                "applied_at": int(r[2]),
                "description": r[3],
                "status": r[4],
            }
            for r in rows
        ]
    except Exception:
        return []


def get_active_schema_version(conn: FalkorDBConnection) -> int:
    """Return the highest applied schema version, or 0 if uninitialized / unversioned."""
    migrations = get_applied_migrations(conn)
    if migrations:
        return max(m["version"] for m in migrations if m["status"] == "completed")
    if not conn.graph_exists():
        return 0
    # Graph exists but no SchemaMigration node: check if v1 elements are present
    state = inspect_schema(conn)
    v1_id_props = {
        ("Account", "account_id"),
        ("Transaction", "transaction_id"),
        ("Alert", "alert_id"),
        ("Investigation", "investigation_id"),
        ("Finding", "finding_id"),
        ("Evidence", "evidence_id"),
        ("Agent", "agent_id"),
    }
    if v1_id_props <= state.present_indexes:
        return 1
    return 0


def record_migration(
    conn: FalkorDBConnection,
    version: int,
    description: str,
    status: str = "completed",
    applied_at: Optional[int] = None,
) -> None:
    """Durably record a migration node in the graph."""
    migration_id = f"migration_v{version}"
    ts = applied_at or _now_ms()
    store = GraphStore(conn)
    store.upsert_node(
        "SchemaMigration",
        {
            "migration_id": migration_id,
            "version": version,
            "applied_at": ts,
            "description": description,
            "status": status,
        },
    )


def migrate_schema(conn: FalkorDBConnection, wait_timeout: float = 30.0) -> MigrationResult:
    """Non-destructive, additive schema migration to the latest SCHEMA_VERSION.

    Safe to run repeatedly. Never drops existing nodes, relationships, or indexes.
    Adds any missing indexes and constraints, verifies operational state,
    and durably logs the migration.
    """
    name = conn.settings.graph_name
    conn.ping()
    existed = conn.graph_exists()
    graph = conn.graph

    if not existed:
        # Bootstrap empty graph
        graph.query("CREATE (:__schema_bootstrap__)")
        graph.query("MATCH (n:__schema_bootstrap__) DELETE n")
        log.info("Created graph %s during migration", name)

    from_ver = get_active_schema_version(conn)
    target_ver = gs.SCHEMA_VERSION
    result = MigrationResult(graph_name=name, from_version=from_ver, to_version=target_ver)

    state = inspect_schema(conn)
    if state.ready and from_ver >= target_ver:
        result.already_current = True
        return result

    try:
        # 1. Apply any missing indexes
        for idx in state.missing_indexes:
            graph.create_node_range_index(idx.label, idx.prop)
            result.indexes_created.append(f"{idx.label}.{idx.prop}")
        if result.indexes_created:
            _wait_indexes(conn, wait_timeout)

        # 2. Apply any missing constraints
        created_constraints: Set[Tuple[str, str, str]] = set()
        for con in state.missing_constraints:
            if con.kind == "UNIQUE":
                graph.create_node_unique_constraint(con.label, con.prop)
            else:
                graph.create_node_mandatory_constraint(con.label, con.prop)
            result.constraints_created.append(f"{con.kind} {con.label}.{con.prop}")
            created_constraints.add((con.kind, con.label, con.prop))

        if created_constraints:
            _wait_operational(conn, created_constraints, wait_timeout)

        # 3. Durably record historical migrations
        if from_ver < 1:
            record_migration(conn, 1, MIGRATION_V1_DESC, "completed")
            result.applied.append(1)
        if from_ver < 2:
            record_migration(conn, 2, MIGRATION_V2_DESC, "completed")
            result.applied.append(2)

    except SchemaError:
        raise
    except Exception as exc:
        raise SchemaError(
            f"Schema migration failed ({type(exc).__name__}): {exc}. "
            "No data was deleted; re-run after resolving the cause."
        ) from None

    final = inspect_schema(conn)
    if not final.ready:
        raise SchemaError("Schema is not ready after migration; refusing to report success.")

    log.info("Schema migration complete on graph %s: v%s -> v%s", name, from_ver, target_ver)
    return result
