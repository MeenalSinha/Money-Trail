# Phase 2 verification report

Environment: Linux x86_64, Python 3.12.3, `falkordb` client 1.8.0, redis client 8.1.0. Database under test: a real
FalkorDB server (graph module build 41803, Redis 8.6.2), password-protected, loopback only, started with
`scripts/dev_server_no_docker.sh`. Docker was not available. Everything below was executed; nothing is mocked in the
database tests (the only test double is the deliberate failure injection in test J, which still hits the real database).

The final gate was run from a fresh extraction of the delivered package, installed into a clean virtualenv from
`requirements.lock`, against a new server with a new password.

## 1. Phase 1 pre-flight

Starting point: the Phase 1 package (`money-trail-phase1.zip`); the working tree was identical to it
(`diff -rq`). Phase 1 reported 78 passing tests and the Compose path unverified.

| Check | Result | Evidence |
|---|---|---|
| P1-01 inspected structure and docs | PASS | tree identical to the delivered ZIP; test inventory, file sizes and documented counts compared with code |
| P1-02 install and imports | PASS | clean venv from `requirements.lock`, `pip install -e .`, package and CLI import |
| P1-03 centralized configuration | PASS | settings unit tests; commands load only through `load_settings` |
| P1-04 no embedded credentials or deployment settings | PASS | scans of `src`: no hosts, URLs or credential literals; environment read only in `settings.py` (the dev shell script reads its own two variables) |
| P1-05 real FalkorDB connection | PASS | connection integration tests and CLI against the live server |
| P1-06 `money-trail-check` | PASS | `graph_not_found` exit 1 before init, `ok` exit 0 after |
| P1-07 `money-trail-init` | PASS | graph created, 9 indexes and 25 constraints |
| P1-08 repeated init | PASS | second run created 0 elements, 34 already present |
| P1-09 schema matches documentation | PASS | documented counts (9 indexes, 7 unique, 18 mandatory) equal the code and the init output |
| P1-10 indexes, constraints, validations | PASS | Phase 1 unit and integration tests, including database-level constraint enforcement |
| P1-11 unit tests | PASS | 60 Phase 1 unit tests (part of 201) |
| P1-12 real-database integration tests | PASS | 18 passed |
| P1-13 no damage to unrelated data | PASS | a node written to the application graph before the integration run was present afterwards; no leftover test graphs |
| P1-14 Docker Compose startup | UNVERIFIED | no Docker in the build environment; `compose.yaml` parses as YAML only |
| P1-15 documented non-Docker startup | PASS | script started the server in every run |
| P1-16 documentation accurate | PASS after repair | see below |

Phase 1 repairs: no defect in Phase 1 logic was found. Two documentation inaccuracies were fixed: the README test
count (78, now stale) and the undocumented fact that FalkorDB keeps the bootstrap label `__schema_bootstrap__` in
its label list after initialization (now in the schema document). Phase 1 modules were extended, not replaced:
`Settings` gained optional query limits and `GraphStore` gained batch helpers. The Phase 1 report is kept as
`PHASE1_VERIFICATION_REPORT.md`. Phase 1 exit gate: met (with P1-14 unverified, an environment limit).

## 2. Phase 2 files

New: `src/money_trail/ingestion/{contract,config,reader,validation,pipeline,report}.py`,
`src/money_trail/generation/{config,generator}.py`, `src/money_trail/investigation/queries.py`,
`examples/generator.example.json`, `examples/ingest.example.json`, `docs/data-ingestion.md`,
`docs/evaluation-data.md`, `tests/support.py`, and tests `tests/unit/{test_contract,test_dataset_config,
test_validation,test_generator,test_report_queries_cli}.py`, `tests/integration/{test_ingestion,
test_investigation_queries,test_cli}.py`.
Changed: `config/settings.py`, `database/graph_store.py`, `exceptions.py`, `cli.py`, `.env.example`, `.gitignore`,
`pyproject.toml` (new entry point, version 0.2.0), `Makefile`, `README.md`, `docs/architecture/graph-schema.md`,
`tests/conftest.py`. No dependency was added; `requirements.lock` is unchanged. The Phase 1 schema is reused without change.

## 3. Configuration requirements
Environment: unchanged from Phase 1, plus optional `MONEY_TRAIL_QUERY_MAX_RESULTS`, `_MAX_HOPS`, `_MAX_PATHS`,
`_MAX_GAP_MS`. Dataset and generator behavior comes from JSON files (all operational values required, unknown keys
rejected); see `docs/data-ingestion.md` and `examples/`.

## 4. Exit-gate workflow, as executed

Generation: `money-trail generate --config examples/generator.example.json` (seed 20261015, namespace
`synthetic-demo`; 60 ordinary accounts, 300 ordinary transactions, 2 instances each of circular, pass-through, fan-in
and fan-out, 12 seed alerts). Output: 95 accounts, 339 transactions, 12 alerts, 8 labelled patterns.

| Step | Result |
|---|---|
| health before init | `graph_not_found`, exit 1 |
| init twice | created 9 + 25, then 0 created / 34 present |
| health after init | `ok`, exit 0 |
| validate | VALID, exit 0 |
| ground truth inspected separately | present only in `ground_truth.json`; ingest config does not mention it |
| ingest 1 | `succeeded`; new 95 accounts, 339 transactions, 12 alerts; SENT 339, RECEIVED_BY 339, FLAGS 12; 3 batches, 0 failed; verification passed against the database |
| ingest 2 | `succeeded`, `writes_occurred` false; 0 new, everything unchanged, 0 relationships created; totals unchanged |
| queries on known records | transaction context returned the expected amount, currency, timestamp and endpoints; fan-in collector showed 8 incoming transactions; a forward trace from the first transaction of a labelled circular pattern recovered every transaction of that pattern; unknown id exit 3; missing limit exit 1 with the setting named |
| invalid input (separate graph) | `validate` exit 1 (3 error kinds); strict ingest `failed_validation`, nothing written; quarantine ingest `succeeded_with_rejections`, 3 rejected, 339 transactions imported and verified |
| same seed regenerated elsewhere | CSVs identical; different seed gave different transactions |

Partial failure and recovery (test J): a real unique-constraint violation in the second transaction batch produced
`failed_write` with the batch number, `writes_occurred` true, accounts and the first batch committed, alerts not
started; rerunning completed the import with no duplicates.

## 5. Tests actually executed (final package)

| Command | Passed | Failed | Skipped |
|---|---|---|---|
| `pytest tests/unit` | 201 | 0 | 0 |
| Phase 1 integration files | 18 | 0 | 0 |
| Phase 2 integration files | 39 | 0 | 0 |
| `pytest` (everything) | 258 | 0 | 0 |

The spec's integration cases map to: A, B, C, D, E, F, G, J, K in `test_ingestion.py`; H and I in
`test_investigation_queries.py`; the documented commands in `test_cli.py`. A mutation check (changing the
relationship `MERGE` to `CREATE`) made 5 of those tests fail and was reverted.

## 6. Defects found and fixed during Phase 2

* Quarantine writer reopened a file in write mode and would have truncated earlier rows (found in review, fixed
  before the first test).
* Manifest copied `seed_alerts.on_pattern_transactions`, leaking a label-related count (caught by a unit test; the
  section is now redacted, test added).
* Trace results listed only interior accounts, omitting the first sender and last receivers (caught by an
  integration test; endpoints are now fetched and returned).
* Full-suite collection failed because two `conftest.py` files made a shared import ambiguous (fixed by moving the
  helper to `tests/support.py`).
* `ground_truth.json` and the manifest embedded machine-specific output paths, so the same seed gave different
  files on different paths; found by the exit-gate reproducibility check, fixed, regression test added.
* My first recovery test triggered its failure in the wrong stage (test defect, fixed).

## 7. Known limitations and unverified items

* Docker Compose path: not executed (P1-14). Image tag `v4.18.2` unverified.
* TLS and ACL connection modes: not exercised against a server.
* Tested on Python 3.12 only; `requires-python` allows 3.10 to 3.13 (ISO-8601 parsing is more limited on 3.10).
* Scale: one measurement of about 60,000 transactions (ingest roughly 9 s, idempotent rerun roughly 17 s). Larger
  datasets, concurrent writers and dense-graph trace cost were not tested.
* Ingestion is not atomic across batches; recovery is by rerun.
* CSV only; no AMLSim adapter; only the generator's output is covered end to end.
* Ground-truth separation is enforced in code and tests, not by file permissions.
* Constraints cover uniqueness and presence only; types and ranges are validated in Python.
* No fraud rules, scoring, pattern detection, agents, LLM or dashboard exist (by design).

## 8. Exit gate

| Criterion | Status |
|---|---|
| Phase 1 core checks passed (P1-14 environment-only) | PASS |
| Existing Phase 1 tests still pass; init and health commands work | PASS |
| Canonical formats documented; generator configurable and reproducible | PASS |
| Generated data consistent; ground truth matches generated patterns | PASS |
| Ground truth isolated from graph and queries | PASS |
| Mappings configurable; invalid amounts, timestamps, identifiers, references rejected or quarantined | PASS |
| Existing schema and validated access layer reused; contract followed | PASS |
| Namespaces prevent collisions; relationship directions correct | PASS |
| Repeated import creates no duplicates; conflict policies behave as configured | PASS |
| Reports reflect actual database results; partial failures visible and recoverable | PASS |
| Four investigation queries work on ingested data, parameterized, bounded, graph-executed | PASS |
| Unit, existing and new integration suites and the full suite executed and passing | PASS (258/258) |
| Documentation matches the code; examples hold no real credentials | PASS |
| Docker Compose startup | UNVERIFIED (environment) |

## 9. Conclusion

**PHASE 2 PASSED.** Every criterion that could be executed here was executed against a real FalkorDB with 258 of 258
tests passing and no skips. The Docker Compose startup path remains unverified, as in Phase 1; run
`docker compose up -d`, `money-trail-check` and `pytest` once on a machine with Docker to close that item.
