# Money Trail — Demonstration Runbook (Phase 8)

**Project:** Money Trail  
**Competition:** Graph Hacks 2026 — FalkorDB  
**Tracks:** Track 1 (Agents That Act on Connected Data) & Track 2 (Agent Memory and Coordination)  
**Version:** 0.8.0 (Release Candidate)  

---

## 1. Overview & Objective

This runbook provides an operator, reviewer, or competition judge with step-by-step instructions to reproduce the complete Money Trail financial investigation workflow from scratch.

Money Trail provides two complementary demonstration workflows:
1. **Automated One-Shot Demonstration (`money-trail demo`)**: An automated CLI walkthrough that exercises the entire pipeline (generate $\to$ validate $\to$ ingest $\to$ detect $\to$ persist findings $\to$ verify evidence provenance $\to$ cleanup) in a disposable FalkorDB graph in under 1 second.
2. **Interactive Dashboard Walkthrough (`money-trail dashboard`)**: A visual, interactive Streamlit investigation workspace that allows analysts to inspect multi-hop transaction graphs, execute automated pattern detection, run AI-assisted investigations with persistent memory and multi-agent coordination, and export tamper-evident, evidence-grounded investigation dossiers.

---

## 2. Prerequisites & Environment Setup

### 2.1 System Requirements
* **Python**: 3.10 – 3.13 on `PATH`
* **Docker & Docker Compose**: For running FalkorDB
* **OS**: Linux, macOS, or Windows (PowerShell 5.1+)

### 2.2 Automated One-Shot Environment Setup

**Windows (PowerShell):**
```powershell
.\scripts\setup.ps1
```

**Linux / macOS / WSL (Bash):**
```bash
chmod +x scripts/setup.sh
./scripts/setup.sh
```

**Or using Make (Linux/macOS):**
```bash
make install
make up
make init
make check
```

The setup scripts automatically:
1. Create and activate a Python virtual environment (`.venv`).
2. Install Money Trail in editable mode with development dependencies (`pip install -e ".[dev]"`).
3. Generate `.env` from `.env.example` if not already present.
4. Launch FalkorDB via `docker compose up -d`.
5. Wait for FalkorDB port 6379 readiness.
6. Initialize the graph schema and indexes (`money-trail init`).
7. Run the health check (`money-trail check`).

---

## 3. Demonstration Workflow 1: The Automated CLI Walkthrough

The fastest way to verify end-to-end functionality is the built-in `money-trail demo` command.

### Command:
```bash
money-trail demo
```

### Expected Output:
```text
============================================================
  Money Trail — End-to-End Demonstration Walkthrough
============================================================
  Demo ID        : 5fbc79bd
  Namespace      : demo_5fbc79bd
  Temporary Graph: money_trail_demo_5fbc79bd
============================================================

[1/6] GENERATE   ok  counts={'accounts': 30, 'transactions': 52, 'alerts': 5}  (0.047s)
[2/6] VALIDATE   ok  errors=0  (0.0s)
[3/6] INGEST     ok  status=succeeded  (0.047s)
[4/6] DETECT     ok  6 pattern(s)  (0.125s)
[5/6] FINDINGS   ok  4 finding(s) retrieved  (0.0s)
[6/6] PROVENANCE ok  2 evidence -> 3 transactions  (0.0s)

============================================================
  Result: PASSED  (total 0.359s)
============================================================
```

### What Occurs Under the Hood:
1. **Generate**: Generates a self-contained synthetic transaction dataset with ordinary traffic plus 4 distinct financial crime topologies (circular laundering, pass-through layer, fan-in structuring, fan-out dispersal).
2. **Validate**: Verifies data schema, timestamps, positive amounts, valid currencies, and account referential integrity.
3. **Ingest**: Connects to FalkorDB and creates a temporary isolated graph (`money_trail_demo_<id>`), ingesting accounts, transactions, and seed alerts with indexed lookups.
4. **Detect**: Executes the 4 Cypher graph pattern detectors (`circular`, `pass_through`, `fan_in`, `fan_out`), detecting the patterns with zero evaluation leakage.
5. **Findings**: Persists an anchor-grounded `Investigation` node, linking `Finding` nodes through `:HAS_FINDING`.
6. **Provenance**: Verifies the strict grounding chain `(f:Finding)-[:SUPPORTED_BY]->(e:Evidence)-[:REFERENCES]->(t:Transaction)` in FalkorDB.
7. **Cleanup**: Drops the temporary demo graph, leaving the primary database clean. (Add `--keep-graph` to inspect it in FalkorDB Browser).

---

## 4. Demonstration Workflow 2: Interactive Dashboard Investigation

To experience the analyst-facing UI, populate the primary graph and launch the Streamlit workspace.

### Step 4.1: Ingest Demo Data into the Primary Graph
```bash
# Generate synthetic dataset
money-trail generate --config examples/generator.example.json

# Ingest into the primary FalkorDB graph
money-trail ingest --dataset-config generated-datasets/synthetic-demo/ingest_config.json
```

### Step 4.2: Launch the Dashboard
```bash
money-trail dashboard
# Or directly via streamlit:
streamlit run src/money_trail/dashboard/app.py
```
Open `http://localhost:8501` in your browser.

---

## 5. Walkthrough Scenarios (A through I)

### Scenario A: Start an Investigation
1. In the sidebar, select **Dataset / Investigation Scope**: `synthetic-demo`.
2. Navigate to the **Investigation Workspace** tab.
3. In the **New Investigation** panel, select an Alert Anchor (e.g., `synthetic-demo:ALT:seed_signal:...`) or input a known Transaction ID.
4. Click **Launch Investigation**.
5. *Expected Result:* The workspace displays an active investigation ID, calculates risk scoring, and populates the investigation context without hallucinated metadata.

### Scenario B: Discover Connected Financial Activity
1. Navigate to the **Graph Explorer** tab.
2. Enter an Account ID (e.g., `synthetic-demo:ACC:...`) or Transaction ID.
3. Select Max Hops = `3`, Max Paths = `50`.
4. Click **Trace Network**.
5. *Expected Result:* The interactive Vis.js graph renders the transaction topology, coloring source/destination accounts, transaction directed edges with amount and timestamp tooltips, and highlighting intermediate laundering hubs.

### Scenario C: Automated Topology Detection
1. Navigate to the **Patterns & Evidence** tab.
2. Select **Run All Detectors**.
3. *Expected Result:* The 4 detectors report detections:
   * **Circular Transfers**: Rapid cyclical flow returning to the origin account with small retention loss.
   * **Pass-Through / Layering**: Rapid sequential hops through intermediary accounts within configured time windows.
   * **Fan-In / Structuring**: Multiple distinct depositors funneling funds into a single collector account.
   * **Fan-Out / Dispersal**: Rapid distribution from a source account to multiple distinct recipients.

### Scenario D: Verify Evidence Provenance
1. In **Patterns & Evidence**, expand any detected pattern.
2. Inspect the **Evidence Chain** table.
3. Click on a linked Transaction ID.
4. *Expected Result:* The transaction details appear with exact timestamp (UTC), minor-unit amount, currency, channel, and counterparty accounts. Evidence cannot exist without a verified FalkorDB transaction node.

### Scenario E: Multi-Agent Coordination (Track 2)
1. Navigate to the **Memory & Coordination** tab.
2. In the **Agent Coordination & Handoffs** section:
   * Observe the active task list: `triage` $\to$ `forensic_analysis` $\to$ `reporting`.
   * Trigger a task handoff: Dispatch `TriageAgent` to classify an anomaly; upon detecting high risk, the task state transitions to `IN_PROGRESS` assigned to `ForensicSpecialistAgent`.
   * Inspect the handoff payload and audit log: Note the deterministic state machine transitions (`PENDING` $\to$ `IN_PROGRESS` $\to$ `COMPLETED`).
3. *Expected Result:* No race conditions, zero orphaned tasks, and complete persistence of agent handoff messages in FalkorDB.

### Scenario F: Investigation Resumption
1. Note the active `Investigation ID` from Scenario A.
2. Stop the dashboard (`Ctrl+C`) or refresh the browser.
3. In the sidebar or Workspace, select **Resume Investigation** and provide the previous `Investigation ID`.
4. *Expected Result:* All findings, graph notes, task states, and evidence linkages are fully restored from FalkorDB graph state without loss.

### Scenario G: Inspect Persistent Memory (Track 2)
1. In the **Memory & Coordination** tab, navigate to the **Persistent Memory Explorer**.
2. Query memory by Entity Anchor (`account_id` or `transaction_id`) or by Tag (`#structuring`, `#layering`).
3. Review episodic records (past analyst notes, hypothesis evaluations) and semantic knowledge entries.
4. *Expected Result:* Memory nodes maintain bi-temporal timestamps, access counts, and confidence scores, providing cross-investigation context recall.

### Scenario H: Review and Export Final Report
1. Navigate to the **Reports & Export** tab.
2. Select the completed investigation and click **Generate Evidence-Grounded Dossier**.
3. Choose format: **Markdown** or **JSON**.
4. Click **Download Report**.
5. *Expected Result:* The generated report contains:
   * Summary executive overview.
   * Complete inventory of validated findings.
   * Strict evidence citations (transaction IDs, timestamps, amounts).
   * Documented investigative limitations and confidence bounds.
   * Digital hash / audit fingerprint.

### Scenario I: Demonstrate Uncertainty & Boundary Safety
1. Choose an account with ordinary transactions that does not meet the pattern threshold (e.g., only 2 fan-in sources when the rule requires 5).
2. Run detection or query the AI agent.
3. *Expected Result:* The agent reports `no_pattern_found` or `inconclusive evidence`. The system explicitly avoids flagging ordinary activity as suspicious and refuses to hallucinate patterns where graph topology does not strictly satisfy the configured rule definitions.

---

## 6. Verification and Teardown

### Full Test Suite Execution:
```bash
pytest -q
```
*Expected Result:* `700 passed, 1 skipped` (the 1 skipped test is for optional live Anthropic API key).

### Stop Database and Retain Data:
```bash
make down
# Or: docker compose down
```

### Complete Reset (Clean Slate):
```bash
docker compose down -v
```
