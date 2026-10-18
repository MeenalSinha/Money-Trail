import pytest

from money_trail.database import graph_schema as gs
from money_trail.exceptions import ValidationError

REQUIRED_NODES = {"Account", "Transaction", "Alert", "Investigation", "Finding", "Evidence", "Agent"}
REQUIRED_RELS = {
    ("SENT", "Account", "Transaction"),
    ("RECEIVED_BY", "Transaction", "Account"),
    ("FLAGS", "Alert", "Transaction"),
    ("STARTED_FROM", "Investigation", "Transaction"),
    ("HAS_FINDING", "Investigation", "Finding"),
    ("SUPPORTED_BY", "Finding", "Evidence"),
    ("REFERENCES", "Evidence", "Transaction"),
}


def test_required_nodes_present():
    assert REQUIRED_NODES <= set(gs.NODE_BY_LABEL)


def test_required_relationships_and_directions():
    actual = {(r.type, r.source, r.target) for r in gs.RELATIONSHIPS}
    assert REQUIRED_RELS <= actual


def test_extra_relationships_are_flagged_non_core():
    extra = [r for r in gs.RELATIONSHIPS if (r.type, r.source, r.target) not in REQUIRED_RELS]
    assert extra and all(not r.core for r in extra)


def test_registry_is_consistent():
    labels = {n.label for n in gs.NODES}
    assert len(labels) == len(gs.NODES)
    assert len({r.type for r in gs.RELATIONSHIPS}) == len(gs.RELATIONSHIPS)
    for r in gs.RELATIONSHIPS:
        assert r.source in labels and r.target in labels
    for n in gs.NODES:
        ident = n.prop(n.id_property)
        assert ident.required and ident.type is str
        assert len({p.name for p in n.properties}) == len(n.properties)


def test_names_are_safe_cypher_identifiers():
    import re
    ident = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
    for n in gs.NODES:
        assert ident.match(n.label)
        assert all(ident.match(p.name) for p in n.properties)
    assert all(ident.match(r.type) for r in gs.RELATIONSHIPS)


def test_every_identity_has_index_and_unique_constraint():
    idx = {(i.label, i.prop) for i in gs.required_indexes()}
    uniq = {(c.label, c.prop) for c in gs.required_constraints() if c.kind == "UNIQUE"}
    for n in gs.NODES:
        assert (n.label, n.id_property) in idx
        assert (n.label, n.id_property) in uniq
    assert uniq <= idx  # FalkorDB unique constraints need an index first


def test_mandatory_constraints_match_required_properties():
    mand = {(c.label, c.prop) for c in gs.required_constraints() if c.kind == "MANDATORY"}
    expected = {(n.label, p) for n in gs.NODES for p in n.required}
    assert mand == expected


def valid_tx(**over):
    d = {"transaction_id": "t1", "amount": 100, "currency": "USD", "timestamp": 1_700_000_000_000}
    d.update(over)
    return d


def test_validate_node_accepts_valid():
    assert gs.validate_node("Transaction", valid_tx())["amount"] == 100


@pytest.mark.parametrize("over,match", [
    ({"amount": -1}, "amount"),
    ({"amount": 1.5}, "amount"),
    ({"amount": True}, "amount"),
    ({"currency": ""}, "currency"),
    ({"timestamp": "yesterday"}, "timestamp"),
    ({"bogus": 1}, "does not allow"),
])
def test_validate_node_rejects_invalid(over, match):
    with pytest.raises(ValidationError, match=match):
        gs.validate_node("Transaction", valid_tx(**over))


def test_missing_required_property():
    d = valid_tx()
    del d["currency"]
    with pytest.raises(ValidationError, match="currency"):
        gs.validate_node("Transaction", d)


def test_closed_vocabularies_and_ranges():
    with pytest.raises(ValidationError, match="kind"):
        gs.validate_node("Finding", {"finding_id": "f", "kind": "guess", "statement": "s", "created_at": 1})
    with pytest.raises(ValidationError, match="confidence"):
        gs.validate_node("Finding", {"finding_id": "f", "kind": "hypothesis", "statement": "s",
                                      "created_at": 1, "confidence": 1.5})
    with pytest.raises(ValidationError, match="status"):
        gs.validate_node("Investigation", {"investigation_id": "i", "status": "weird", "created_at": 1})


def test_unknown_label_and_rel_rejected():
    with pytest.raises(ValidationError):
        gs.get_node_spec("Account) DETACH DELETE (n")
    with pytest.raises(ValidationError):
        gs.get_rel_spec("SENT]->(x) //")


def test_relationship_property_validation():
    assert gs.validate_relationship("ASSIGNED_TO", {"assigned_at": 5}) == {"assigned_at": 5}
    with pytest.raises(ValidationError):
        gs.validate_relationship("SENT", {"anything": 1})
