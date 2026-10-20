"""Unit tests for Phase 7 QA and Benchmark Configuration."""
from pathlib import Path
import pytest

from money_trail.evaluation.qa_config import (
    BenchmarkSettings,
    DatasetProfile,
    PerformanceBudgets,
    QAConfig,
    load_qa_config,
)
from money_trail.exceptions import ConfigurationError


def test_default_qa_config():
    cfg = load_qa_config()
    assert cfg.version == "7.0.0"
    assert cfg.dev_dataset.namespace == "qa-dev-ns"
    assert cfg.dev_dataset.accounts == 30
    assert cfg.eval_dataset.namespace == "qa-eval-ns"
    assert cfg.eval_dataset.transactions == 400
    assert len(cfg.scaling_datasets) == 3
    assert cfg.min_gt_coverage_percent == 100
    assert cfg.min_detection_purity_percent == 100
    assert cfg.budgets.max_graph_query_p95_ms > 0
    assert cfg.benchmarks.repetitions >= 1


def test_qa_config_serialization():
    cfg = QAConfig()
    d = cfg.to_dict()
    assert d["version"] == "7.0.0"
    assert "dev_dataset" in d
    assert "eval_dataset" in d
    assert "scaling_datasets" in d
    assert "budgets" in d
    assert "benchmarks" in d


def test_custom_qa_config_file(tmp_path):
    custom_json = tmp_path / "custom_qa.json"
    custom_json.write_text("""{
        "version": "7.1.0",
        "dev_dataset": {
            "namespace": "custom-dev",
            "seed": 999,
            "accounts": 15,
            "transactions": 50,
            "patterns_per_type": 1
        },
        "min_gt_coverage_percent": 90,
        "min_detection_purity_percent": 85
    }""")
    cfg = load_qa_config(custom_json)
    assert cfg.version == "7.1.0"
    assert cfg.dev_dataset.namespace == "custom-dev"
    assert cfg.dev_dataset.seed == 999
    assert cfg.min_gt_coverage_percent == 90
    assert cfg.min_detection_purity_percent == 85


def test_invalid_qa_config_validations(tmp_path):
    # Nonexistent file
    with pytest.raises(ConfigurationError, match="not found"):
        load_qa_config(tmp_path / "nonexistent.json")

    # Invalid JSON
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("NOT JSON")
    with pytest.raises(ConfigurationError, match="Failed to parse"):
        load_qa_config(bad_json)

    # Invalid range for coverage percentage
    invalid_cov = tmp_path / "invalid_cov.json"
    invalid_cov.write_text('{"min_gt_coverage_percent": 150}')
    with pytest.raises(ConfigurationError, match="min_gt_coverage_percent must be between 1 and 100"):
        load_qa_config(invalid_cov)
