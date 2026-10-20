# Money Trail — Release Notes v0.8.0 (Phase 8 Release Candidate)

**Release Version:** `0.8.0`  
**Phase:** Phase 8 — Final Integration, Release Readiness, Reproducible Deployment & Demonstration Preparation  
**Target:** Graph Hacks 2026 (FalkorDB)  
**Date:** October 2026  

---

## 1. Executive Summary

Version 0.8.0 represents the final, production-hardened Release Candidate of **Money Trail**, a graph-backed financial investigation agent powered by **FalkorDB**. Across Phases 1 through 8, Money Trail has evolved from a foundational graph database schema into an integrated, multi-agent AML investigation system with persistent memory, interactive visual exploration, robust benchmark performance budgets, and tamper-evident reporting.

The complete test suite stands at **701 total tests (700 passed, 1 skipped)**, with zero failures.

---

## 2. Summary of Phase 8 Additions & Changes

### 2.1 Guided End-to-End Demonstration Command (`money-trail demo`)
* Added a first-class CLI subcommand `money-trail demo` (with optional `--json` and `--keep-graph` flags) that executes a 6-stage investigation pipeline in an isolated, temporary FalkorDB graph (`money_trail_demo_<id>`):
  1. **Generate**: Synthesizes a valid, isolated banking transaction dataset with seeded crime topologies.
  2. **Validate**: Performs contract validation on timestamps, amounts, channels, and accounts.
  3. **Ingest**: Ingests nodes and edges into a disposable FalkorDB graph with indexed schema creation.
  4. **Detect**: Executes all 4 pattern detectors (`circular`, `pass_through`, `fan_in`, `fan_out`).
  5. **Findings**: Persists transaction-anchored investigation and finding nodes.
  6. **Provenance**: Formally verifies the evidence custody chain `(f:Finding)-[:SUPPORTED_BY]->(e:Evidence)-[:REFERENCES]->(t:Transaction)` in FalkorDB Cypher.
* Total execution time is under 0.50 seconds against live FalkorDB.
* Automatic cleanup drops the temporary graph on completion unless `--keep-graph` is passed.

### 2.2 Reproducible Deployment Scripts & Make Targets
* Created `scripts/setup.ps1` for reproducible, one-shot setup on Windows (PowerShell 5.1+).
* Created `scripts/setup.sh` for reproducible setup on Linux, macOS, and WSL.
* Updated root `Makefile` with targets:
  * `demo`: Executes the automated demonstration walkthrough.
  * `qa`: Runs the comprehensive evaluation and benchmark suite.
* Upgraded `pyproject.toml` version metadata to `0.8.0`.

### 2.3 Demonstration & Release Documentation
* Authored `docs/DEMO_RUNBOOK.md` providing step-by-step developer instructions for both CLI and interactive dashboard workflows across Scenarios A through I.
* Authored `docs/DEMO_SCRIPT.md` detailing a 5-to-7 minute narrated demonstration script for video presentation or live jury demonstration.
* Completely revised root `README.md` to reflect all 8 phases, exact CLI commands, current test metrics (700 passed, 1 skipped), and architecture details.
* Created `docs/PHASE8_VERIFICATION_REPORT.md` documenting the pre-flight checklist, test suite execution, security audits, and release decision.

### 2.4 Test Suite Expansion
* Added `test_cli_demo_command` in `tests/integration/test_cli.py` to ensure the new demonstration walkthrough command is continuously verified in CI/CD.

---

## 3. Cumulative Capabilities (Phases 1–8)

1. **FalkorDB Graph Foundation (Phase 1)**:
   * Formal schema with `Account`, `Transaction`, `Alert`, `Investigation`, `Finding`, and `Evidence` labels.
   * Parameterized Cypher queries preventing injection attacks.
   * Safety ceilings bounding query depth (`hops <= 10`), result counts, and execution timeouts.
2. **Deterministic Data Ingestion (Phase 2)**:
   * CSV ingestion pipeline with transaction contract validation, batching, and idempotency guarantees.
   * Schema index management on accounts and transactions.
3. **Graph-Native Pattern Detection (Phase 3)**:
   * Four Cypher-based topological detectors: Circular transfers, Pass-through mules, Fan-in structuring, and Fan-out dispersal.
   * Ground-truth evaluation harness with zero runtime data leakage.
4. **AI Investigation Agent (Phase 4)**:
   * Multi-tool investigation agent equipped with read-only graph inspection tools.
   * Grounding safeguards enforcing that all assertions reference verified graph transactions.
5. **Persistent Memory & Multi-Agent Coordination (Phase 5 - Track 2)**:
   * Episodic and semantic graph-backed memory with bi-temporal timestamps.
   * Multi-agent coordination state machine (`TriageAgent` $\to$ `ForensicSpecialistAgent`) with deterministic handoffs and persistent task tracking.
6. **Interactive Dashboard Workspace (Phase 6)**:
   * Streamlit dashboard with interactive Vis.js graph visualization, dynamic neighborhood tracing, risk scoring, and evidence inspection.
7. **Comprehensive Evaluation & Security Hardening (Phase 7)**:
   * Performance benchmark framework verifying sub-10ms query latencies.
   * Fault injection resilience (database disconnects, invalid inputs, query timeouts).
8. **Final Integration & Release Readiness (Phase 8)**:
   * One-shot automated demo CLI, reproducible cross-platform setup, comprehensive runbooks, and release verification.

---

## 4. Test & Verification Results

* **Total Tests Executed:** 701
* **Passed:** 700
* **Skipped:** 1 (Live Anthropic API key test; gracefully skipped when `ANTHROPIC_API_KEY` is omitted, activating verified scripted fallback mode).
* **Failures:** 0
* **Execution Time:** ~105 seconds for full end-to-end integration suite.

---

## 5. Known Limitations & Operating Notes

1. **Live LLM API Keys**:
   * To execute investigations using a live Claude model, set `ANTHROPIC_API_KEY` in `.env`.
   * When no key is provided, Money Trail automatically activates its verified offline rule-based agent mode, which passes all unit and integration tests without external network dependencies.
2. **Graph Visualization Node Ceilings**:
   * In the Streamlit dashboard, graph rendering is bounded by `query_max_results` (default: 500) to maintain high frame-rate Vis.js rendering in the browser. Large neighborhoods are paginated.
3. **FalkorDB Single-Node Deployment**:
   * The current configuration targets a single-node FalkorDB instance via Docker Compose, which delivers sub-millisecond graph query times. Distributed cluster coordination is out of scope for the current release.

---

## 6. Release Verdict

**Verdict:** **APPROVED AS RELEASE CANDIDATE (v0.8.0)**  
All Phase 8 exit gates have been verified against a live FalkorDB instance.
