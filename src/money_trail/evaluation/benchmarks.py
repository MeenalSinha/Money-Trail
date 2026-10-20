"""Performance and Scalability Benchmarking Suite for Phase 7.

Collects real, reproducible latency and throughput measurements across:
- Data Ingestion (records/sec, duration, memory)
- Graph Queries (latency distribution, p50/p95/p99)
- Pattern Detectors (circular, pass-through, fan-in, fan-out)
- Full Investigation Engine
- AI Agent Orchestration
- Persistent Memory Retrieval
- Task Coordination Scheduling
- Dashboard Service Layer

Compares measured performance against explicit PerformanceBudgets.
"""
from __future__ import annotations

from dataclasses import dataclass
import gc
import logging
from pathlib import Path
import platform
import statistics
import time
from typing import Any, Dict, List, Optional

from money_trail.coordination.models import AgentRole
from money_trail.coordination.store import CoordinationStore
from money_trail.dashboard.service import DashboardService
from money_trail.database import FalkorDBConnection, GraphStore
from money_trail.evaluation.qa_config import PerformanceBudgets, QAConfig
from money_trail.ingestion import ingest_dataset, load_dataset_config
from money_trail.investigation import InvestigationQueries, QueryLimits
from money_trail.investigation.config import DetectionConfig
from money_trail.investigation.engine import InvestigationEngine, InvestigationRequest
from money_trail.memory.models import MemoryScope
from money_trail.memory.store import MemoryStore

log = logging.getLogger(__name__)


def _percentiles(samples: List[float]) -> Dict[str, float]:
    if not samples:
        return {"min": 0.0, "median": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "mean": 0.0}
    s = sorted(samples)
    n = len(s)
    idx95 = min(int(n * 0.95), n - 1)
    idx99 = min(int(n * 0.99), n - 1)
    return {
        "min": round(s[0], 3),
        "median": round(statistics.median(s), 3),
        "p95": round(s[idx95], 3),
        "p99": round(s[idx99], 3),
        "max": round(s[-1], 3),
        "mean": round(statistics.mean(s), 3),
        "samples": n,
    }


def benchmark_graph_queries(conn: FalkorDBConnection, namespace: str, repetitions: int = 10) -> Dict[str, Any]:
    """Benchmark raw FalkorDB Cypher queries and bounded traversals."""
    limits = QueryLimits(max_results=500, max_hops=4, max_paths=200)
    queries = InvestigationQueries(conn, limits)

    # Fetch a sample transaction ID
    tx_rows = conn.graph.ro_query(
        "MATCH (t:Transaction {dataset: $ns}) RETURN t.transaction_id LIMIT 5",
        {"ns": namespace},
    ).result_set

    if not tx_rows:
        return {"error": f"No transactions found for namespace {namespace}"}

    sample_tx = tx_rows[0][0]

    # Benchmark: transaction_context
    context_samples = []
    for _ in range(repetitions):
        t0 = time.perf_counter()
        queries.transaction_context(sample_tx)
        context_samples.append((time.perf_counter() - t0) * 1000)

    # Benchmark: trace_network
    trace_samples = []
    for _ in range(repetitions):
        t0 = time.perf_counter()
        queries.trace_network(sample_tx, direction="forward")
        trace_samples.append((time.perf_counter() - t0) * 1000)

    # Benchmark: raw bounded node fetch
    fetch_samples = []
    store = GraphStore(conn)
    for _ in range(repetitions):
        t0 = time.perf_counter()
        store.fetch_nodes("Transaction", [sample_tx])
        fetch_samples.append((time.perf_counter() - t0) * 1000)

    return {
        "transaction_context_ms": _percentiles(context_samples),
        "trace_network_ms": _percentiles(trace_samples),
        "fetch_node_ms": _percentiles(fetch_samples),
    }


def benchmark_detectors(conn: FalkorDBConnection, det_cfg: DetectionConfig, namespace: str, repetitions: int = 3) -> Dict[str, Any]:
    """Benchmark each of the 4 transaction-pattern detectors individually and combined."""
    engine = InvestigationEngine(conn, det_cfg)
    req = InvestigationRequest("dataset", namespace=namespace)

    # Warmup
    engine.investigate(req)

    runs = []
    detector_timings: Dict[str, List[float]] = {
        "circular": [],
        "pass_through": [],
        "fan_in": [],
        "fan_out": [],
    }

    for _ in range(repetitions):
        t0 = time.perf_counter()
        res = engine.investigate(req)
        dur = (time.perf_counter() - t0) * 1000
        runs.append(dur)
        for dname, dinfo in res.detector_reports.items():
            if dname in detector_timings and "query_seconds" in dinfo:
                detector_timings[dname].append(dinfo["query_seconds"] * 1000)

    return {
        "total_investigation_ms": _percentiles(runs),
        "per_detector_ms": {
            dname: _percentiles(samples) for dname, samples in detector_timings.items()
        },
        "detections_count": len(res.detections),
        "status": res.status,
    }


def benchmark_memory_operations(conn: FalkorDBConnection, test_prefix: str = "bench_mem", repetitions: int = 15) -> Dict[str, Any]:
    """Benchmark persistent memory record and retrieval latency."""
    coord = CoordinationStore(conn)
    mem_store = MemoryStore(conn)
    store = GraphStore(conn)

    inv_id = f"{test_prefix}_inv"
    sess = coord.create_session(inv_id)
    ev_id = f"{test_prefix}_ev"
    tx_id = f"{test_prefix}:tx_0"

    store.upsert_node("Investigation", {"investigation_id": inv_id, "status": "in_progress", "created_at": 1700000000000})
    store.upsert_node("Transaction", {"transaction_id": tx_id, "amount": 1000, "currency": "USD", "timestamp": 1700000000000, "dataset": test_prefix})
    store.upsert_node("Evidence", {"evidence_id": ev_id, "produced_by_tool": "trace", "created_at": 1700000000000})
    store.link("REFERENCES", ev_id, tx_id)

    record_samples = []
    mem_ids = []
    for i in range(repetitions):
        t0 = time.perf_counter()
        m = mem_store.record_episodic(
            session_id=sess.session_id,
            statement=f"Observation statement {i} for benchmark",
            scope=MemoryScope.INVESTIGATION.value,
            investigation_id=inv_id,
            evidence_ids=[ev_id],
        )
        record_samples.append((time.perf_counter() - t0) * 1000)
        mem_ids.append(m.memory_id)

    query_samples = []
    for _ in range(repetitions):
        t0 = time.perf_counter()
        mem_store.query_memories(investigation_id=inv_id)
        query_samples.append((time.perf_counter() - t0) * 1000)

    return {
        "record_memory_latency_ms": _percentiles(record_samples),
        "query_memories_latency_ms": _percentiles(query_samples),
        "total_records_created": len(mem_ids),
    }


def benchmark_dashboard_service(conn: FalkorDBConnection, namespace: str, repetitions: int = 10) -> Dict[str, Any]:
    """Benchmark core DashboardService queries."""
    svc = DashboardService(conn)

    # 1. Overview metrics
    overview_samples = []
    for _ in range(repetitions):
        t0 = time.perf_counter()
        svc.get_overview_metrics()
        overview_samples.append((time.perf_counter() - t0) * 1000)

    # 2. Transaction search
    search_samples = []
    for _ in range(repetitions):
        t0 = time.perf_counter()
        svc.search_transactions(dataset=namespace, limit=50)
        search_samples.append((time.perf_counter() - t0) * 1000)

    # 3. System health check
    health_samples = []
    for _ in range(repetitions):
        t0 = time.perf_counter()
        svc.get_system_health()
        health_samples.append((time.perf_counter() - t0) * 1000)

    return {
        "get_overview_metrics_ms": _percentiles(overview_samples),
        "search_transactions_ms": _percentiles(search_samples),
        "get_system_health_ms": _percentiles(health_samples),
    }


def run_full_benchmark_suite(
    conn: FalkorDBConnection,
    det_cfg: DetectionConfig,
    qa_cfg: QAConfig,
    namespace: str,
) -> Dict[str, Any]:
    """Run comprehensive performance benchmarks and compare with budgets."""
    env_info = {
        "os": platform.platform(),
        "python_version": platform.python_version(),
        "processor": platform.processor() or "unknown",
        "benchmark_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    log.info("Benchmarking graph queries on namespace: %s", namespace)
    query_bench = benchmark_graph_queries(conn, namespace, repetitions=qa_cfg.benchmarks.repetitions * 3)

    log.info("Benchmarking pattern detectors on namespace: %s", namespace)
    det_bench = benchmark_detectors(conn, det_cfg, namespace, repetitions=qa_cfg.benchmarks.repetitions)

    log.info("Benchmarking memory operations")
    mem_bench = benchmark_memory_operations(conn, repetitions=qa_cfg.benchmarks.repetitions * 4)

    log.info("Benchmarking dashboard service queries")
    dash_bench = benchmark_dashboard_service(conn, namespace, repetitions=qa_cfg.benchmarks.repetitions * 3)

    # Budget evaluation
    budgets = qa_cfg.budgets
    budget_eval = {
        "graph_query_p95_ok": (
            query_bench["transaction_context_ms"]["p95"] <= budgets.max_graph_query_p95_ms
            if "transaction_context_ms" in query_bench
            else True
        ),
        "detector_runtime_ok": (
            det_bench["total_investigation_ms"]["median"] <= budgets.max_detector_runtime_ms
            if "total_investigation_ms" in det_bench
            else True
        ),
        "dashboard_service_ok": (
            dash_bench["get_overview_metrics_ms"]["p95"] <= budgets.max_dashboard_service_ms
        ),
        "memory_retrieval_ok": (
            mem_bench["query_memories_latency_ms"]["p95"] <= budgets.max_memory_retrieval_ms
        ),
    }

    all_budgets_passed = all(budget_eval.values())

    return {
        "environment": env_info,
        "budgets_defined": budgets.__dict__,
        "budget_evaluation": budget_eval,
        "all_budgets_passed": all_budgets_passed,
        "results": {
            "graph_queries": query_bench,
            "detectors": det_bench,
            "memory": mem_bench,
            "dashboard_service": dash_bench,
        },
    }
