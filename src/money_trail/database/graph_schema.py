"""Single source of truth for the Money Trail graph model.

Labels, relationship types, property contracts, indexes and constraints are
defined here and nowhere else. Query builders elsewhere may only interpolate
identifiers that come from this registry; user data always travels as query
parameters.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple

from money_trail.exceptions import ValidationError

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PropertySpec:
    name: str
    type: type
    required: bool = False
    description: str = ""
    allowed: Optional[Tuple[str, ...]] = None  # closed vocabulary for strings
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    indexed: bool = False  # extra range index beyond the identity property


@dataclass(frozen=True)
class NodeSpec:
    label: str
    id_property: str
    purpose: str
    properties: Tuple[PropertySpec, ...]

    def prop(self, name: str) -> PropertySpec:
        for p in self.properties:
            if p.name == name:
                return p
        raise ValidationError(f"{self.label} has no property '{name}'.")

    @property
    def required(self) -> Tuple[str, ...]:
        return tuple(p.name for p in self.properties if p.required)


@dataclass(frozen=True)
class RelSpec:
    type: str
    source: str
    target: str
    purpose: str
    properties: Tuple[PropertySpec, ...] = ()
    core: bool = True  # False: added beyond the mandated relationship list


def _id(name: str, what: str) -> PropertySpec:
    return PropertySpec(name, str, required=True, description=f"Stable unique identifier of the {what}.")


FINDING_KINDS = ("observation", "interpretation", "hypothesis")
INVESTIGATION_STATUSES = ("open", "in_progress", "closed")

NODES: Tuple[NodeSpec, ...] = (
    NodeSpec(
        "Account", "account_id",
        "A financial account in a dataset. No owner or personal identity is assumed.",
        (
            _id("account_id", "account (namespaced by the ingestion pipeline)"),
            PropertySpec("dataset", str, description="Name of the source dataset or system."),
            PropertySpec("created_at", int, description="Record creation time, epoch milliseconds UTC."),
        ),
    ),
    NodeSpec(
        "Transaction", "transaction_id",
        "A single transfer of funds between two accounts.",
        (
            _id("transaction_id", "transaction (namespaced by the ingestion pipeline)"),
            PropertySpec("amount", int, required=True, minimum=0,
                         description="Non-negative integer in minor currency units (for example cents or paise)."),
            PropertySpec("currency", str, required=True, description="ISO 4217 alphabetic currency code."),
            PropertySpec("timestamp", int, required=True, indexed=True,
                         description="Time the transfer occurred, epoch milliseconds UTC."),
            PropertySpec("channel", str, description="Payment channel as reported by the source."),
            PropertySpec("reference", str, description="Free-text reference from the source."),
            PropertySpec("dataset", str, description="Name of the source dataset or system."),
        ),
    ),
    NodeSpec(
        "Alert", "alert_id",
        "An investigative signal attached to a transaction. Alert generation is not part of Phase 1.",
        (
            _id("alert_id", "alert"),
            PropertySpec("created_at", int, required=True, description="Epoch milliseconds UTC."),
            PropertySpec("alert_type", str, description="Source-defined category."),
            PropertySpec("severity", str, description="Source-defined severity label."),
            PropertySpec("source", str, description="Origin of the signal."),
            PropertySpec("description", str),
        ),
    ),
    NodeSpec(
        "Investigation", "investigation_id",
        "Persistent state of one investigation, kept across runs and sessions.",
        (
            _id("investigation_id", "investigation"),
            PropertySpec("status", str, required=True, allowed=INVESTIGATION_STATUSES, indexed=True,
                         description="Lifecycle state."),
            PropertySpec("created_at", int, required=True, description="Epoch milliseconds UTC."),
            PropertySpec("updated_at", int, description="Epoch milliseconds UTC."),
            PropertySpec("title", str),
        ),
    ),
    NodeSpec(
        "Finding", "finding_id",
        "An analytical statement produced during an investigation.",
        (
            _id("finding_id", "finding"),
            PropertySpec("kind", str, required=True, allowed=FINDING_KINDS,
                         description="Keeps observed facts, interpretations and open hypotheses distinct."),
            PropertySpec("statement", str, required=True, description="The finding text."),
            PropertySpec("created_at", int, required=True, description="Epoch milliseconds UTC."),
            PropertySpec("confidence", float, minimum=0.0, maximum=1.0,
                         description="Optional confidence in [0, 1]."),
        ),
    ),
    NodeSpec(
        "Evidence", "evidence_id",
        "A record or reference supporting a finding, traceable to the tool or query that produced it.",
        (
            _id("evidence_id", "evidence item"),
            PropertySpec("created_at", int, required=True, description="Epoch milliseconds UTC."),
            PropertySpec("produced_by_tool", str, description="Name of the tool that produced it."),
            PropertySpec("query_ref", str, description="Identifier or text of the query that produced it."),
            PropertySpec("description", str),
        ),
    ),
    NodeSpec(
        "Agent", "agent_id",
        "An AI component that may take part in investigations. Agents themselves are not implemented in Phase 1.",
        (
            _id("agent_id", "agent"),
            PropertySpec("role", str, required=True, description="Functional role of the agent."),
            PropertySpec("name", str, description="Display name; never used as identity."),
        ),
    ),
)

RELATIONSHIPS: Tuple[RelSpec, ...] = (
    RelSpec("SENT", "Account", "Transaction", "The account originated the transaction."),
    RelSpec("RECEIVED_BY", "Transaction", "Account", "The transaction credited the account."),
    RelSpec("FLAGS", "Alert", "Transaction", "The alert refers to the transaction."),
    RelSpec("STARTED_FROM", "Investigation", "Transaction", "The investigation entry point."),
    RelSpec("HAS_FINDING", "Investigation", "Finding", "A finding recorded in the investigation."),
    RelSpec("SUPPORTED_BY", "Finding", "Evidence", "The evidence backing the finding."),
    RelSpec("REFERENCES", "Evidence", "Transaction", "The transaction record the evidence points to."),
    RelSpec("ASSIGNED_TO", "Agent", "Investigation",
            "The agent participates in the investigation (coordination, Track 2).",
            (PropertySpec("assigned_at", int, description="Epoch milliseconds UTC."),), core=False),
    RelSpec("AUTHORED", "Agent", "Finding",
            "The agent produced the finding, so provenance survives across agents.",
            (PropertySpec("authored_at", int, description="Epoch milliseconds UTC."),), core=False),
)

NODE_BY_LABEL: Mapping[str, NodeSpec] = {n.label: n for n in NODES}
REL_BY_TYPE: Mapping[str, RelSpec] = {r.type: r for r in RELATIONSHIPS}


def get_node_spec(label: str) -> NodeSpec:
    try:
        return NODE_BY_LABEL[label]
    except KeyError:
        raise ValidationError(f"Unknown node label '{label}'.") from None


def get_rel_spec(rel_type: str) -> RelSpec:
    try:
        return REL_BY_TYPE[rel_type]
    except KeyError:
        raise ValidationError(f"Unknown relationship type '{rel_type}'.") from None


# --- schema operations -------------------------------------------------------

@dataclass(frozen=True)
class IndexSpec:
    label: str
    prop: str


@dataclass(frozen=True)
class ConstraintSpec:
    kind: str  # "UNIQUE" or "MANDATORY"
    label: str
    prop: str


def required_indexes() -> Tuple[IndexSpec, ...]:
    """Range indexes: every identity property plus explicitly indexed properties."""
    out = []
    for n in NODES:
        out.append(IndexSpec(n.label, n.id_property))
        out.extend(IndexSpec(n.label, p.name) for p in n.properties if p.indexed)
    return tuple(out)


def required_constraints() -> Tuple[ConstraintSpec, ...]:
    """UNIQUE on identity (needs the index above); MANDATORY on required properties."""
    out = []
    for n in NODES:
        out.append(ConstraintSpec("UNIQUE", n.label, n.id_property))
        out.extend(ConstraintSpec("MANDATORY", n.label, name) for name in n.required)
    return tuple(out)


# --- validation --------------------------------------------------------------

def _check_value(owner: str, spec: PropertySpec, value: Any) -> None:
    t = spec.type
    if t is float:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    else:
        ok = isinstance(value, t) and not (t is int and isinstance(value, bool))
    if not ok:
        raise ValidationError(f"{owner}.{spec.name} must be of type {t.__name__}.")
    if t is str and spec.required and not value.strip():
        raise ValidationError(f"{owner}.{spec.name} must not be empty.")
    if spec.allowed is not None and value not in spec.allowed:
        raise ValidationError(f"{owner}.{spec.name} must be one of {', '.join(spec.allowed)}.")
    if spec.minimum is not None and value < spec.minimum:
        raise ValidationError(f"{owner}.{spec.name} must be >= {spec.minimum:g}.")
    if spec.maximum is not None and value > spec.maximum:
        raise ValidationError(f"{owner}.{spec.name} must be <= {spec.maximum:g}.")


def validate_properties(owner: str, specs: Tuple[PropertySpec, ...], props: Mapping[str, Any],
                        require_all_required: bool = True) -> dict:
    by_name = {p.name: p for p in specs}
    unknown = sorted(set(props) - set(by_name))
    if unknown:
        raise ValidationError(f"{owner} does not allow properties: {', '.join(unknown)}.")
    if require_all_required:
        missing = [p.name for p in specs if p.required and props.get(p.name) is None]
        if missing:
            raise ValidationError(f"{owner} is missing required properties: {', '.join(missing)}.")
    clean = {}
    for key, value in props.items():
        if value is None:
            continue
        _check_value(owner, by_name[key], value)
        clean[key] = value
    return clean


def validate_node(label: str, props: Mapping[str, Any]) -> dict:
    return validate_properties(label, get_node_spec(label).properties, props)


def validate_relationship(rel_type: str, props: Optional[Mapping[str, Any]] = None) -> dict:
    return validate_properties(rel_type, get_rel_spec(rel_type).properties, props or {})
