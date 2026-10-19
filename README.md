# Money Trail

Money Trail is a graph-backed financial investigation agent being built for Graph Hacks 2026
(Track 1: Agents That Act on Connected Data, designed to accommodate Track 2: Agent Memory and Coordination).

Current state: **Phase 1** (verified foundation), **Phase 2** (data contract, synthetic data, CSV ingestion, foundational
read-only queries), **Phase 3** (deterministic graph investigation engine: four configurable detectors, evidence-backed
findings stored in FalkorDB, evaluation against synthetic ground truth) and **Phase 4** (an AI investigation agent that
selects registered graph tools and writes an evidence-grounded report) are implemented. The agent's **live provider
integration is unverified** (no credential was available when it was built; see `docs/VERIFICATION_REPORT.md`). There is no
agent framework, no MCP server, no persistent agent memory beyond stored findings, and no dashboard yet.

## Problem and approach

Suspicious activity often spans many accounts and transfers, so one transaction rarely shows the pattern. Money Trail
will follow relationships between accounts and transactions, gather evidence, remember earlier investigations and
explain its findings. FalkorDB is central because the question is relational (who sent funds to whom, through which
transfers, supported by which evidence): the graph itself performs the traversal, and the same graph will hold
investigation state for agent memory and coordination.

## What exists

Phase 1 (unchanged and still passing): validated environment-driven configuration, FalkorDB connection layer, the
graph schema (`docs/architecture/graph-schema.md`), idempotent non-destructive initialization, read-only health
check, Docker Compose file and a no-Docker development server script.

Phase 2:

* canonical data contract with dataset namespacing, exact decimal amount conversion, strict timestamp handling
* configurable, reproducible synthetic data generator (circular, pass-through, fan-in, fan-out patterns, seed alerts)
  with evaluation ground truth kept in a separate file that never reaches the graph
* source-flexible CSV pipeline: configurable column mappings, validation, strict and quarantine modes, duplicate and
  conflict policies, batching, idempotent writes, post-write verification against the database, JSON and text reports
* four read-only investigation queries (transaction context, account activity, bounded network trace, evidence path)
* one `money-trail` command with subcommands; `money-trail-check` and `money-trail-init` unchanged

Phase 3:

* validated detection configuration (`examples/detection.example.json`): every threshold, traversal limit and persistence option is explicit
* four detectors on a common interface: circular transfers, rapid pass-through chains, fan-in, fan-out; graph-native discovery with
  per-hop pruned Cypher, exact integer amount rules, currency-aware, bounded, with truncation and timeouts reported as incomplete
* an investigation engine that anchors on a transaction, an account, or a whole namespace, verifies every detection against the graph, and
  reports distinct outcomes (detected, none found, incomplete, anchor missing, evidence failure, database error)
* findings and evidence persisted with deterministic identities in the existing schema; reruns create nothing new
* an evaluation path, separate from detection, that matches detections to the ground-truth file and reports precision, recall and F1

Phase 4 (details in `docs/agent.md`):

* `money-trail agent investigate`: a bounded tool-use loop. The model picks among seven registered, read-only tools (transaction context,
  account activity, bounded network trace, one detector, all detectors, stored finding, stored evidence); every call is validated before it
  runs; the model cannot run Cypher or change thresholds
* deterministic safeguards: a ledger of everything the model was shown, report-time re-verification against the graph, a sentence-level
  grounding filter, deterministic outcome and classifications, honest incomplete / failed / not-found statuses
* one provider (Anthropic, pinned SDK) with bounded retries and distinct handling of authentication, rate-limit, timeout and malformed-output failures
* the Phase 3 engine, tests and commands are unchanged and usable without any model (`money-trail investigate`)

Details: `docs/data-ingestion.md` (contract, policies, pipeline, reports, queries), `docs/detection.md` (engine, rules, persistence),
`docs/evaluation.md` (matching, metrics, measured results), `docs/evaluation-data.md` (generator, patterns, ground-truth separation).
Verification: `docs/VERIFICATION_REPORT.md` (Phase 4, including the Phase 3 pre-flight), `docs/PHASE3_VERIFICATION_REPORT.md`, `docs/PHASE2_VERIFICATION_REPORT.md`, `docs/PHASE1_VERIFICATION_REPORT.md`.

## Stack

Python 3.10-3.13, `falkordb==1.8.0` (official client), `anthropic==1.11.0` (the only LLM SDK; used only by the agent), `python-dotenv`, pytest. Standard library for everything else
(JSON configuration, csv, decimal, zoneinfo).

## Layout

```text
src/money_trail/
  config/settings.py          only module that reads environment variables
  database/                   client, schema registry, init, health, GraphStore (single and batch writes)
  ingestion/                  contract, config, reader, validation, pipeline, report
  generation/                 generator config and generator
  investigation/               queries.py (Phase 2), config.py, engine.py, evidence.py, findings.py,
                               detectors/ (circular, pass_through, fan_in, fan_out, registry, models, base, windows)
  agent/                       config, request, prompts, agent (loop), tools/ (schema, registry, handlers), ledger, grounding, verify, report,
                               llm/ (base, anthropic_provider, retry, factory), testing (test doubles; not a provider)
  evaluation/                  ground_truth.py, matching.py, metrics.py, report.py, agent_eval.py (never imported by detection or agent code)
  cli.py                      money-trail, money-trail-check, money-trail-init
examples/                     generator.example.json, ingest.example.json, detection.example.json, agent.example.json
scripts/                      check_environment.py, dev_server_no_docker.sh
tests/unit, tests/integration, tests/support.py
docs/
```

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"                 # or: pip install -r requirements.lock && pip install -e . --no-deps
cp .env.example .env                    # replace every placeholder; see the table below
```

| Key | Required | Meaning |
|---|---|---|
| MONEY_TRAIL_ENV | yes | development, test or production (production requires a password and TLS) |
| MONEY_TRAIL_LOG_LEVEL | no (INFO) | DEBUG, INFO, WARNING, ERROR, CRITICAL |
| FALKORDB_HOST, FALKORDB_PORT | yes | server endpoint, no defaults (`FALKORDB_PORT` is also the host port in `compose.yaml`) |
| FALKORDB_USERNAME, FALKORDB_PASSWORD | production: password | a username requires a password |
| FALKORDB_TLS, FALKORDB_TLS_CA_CERT | no | TLS flag and optional CA file |
| FALKORDB_CONNECT_TIMEOUT, FALKORDB_SOCKET_TIMEOUT | yes | seconds |
| FALKORDB_GRAPH_NAME | yes | letters, digits, underscore; no default graph |
| MONEY_TRAIL_TEST_GRAPH_PREFIX | integration tests | prefix of disposable test graphs; must differ from the graph name |
| MONEY_TRAIL_QUERY_MAX_RESULTS / _MAX_HOPS / _MAX_PATHS / _MAX_GAP_MS | no | limits for investigation queries; can be given per command instead |

Dataset and generator settings live in JSON files (`examples/`), not in the environment.

Start FalkorDB with `docker compose up -d` (stop with `docker compose down`; the data volume is kept), or without
Docker: `FALKORDB_PORT=<port> FALKORDB_PASSWORD=<password> scripts/dev_server_no_docker.sh start` (and `stop`).

## Workflow

```bash
money-trail-init                                   # create graph and schema if missing; never deletes data
money-trail-check                                  # exit 0 only when reachable, graph accessible, schema ready

# 1. generate a reproducible synthetic dataset (writes files only; refuses to overwrite without --overwrite)
money-trail generate --config examples/generator.example.json
#    -> generated-datasets/synthetic-demo/{accounts,transactions,alerts}.csv, ground_truth.json,
#       dataset_manifest.json, ingest_config.json

# 2. validate offline (no database access, nothing written; exit 1 if any record would be rejected)
money-trail validate --dataset-config generated-datasets/synthetic-demo/ingest_config.json

# 3. ingest (the only command that writes the graph; run it again and nothing is duplicated)
money-trail ingest --dataset-config generated-datasets/synthetic-demo/ingest_config.json [--json]

# 4. inspect ingestion reports
money-trail report --dataset-config generated-datasets/synthetic-demo/ingest_config.json list
money-trail report --dataset-config generated-datasets/synthetic-demo/ingest_config.json show <run_id>

# 5. investigate (limits from MONEY_TRAIL_QUERY_* or flags; exit 3 when the anchor record does not exist)
money-trail query transaction <namespace>:<transaction id>
money-trail query account <namespace>:<account id> --direction both --limit 20 [--start-ms N --end-ms N]
money-trail query trace <namespace>:<transaction id> --direction forward --max-hops 4 --max-paths 100 [--max-gap-ms N]
money-trail query evidence <investigation id> --limit 20
```

### Phase 3: investigate, persist, evaluate

```bash
money-trail detectors validate --detection-config examples/detection.example.json      # offline check, prints rule versions
money-trail detectors list                                                              # supported detectors

# investigate from a flagged transaction (or --account-id, or --namespace for a bounded dataset-wide read-only run)
money-trail investigate --detection-config examples/detection.example.json --transaction-id <namespace>:<transaction id> [--persist] [--json]

# read what was persisted
money-trail findings list --transaction-id <namespace>:<transaction id> --limit 20
money-trail findings show <investigation id>

# detection first, then a comparison with the label file (needs ground_truth.json from `generate`)
money-trail evaluate --detection-config examples/detection.example.json --namespace synthetic-demo \
    --ground-truth generated-datasets/synthetic-demo/ground_truth.json
```

Exit codes for `investigate` and `evaluate`: 0 complete, 1 failure (invalid config, database error, evidence failure), 3 anchor not found,
4 incomplete (a search limit or timeout was hit). `limits.query_timeout_ms` must be smaller than `FALKORDB_SOCKET_TIMEOUT`.
Only transaction-anchored investigations can be persisted. A detection is a configured structural pattern, not a conclusion about wrongdoing.

### Phase 4: AI-assisted investigation

```bash
# credentials: put the key in the variable named by provider.api_key_env (default ANTHROPIC_API_KEY) in .env or the environment
money-trail agent validate-config --agent-config examples/agent.example.json --check-credentials   # offline; never prints the key
money-trail agent tools --agent-config examples/agent.example.json --detection-config examples/detection.example.json   # the registered tools

# graph-only investigation (no model, unchanged Phase 3 engine)
money-trail investigate --detection-config examples/detection.example.json --transaction-id <namespace>:<transaction id> --json

# AI-assisted investigation: text report, optionally with the tool-call trace
money-trail agent investigate --agent-config examples/agent.example.json --detection-config examples/detection.example.json \
    --transaction-id <namespace>:<transaction id> --question "Do the connected transfers warrant further review?" --trace
#   other forms:  --format markdown | --format json (or --json)   --account-id ...   --request-file request.json
#   limits:       --max-steps N  --max-tool-calls N  --max-duration-seconds S   (can only lower the configured limits)
#   persistence:  --persist   (transaction anchors only; writes verified findings with the Phase 3 identities; default is read-only)

# live agent-quality evaluation against the real provider (refuses without a credential)
money-trail agent evaluate --agent-config examples/agent.example.json --detection-config examples/detection.example.json \
    --ground-truth generated-datasets/synthetic-demo/ground_truth.json
```

Exit codes for `agent investigate`: 0 completed, 1 failure (invalid request or configuration, missing credential, provider failure, database error),
3 anchor not found, 4 incomplete (a limit, truncated search, tool failure or provider interruption; verified evidence collected so far is still reported),
5 unsupported intent. A missing credential or a provider outage is reported as such; no report text is invented. Requests that are invalid, unknown or unsupported
never reach the model. The command never regenerates data, initialises a graph or resets investigations.

Every command accepts `--env-file PATH` (default `./.env`) and most accept `--json`. Generated datasets and ingestion
output go to `generated-datasets/` and `ingest-output/` (git-ignored).

## Tests

```bash
pytest tests/unit                 # no database needed
pytest tests/integration          # needs a running FalkorDB and a configured .env (Phase 1 to 4 files)
pytest                            # everything (MONEY_TRAIL_TEST_ENV_FILE=path selects another env file)
pytest -m provider -rs            # live provider checks only (see below)
```

Agent tests that need a model use a scripted test double (`money_trail/agent/testing.py`, `tests/agent_fakes.py`). They verify orchestration, limits,
validation, verification and reporting against a real FalkorDB, **not** how well a real model chooses tools. `tests/integration/test_provider_live.py`
holds the provider tests: one sends a deliberately invalid key to the real endpoint (proves authentication-failure handling), and one runs a real
investigation with a real credential. Without a credential the latter is reported as `SKIPPED: BLOCKED ... UNVERIFIED`, never as passed.

Integration tests run against a real server and never skip for missing configuration or an unreachable database.
Each test creates its own graph `<MONEY_TRAIL_TEST_GRAPH_PREFIX>_<random>` and deletes only that graph. The
wrong-password test skips with an explicit reason if the server has no password. Test data is generated per run
(never real records).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Invalid configuration` listing keys | missing or placeholder values in `.env` |
| `Could not connect to FalkorDB` | server not running, wrong host/port, or TLS mismatch |
| `failed_preflight` on ingest | graph or schema missing: run `money-trail-init` |
| `failed_validation` | see the report's error codes and sample line numbers; `money-trail validate` reproduces it offline |
| `failed_conflict_check` / `GRAPH_CONFLICT` | the graph already holds different data for an id; choose `graph_conflict_policy` deliberately |
| `failed_write` | read `failure_detail` in the report, fix the cause, rerun the same command |
| `max_hops is required` | give `--max-hops` or set `MONEY_TRAIL_QUERY_MAX_HOPS` |
| `agent config ... provider.name` / `Unsupported provider` | only `anthropic` is implemented |
| `credential is not set` (agent) | set the variable named by `provider.api_key_env`; `agent validate-config --check-credentials` shows whether it is visible |
| `authentication_failed` | the provider rejected the key; not retried |
| agent run is `incomplete` with `tool_call_limit_reached` etc. | a configured limit stopped the run; raise it in the agent config or ask a narrower question |
| `result_too_large` in a trace | lower `max_hops`/`max_paths` in the call, or raise `agent.max_tool_result_chars` |
| `Output directory ... already contains files` | pass `--overwrite` to `generate`, or change `namespace` |

## Known limitations

* Docker Compose (`compose.yaml`) has never been run (no Docker in the build environments). The image tag
  `v4.18.2` comes from published documentation and was not pulled; all verification used a real FalkorDB server
  (module build 41803) started with the no-Docker script.
* TLS and username/ACL connection modes are validated in configuration but were not tested against such a server.
* Ingestion is not atomic across batches (see `docs/data-ingestion.md`); reruns reconcile.
* Only CSV is supported, and only the generator's own output is covered by end-to-end tests. No AMLSim adapter exists.
* The generator keeps a whole dataset in memory; ingestion streams. Measured scale: about 60,000 transactions.
* Constraints cover node uniqueness and presence only; types and ranges are validated in Python.
* Trace queries can become expensive on dense graphs; hops and paths are mandatory and capped.
* The ground-truth separation is enforced in this repository's code paths; the label file sits next to the data.
* Linux only; the schema version is not stored in the graph and there is no migration tooling.
* Detection rules are structural. Legitimate activity (payroll, merchants, refunds, quick payment chains) satisfies them, and a cycle also contains
  chains that satisfy the pass-through rule. Precision depends on the data's base rates and the configured thresholds (see `docs/evaluation.md`).
* Evaluation uses only synthetic data with patterns shaped like the detectors' own rules; thresholds in the example were set from the generator's
  documented parameters. The figures do not estimate real-world accuracy.
* A search that hits `max_candidate_paths` (per path length and query), a detection cap or the timeout is reported as incomplete and returns a
  subset whose selection is not guaranteed. Dataset-wide analysis of a very large and dense graph may therefore be incomplete.
* Findings are never edited or deleted; if the graph changes, earlier findings remain as the record of the earlier evaluation.
* The Phase 2 `query trace` command still has no server-side timeout; the agent's trace tool uses one (`tool_limits.query_timeout_ms`).
* Phase 4 limitations are listed in `docs/agent.md#limitations`: the live provider path is unverified without a credential, the grounding filter cannot judge
  meaning, only the Anthropic provider exists, and agent-quality figures recorded so far come from a scripted model.

## Next phase

Phase 5 (persistent agent memory and multi-agent coordination) is not started. Before it, the live-provider verification in
`docs/VERIFICATION_REPORT.md` should be completed with a real credential.
