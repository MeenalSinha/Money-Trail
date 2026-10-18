"""Safe, idempotent graph initialization.

Initialization only ever adds missing indexes and constraints. It never
deletes data, drops schema elements, or recreates a graph. A read-only
inspection (inspect_schema) is shared with the health check.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import List, Set, Tuple

from money_trail.database import graph_schema as gs
from money_trail.database.falkordb_client import FalkorDBConnection
from money_trail.exceptions import SchemaError

log = logging.getLogger(__name__)

_CONSTRAINT_KINDS = {"UNIQUE": "unique", "MANDATORY": "mandatory"}


@dataclass
class SchemaState:
    present_indexes: Set[Tuple[str, str]]
    present_constraints: Set[Tuple[str, str, str]]  # (kind, label, prop), OPERATIONAL only
    pending_constraints: Set[Tuple[str, str, str]]
    missing_indexes: List[gs.IndexSpec]
    missing_constraints: List[gs.ConstraintSpec]

    @property
    def ready(self) -> bool:
        return not self.missing_indexes and not self.missing_constraints and not self.pending_constraints


@dataclass
class InitResult:
    graph_name: str
    graph_created: bool
    indexes_created: List[str] = field(default_factory=list)
    constraints_created: List[str] = field(default_factory=list)
    already_present: int = 0
    schema_version: int = gs.SCHEMA_VERSION

    @property
    def changed(self) -> bool:
        return bool(self.graph_created or self.indexes_created or self.constraints_created)


def inspect_schema(conn: FalkorDBConnection) -> SchemaState:
    """Read-only. Requires the graph to exist."""
    graph = conn.graph
    idx_rows = graph.ro_query(
        "CALL db.indexes() YIELD label, properties, types, entitytype, status "
        "RETURN label, properties, types, entitytype, status"
    ).result_set
    present_indexes: Set[Tuple[str, str]] = set()
    for label, props, types, entity, status in idx_rows:
        if entity != "NODE":
            continue
        for prop in props:
            if "RANGE" in (types.get(prop) or []):
                present_indexes.add((label, prop))

    present, pending = set(), set()
    for c in graph.list_constraints():
        if c.get("entitytype") != "NODE":
            continue
        for prop in c["properties"]:
            key = (c["type"], c["label"], prop)
            (present if c.get("status") == "OPERATIONAL" else pending).add(key)

    missing_idx = [i for i in gs.required_indexes() if (i.label, i.prop) not in present_indexes]
    missing_con = [
        c for c in gs.required_constraints()
        if (c.kind, c.label, c.prop) not in present and (c.kind, c.label, c.prop) not in pending
    ]
    return SchemaState(present_indexes, present, pending, missing_idx, missing_con)


def _wait_operational(conn: FalkorDBConnection, wanted: Set[Tuple[str, str, str]], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        state = inspect_schema(conn)
        if wanted <= state.present_constraints:
            return
        if time.monotonic() >= deadline:
            raise SchemaError(
                "Constraints were requested but did not become operational in time; "
                "existing data may violate them. Inspect the graph before retrying."
            )
        time.sleep(0.2)


def initialize_graph(conn: FalkorDBConnection, wait_timeout: float = 30.0) -> InitResult:
    """Create the configured graph if absent and add any missing schema elements."""
    name = conn.settings.graph_name
    conn.ping()
    existed = conn.graph_exists()
    graph = conn.graph
    result = InitResult(graph_name=name, graph_created=not existed)

    if not existed:
        # A graph key only exists once something is written. Create and remove a
        # marker node whose label is outside the schema; no other data is touched.
        graph.query("CREATE (:__schema_bootstrap__)")
        graph.query("MATCH (n:__schema_bootstrap__) DELETE n")
        log.info("Created graph %s", name)

    state = inspect_schema(conn)
    result.already_present = len(gs.required_indexes()) - len(state.missing_indexes) + (
        len(gs.required_constraints()) - len(state.missing_constraints)
    )

    try:
        for idx in state.missing_indexes:
            graph.create_node_range_index(idx.label, idx.prop)
            result.indexes_created.append(f"{idx.label}.{idx.prop}")
        _wait_indexes(conn, wait_timeout)
        created = set()
        for con in state.missing_constraints:
            if con.kind == "UNIQUE":
                graph.create_node_unique_constraint(con.label, con.prop)
            else:
                graph.create_node_mandatory_constraint(con.label, con.prop)
            result.constraints_created.append(f"{con.kind} {con.label}.{con.prop}")
            created.add((con.kind, con.label, con.prop))
        if created:
            _wait_operational(conn, created, wait_timeout)
    except SchemaError:
        raise
    except Exception as exc:
        raise SchemaError(f"Schema initialization failed ({type(exc).__name__}). "
                          "No data was deleted; re-run after fixing the cause.") from None

    final = inspect_schema(conn)
    if not final.ready:
        raise SchemaError("Schema is not ready after initialization; refusing to report success.")
    log.info("Schema ready on graph %s (changed=%s)", name, result.changed)
    return result


def _wait_indexes(conn: FalkorDBConnection, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        rows = conn.graph.ro_query("CALL db.indexes() YIELD status RETURN status").result_set
        if all(r[0] == "OPERATIONAL" for r in rows):
            return
        if time.monotonic() >= deadline:
            raise SchemaError("Indexes did not become operational in time.")
        time.sleep(0.2)
