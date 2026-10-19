# Money Trail — Phase 6: Interactive Dashboard & Investigation Workspace

## Overview

Phase 6 provides a production-grade, interactive Streamlit investigation workspace for Money Trail. It exposes all capabilities developed in Phases 1–5 through a unified financial crime investigation interface connected directly to live FalkorDB graph data.

No numbers, graphs, findings, or tasks are simulated or hardcoded. FalkorDB is the authoritative context and execution layer.

---

## Architecture & Technology Stack

- **Frontend Engine**: Streamlit (`streamlit>=1.30.0`)
- **Interactive Graph Canvas**: Embedded Vis.js (`vis-network`) physics simulation with customizable dark-mode design tokens and responsive zoom/pan controls.
- **Accessible Alternative**: Tabular node and directed edge view (`st.dataframe`) alongside every interactive visualizer.
- **Service Layer**: `DashboardService` (`money_trail.dashboard.service`) provides bounded, validated Cypher operations delegating directly to `GraphStore`, `MemoryStore`, `CoordinationStore`, and `MultiAgentOrchestrator`.
- **Primary Database**: FalkorDB graph database on Redis protocol.

---

## Dashboard Navigation & Views

### 1. Overview Dashboard (`overview.py`)
- Real-time database readiness and latency ping indicators.
- Global and scoped dataset selectors.
- Live operational metrics:
  - Transactions
  - Accounts
  - Active & Closed Investigations
  - Observation & Hypothesis Findings
  - Tasks (Pending, Running, Completed, Blocked)
  - Persistent Memories & Agent Sessions
- Recent investigations and multi-agent handoff event feeds.

### 2. Investigation Workspace (`investigations.py`)
- **Browse Cases**: Filter persisted investigations by status (`open`, `in_progress`, `closed`), view starting anchors, and inspect finding counts.
- **New Investigation**: Select a qualified transaction anchor, specify investigative questions, and launch coordinated multi-agent runs (`MultiAgentOrchestrator`).
- **Case Workspace**:
  - **Summary**: Anchor transaction details, timestamp, amount, and case status.
  - **Findings & Evidence**: List of verified detector statements with supporting evidence IDs and transaction count.
  - **Transaction Path**: Directed transfer graph showing source accounts, transactions, and destination accounts.
  - **Agent Timeline & Handoffs**: Chronological audit trail of agent interactions and handoff reasons.
  - **Tasks & Blockers**: Unmet dependencies, task execution states, and session resumption button.

### 3. Interactive Graph Explorer (`graph_explorer.py`)
- Traversal depth controls (1 to 3 hops).
- Node limit guards (up to 100 nodes per view) to prevent browser overload.
- Vis.js canvas with directional transfer arrows, colored node classification:
  - **Emerald / Cyan**: Accounts
  - **Amber / Orange**: Transactions
  - **Diamond**: Root / Selected anchor node
  - **Purple**: Findings
  - **Blue**: Evidence
- Accessible tabular table displaying exact node IDs and directed relationship pairs.

### 4. Findings & Evidence Explorer (`findings.py`)
- Search and filter findings by kind (`observation`, `hypothesis`, `interpretation`) or statement keywords.
- Supporting evidence cards detailing tool provenance (`money_trail.detector.circular`, `pass_through`, `fan_in`, `fan_out`).
- Pattern Subgraph Visualizer: Renders the exact chain of transactions supporting the selected finding.

### 5. Agent Activity & Coordination (`coordination.py`)
- Multi-agent coordination visibility across the 4 specialized roles:
  - **Coordinator / Planner**: Goal decomposition, task sequencing, and dependency tracking.
  - **Graph Investigator**: Bounded graph traversal, Cypher queries, and transaction pattern detection.
  - **Evidence Auditor**: Grounding verification, transaction reference audits, and anti-hallucination validation.
  - **Memory Curator**: Memory distillation, scope isolation, supersession, and contradiction tracking.
- Task DAG and dependency blockers inspection.
- Inter-agent handoff log with execution status and rationale.

### 6. Persistent Graph Memory (`memory.py`)
- Query episodic and semantic memories with strict scope isolation:
  - `investigation` scope: isolated to the specific case.
  - `dataset` scope: isolated to the qualified synthetic dataset.
- Evidence Provenance tracer displaying the complete grounding chain:
  `Investigation -> Finding -> Evidence -> Transaction -> Memory`
- Memory supersession, contradiction, and invalidation history.

### 7. Evaluation & Benchmark Metrics (`evaluation.py`)
- Live display of automated evaluation reports from `docs/agent-eval-harness-validation.json`.
- Agent accuracy metrics:
  - Tool Selection Precision & Recall: 100.0%
  - Evidence Grounding Rate: 100.0%
  - Hallucination / Unsupported Finding Rate: 0.0%
- Ground-truth isolation safeguards preventing runtime leaks to the investigation agent.

### 8. System Status & Diagnostics (`system_status.py`)
- Live FalkorDB connectivity check with millisecond ping latency.
- Applied schema migrations history (`SchemaMigration` node inspection).
- Subsystem diagnostics across Ingestion, Graph Store, Detection, AI Agent, Memory, and Coordination.

---

## Starting the Dashboard

### Via CLI:
```bash
money-trail dashboard --port 8501 --host localhost
```

Run in headless mode:
```bash
money-trail dashboard --headless
```

### Direct Streamlit invocation:
```bash
streamlit run src/money_trail/dashboard/app.py
```
