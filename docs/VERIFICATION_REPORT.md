# Phase 3 verification report

Environment: Linux x86_64, Python 3.12.3, `falkordb` client 1.8.0, redis client 8.1.0. Database under test: a real FalkorDB server (graph module
build 41803, Redis 8.6.2), password-protected, loopback only, started with `scripts/dev_server_no_docker.sh`. Docker was not available. Database
tests use the real server; the only test doubles are (a) a fake graph object that raises the server's timeout error, to test the translation of that
single error path, and (b) deliberate failure injection in the Phase 2 recovery test. The final gate ran from a fresh extraction of the package, in a
clean virtualenv built from `requirements.lock` (unchanged since Phase 2; no new dependency), against a new server with a new password.

## 1. Phase 2 pre-flight

Starting point: `money-trail-phase2.zip`; the working tree was identical to it (`diff -rq`). The Phase 2 report claimed 258 passing tests.

| ID | Check | Result | Evidence |
|---|---|---|---|
| P2-01 | inspect project and Phase 2 report | PASS | tree identical to delivered ZIP; report read; claims re-run below |
| P2-02 | install by documented procedure | PASS | clean venv from `requirements.lock`, `pip install -e .` |
| P2-03 | imports and CLI | PASS | package, subcommands and `--help` load |
| P2-04 | centralized configuration | PASS | 34 settings unit tests |
| P2-05 | real FalkorDB connection | PASS | health and init against a fresh server |
| P2-06 | `money-trail-check` | PASS | `graph_not_found` exit 1 before init, `ok` exit 0 after |
| P2-07 | `money-trail-init` non-destructive | PASS | created graph, 9 indexes, 25 constraints |
| P2-08 | idempotent init | PASS | second run created 0, 34 already present |
| P2-09 | schema matches documentation | PASS | 9 indexes / 7 unique / 18 mandatory in code and document |
| P2-10 | indexes and constraints operational | PASS | `inspect_schema`: ready, 0 pending |
| P2-11 | unit tests | PASS | 201 passed |
| P2-12 | Phase 1 integration | PASS | 18 passed |
| P2-13 | Phase 2 integration | PASS | 39 passed |
| P2-14 | generator reproducible for a fixed seed | PASS | same seed written to two different output locations gave identical files |
| P2-15 | validated external configuration | PASS | config unit tests; generator refused to overwrite an existing dataset |
| P2-16 | strict and quarantine modes | PASS | strict: `failed_validation`, nothing written; quarantine: `succeeded_with_rejections`, 339 imported |
| P2-17 | namespaces, duplicate/conflict handling | PASS | covered by the Phase 2 integration tests (E, F) |
| P2-18 | repeat ingestion creates no duplicates | PASS | second ingest: 0 new, 0 relationships, `writes_occurred` false |
| P2-19 | correct account relationships | PASS | verification step and Test B |
| P2-20 | four read-only queries | PASS | transaction, account, trace and evidence (absent investigation exit 3) |
| P2-21 | ground truth separate from graph and queries | PASS | Test I; re-verified by new Phase 3 tests |
| P2-22 | reports reflect writes and partial failures | PASS | ingest reports and Test J |
| P2-23 | full existing suite | PASS | 258 passed, 0 failed, 0 skipped |
| P2-24 | limitations and documentation accurate | PASS | README, schema document and limitation list compared with behavior |

Phase 2 defects found at pre-flight: none. Phase 2 exit gate: met (Docker Compose remains unverified, an environment limit).
One Phase 2 observation was recorded rather than changed: `money-trail query trace` uses a variable-length pattern and has no server-side timeout.
It is anchored and hop-capped; on a dense 1,844-transaction dataset it returned instantly for 4 and 6 hops.

## 2. Phase 3 modules

Created: `investigation/{config,engine,evidence,findings}.py`, `investigation/detectors/{base,models,settings_util,circular,pass_through,windows,fan_common,fan_in,fan_out,registry}.py`,
`evaluation/{ground_truth,matching,metrics,report}.py`, `examples/detection.example.json`, `docs/detection.md`, `docs/evaluation.md`,
and tests `tests/unit/{test_detection_config,test_detector_logic,test_findings_evaluation}.py`,
`tests/integration/{test_detection,test_evaluation,test_cli_detection}.py`.
Changed: `cli.py` (new subcommands; existing commands and exit codes unchanged), `tests/support.py`, `README.md`,
`docs/architecture/graph-schema.md` (Phase 3 note only; no schema change), `pyproject.toml` (0.3.0), docs layout (Phase 2 report moved to
`PHASE2_VERIFICATION_REPORT.md`). `database/` and `ingestion/` were not modified in Phase 3.

## 3. Detection configuration

`examples/detection.example.json`: rule set `demo-rules@1`; limits (20,000 candidate paths, 500 detections per detector, 200 anchor transactions, 100 evidence
transactions, 20 s query timeout); circular 3..5 hops, 6 h gap, 24 h window, retention 9000..10000 bps; pass-through 2..4 hops, 2 h gap, 8 h window,
retention 9000..10000 bps; fan-in and fan-out at least 5 distinct counterparties and 5 transactions within 24 h; hypothesis findings on; matching 100 %/100 %.
Every key is required, unknown keys are rejected, ranges and ceilings are validated (107 new unit tests, many parametrized). The example values were
chosen from the documented generator parameters before any evaluation. Rule version example: `circular:demo-rules@1+089124bfebeb`.

## 4. Graph-native strategy per detector

* **circular**: one staged Cypher query per path length (a `MATCH ... WHERE` per hop with chronology, gap, window, distinct-account and no-revisit
  conditions), closing on the first sender; Python applies exact retention and currency checks, canonical rotation and deduplication.
* **pass-through**: the same staged queries without closing, `OPTIONAL MATCH` for the end accounts; Python applies exact retention, drops chains contained in longer
  qualifying chains.
* **fan-in / fan-out**: a graph aggregation (`count(DISTINCT counterparty)`, `count`) selects candidate accounts, then each candidate's transfers are read in time
  order and the exact inclusive sliding window is applied.
* Anchored runs find predecessor transactions with a parameter-bound, index-started `UNION` query.
* All queries are read-only parameterized Cypher; the only values in query text are the validated hop count and schema names.

## 5. Actual sample run

Dataset `synthetic-demo` (seed 20261015, 339 transactions, 12 seed alerts). Investigating each alert-flagged transaction, without consulting ground truth:
6 of the 12 flagged transactions gave `patterns_detected` (3 fan-in, 2 fan-out, 1 pass-through) and 6 gave `no_pattern_found`; all runs were complete and took 10 to 22 ms.
(Alert `ALT0000001`: pass_through; `ALT0000002` and `ALT0000011`: fan_out; `ALT0000003`, `ALT0000006` and `ALT0000012`: fan_in.) Seed alerts are noisy by design,
so a flagged transaction that matches no configured pattern is an expected outcome, not a failure.

Investigating a transaction inside a constructed cycle with `--persist`:

```text
investigation status: patterns_detected (complete: True, 18 ms)
  anchor: transaction synthetic-demo:TXN000000018 (namespace synthetic-demo)
  circular      1 detection(s), truncated=False, 4 queries, 1 rows, 0.0061s
  pass_through  1 detection(s), truncated=False, 4 queries, 2 rows, 0.0055s
    warning: 1 candidate chain(s) skipped: repeated accounts
  fan_in        0 detection(s), truncated=False, 1 queries, 1 rows, 0.0003s
  fan_out       0 detection(s), truncated=False, 1 queries, 1 rows, 0.0004s
  - circular det:c907bc6daba0fd90d607201a82c1849e: 3 transactions, 3 accounts
      transactions: synthetic-demo:TXN000000018, synthetic-demo:TXN000000019, synthetic-demo:TXN000000020
  - pass_through det:0dc7326535ffe53e2a69a7291f1d1c39: 2 transactions, 3 accounts
      transactions: synthetic-demo:TXN000000018, synthetic-demo:TXN000000019
```

The cycle's evidence, re-read from the graph (amounts in minor units, timestamps in epoch ms): ACC00000010 to ACC00000057 (110982 USD, 1767398700675),
ACC00000057 to ACC00000083 (109872 USD, 1767399005299), ACC00000083 back to ACC00000010 (108773 USD, 1767400655836); observed: 3 transactions, 3
accounts, total window 1,955,161 ms, largest gap 1,650,537 ms. The 2-transaction pass-through detection is a chain inside the same cycle (see the
false-positive analysis).

`findings show` (first lines): `investigation inv:c23b... status open, started from synthetic-demo:TXN000000018`; an `observation` "Observed a
time-respecting directed cycle of 3 transactions through 3 accounts (rule circular:demo-rules@1+089124bfebeb). Structural observation from configured
rules only; it does not establish wrongdoing."; an `observation` for the chain; and two `hypothesis` findings ("Hypothesis (unconfirmed): this transfer
structure may warrant human review ..."), each linked to its evidence and transactions.

Persistence: first run created 1 investigation, 2 evidence items, 4 findings and 1 + 4 + 4 + 5 relationships (STARTED_FROM, HAS_FINDING, SUPPORTED_BY,
REFERENCES); the repeated run created 0 of everything. Node labels in the graph: Account, Alert, Evidence, Finding, Investigation, Transaction.

## 6. Evaluation results (actual)

Full tables, matching rules, metric definitions and caveats are in `docs/evaluation.md`. Summary:

| Dataset | Labelled | TP | FP | FN | Precision | Recall | F1 | Detection time |
|---|---|---|---|---|---|---|---|---|
| 4 standard fresh seeds, pooled | 32 | 32 | 17 | 0 | 0.653 | 1.000 | 0.790 | 68 to 86 ms each |
| noisy (1,800 ordinary tx), seed 737373 | 8 | 8 | 76 | 0 | 0.095 | 1.000 | 0.174 | 480 ms |
| large (61,013 tx), seed 909090 | 200 | 197 | 162 | 3 | 0.549 | 0.985 | 0.705 | 10.1 s |

Per type on the standard seeds: circular and fan-in 1.000/1.000; fan-out precision 0.889 (1 false positive); pass-through precision 0.333 (16 false positives).
False positives: all 16 standard-seed pass-through false positives were verified to be strict sub-chains of labelled circular instances; the fan-out false positive and
the large-dataset fan false positives are windows containing an extra ordinary transfer, or incidental structures with no overlap with any label. False negatives: only on the
large dataset (1 fan-in, 2 fan-out); each is a labelled instance contained in a larger detection with exactly one extra transfer, so exact-match purity counts it as missed.
No search was truncated in any evaluation. Evaluation caveats: synthetic data, in-distribution thresholds, no tuning on the labels; see the document.

## 7. Truncation and incomplete-search behavior (executed)

With `max_candidate_paths` = 1 on `synthetic-demo`: status `patterns_detected`, `complete: False`, warnings "candidate paths of N transactions exceeded
max_candidate_paths; search is incomplete", CLI exit 4. Tests also cover: nothing found plus truncation gives `incomplete` (never `no_pattern_found`); a complete empty
search gives `no_pattern_found`; detection cap, anchor cap and server timeout (a real 1 ms server timeout raises, and a timeout in any detector marks the run incomplete);
`anchor_not_found`; `database_error` (also with the database down: no traceback, exit 1); evidence tampering gives `evidence_validation_failed`.

## 8. Tests executed (final package)

| Command | Passed | Failed | Skipped |
|---|---|---|---|
| `pytest tests/unit` | 308 | 0 | 0 |
| Phase 1 integration files | 18 | 0 | 0 |
| Phase 2 integration files | 39 | 0 | 0 |
| Phase 3 integration files | 51 | 0 | 0 |
| `pytest` (everything) | 416 | 0 | 0 |

Phase 3 added 107 unit and 51 integration tests (416 = 258 + 158). Required test mapping: A to D (`test_a_to_d...`, anchor tests), E (near-miss negatives for every
detector), F (duplicates and rotations), G (evidence completeness and tampering), H and I (persistence, read-back, idempotency), J (configuration changes), K (bounds,
truncation, timeouts, anchors), L (separation, new seeds, renamed namespaces), M (hand-computed metrics, end-to-end controlled case), N (the whole earlier suite).
A mutation check in Phase 2 (relationship `MERGE` to `CREATE`) and the new planner-plan regression test show the tests can fail.

## 9. Performance measurements (executed)

| Measurement | Result |
|---|---|
| dataset-wide detection, 61,013 transactions, 4 detectors | 10.1 s, complete (about 2.2 to 2.7 s of query time per detector) |
| anchored investigation, 20 samples per type, 61,013 transactions | median 8.1 to 10.6 ms, max 23.4 ms; exact pattern found 20/20 circular, 20/20 pass-through, 19/20 fan-in, 19/20 fan-out |
| anchored + persist | 17 ms |
| dense dataset (1,844 transactions among about 100 accounts) | variable-length form: 8 s at 3 hops, over 20 s at 4 hops; staged form 0.06 s; full detection 0.48 s |
| anchored backward discovery, 60,000 transactions | about 750 ms with the anchor bound through UNWIND, about 1 ms bound by parameter |

## 10. Defects found and fixed during Phase 3

* Variable-length path discovery could not prune by time and ran past the client socket timeout on the dense dataset (reported honestly as `database_error`).
  Replaced by staged per-hop queries; added the rule that `query_timeout_ms` must be below the socket timeout so slow searches report as `incomplete`.
* Anchored backward discovery scanned all transactions when the anchor was bound through `UNWIND` (about 5.4 s per investigation); now parameter-bound (about 10 ms),
  with a regression test on the query plan.
* A fan detector referenced an unbound variable when its first query timed out (caught by the timeout test).
* Metric output reduced fractions (2/2 shown as 1/1), hiding counts; unreduced counts are now shown.
* Several of my own test expectations were wrong and were corrected without weakening the checks they exist for (for example: a 3-cycle also yields anchor-specific
  pass-through chains, unchanged detectors correctly keep identical finding ids across rule changes).

## 11. Known limitations and unverified items

* Docker Compose path: not executed (no Docker). Image tag `v4.18.2` unverified. TLS and ACL modes not exercised against a server. Python 3.12 and Linux only.
* Detections are structural; legitimate activity satisfies every rule, and a cycle also satisfies the pass-through rule for its sub-chains.
* The evaluation is synthetic and in-distribution; it does not estimate real-world accuracy. Precision depends strongly on the base rate of ordinary activity.
* The `max_candidate_paths` cap applies per path length and per query; hitting any cap makes the run incomplete and the returned subset is not guaranteed.
* Persistence is not atomic across statements (rerun repairs it); findings are never invalidated automatically when data changes; account and dataset anchors cannot be persisted.
* Account-anchored circular and pass-through runs cover patterns containing any of at most `max_anchor_transactions` incident transactions.
* No concurrent-writer testing; scale measured once at about 61,000 transactions.
* No LLM, agent, MCP server or dashboard exists (by design).

## 12. Exit gate

| Criterion | Status |
|---|---|
| Phase 2 pre-flight executed; no defects found; existing suites pass; ground-truth separation intact | PASS |
| Validated, externally configurable detection configuration; missing and invalid settings give actionable errors; bounds enforced | PASS |
| Circular, pass-through, fan-in, fan-out detection on real FalkorDB data, with configured criteria, deduplication, evidence and explicit incompleteness | PASS |
| Engine investigates transactions, accounts and datasets; distinct outcomes; invalid anchors and incomplete data handled | PASS |
| Findings use the existing schema; linked to investigation, evidence and transactions; persist across runs; reruns create no duplicates | PASS |
| No unsupported criminal conclusions; hypotheses are labelled unconfirmed; no confidence invented | PASS |
| Ground truth only through the isolated evaluation path; deterministic documented matching; correct metrics; FP/FN reported | PASS |
| Evaluation executed against generated datasets; metrics come from actual detector output; synthetic limits documented | PASS |
| New unit and real-database integration tests pass; earlier tests pass; full suite executed | PASS (416/416) |
| Existing CLI commands unchanged; new commands documented | PASS |
| No credentials, hardcoded transaction records or hidden thresholds introduced | PASS (scans below) |
| Docker Compose startup | UNVERIFIED (environment) |

## 13. Conclusion

**PHASE 3 PASSED.** Everything that could be executed here was executed against a real FalkorDB, with 416 of 416 tests passing and none skipped. The Docker
Compose startup path remains unverified, as in Phases 1 and 2; run `docker compose up -d`, `money-trail-check` and `pytest` once on a machine with Docker
to close that item. The detection results are measured on synthetic data and are not evidence of real-world detection accuracy.
