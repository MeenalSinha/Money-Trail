# Money Trail

Money Trail is a graph-backed financial investigation agent built for Graph Hacks 2026
(**Track 1: Agents That Act on Connected Data**; **Track 2: Agent Memory and Coordination**).

Current state: Phases 1–8 fully implemented and verified against real FalkorDB.
The agent's **live external provider request remains unverified** because no external API
credential was provided in the execution environment (see `docs/VERIFICATION_REPORT.md`);
all deterministic interfaces, schema migrations, coordination engines, evaluation harnesses,
security hardening, and dashboard workspaces are 100% verified.

## Problem and Approach

Suspicious financial activity often spans multiple accounts, layered transactions, and complex
timing patterns. Money Trail follows relationships between accounts and transactions, collects
evidence, remembers context across sessions and processes, and coordinates specialized agents
directly through FalkorDB.

FalkorDB is the central context and coordination layer:
* **The Connected Financial Graph**: Stores accounts, transactions, and signals.
* **Persistent Graph Memory**: Stores episodic investigation traces and evidence-grounded
  semantic facts with provenance, temporal validity, and supersession links.
* **Shared Multi-Agent Coordination State**: Coordinates tasks, dependencies, state machines,
  and auditable handoffs directly within FalkorDB.

## What Exists

### Phase 1: Foundation
* Validated environment-driven configuration and connection layer for FalkorDB.
* Graph schema (`docs/architecture/graph-schema.md`) with idempotent initialization.
* Health check and database connectivity verification.

### Phase 2: Ingestion & Queries
* Canonical data contract with dataset namespacing and integer minor-unit currencies.
* Synthetic data generator with ground truth isolated from graph data.
* Streaming CSV ingestion with quarantine, duplicate handling, and post-write verification.
* Four foundational read-only queries (transaction context, account activity, network trace, evidence path).

### Phase 3: Deterministic Investigation Engine
* Validated detection configuration (`examples/detection.example.json`).
* Four graph-native pattern detectors: circular transfers, pass-through chains, fan-in, fan-out.
* Deterministic investigation engine with complete finding and evidence persistence in FalkorDB.
* Evaluation framework measuring precision, recall, and F1 against isolated ground truth.

### Phase 4: AI Investigation Agent & Safeguards
* Bounded AI tool-use loop selecting among seven registered read-only graph tools.
* Strict validation: the model cannot execute arbitrary Cypher or mutate graph thresholds.
* Sentence-level report grounding safeguards and graph reverification before reporting.
* Fallback error classification for rate limits, timeouts, and missing anchors.

### Phase 5: Persistent Graph Memory & Multi-Agent Coordination
* **Additive Versioned Schema Upgrade (v2)**: Non-destructive migration preserving all existing v1 data, with durable migration history recorded in `(:SchemaMigration)`.
* **Persistent Agent Sessions (`AgentSession`)**: Durable sessions persisting across separate application processes, supporting seamless recovery of interrupted investigations.
* **Provenance-Aware Graph Memory (`Memory`)**:
  - *Episodic memory*: Chronological records of actions and detections tied to specific sessions.
  - *Semantic memory*: Verified factual claims strictly grounded by graph evidence nodes (`GROUNDED_BY`) and validated transactions.
  - *Anti-poisoning safeguards*: Unsupported LLM statements are blocked from becoming semantic memory.
  - *Supersession & Contradictions*: Preserves historical provenance with `SUPERSEDES` and `CONTRADICTS` graph relationships.
  - *Scope Isolation*: Strict boundaries between investigation scopes and dataset namespaces.
* **Multi-Agent Task Coordination (`AgentTask` & `AgentHandoff`)**:
  - Four specialized agent roles: **Coordinator / Planner**, **Graph Investigator**, **Evidence Auditor**, and **Memory Curator**.
  - Explicit, validated task state machine (`pending`, `ready`, `running`, `completed`, `blocked`, `failed`, `cancelled`).
  - Graph-based dependency DAG scheduling (`DEPENDS_ON`), cycle detection, and automatic unblocking.
  - Atomic task claiming in FalkorDB preventing race conditions and duplicate execution.
  - Persistent, auditable handoffs (`INITIATED` -> `TRANSFERRED` -> `ACCEPTED` -> `COMPLETED`).
  - Resumption of interrupted investigations directly from FalkorDB graph state without process state dependency.

### Phase 6: Interactive Dashboard & Complete Investigation Workspace
* **Unified Streamlit Frontend**: Production-quality investigative workspace with dark-mode financial styling.
* **Interactive Graph Visualization**: Vis.js canvas with directional transfer arrows, color-coded node entities, and an accessible tabular alternative table (`st.dataframe`).
* **Multi-Tab Investigation Workspace**: Launch investigations, trace transaction paths, view findings, inspect evidence, follow agent handoffs, and resume interrupted tasks.
* **8 Core Navigation Views**:
  - *Overview*: Operational metrics, database readiness, recent investigations, and handoffs.
  - *Investigations*: Case listing, new case dispatch, case workspace with 5 sub-tabs.
  - *Graph Explorer*: Multi-hop interactive transaction graph exploration.
  - *Findings & Evidence*: Deep-dive into verified pattern findings with supporting subgraph visualizer.
  - *Agent Activity*: Task state DAG, unmet dependencies, and role handoffs timeline.
  - *Memory Explorer*: Episodic & semantic memory viewer with full evidence provenance tracing.
  - *Evaluation*: Ground-truth isolated benchmark metrics and AI tool accuracy dashboards.
  - *System Status*: Real-time FalkorDB ping latency, schema v2 migration audit, and subsystem health checks.

### Phase 7: Comprehensive Evaluation, Reliability, Performance & Security Hardening
* **4-Detector Evaluation**: 100% recall (8/8 ground-truth patterns detected); precision 8/14 with transparent error analysis.
* **AI Agent Evaluation**: 15 critical failure scenarios verified; 100% evidence-grounding rate; 0 hallucinated facts in persistent memory.
* **Reliability Testing**: State recovery across 6 distinct interruption points; zero cross-scope memory leakage; DAG cycle detection verified.
* **Performance Benchmarks**: Sub-second p50/p95 latency budgets met for all 4 benchmark categories against real FalkorDB.
* **Security Hardening**: 0 exposed credentials in source; 100% parameterized Cypher; adversarial inputs rejected; strict ground-truth isolation.
* **Unified QA CLI**: `money-trail qa` produces machine-readable JSON and human-readable Markdown evaluation reports.

### Phase 8: Final Integration, Release Readiness & Demonstration
* **End-to-End Pipeline Verification**: Full pipeline (generate → validate → ingest → investigate → persist → findings → evidence provenance) exercised and verified against real FalkorDB.
* **Guided Demonstration Workflow** (`money-trail demo`): Self-contained CLI walkthrough that generates a synthetic dataset, ingests it into a temporary disposable graph, runs all 4 detectors, persists findings, verifies evidence provenance, and reports results. Cleans up automatically on exit.
* **Reproducible Setup Scripts**: `scripts/setup.ps1` (Windows) and `scripts/setup.sh` (Linux/macOS) perform full one-shot environment setup: venv, install, .env, Docker, schema init, and health check.
* **Release Documentation**: Updated `README.md`, `pyproject.toml` v0.8.0, and `docs/PHASE8_VERIFICATION_REPORT.md`.
* **Final Test Suite Result**: **700 passed, 1 skipped** (491 unit tests, 209 real FalkorDB integration tests, 0 failures).

Details: `docs/dashboard.md`, `docs/memory.md`, `docs/coordination.md`, `docs/architecture/graph-schema.md`, `docs/agent.md`, `docs/detection.md`, `docs/data-ingestion.md`, `docs/evaluation.md`.  
Verification: `docs/VERIFICATION_REPORT.md`, `docs/PHASE7_VERIFICATION_REPORT.md`, `docs/PHASE8_VERIFICATION_REPORT.md`.

## Stack

* **Python**: 3.10–3.13 (Python 3.11 recommended; `tzdata` required on Windows for timezone support)
* **Database**: FalkorDB `falkordb/falkordb-server:v4.18.2` (client `falkordb==1.8.0`)
* **LLM SDK**: `anthropic==1.11.0` (AI agent; deterministic engine runs without any LLM)
* **Frontend**: `streamlit>=1.30.0` with embedded Vis.js (`vis-network`)
* **Testing & Tools**: `pytest`, `python-dotenv`

## Project Layout

```text
src/money_trail/
  config/settings.py          centralized environment configuration
  database/                   client, schema registry (v2), init, health, GraphStore, migration.py
  ingestion/                  contract, config, reader, validation, pipeline, report
  generation/                 generator config and reproducible synthetic generator
  investigation/              queries.py, config.py, engine.py, evidence.py, findings.py, detectors/
  agent/                      config, request, prompts, agent loop, tools, ledger, grounding, verify, llm/
  memory/                     models.py, store.py (episodic & semantic graph memory, anti-poisoning)
  coordination/               models.py, state_machine.py, store.py, orchestrator.py, roles/
  dashboard/                  app.py, service.py, components/, views/
  evaluation/                 ground_truth.py, matching.py, metrics.py, report.py, agent_eval.py,
                              qa_config.py, qa_runner.py, detector_eval.py, agent_harness.py,
                              memory_coordination_eval.py, benchmarks.py, security_audit.py
  cli.py                      money-trail, money-trail-check, money-trail-init
docs/                         VERIFICATION_REPORT.md, PHASE7_VERIFICATION_REPORT.md,
                              PHASE8_VERIFICATION_REPORT.md, dashboard.md, memory.md,
                              coordination.md, architecture/graph-schema.md
examples/                     generator.example.json, ingest.example.json, detection.example.json, agent.example.json
scripts/                      setup.ps1 (Windows), setup.sh (Linux/macOS), check_environment.py
tests/unit, tests/integration, tests/support.py
```

## Quickstart (Reproducible Setup)

### Windows (PowerShell)

```powershell
# One-shot setup (venv + install + docker + schema init + health check)
.\scripts\setup.ps1

# Activate and run the demonstration walkthrough
.venv\Scripts\Activate.ps1
money-trail demo
```

### Linux / macOS / WSL (bash)

```bash
# One-shot setup
bash scripts/setup.sh

# Activate and run the demonstration walkthrough
source .venv/bin/activate
money-trail demo
```

### Manual Setup

```bash
# 1. Virtual environment
python -m venv .venv
source .venv/bin/activate       # On Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pip install tzdata              # Required on Windows for timezone support

# 2. Environment configuration
cp .env.example .env            # Set FALKORDB_HOST, FALKORDB_PORT, FALKORDB_PASSWORD,
                                # FALKORDB_GRAPH_NAME, MONEY_TRAIL_TEST_GRAPH_PREFIX

# 3. Start FalkorDB
docker compose up -d

# 4. Initialize schema
money-trail-init
money-trail-check
```

## Phase 8 Demonstration Walkthrough

```bash
# Runs the complete pipeline against a temporary disposable graph — no production data touched:
#   generate -> validate -> ingest -> investigate -> findings -> evidence provenance -> cleanup
money-trail demo

# Machine-readable JSON output
money-trail demo --json

# Keep the temporary graph for manual inspection after the walkthrough
money-trail demo --keep-graph
```

## Phase 7 QA Pipeline

```bash
# Run the comprehensive evaluation, reliability, performance, and security suite
money-trail qa --output-dir evaluation_reports

# Reports are written to:
#   evaluation_reports/phase7_evaluation_report.json   (machine-readable)
#   docs/PHASE7_VERIFICATION_REPORT.md                 (human-readable)
```

## Phase 5 CLI Workflow

### Schema Management & Migrations
```bash
money-trail schema-version
money-trail migrate
```

### Persistent Memory Inspection
```bash
money-trail memory list --scope investigation --investigation-id inv_demo_1
money-trail memory show mem_1234567890abcdef
money-trail memory provenance mem_1234567890abcdef
money-trail memory history mem_1234567890abcdef
```

### Multi-Agent Coordination & Tasks
```bash
money-trail task list --investigation-id inv_demo_1
money-trail task blockers task_1234567890
money-trail handoff list --investigation-id inv_demo_1
money-trail audit inv_demo_1
money-trail session resume sess_1234567890
```

## Running the Automated Test Suite

```bash
# Unit tests only (no FalkorDB required)
pytest tests/unit

# Integration tests against real FalkorDB
pytest tests/integration

# Complete test suite (Phases 1–8)
pytest
```

*Current test suite status:* **700 passed, 1 skipped** (the live Anthropic provider request is
skipped when no API key is provided; all deterministic interfaces fully verified).

## Known Limitations

1. **Live Provider Verification**: Real-model requests remain unverified due to the absence of
   valid provider credentials in the local environment. All deterministic interfaces, tool schemas,
   safety safeguards, and model doubles are fully verified.
2. **Single-Coordinator Process Model**: Atomic single-worker task claiming is verified in FalkorDB;
   true distributed multi-coordinator clustering across separate network nodes is restricted to
   single coordinator scheduling in this implementation.
3. **Vis.js CDN Dependency**: Interactive graph rendering loads `vis-network` from `unpkg.com`.
   In air-gapped environments, the accessible tabular alternative (`st.dataframe`) is always
   co-rendered alongside every graph canvas.
