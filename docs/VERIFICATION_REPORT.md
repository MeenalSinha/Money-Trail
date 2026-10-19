# Phase 4 verification report

Date of execution: 2026-10-19 (sandbox clock). Environment: Linux, Python 3.12, no Docker, real FalkorDB server started with
`scripts/dev_server_no_docker.sh` (loopback, password-protected). **No LLM provider credential was available.**

**Final status: PHASE 4 BLOCKED** (see section 12 for the single blocker and how to clear it). Everything that can be verified without a provider
credential was executed and passed. The agent's behaviour with a *real* model has not been observed.

Three kinds of evidence are kept separate throughout:

| Kind | What it proves | What it does not prove |
|---|---|---|
| Unit tests | logic in isolation (no database, no provider) | anything about FalkorDB or a model |
| Real FalkorDB tests | tools, detectors, verification, persistence, CLI against a real server | how a real model behaves |
| Scripted-model tests | the loop, limits, validation, grounding, reporting work end to end on real graph data | that a real model picks good tools |
| Live provider tests | real endpoint behaviour | (only the failure path ran here) |

## 1. Phase 3 pre-flight (executed before any Phase 4 code)

Procedure: unit, integration and full suites run separately; then checks independent of the repository's own tests (below).

| ID | Result | Evidence |
|---|---|---|
| P3-01 Phase 1/2 foundation operational | PASS | health check `ok`; init; full suite |
| P3-02 configuration loads and validates | PASS | `money-trail-check` configuration valid; invalid detection config rejected (exit 1) |
| P3-03 real FalkorDB connectivity | PASS | real server via the documented no-Docker script (Docker itself unavailable; Docker Compose remains unrun) |
| P3-04 init safe and idempotent | PASS | first run created the schema, second run created 0 and found 34 present |
| P3-05 schema matches documentation | PASS | 9 indexes, 25 constraints (7 unique + 18 mandatory); the `Agent` node type is documented as defined-but-unused |
| P3-06 generator reproducible | PASS | regenerating with the same config gave identical SHA-256 for accounts, transactions, alerts, ground truth |
| P3-07 CSV validation/ingestion | PASS | validate and ingest succeeded; verification passed; hand-built dataset with a currency outside the table was correctly rejected |
| P3-08 reimport creates no duplicates | PASS | second ingest created nothing; existing integration tests agree |
| P3-09 read-only queries correct | PASS | `query transaction/account/trace/evidence` exit 0; unknown id exit 3 |
| P3-10..13 circular / pass-through / fan-in / fan-out on real data | PASS | **independent brute-force reference** (raw Cypher reads of 339 and of 943 transactions, rules re-implemented from the documentation) matched the detectors exactly: 2/6/2/3 and 2/7/2/3 distinct pattern sets, zero differences |
| P3-14 configuration validated and external | PASS | `max_hops=99` rejected with exit 1 |
| P3-15 bounds, time and result limits | PASS | `max_candidate_paths=1` produced `truncated=True`, run incomplete, exit 4 |
| P3-16 positive and negative cases | PASS | 51 hand-built transactions: boundary positives (gap exactly 6 h, window exactly 24 h, gap exactly 2 h) detected; negatives (gap +1 ms, out-of-order, retention 89.9%, currency change, open chain, 4 distinct senders, window +1 ms, 4 recipients) rejected by the relevant detector. Three transactions I had labelled "circular negatives" also appeared in `pass_through` detections; checked by hand, those chains do satisfy the pass-through rule, so that was an error in my test design, not a detector defect |
| P3-17 equivalent detections deduplicated | PASS | detector set sizes equal the reference's deduplicated sets |
| P3-18 findings reference valid transactions | PASS | re-read independently |
| P3-19 persisted and retrieved | PASS | persisted, then read back through `findings list/show` and later through the Phase 4 tools |
| P3-20 reruns create nothing new | PASS | second persist created 0 investigations/findings/evidence; raw node counts unchanged |
| P3-21 ground truth separate | PASS | graph property keys contain no label fields; only `evaluate` imports the evaluation package |
| P3-22 metrics correct | PASS | micro TP 8, FP 5, FN 0 gives precision 8/13 = 0.6154, F1 16/21 = 0.7619 by hand; per-type figures also match |
| P3-23 CLI as documented | PASS | exit codes 0/1/3/4 observed as documented |
| P3-24 unit tests | PASS | 308 passed |
| P3-25 integration tests | PASS | 108 passed (real FalkorDB) |
| P3-26 complete suite | PASS | 416 passed, 0 skipped |
| P3-27 documentation matches implementation | PASS | spot-checked commands and counts |

**Phase 3 defects found: none.** The exit gate passed, so Phase 4 proceeded.

## 2. Phase 4 defects found and repaired during development

These were found by the new tests and by reading real outputs (all fixed, each with a regression test):

1. **Vacuous test.** My "invalid calls reach no query" test patched one `Graph` instance, but `conn.graph` builds a new object per access, so the patch did nothing and the assertion could not fail. Replaced by a class-level spy with a positive control (valid calls must be visible).
2. **Undetected evidence shrinkage.** Deleting a transaction removes its `REFERENCES` edge, so `get_evidence` called the shrunken evidence complete. It now compares the transaction count recorded in the evidence description with what is present.
3. **Ledger downgrade.** A listing stub overwrote a detailed finding already retrieved (`dict.update`). Detailed records now replace stubs, never the reverse.
4. **Sentence splitter over-rejection.** Splitting only before capital letters merged "…cycle. ns:NOPE…" into one sentence and discarded grounded text. Now splits after any sentence end.
5. **Rejected text echoed.** The audit list repeated fabricated sentences verbatim. It now records reason, length and hash only.
6. **Tool-reported numbers rejected.** "the trace found 0 path(s)" was dropped because path counts were not allowed numbers. Numbers a delivered result states (top-level integers, list lengths) are now grounded.
7. **Finding schema inconsistency.** Model findings lacked `pattern_type`; now uniform and validated.
8. **Unsupported SDK parameter.** The pinned SDK's `messages.create` has no temperature parameter (checked by inspecting its signature), so none is offered.
9. Evaluator bugs: detection ids treated as graph nodes; model findings' deterministic evidence ids checked against nodes. Fixed, and a test injects a fabricated id to prove the evaluator catches it.
10. Smaller: schema registry now supplies anchor labels in request validation; a credential-detector literal was replaced by a pattern; duplicate test module basenames; fixture wiring.

## 3. Implementation summary

New package `src/money_trail/agent/` (config, request, prompts, agent loop, ledger, grounding, verify, report, `llm/` provider layer, `tools/` schema, registry and seven handlers, `testing.py` doubles), `evaluation/agent_eval.py`, `examples/agent.example.json`, `docs/agent.md`.
Changed shared code (backward compatible, existing 416 tests unchanged and passing): `exceptions.py`, `config/settings.py` (`load_secret`), `investigation/queries.py` (optional server-side timeout; account-anchored trace), `investigation/engine.py` (optional enabled-detector subset; a subset can never be persisted), `investigation/findings.py` (`get_finding`, `get_evidence`), `cli.py` (`agent validate-config|tools|investigate|evaluate`), `pyproject.toml` (`anthropic==1.11.0`, version 0.4.0, `provider` marker), `requirements.lock`, `.env.example`, `README.md`.
The Phase 3 engine's output on the demo data is identical before and after (13 detections, same ids) and still agrees with the independent reference.

Provider and model configuration (no credentials): provider `anthropic`; SDK `anthropic==1.11.0` (released 18 days before use); model identifier is read from `examples/agent.example.json` (`claude-sonnet-5-5`) and has **not** been verified against the live API; credential variable `ANTHROPIC_API_KEY`; timeout 60 s, 3 retries with 1 s to 20 s backoff, 4096 output tokens; loop limits 10 steps, 12 tool calls, 180 s.

Registered tools (7, all read-only): `get_transaction_context`, `get_account_activity`, `trace_transaction_network`, `run_pattern_detector`, `get_finding_details`, `get_evidence_details`, `run_investigation`.

## 4. Tests executed

| Suite | Result |
|---|---|
| Unit (`tests/unit`) | **462 passed** (308 original + 154 new) |
| Integration, real FalkorDB (`tests/integration`) | **171 passed, 1 skipped** (108 original + 63 new + the blocked provider test) |
| Full suite | **633 passed, 1 skipped** (416 original all still passing; 217 new passing) |
| Clean environment built only from `requirements.lock` | `pip check` clean; 462 unit tests passed |
| Live-provider marker (`pytest -m provider`) | 1 passed (real endpoint rejects an invalid key), 1 **BLOCKED** (needs a credential) |

Spec test coverage: A transaction context (compared with independent raw Cypher), B tracing and bounds including account anchors and truncation, C each detector tool equals the Phase 3 engine output and finds the generated pattern, D stored finding and evidence retrieval including missing and incomplete evidence, E unknown anchors in all nine call forms, F malformed calls rejected with zero queries (with a positive control), G tool-call, step, duration and invalid-call limits, H tool results and report-time verification follow graph changes, I fabricated records and conclusions cannot enter the report, J ground-truth isolation (AST scan, graph property scan, subprocess import check, report and prompt scan), K all original tests.
End-to-end scenarios 1 to 5 all ran (connected activity, insufficient evidence, missing transaction, truncated search and tool failure, unsupported request).

## 5. Provider integration status

| Item | Status |
|---|---|
| Configuration validation, missing credential (no network attempted) | verified (CLI exit 1, "no request was made") |
| Real endpoint reached; **invalid key rejected, mapped to `authentication_failed`, 1 request, 0 retries, key absent from the report** | verified against the real service |
| SDK exception translation (real SDK exception classes), retry/backoff, parsing of SDK-shaped responses | verified by unit tests (no network) |
| **A successful real model request, real tool selection, real structured report submission** | **NOT VERIFIED (no credential)** |
| The model identifier in the example file | **NOT VERIFIED** |

## 6. Actual tool-selection behaviour (scripted model, real graph)

The scripted policy chooses each call from the real results in the conversation. It is a test double and **says nothing about a real model**; it shows the loop adapts when the data differs:

* Scenario 1 (flagged cycle): `get_transaction_context` (system) -> `get_account_activity` -> `run_investigation` -> submit.
* Scenario 2 (ordinary transaction, no pattern): `get_transaction_context` -> `get_account_activity` -> `run_investigation` (nothing found) -> `trace_transaction_network` -> submit.
* Sparse fan-out anchor: probed the destination, found it sparse, probed the source, then ran the investigation.
* Unknown transaction: no tool and no model request at all.

## 7. An actual report (scripted model, real graph; abridged)

```text
Investigation report  [completed]  request req-55abad881ad7
  anchor: transaction synthetic-demo:TXN000000018
  outcome: patterns_identified
  2 verified structural pattern(s) identified under the configured rules (circular, pass_through). This is not a finding of wrongdoing.
Findings (5 verified):
  - [observation/deterministic_rule] circular fnd:89113ba30403d1abaf2480e9a07a38f8
      Observed a time-respecting directed cycle of 3 transactions through 3 accounts (rule circular:demo-rules@1+089124bfebeb). ...does not establish wrongdoing.
      transactions: synthetic-demo:TXN000000018, ...019, ...020      evidence: evd:39290f5ee7dda3b61f9df6e8bb39e0dc
  - [hypothesis/deterministic_rule] ... "Hypothesis (unconfirmed): this transfer structure may warrant human review."
  - [interpretation/model] circular stm:e88062d25c485d52a32bdbd78aacbbb2   (filtered, labelled model-written)
  ...
Verification: 2/2 detections re-verified against the graph; model text: 0 of 2 sentence(s) rejected as ungrounded.
Tools executed: get_transaction_context, get_account_activity, run_investigation
```

The token counts in that run are the test double's placeholders, not provider usage.

## 8. Agent-quality evaluation (SCRIPTED model: validates the harness, tools and safeguards, not model quality)

Raw output: `docs/agent-eval-harness-validation.json`. Seven scenarios per dataset, scripted policy, real FalkorDB.

| Metric | synthetic-demo (339 tx) | audit-noisy (943 tx) |
|---|---|---|
| Tool-selection accuracy (agreement with scenario expectations) | 1.00 | 1.00 |
| Tool-execution success | 1.00 | 1.00 |
| Completion rate | 0.714 | 0.857 |
| Unnecessary-call rate | 0.00 | 0.00 |
| Evidence-grounding rate / unsupported-claim rate | 1.00 / 0.00 | 1.00 / 0.00 |
| Findings independently re-verified | 19/19 | 21/21 |
| Unresolved identifiers after filtering | 0 | 0 |
| Expected generated pattern reported | 1.00 | 1.00 |
| Median / max run latency | 22 ms / 45 ms | 17 ms / 46 ms |
| `run_investigation` mean tool time | 12 ms | 9.7 ms |

Completion below 1.0 is the harness being honest: the scripted policy's `limit=10` activity probes were truncated, so those runs are correctly `incomplete`. Latency excludes any model time. Provider usage is not measured.

Safeguard corpus (`tests/unit/test_agent_safeguard_corpus.py`, authored by me, so optimistic versus an independent red team): **32/32** in-scope adversarial statements rejected (fabricated identifiers, invented or converted numbers, wrong timestamps, unhedged conclusions, mixed, bare identifier-like tokens); **16/16** grounded statements kept; **5/5** purely semantic fabrications (no identifier, number, timestamp or configured term) **pass the filter**, a documented limitation.

Metric arithmetic itself is tested on a run with known answers (for example unnecessary-call rate exactly 0.6 for a run containing one repeat, one covered detector call and one rejected call).

**Not measured:** real-model tool-selection accuracy, grounding, unnecessary calls, latency and token usage. Run `money-trail agent evaluate` with a credential.

## 9. Repository inspection (step 24)

* Cypher in `agent/`: one query (anchor existence), label and id property taken from the schema registry, id parameterised. `queries.py` places only a validated, capped integer hop count in query text. The model cannot supply Cypher.
* Writes: no model-callable tool writes; the only write path is the orchestrator's explicit `--persist` using Phase 3 `persist_investigation`.
* Hardcoded model identifiers, keys or endpoints in `src/`: none (a test enforces it); the one model id is in the example config. Environment is read only in `config/settings.py`.
* Tools defined equal tools registered (7 = 7). Forbidden argument names (`cypher`, `path`, `file`, ...) appear in no tool schema (tested).
* Fixed numbers in agent code are output-size safeguards (at most 20 list items, 50 detections, 300 characters of trace text), not detection or investigation thresholds.

## 10. Known limitations

Synthetic data only; structural patterns are not proof of crime (false positives and negatives expected); the grounding filter cannot judge meaning (see the measured pass-through of semantic fabrications) and recognises bare identifiers only when they look like identifiers; model reasoning is probabilistic; only registered tools can touch the graph; a truncated activity list or trace marks a run `incomplete` by design; only the Anthropic provider exists; the dev database process does not survive between sandbox calls (environment only).

## 11. Still unverified

Live provider success path and the model identifier; real-model tool selection and quality metrics; Docker Compose; TLS and ACL connection modes; behaviour on very large or dense graphs (the account trace shares the variable-length-pattern cost profile of the Phase 2 trace, now bounded by a server timeout and a result-size guard).

## 12. Exit gate, criterion by criterion

| Criterion | Status |
|---|---|
| Phase 3 pre-flight completed; defects fixed first; detector/evaluation functionality kept; schema compatible; existing tests pass | PASS (no Phase 3 defects; 416 original tests pass) |
| Provider configuration centralised and external; secrets not hardcoded; provider implemented; failures handled; retries/timeouts bounded | PASS |
| At least one real provider request verified **or** integration explicitly marked unverified | MARKED UNVERIFIED (no credential) |
| Registry contains only implemented tools; arguments validated; tools run real queries/detectors; limits enforced; no arbitrary Cypher; unsupported calls rejected | PASS |
| Agent selects tools from the request and discovered evidence, and adapts | PASS for the loop (scripted); **NOT OBSERVED with a real model** |
| Facts backed by retrieved data; findings link to detector results; evidence resolves; observation/interpretation/hypothesis distinguished; unverified rejected or marked; incomplete searches and failures reported; ground truth isolated | PASS |
| Agent unit tests; real FalkorDB tool tests; end-to-end scenarios; existing suites; full suite | PASS |
| Provider tests accurately classified | PASS (1 passed on the failure path, 1 BLOCKED) |
| Quality metrics from actual executions; none fabricated | PASS for scripted runs; real-model metrics not produced |
| CLI compatible and documented; examples contain no credentials; documentation matches; limitations disclosed | PASS |
| Part H steps 9, 10, 22 and the real-model part of 23 (AI-assisted run with a real model, real tool selection, live connectivity, real quality evaluation) | **NOT EXECUTED** |

## PHASE 4 BLOCKED

**Blocker:** no LLM provider credential exists in the build environment, so Part H steps 9, 10 and 22 (an AI-assisted investigation with a real model that really selects tools) and the real-model portion of step 23 could not be executed. Every other gate passed, and nothing was simulated to cover the gap.

**To clear it** (about a minute): set `ANTHROPIC_API_KEY` (and confirm `provider.model` in `examples/agent.example.json` is valid for your account), then run `pytest -m provider -rs`, one `money-trail agent investigate ...` on a generated anchor, and `money-trail agent evaluate ...`. If those pass, this status becomes PHASE 4 PASSED; any defect they reveal in the Anthropic request or response handling would be a Phase 4 defect to fix before Phase 5.
