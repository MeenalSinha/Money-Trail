"""Read-only health check separating connectivity, graph access and schema readiness."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional

from money_trail.database.falkordb_client import FalkorDBConnection
from money_trail.database.initialize_graph import inspect_schema
from money_trail.exceptions import ConfigurationError, DatabaseConnectionError, SchemaError

# Overall states
OK = "ok"
CONFIG_ERROR = "configuration_error"
UNREACHABLE = "database_unreachable"
GRAPH_MISSING = "graph_not_found"
SCHEMA_NOT_READY = "schema_not_ready"


@dataclass
class HealthReport:
    status: str
    configuration_valid: bool = False
    database_reachable: bool = False
    graph_accessible: bool = False
    schema_ready: bool = False
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        return self.status == OK

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def report_configuration_error(exc: ConfigurationError) -> HealthReport:
    return HealthReport(status=CONFIG_ERROR, message=str(exc))


def check_health(conn: FalkorDBConnection) -> HealthReport:
    """Contacts the real database. Never writes, never initializes."""
    report = HealthReport(status=UNREACHABLE, configuration_valid=True,
                          details=conn.settings.safe_summary())
    try:
        conn.ping()
        report.database_reachable = True
    except DatabaseConnectionError as exc:
        report.message = str(exc)
        return report

    try:
        if not conn.graph_exists():
            report.status = GRAPH_MISSING
            report.message = "Configured graph does not exist yet. Run money-trail-init."
            return report
        conn.graph.ro_query("RETURN 1")
        report.graph_accessible = True
    except DatabaseConnectionError as exc:
        report.status = UNREACHABLE
        report.database_reachable = False
        report.message = str(exc)
        return report
    except Exception as exc:
        report.status = GRAPH_MISSING
        report.message = f"Graph is not accessible ({type(exc).__name__})."
        return report

    try:
        state = inspect_schema(conn)
    except Exception as exc:
        report.status = SCHEMA_NOT_READY
        report.message = f"Schema could not be inspected ({type(exc).__name__})."
        return report

    report.details["missing_indexes"] = [f"{i.label}.{i.prop}" for i in state.missing_indexes]
    report.details["missing_constraints"] = [
        f"{c.kind} {c.label}.{c.prop}" for c in state.missing_constraints
    ]
    report.details["pending_constraints"] = sorted(" ".join(k) for k in state.pending_constraints)
    if state.ready:
        report.schema_ready = True
        report.status = OK
        report.message = "FalkorDB reachable, graph accessible, schema ready."
    else:
        report.status = SCHEMA_NOT_READY
        report.message = "Schema is incomplete. Run money-trail-init."
    return report
