# Money Trail

Money Trail is a graph-backed financial investigation agent built for Graph Hacks 2026
(**Track 2: Agent Memory and Coordination**; **Track 1: Agents That Act on Connected Data**).

Current state: **Phase 1** (verified foundation), **Phase 2** (data contract, synthetic data, CSV ingestion, foundational
read-only queries), **Phase 3** (deterministic graph investigation engine: four configurable detectors, evidence-backed
findings stored in FalkorDB, evaluation against synthetic ground truth), **Phase 4** (an AI investigation agent that
selects registered graph tools and writes an evidence-grounded report), **Phase 5** (**persistent graph memory,
shared agent state & multi-agent coordination**), and **Phase 6** (**interactive dashboard, vis.js graph visualization & complete investigation workspace**) are fully implemented and verified against real FalkorDB.
The agent's **live external provider request remains unverified** because no external API credential was provided in the environment (see `docs/VERIFICATION_REPORT.md`); all deterministic interfaces, provider validation, schema migrations, coordination engines, and dashboard workspaces are 100% verified.

## Problem and Approach

Suspicious financial activity often spans multiple accounts, layered transactions, and complex timing patterns. Money Trail follows relationships between accounts and transactions, collects evidence, remembers context across sessions and processes, and coordinates specialized agents directly through FalkorDB.

FalkorDB is the central context and coordination layer:
* **The Connected Financial Graph**: Stores accounts, transactions, and signals.
* **Persistent Graph Memory**: Stores episodic investigation traces and evidence-grounded semantic facts with provenance, temporal validity, and supersession links.
* **Shared Multi-Agent Coordination State**: Coordinates tasks, dependencies, state machines, and auditable handoffs directly within FalkorDB.

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

Details: `docs/dashboard.md` (dashboard architecture & views), `docs/memory.md` (persistent memory architecture), `docs/coordination.md` (multi-agent coordination), `docs/architecture/graph-schema.md` (schema v2 & migrations), `docs/agent.md`, `docs/detection.md`, `docs/data-ingestion.md`.
Verification: `docs/VERIFICATION_REPORT.md`.

## Stack

* **Python**: 3.10–3.13 (Python 3.11 recommended; `tzdata` installed for Windows timezones)
* **Database**: FalkorDB (official client `falkordb==1.8.0`)
* **LLM SDK**: `anthropic==1.11.0` (used for AI agent; deterministic engine runs without any LLM)
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
  coordination/               models.py, state_machine.py, store.py, orchestrator.py, roles/ (coordinator, investigator, auditor, curator)
  dashboard/                  app.py, service.py, components/ (styles, graph_viz), views/ (overview, investigations, graph, findings, coordination, memory, evaluation, system_status)
  evaluation/                 ground_truth.py, matching.py, metrics.py, report.py, agent_eval.py
  cli.py                      money-trail, money-trail-check, money-trail-init
docs/                         VERIFICATION_REPORT.md, dashboard.md, memory.md, coordination.md, architecture/graph-schema.md
examples/                     generator.example.json, ingest.example.json, detection.example.json, agent.example.json
tests/unit, tests/integration, tests/support.py
```

## Setup & Quickstart

```bash
# 1. Virtual environment
python -m venv .venv
source .venv/bin/activate       # On Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pip install tzdata              # Required on Windows for America/New_York timezone support

# 2. Environment configuration
cp .env.example .env            # Ensure FALKORDB_HOST, FALKORDB_PORT, FALKORDB_PASSWORD are set
# Note: Ensure FALKORDB_SOCKET_TIMEOUT=30 in .env to accommodate test query timeouts

# 3. Start FalkorDB
docker compose up -d

# 4. Initialize or migrate graph
python -m money_trail.cli migrate
money-trail-check
```

## Phase 5 CLI Workflow

### Schema Management & Migrations
```bash
# Check current schema version and migration history
money-trail schema-version

# Execute non-destructive schema migration to v2
money-trail migrate
```

### Persistent Memory Inspection
```bash
# List memories for an investigation
money-trail memory list --investigation-id inv_demo_1

# Show detailed memory properties and grounded evidence
money-trail memory show mem_1234567890abcdef

# View provenance evidence chain
money-trail memory provenance mem_1234567890abcdef

# View supersession and contradiction history
money-trail memory history mem_1234567890abcdef
```

### Multi-Agent Coordination & Tasks
```bash
# List tasks in an investigation
money-trail task list --investigation-id inv_demo_1

# View task dependency blockers
money-trail task blockers task_1234567890

# List auditable handoffs across agent roles
money-trail handoff list --investigation-id inv_demo_1

# Inspect agent participation and audit summary
money-trail audit inv_demo_1

# Resume an interrupted investigation from FalkorDB
money-trail session resume sess_1234567890
```

## Running the Automated Test Suite

```bash
# 1. Run all unit tests (no database required)
pytest tests/unit

# 2. Run Phase 5 integration tests against real FalkorDB
pytest tests/integration/test_phase5_falkordb_integration.py
pytest tests/integration/test_coordination_e2e.py

# 3. Run the complete test suite (Phase 1 through Phase 5)
pytest
```
*Current test suite status:* **666 passed, 1 skipped** (the live Anthropic request is skipped when no API key is provided).

## Known Limitations

1. **Live Provider Verification**: Real-model requests remain unverified due to the absence of valid provider credentials in the local environment. All deterministic interfaces, tool schemas, and model doubles are fully verified.
2. **Single-Coordinator Process Model**: Concurrency testing verifies atomic single-worker task claiming in FalkorDB; true distributed multi-coordinator clustering across distributed networks is documented and restricted to single coordinator scheduling.
3. **Evidence Expiry**: Semantic memory revalidation flags dependent memories when source transactions change; automatic background re-audit cron jobs are an optional future extension.
