"""Centralized Quality Assurance and Benchmark Configuration for Phase 7.

Maintains strict validation and reproducible parameters for detector evaluation,
agent benchmarking, reliability testing, and performance profiling.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from money_trail.exceptions import ConfigurationError


@dataclass(frozen=True)
class DatasetProfile:
    namespace: str
    seed: int
    accounts: int
    transactions: int
    patterns_per_type: int
    amount_min_minor: int = 10000
    amount_max_minor: int = 900000
    description: str = ""


@dataclass(frozen=True)
class PerformanceBudgets:
    min_ingest_records_per_sec: float = 100.0
    max_graph_query_p95_ms: float = 100.0
    max_detector_runtime_ms: float = 2000.0
    max_dashboard_service_ms: float = 250.0
    max_memory_retrieval_ms: float = 100.0
    max_agent_step_ms: float = 5000.0


@dataclass(frozen=True)
class BenchmarkSettings:
    repetitions: int = 3
    warmup_runs: int = 1
    query_max_results: int = 500
    max_hops: int = 4
    timeout_seconds: float = 60.0


@dataclass(frozen=True)
class QAConfig:
    version: str = "7.0.0"
    dev_dataset: DatasetProfile = field(
        default_factory=lambda: DatasetProfile(
            namespace="qa-dev-ns",
            seed=42,
            accounts=30,
            transactions=100,
            patterns_per_type=1,
            description="Development and threshold validation dataset",
        )
    )
    eval_dataset: DatasetProfile = field(
        default_factory=lambda: DatasetProfile(
            namespace="qa-eval-ns",
            seed=20261020,
            accounts=80,
            transactions=400,
            patterns_per_type=2,
            description="Held-out evaluation dataset for final detector accuracy reporting",
        )
    )
    scaling_datasets: List[DatasetProfile] = field(
        default_factory=lambda: [
            DatasetProfile(namespace="qa-scale-100", seed=101, accounts=25, transactions=100, patterns_per_type=1, description="Scale: 100 tx"),
            DatasetProfile(namespace="qa-scale-500", seed=102, accounts=100, transactions=500, patterns_per_type=2, description="Scale: 500 tx"),
            DatasetProfile(namespace="qa-scale-1200", seed=103, accounts=240, transactions=1200, patterns_per_type=3, description="Scale: 1200 tx"),
        ]
    )
    budgets: PerformanceBudgets = field(default_factory=PerformanceBudgets)
    benchmarks: BenchmarkSettings = field(default_factory=BenchmarkSettings)
    output_dir: Path = field(default_factory=lambda: Path("evaluation_reports"))
    min_gt_coverage_percent: int = 100
    min_detection_purity_percent: int = 100

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "dev_dataset": self.dev_dataset.__dict__,
            "eval_dataset": self.eval_dataset.__dict__,
            "scaling_datasets": [d.__dict__ for d in self.scaling_datasets],
            "budgets": self.budgets.__dict__,
            "benchmarks": self.benchmarks.__dict__,
            "output_dir": str(self.output_dir),
            "min_gt_coverage_percent": self.min_gt_coverage_percent,
            "min_detection_purity_percent": self.min_detection_purity_percent,
        }


def load_qa_config(path: Optional[os.PathLike] = None) -> QAConfig:
    """Load and validate QA configuration from a JSON file, or return default validated config."""
    if path is None:
        return QAConfig()

    p = Path(path)
    if not p.is_file():
        raise ConfigurationError(f"QA configuration file not found: {p}")

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigurationError(f"Failed to parse QA configuration JSON: {exc}") from None

    if not isinstance(data, dict):
        raise ConfigurationError("QA configuration root must be a JSON object.")

    default_cfg = QAConfig()

    # Validation
    dev_raw = data.get("dev_dataset", {})
    eval_raw = data.get("eval_dataset", {})
    scale_raw = data.get("scaling_datasets", [])
    budgets_raw = data.get("budgets", {})
    bench_raw = data.get("benchmarks", {})

    dev_profile = DatasetProfile(**dev_raw) if dev_raw else default_cfg.dev_dataset
    eval_profile = DatasetProfile(**eval_raw) if eval_raw else default_cfg.eval_dataset
    scale_profiles = [DatasetProfile(**s) for s in scale_raw] if scale_raw else default_cfg.scaling_datasets

    budgets = PerformanceBudgets(**budgets_raw) if budgets_raw else PerformanceBudgets()
    bench = BenchmarkSettings(**bench_raw) if bench_raw else BenchmarkSettings()

    out_dir = Path(data.get("output_dir", "evaluation_reports"))
    min_cov = int(data.get("min_gt_coverage_percent", 100))
    min_pur = int(data.get("min_detection_purity_percent", 100))

    if min_cov <= 0 or min_cov > 100:
        raise ConfigurationError("min_gt_coverage_percent must be between 1 and 100.")
    if min_pur <= 0 or min_pur > 100:
        raise ConfigurationError("min_detection_purity_percent must be between 1 and 100.")

    return QAConfig(
        version=str(data.get("version", "7.0.0")),
        dev_dataset=dev_profile,
        eval_dataset=eval_profile,
        scaling_datasets=scale_profiles,
        budgets=budgets,
        benchmarks=bench,
        output_dir=out_dir,
        min_gt_coverage_percent=min_cov,
        min_detection_purity_percent=min_pur,
    )
