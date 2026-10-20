"""Unit tests for Phase 7 Security and Data-Integrity Audit."""
from pathlib import Path
import pytest

from money_trail.evaluation.security_audit import (
    audit_cypher_query_parameterization,
    audit_financial_interpretation_safety,
    audit_ground_truth_isolation,
    audit_repository_for_secrets,
)


def test_secrets_scanner_detects_mock_secret(tmp_path):
    # Create clean file
    clean_file = tmp_path / "clean.py"
    clean_file.write_text("x = 10\ny = 20\n")

    res_clean = audit_repository_for_secrets(tmp_path)
    assert res_clean["clean"] is True
    assert len(res_clean["findings"]) == 0

    # Inject mock API key pattern
    dirty_file = tmp_path / "dirty.py"
    dirty_file.write_text('API_KEY = "sk-live-sample-token-11112222333344445555"\n')

    res_dirty = audit_repository_for_secrets(tmp_path)
    assert res_dirty["clean"] is False
    assert len(res_dirty["findings"]) >= 1
    assert res_dirty["findings"][0]["severity"] == "CRITICAL"


def test_repository_secrets_clean():
    root = Path(__file__).resolve().parent.parent.parent
    res = audit_repository_for_secrets(root)
    # The tracked repository should have 0 committed secrets
    assert res["clean"] is True, f"Found secrets in repository: {res['findings']}"


def test_cypher_query_parameterization_clean():
    src_dir = Path(__file__).resolve().parent.parent.parent / "src"
    res = audit_cypher_query_parameterization(src_dir)
    assert res["parameterization_clean"] is True, f"Insecure query parameterization found: {res['flagged_issues']}"


def test_ground_truth_runtime_isolation():
    root = Path(__file__).resolve().parent.parent.parent
    res = audit_ground_truth_isolation(root)
    assert res["isolated"] is True, f"Ground truth leaked into runtime: {res['leakage_imports']}"


def test_financial_interpretation_safety():
    src_dir = Path(__file__).resolve().parent.parent.parent / "src"
    res = audit_financial_interpretation_safety(src_dir)
    assert res["passed"] is True
    assert res["accusatory_terms_blocked"] is True
    assert res["neutral_observation_accepted"] is True
