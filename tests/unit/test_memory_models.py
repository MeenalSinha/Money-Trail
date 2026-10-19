"""Unit tests for Phase 5 persistent memory models and identity policies."""
import pytest

from money_trail.exceptions import ValidationError
from money_trail.memory.models import (
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    stable_memory_id,
)


def test_stable_memory_id_deterministic():
    id1 = stable_memory_id("investigation", "inv_123", "semantic", "Cycle of 3 transfers", "circular")
    id2 = stable_memory_id("investigation", "inv_123", "semantic", "Cycle of 3 transfers", "circular")
    assert id1 == id2
    assert id1.startswith("mem_")


def test_stable_memory_id_distinguishes_scopes_and_statements():
    id_inv = stable_memory_id("investigation", "inv_1", "semantic", "Statement A")
    id_ds = stable_memory_id("dataset", "ds_1", "semantic", "Statement A")
    id_stmt_b = stable_memory_id("investigation", "inv_1", "semantic", "Statement B")
    id_episodic = stable_memory_id("investigation", "inv_1", "episodic", "Statement A")

    assert len({id_inv, id_ds, id_stmt_b, id_episodic}) == 4


def test_memory_record_validation_success():
    rec = MemoryRecord(
        memory_id="mem_abc",
        memory_type="semantic",
        scope="investigation",
        statement="Verified circular transfer pattern",
        status="active",
        created_at=1700000000000,
        investigation_id="inv_123",
        confidence=0.95,
        evidence_ids=["ev_1", "ev_2"],
    )
    rec.validate()
    props = rec.as_graph_props()
    assert props["memory_id"] == "mem_abc"
    assert props["confidence"] == 0.95
    assert props["status"] == "active"


def test_memory_record_validation_failures():
    # Invalid memory_type
    with pytest.raises(ValidationError, match="Invalid memory_type"):
        MemoryRecord("m1", "unknown_type", "investigation", "stmt", "active", 1, investigation_id="i1").validate()

    # Invalid scope
    with pytest.raises(ValidationError, match="Invalid memory scope"):
        MemoryRecord("m1", "semantic", "global_unknown", "stmt", "active", 1, investigation_id="i1").validate()

    # Invalid status
    with pytest.raises(ValidationError, match="Invalid memory status"):
        MemoryRecord("m1", "semantic", "investigation", "stmt", "deleted", 1, investigation_id="i1").validate()

    # Empty statement
    with pytest.raises(ValidationError, match="must not be empty"):
        MemoryRecord("m1", "semantic", "investigation", "   ", "active", 1, investigation_id="i1").validate()

    # Statement too long
    with pytest.raises(ValidationError, match="exceeds maximum allowed length"):
        MemoryRecord("m1", "semantic", "investigation", "x" * 5001, "active", 1, investigation_id="i1").validate()

    # Missing investigation_id for investigation scope
    with pytest.raises(ValidationError, match="requires a non-empty investigation_id"):
        MemoryRecord("m1", "semantic", "investigation", "stmt", "active", 1).validate()

    # Missing dataset for dataset scope
    with pytest.raises(ValidationError, match="requires a non-empty dataset"):
        MemoryRecord("m1", "semantic", "dataset", "stmt", "active", 1).validate()

    # Confidence out of bounds
    with pytest.raises(ValidationError, match="confidence must be between"):
        MemoryRecord("m1", "semantic", "investigation", "stmt", "active", 1, investigation_id="i1", confidence=1.5).validate()
