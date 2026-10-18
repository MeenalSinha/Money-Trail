"""Minimal validated read/write layer over the schema.

Only labels and relationship types from graph_schema are interpolated into
Cypher; all values are bound as parameters. This is not an ingestion pipeline.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from redis import exceptions as redis_exc

from money_trail.database import graph_schema as gs
from money_trail.database.falkordb_client import FalkorDBConnection
from money_trail.exceptions import GraphWriteError, ValidationError


class GraphStore:
    def __init__(self, conn: FalkorDBConnection) -> None:
        self._conn = conn

    def upsert_node(self, label: str, props: Mapping[str, Any]) -> None:
        """Create the node, or update its properties if the identity already exists.

        FalkorDB checks mandatory constraints at node creation, so all properties
        must be supplied in the CREATE itself (MERGE-then-SET would be rejected).
        Property names are validated against the registry before being interpolated.
        """
        spec = gs.get_node_spec(label)
        clean = gs.validate_node(label, props)
        key = clean[spec.id_property]
        graph = self._conn.graph

        update = f"MATCH (n:{spec.label} {{{spec.id_property}: $key}}) SET n += $props RETURN count(n)"
        create = (
            f"CREATE (n:{spec.label} {{"
            + ", ".join(f"{name}: $p_{name}" for name in clean)
            + "})"
        )
        create_params = {f"p_{name}": value for name, value in clean.items()}

        if graph.query(update, {"key": key, "props": clean}).result_set[0][0]:
            return
        try:
            graph.query(create, create_params)
        except redis_exc.ResponseError:
            # A concurrent writer created the same identity first (unique constraint).
            # Fall back to updating it; any other cause makes this second call fail loudly too.
            if not graph.query(update, {"key": key, "props": clean}).result_set[0][0]:
                raise

    def link(self, rel_type: str, source_id: str, target_id: str,
             props: Optional[Mapping[str, Any]] = None) -> None:
        rel = gs.get_rel_spec(rel_type)
        clean = gs.validate_relationship(rel_type, props)
        src, dst = gs.get_node_spec(rel.source), gs.get_node_spec(rel.target)
        query = (
            f"MATCH (a:{src.label} {{{src.id_property}: $src}}), "
            f"(b:{dst.label} {{{dst.id_property}: $dst}}) "
            f"MERGE (a)-[r:{rel.type}]->(b) SET r += $props RETURN count(r)"
        )
        rows = self._conn.graph.query(query, {"src": source_id, "dst": target_id, "props": clean}).result_set
        if not rows or rows[0][0] == 0:
            raise ValidationError(
                f"Cannot create {rel_type}: {rel.source} or {rel.target} endpoint does not exist."
            )

    def get_node(self, label: str, node_id: str) -> Optional[Dict[str, Any]]:
        spec = gs.get_node_spec(label)
        rows = self._conn.graph.ro_query(
            f"MATCH (n:{spec.label} {{{spec.id_property}: $id}}) RETURN n", {"id": node_id}
        ).result_set
        return dict(rows[0][0].properties) if rows else None

    def related(self, rel_type: str, source_id: str) -> List[str]:
        """Identity values of targets reachable from source via one edge in schema direction."""
        rel = gs.get_rel_spec(rel_type)
        src, dst = gs.get_node_spec(rel.source), gs.get_node_spec(rel.target)
        rows = self._conn.graph.ro_query(
            f"MATCH (a:{src.label} {{{src.id_property}: $src}})-[:{rel.type}]->(b:{dst.label}) "
            f"RETURN b.{dst.id_property} ORDER BY b.{dst.id_property}",
            {"src": source_id},
        ).result_set
        return [r[0] for r in rows]

    def count_nodes(self) -> int:
        return self._conn.graph.ro_query("MATCH (n) RETURN count(n)").result_set[0][0]


    # --- batch helpers (used by ingestion) -----------------------------------
    # Same rules as the single-record helpers: labels, relationship types and
    # property names come from the schema registry; every value is a parameter.

    @staticmethod
    def _wrap(exc: Exception, what: str) -> GraphWriteError:
        return GraphWriteError(f"{what} failed: {type(exc).__name__}: {str(exc)[:200]}")

    def fetch_nodes(self, label: str, ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """Existing nodes (properties) for the given identities. Read-only."""
        spec = gs.get_node_spec(label)
        if not ids:
            return {}
        rows = self._conn.graph.ro_query(
            f"MATCH (n:{spec.label}) WHERE n.{spec.id_property} IN $ids RETURN n",
            {"ids": list(ids)},
        ).result_set
        return {r[0].properties[spec.id_property]: dict(r[0].properties) for r in rows}

    def fetch_transaction_endpoints(self, ids: Sequence[str]) -> Dict[str, Tuple[Set[str], Set[str]]]:
        """Existing (senders, receivers) account ids for each existing transaction. Read-only."""
        if not ids:
            return {}
        rows = self._conn.graph.ro_query(
            "MATCH (t:Transaction) WHERE t.transaction_id IN $ids "
            "OPTIONAL MATCH (a:Account)-[:SENT]->(t) "
            "OPTIONAL MATCH (t)-[:RECEIVED_BY]->(b:Account) "
            "RETURN t.transaction_id, collect(DISTINCT a.account_id), collect(DISTINCT b.account_id)",
            {"ids": list(ids)},
        ).result_set
        return {r[0]: (set(r[1]), set(r[2])) for r in rows}

    def create_nodes(self, label: str, rows: Sequence[Mapping[str, Any]]) -> int:
        """Create nodes that are known not to exist. Returns nodes actually created.

        All properties are given in CREATE because FalkorDB checks mandatory
        constraints at creation time. A constraint violation rolls back the whole statement.
        """
        spec = gs.get_node_spec(label)
        clean = [gs.validate_node(label, r) for r in rows]
        if not clean:
            return 0
        keys = sorted({k for r in clean for k in r})  # registry-validated property names
        body = ", ".join(f"{k}: r.{k}" for k in keys)
        try:
            res = self._conn.graph.query(f"UNWIND $rows AS r CREATE (n:{spec.label} {{{body}}})", {"rows": clean})
        except Exception as exc:
            raise self._wrap(exc, f"create {label} batch") from None
        return int(res.nodes_created)

    def update_nodes(self, label: str, rows: Sequence[Mapping[str, Any]]) -> int:
        """Merge supplied properties into existing nodes (never removes properties)."""
        spec = gs.get_node_spec(label)
        clean = [gs.validate_node(label, r) for r in rows]
        if not clean:
            return 0
        try:
            res = self._conn.graph.query(
                f"UNWIND $rows AS r MATCH (n:{spec.label} {{{spec.id_property}: r.{spec.id_property}}}) "
                "SET n += r RETURN count(n)",
                {"rows": clean},
            )
        except Exception as exc:
            raise self._wrap(exc, f"update {label} batch") from None
        return int(res.result_set[0][0])

    def link_batch(self, rel_type: str, pairs: Sequence[Tuple[str, str]]) -> Tuple[int, int]:
        """MERGE one relationship per (source_id, target_id). Returns (matched, created).

        Raises GraphWriteError when any endpoint is missing (matched < requested).
        """
        rel = gs.get_rel_spec(rel_type)
        src, dst = gs.get_node_spec(rel.source), gs.get_node_spec(rel.target)
        if not pairs:
            return 0, 0
        rows = [{"src": a, "dst": b} for a, b in pairs]
        try:
            res = self._conn.graph.query(
                f"UNWIND $rows AS r MATCH (a:{src.label} {{{src.id_property}: r.src}}), "
                f"(b:{dst.label} {{{dst.id_property}: r.dst}}) "
                f"MERGE (a)-[x:{rel.type}]->(b) RETURN count(x)",
                {"rows": rows},
            )
        except Exception as exc:
            raise self._wrap(exc, f"link {rel_type} batch") from None
        matched = int(res.result_set[0][0]) if res.result_set else 0
        if matched != len(pairs):
            raise GraphWriteError(
                f"link {rel_type} batch: {len(pairs) - matched} of {len(pairs)} endpoint pairs did not exist"
            )
        return matched, int(res.relationships_created)

    def count_matching(self, label: str, prop: str, value: Any) -> int:
        spec = gs.get_node_spec(label)
        spec.prop(prop)  # must be a schema property
        return self._conn.graph.ro_query(
            f"MATCH (n:{spec.label} {{{prop}: $v}}) RETURN count(n)", {"v": value}
        ).result_set[0][0]

    def count_id_prefix(self, label: str, prefix: str) -> int:
        spec = gs.get_node_spec(label)
        return self._conn.graph.ro_query(
            f"MATCH (n:{spec.label}) WHERE n.{spec.id_property} STARTS WITH $p RETURN count(n)", {"p": prefix}
        ).result_set[0][0]

    def verify_transactions(self, rows: Sequence[Mapping[str, str]]) -> int:
        """How many (id, src, dst) rows exist with SENT and RECEIVED_BY in the schema direction."""
        if not rows:
            return 0
        return self._conn.graph.ro_query(
            "UNWIND $rows AS r MATCH (a:Account {account_id: r.src})-[:SENT]->"
            "(t:Transaction {transaction_id: r.id})-[:RECEIVED_BY]->(b:Account {account_id: r.dst}) "
            "RETURN count(t)", {"rows": list(rows)},
        ).result_set[0][0]

    def verify_alert_links(self, rows: Sequence[Mapping[str, str]]) -> int:
        """How many (alert_id, transaction_id) rows exist as (Alert)-[:FLAGS]->(Transaction)."""
        if not rows:
            return 0
        return self._conn.graph.ro_query(
            "UNWIND $rows AS r MATCH (al:Alert {alert_id: r.id})-[:FLAGS]->(t:Transaction {transaction_id: r.tx}) "
            "RETURN count(al)", {"rows": list(rows)},
        ).result_set[0][0]
