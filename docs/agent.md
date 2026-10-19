# Phase 4: the AI investigation agent

The agent is a thin, bounded layer on top of the deterministic Phase 3 engine. A language model chooses which registered tool
to call next and writes commentary. Everything factual (records, patterns, evidence, identifiers, amounts, counts) comes from
FalkorDB through deterministic code, and the final report is built from a ledger of what the tools actually returned.

```text
 request (JSON / CLI flags)
   -> validate (invalid | missing anchor | unknown anchor | unsupported intent | valid)
   -> system resolves the anchor: get_transaction_context / get_account_activity   (recorded, counts against the tool limit)
   -> loop (bounded): model picks ONE registered tool -> arguments validated -> executed against FalkorDB
                      -> result delivered to the model AND recorded in the ledger
      ... until the model submits its report, or a limit / failure is reached
   -> re-verify every detection against the graph   (graph is authoritative at report time)
   -> optional persistence of verified findings (explicit --persist)
   -> grounding filter on model-written text -> structured report
```

## Division of labour

| The model may | Deterministic code always does |
|---|---|
| choose the next registered tool and its arguments (within validated ranges) | database queries, bounded traversal, timeouts |
| decide whether more evidence is needed, or that none is | pattern detection with the configured thresholds |
| write a summary, interpretations, hypotheses, open questions | evidence retrieval, identity and arithmetic, finding/evidence ids |
| flag that the question asks for something no tool can establish | verification, grounding, classification, run status, persistence |

The model cannot run Cypher, name a function, read a file, change a threshold, or write to the graph. Tool names, argument
names, enums (the detector list comes from the detection configuration), integer ranges and identifier shape are all validated
before anything executes; rejected calls reach no query and consume no tool-call budget.

## Configuration

`examples/agent.example.json` (strict: every key required, unknown keys rejected, ceilings enforced):

| Key | Meaning |
|---|---|
| `provider.name` | `anthropic` is the only implemented provider; any other value is rejected |
| `provider.model` | model identifier, operator-supplied (the value in the example file has **not** been verified against the live API) |
| `provider.api_key_env` | the **name** of the environment variable that holds the key (read from the process environment or `.env`; never stored in a config file) |
| `provider.base_url` | `null` for the default endpoint, or an http(s) URL |
| `provider.request_timeout_seconds`, `max_output_tokens` | per-request limits |
| `provider.max_retries`, `retry_initial_backoff_seconds`, `retry_max_backoff_seconds` | bounded exponential backoff for rate limits, timeouts and unavailability only |
| `agent.prompt_version` | selects a versioned instruction text (`v1`); limits are injected into it from this file |
| `agent.max_steps` | model requests per investigation, including the final report request |
| `agent.max_tool_calls` | executed tools, including the system's anchor lookup |
| `agent.max_duration_seconds` | wall-clock budget (also stops retries) |
| `agent.max_invalid_tool_calls`, `max_output_recoveries` | rejected calls tolerated; re-prompts after an unusable response |
| `agent.max_tool_result_chars` | a result larger than this is **not delivered** (the model is told to narrow the request) |
| `tool_limits.*` | hard upper bounds for results, hops, paths, gap, per-query timeout (`query_timeout_ms` must be below `FALKORDB_SOCKET_TIMEOUT`) |
| `grounding.*` | forbidden conclusion terms, hedging markers, maximum text length |

The pinned SDK (`anthropic==1.11.0`, verified by inspecting its `messages.create` signature) exposes **no sampling-temperature
parameter**, so none is configurable. Only `max_tokens`, `tools`, `tool_choice`, `system`, `messages` and `timeout` are used. SDK-level
retries are disabled so that `max_retries` is the only retry policy in effect.

Request limits (`--max-steps`, `--max-tool-calls`, `--max-duration-seconds`, or `limits` in a request file) can only **lower** these values.

Failure behaviour: a missing credential or invalid configuration is reported before any network call; authentication, bad-request,
context-overflow and refusal outcomes are never retried; rate limit, timeout and unavailability are retried up to `max_retries` and
then reported. Earlier verified evidence is always kept; no analysis is invented for the part that did not run.

## Request format and validation

```json
{"anchor_type": "transaction|account", "anchor_id": "<namespace>:<id>", "question": "...", "intent": "pattern_review",
 "namespace": "<optional>", "detection_config": "<optional path>", "limits": {"max_tool_calls": 6}, "request_id": "<optional>"}
```

Outcomes: `invalid_request`, `missing_anchor`, `unknown_anchor` (checked against the graph), `valid`, `valid_unsupported_intent`.
None of them except `valid` reaches the model or any tool.

Precedence: (1) the structured anchor always decides what is investigated; (2) a `namespace` qualifies a bare id exactly as ingestion
does; (3) identifiers in the question that differ from the anchor are reported as warnings and **not** investigated; (4) a structured
`intent` (`pattern_review`, `evidence_verification`) is authoritative, and an unsupported value is refused deterministically;
a free-text question the tools cannot answer is assessed by the model (`question_assessment`) and cannot widen what runs; (5) request limits only lower configured limits.

## Registered tools (all read-only)

`agent tools --agent-config ... --detection-config ... [--json]` prints the live registry with schemas. None of the tools writes to the graph.

| Tool | Purpose | Notable limits and failure modes |
|---|---|---|
| `get_transaction_context` | transaction, endpoints, alerts, stored findings | `not_found` for an unknown id |
| `get_account_activity` | in/out transactions of an account, optional window | `limit` ≤ `tool_limits.max_results`; truncated lists are `incomplete` |
| `trace_transaction_network` | time-respecting paths from a transaction or account, forward or backward | hops ≤ `max_hops` (≥ 2 for accounts), paths ≤ `max_paths`; truncation and server timeout are `incomplete` (never reported as "no paths") |
| `run_pattern_detector` | one configured detector at a transaction or account anchor | detector must be enabled in the detection configuration; thresholds cannot be changed |
| `get_finding_details` | a stored finding with evidence and referenced transactions | `not_found`, or `incomplete` with the problems if references are missing |
| `get_evidence_details` | a stored evidence record, its detector metadata and transactions | detects missing transactions and a shrunken reference set |
| `run_investigation` | every enabled detector at an anchor (broad analysis) | same limits; does not start another agent |

Every result carries `status` (`ok`, `not_found`, `incomplete`, `error`, `invalid_arguments`), `truncated`, `warnings`, `data`. Amounts are
integer minor units; timestamps are epoch milliseconds UTC. Only results that were actually delivered enter the ledger and become citable.

## Loop control

The system performs the anchor lookup first (a missing anchor stops here with zero model calls). Every later step is the model's.
When `max_steps` or `max_tool_calls` is reached, or the deadline passes, the loop stops; on the last permitted request the model is
forced to submit its report (`tool_choice` restricted to the submission tool), the run is marked `incomplete`, and the reason is recorded.
Prose instead of a tool call, a truncated output or a malformed submission gets `max_output_recoveries` re-prompts, then the run ends as
`invalid_model_output`. Model text is never executed.

## Evidence verification and the report

At report time every detection the model was shown is re-read from the graph (transactions, amounts, currencies, timestamps, endpoints,
structure) and its pattern type is checked against a detector that actually ran for that anchor. A finding that fails is listed under
`unverified_findings` with the reason and excluded from the headline and outcome.

Report policy (schema version 1; see `money_trail/agent/report.py`):

* **observation** and the optional deterministic **hypothesis** come from verified detections; the model cannot create, edit, upgrade or hide them
  (every verified detection is reported whether or not the model mentions it);
* **interpretation / hypothesis** statements by the model are attached only to verified detections, labelled `source: model`, and filtered;
* `summary.headline` and `summary.outcome` are deterministic: `patterns_identified`, `no_pattern_identified_under_tested_criteria`
  (never "legitimate"), or `not_determined` (nothing ran, a search was incomplete, or a detection failed verification);
* `outstanding_questions` list truncation, tool failures, missing records, unverified findings, question warnings, limits, provider failures and
  anything the model flags;
* `investigation_activity` records each call (tool, validated arguments, status, duration, truncation, errors, result references; origin
  `system_anchor_resolution` or `model`), each model step, and provider usage (tokens only when the provider reported them; no cost is computed).

Grounding filter (sentence level, over all model-written text): a sentence is dropped if it contains an identifier the model was not shown, a number that
does not appear in the retrieved records, tool-reported counts, run counts or the active configuration (unit conversions of configured times
are allowed), a timestamp that matches no retrieved record, or a term from `grounding.forbidden_conclusion_terms` outside hedged or negated wording.
Rejected sentences are recorded by reason, length and hash; their content is not echoed into the report.

## Persistence

Runs are read-only by default. `--persist` (transaction anchors only) writes the verified detections found from the anchor using the Phase 3 deterministic
identities, so reruns create nothing new; each persisted evidence record is then read back and compared with the detection. A later run sees stored
findings in `get_transaction_context` and can verify them with `get_finding_details` and `get_evidence_details`.

## Agent-quality evaluation

`money_trail/evaluation/agent_eval.py` (never imported by the agent) computes tool-selection accuracy, tool-execution success, completion rate,
unnecessary-call rate, evidence-grounding rate, unsupported-claim rate (plus post-filter unresolved identifiers, which must be 0), independent re-verification of
findings, latency and token usage. Definitions are in the module docstring. `money-trail agent evaluate` runs the scenario set against the **real** provider
and refuses to run without a credential.

Results recorded so far (`docs/agent-eval-harness-validation.json`, `docs/VERIFICATION_REPORT.md`) come from a **scripted** model and validate the harness, the tools and the
safeguards. They are not a measurement of how well any real model selects tools; that measurement is unverified.

## Limitations

* The synthetic data is shaped like the detectors' own rules; results do not estimate real-world performance.
* A structural pattern is not proven financial crime; false positives (payroll, merchants, refunds) and false negatives (patterns that evade thresholds) are expected.
* The grounding filter checks identifiers, numbers, timestamps and a configured term list. It cannot judge meaning: a purely semantic fabrication with none of those
  elements passes it (measured in `tests/unit/test_agent_safeguard_corpus.py`). Bare identifiers are recognised only when they look like identifiers (letters and digits).
  The primary protection is structural: the model cannot create findings or change their classification.
* Model reasoning is probabilistic. Tool choice and prose quality depend on the model and the instructions.
* Only registered tools execute graph operations; the tool list is code, and a new capability needs new code.
* A truncated activity list or trace marks the run `incomplete` even when the model reports well; that is deliberate.
* Only the Anthropic provider is implemented. **Live provider integration is unverified** unless the verification report says otherwise: the successful-request test needs a credential.
* `get_evidence_details` can detect a shrunken reference set only for evidence whose description records its transaction count (all evidence written by Phase 3 does).
