"""Integration test for Phase 7 performance and scalability benchmarking suite."""
from __future__ import annotations

import tempfile
from pathlib import Path
import pytest

from money_trail.database.migration import migrate_schema
from money_trail.evaluation.benchmarks import (
    benchmark_dashboard_service,
    benchmark_detectors,
    benchmark_graph_queries,
    benchmark_memory_operations,
    run_full_benchmark_suite,
)
from money_trail.evaluation.detector_eval import (
    ingest_dataset_into_graph,
    prepare_synthetic_dataset,
)
from money_trail.evaluation.qa_config import DatasetProfile, QAConfig, load_qa_config
from money_trail.investigation.config import DetectionConfig, load_detection_config

pytestmark = pytest.mark.integration


def test_performance_benchmarks_on_real_graph(ready, tmp_path):
    """Verify performance metrics collection, percentiles calculation, and budget evaluation against FalkorDB."""
    migrate_schema(ready)
    ns = "perf_bench_ns"

    # Seed test dataset with synthetic patterns
    profile = DatasetProfile(
        namespace=ns,
        seed=42,
        accounts=15,
        transactions=50,
        patterns_per_type=1,
    )
    ds_dir, _ = prepare_synthetic_dataset(profile, tmp_path)
    ingest_dataset_into_graph(ready, ds_dir)

    det_cfg = load_detection_config(Path("examples/detection.example.json"))
    qa_cfg = load_qa_config()

    # 1. Graph Queries benchmark
    query_res = benchmark_graph_queries(ready, ns, repetitions=3)
    assert "transaction_context_ms" in query_res
    assert query_res["transaction_context_ms"]["median"] > 0.0
    assert query_res["transaction_context_ms"]["samples"] == 3

    # 2. Detectors benchmark
    det_res = benchmark_detectors(ready, det_cfg, ns, repetitions=2)
    assert "total_investigation_ms" in det_res
    assert det_res["total_investigation_ms"]["median"] > 0.0
    assert "circular" in det_res["per_detector_ms"]

    # 3. Memory benchmark
    mem_res = benchmark_memory_operations(ready, test_prefix="perf_mem", repetitions=3)
    assert "record_memory_latency_ms" in mem_res
    assert mem_res["record_memory_latency_ms"]["median"] > 0.0
    assert "query_memories_latency_ms" in mem_res
    assert mem_res["query_memories_latency_ms"]["median"] > 0.0

    # 4. Dashboard Service benchmark
    dash_res = benchmark_dashboard_service(ready, ns, repetitions=2)
    assert "get_overview_metrics_ms" in dash_res
    assert dash_res["get_overview_metrics_ms"]["median"] > 0.0

    # 5. Full suite execution
    full_bench = run_full_benchmark_suite(ready, det_cfg, qa_cfg, ns)
    assert "environment" in full_bench
    assert "budget_evaluation" in full_bench
    assert isinstance(full_bench["all_budgets_passed"], bool)
    assert "results" in full_bench
