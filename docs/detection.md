# Detection engine (Phase 3)

A deterministic graph investigation engine: four configurable structural detectors, an engine that combines
them, evidence verification, and persistence of findings in the Phase 1 schema. There is no LLM, agent
framework, MCP server or dashboard. A detection states that a configured structure exists in the graph; it
is not a finding of wrongdoing, and legitimate activity can satisfy every rule below.

## Architecture

```text
InvestigationRequest (transaction | account | whole dataset)
  -> engine resolves the anchor from the graph (not_found otherwise) -> Scope
  -> enabled detectors (registry), each: staged Cypher in FalkorDB -> exact checks in Python -> Detection objects
  -> merge and dedupe by detection id -> evidence verification (re-read every transaction from the graph)
  -> optional persistence (transaction anchors) -> InvestigationResult
```

| Module | Role |
|---|---|
| `investigation/config.py` | validated detection configuration, rule versions |
| `investigation/detectors/{circular,pass_through,fan_in,fan_out}.py` | the four detectors (`fan_common.py` and `windows.py` are shared by the two fan detectors) |
| `investigation/detectors/base.py` | detector interface, bounded read-only graph access, Cypher builders, exact amount rule |
| `investigation/detectors/registry.py` | name -> detector class; the engine needs no change to add a detector |
| `investigation/detectors/models.py` | `Detection`, `TxRecord`, `Scope`, deterministic id policy, canonical rotation |
| `investigation/engine.py` | orchestration and outcome classification |
| `investigation/evidence.py` | verification of detections against the graph |
| `investigation/findings.py` | persistence and retrieval of investigations, findings, evidence |
| `evaluation/` | ground-truth loader, matching, metrics, report (never imported by anything above) |

Detectors are usable without the CLI (`InvestigationEngine(conn, config).investigate(request)`), so later
agents can call them as a tool.

## Configuration (`examples/detection.example.json`)

All keys are required; unknown keys are errors; nothing is defaulted. Units: time values are integer
milliseconds, amounts integer minor units, ratios integer basis points (10000 = 100 %), and a "hop" is one
transaction (two graph edges). Check a file with `money-trail detectors validate --detection-config FILE`.

```text
rule_set { name, version }
limits   { max_candidate_paths, max_detections_per_detector, max_anchor_transactions,
           max_evidence_transactions, query_timeout_ms (null = none) }
detectors.<name> { enabled, ...settings }     for circular, pass_through, fan_in, fan_out
persistence { hypothesis_findings }           also store an unconfirmed "hypothesis" finding per detection
evaluation.match { min_gt_coverage_percent, min_detection_purity_percent }    used only by `evaluate`
```

| Detector | Settings |
|---|---|
| circular | `min_hops` (>= 2), `max_hops`, `max_gap_ms`, `max_total_window_ms`, `allow_repeated_accounts`, `amount_retention` (`null` or `{min_bps, max_bps}`) |
| pass_through | `min_hops` (>= 2), `max_hops`, `max_gap_ms`, `max_total_window_ms`, `allow_repeated_accounts`, `retention` (`null` or `{min_bps, max_bps}`) |
| fan_in / fan_out | `min_distinct_counterparties` (>= 2), `min_transactions` (>= that), `window_ms`, `min_total_amount` (`null` or `{currency, minor}`) |

Safety ceilings are enforced centrally and are not business thresholds: hops <= 10; candidate paths and
detections <= 100,000; anchor transactions and evidence transactions <= 10,000; timeout <= 3,600,000 ms.
`query_timeout_ms` must be smaller than `FALKORDB_SOCKET_TIMEOUT` (the engine refuses to start otherwise),
so a slow search ends as an `incomplete` result instead of a client socket error.

**Rule version.** `<detector>:<rule set name>@<version>+<12 hex of a hash of that detector's settings and the
result caps>`. Changing a threshold or cap changes it; timeouts, evaluation settings and other detectors'
settings do not. It is part of every detection and finding identity.

## Graph-native discovery

* **circular / pass_through**: one staged Cypher query per path length. Each hop is its own `MATCH ... WHERE`
  carrying the chronology (non-decreasing timestamps, ties allowed), maximum gap, maximum total window,
  never-revisit-a-transaction and (unless allowed) distinct-account conditions, so candidates are pruned at every
  hop. Variable-length patterns were tried and abandoned: they cannot prune by time while expanding. On a dense
  test graph one such query took 8 s at 3 hops and timed out beyond 20 s at 4 hops, while the staged form took
  0.06 s. Circular closes the chain by requiring the last transaction to be received by the account that sent
  the first. The only values placed in query text are the validated integer hop count and schema names.
* **Anchored starts**: for a transaction anchor, starts are the anchor plus every transaction that could precede
  it within the gap/window limits (one parameter-bound query per anchor with one `UNION` branch per depth).
  The anchor is bound by parameter because the planner then starts from an index lookup; binding through
  `UNWIND` made it scan every transaction (about 750 ms versus about 1 ms at 60,000 transactions; a test guards the
  plan). Results must contain the anchor.
* **fan_in / fan_out**: a graph aggregation (`count(DISTINCT counterparty)`, `count(transactions)`) selects candidate
  accounts (necessary conditions only), then the transfers of each candidate are read in time order and the
  exact sliding window is applied in Python.
* Anchors: a transaction anchor means patterns containing that transaction (circular and chains: anywhere in the
  pattern; fan-in: windows at its destination account; fan-out: windows at its source account). An account anchor
  means patterns containing any transaction of that account (its incident transactions, capped by
  `max_anchor_transactions`), and for fans the windows at that account. A dataset anchor (`--namespace`) is an
  explicit bounded analysis of one namespace. All reads are `ro_query` calls with parameters.

## Detector rules

**Circular.** A directed cycle of `min_hops..max_hops` transactions, chronologically ordered, consecutive gaps
<= `max_gap_ms`, first-to-last <= `max_total_window_ms`, returning to the first sender; accounts are distinct unless
`allow_repeated_accounts`. If `amount_retention` is set, every consecutive pair must be in the same currency and
`next/prev` within `[min_bps, max_bps]`. A cycle is reported once whichever transaction it was reached from
(rotations are canonicalized to start at the earliest `(timestamp, id)` transaction; a cycle is never reversed).
*False positives:* ordinary money circulating (rent between flatmates, refunds, treasury sweeps). *False negatives:*
cycles longer than `max_hops` or slower than the limits, cycles whose amounts leave the configured band, and cycles
that need ties broken in an order the timestamps do not show.

**Pass-through.** A maximal chain of `min_hops..max_hops` transactions with the same chronology, gap, window and
distinctness rules; if `retention` is set each hop must keep an amount within `[min_bps, max_bps]` of the previous
one, in the same currency. Maximal means a chain is dropped when it is a contiguous part of a longer chain that also
qualifies. *False positives:* any payment chain that is quick (payroll to employee to landlord), and linear
sub-chains of a longer cycle: on generated data all 16 pass-through false positives on four standard seeds were
sub-chains of constructed cycles, which satisfy the rule though only the cycle is labelled. *False negatives:*
chains longer than `max_hops`, slower than the gap, or retaining outside the band.

**Fan-in / fan-out.** Window semantics are exact: for transfers sorted by `(timestamp, id)`, the window starting at
transfer *i* holds every transfer with `timestamp - timestamp_i <= window_ms` (end inclusive; one millisecond over
fails, tested). Only maximal windows are examined; each qualifying window is a separate detection if its transfer set
differs. A window qualifies with at least `min_transactions` transfers from at least `min_distinct_counterparties`
distinct accounts (repeated transfers from one account count once as a counterparty). Transfers between an account and
itself are excluded. If `min_total_amount` is set, only transfers in that currency take part (others are counted in a
warning, never converted) and their sum must reach `minor`. *False positives:* merchants, payroll, collection
accounts, exchanges, and a window that also sweeps in one unrelated transfer (impure). *False negatives:* slow
collection, fewer counterparties than configured, and instances split across windows.

**Currency and amounts.** Amounts are never compared across currencies and never converted. Retention uses exact
integer cross-multiplication (no rounding, no floats); a zero previous amount makes the ratio undefined, so the
criterion is not met. If no amount rule is configured, amounts and currencies are not examined.

## Detection result

`Detection`: `detection_id`, `pattern_type`, `anchor_id`, `dataset_namespace`, `account_ids`, `transaction_ids`,
`ordered_path` (each transaction with source, destination, amount, currency, timestamp), `criteria` (`configured`
settings and `observed` values), `evidence_refs`, `truncated`, `warnings`, `rule_version`, `created_at`, `roles`.
No confidence is computed or stored.

## Outcomes

| status | meaning | `investigate` exit |
|---|---|---|
| `patterns_detected` | at least one verified detection | 0 (4 when `complete` is false) |
| `no_pattern_found` | complete search, nothing in scope matches | 0 |
| `incomplete` | nothing found but a limit or timeout cut the search | 4 |
| `anchor_not_found` | transaction, account or namespace absent | 3 |
| `evidence_validation_failed` | a detection did not verify against the graph (verified ones are still returned) | 1 |
| `database_error` | database failure (message without credentials) | 1 |

`complete` is false whenever any detector hit `max_candidate_paths`, `max_detections_per_detector`,
`max_anchor_transactions`, `max_evidence_transactions` or the query timeout; the returned set is then not exhaustive
and which subset was returned is not guaranteed. Invalid configuration is raised before an engine exists (exit 1).

## Evidence verification

Before anything is reported as verified or persisted, every transaction of every detection is re-read from the
graph: it must exist with the detected amount, currency and timestamp, exactly the detected sender and receiver, and
the structure must hold (connected through shared accounts, chronological, closed for cycles, one common account for
fans). Tests tamper with an amount, delete an edge and delete a transaction, and verify each is caught.

## Persistence (existing schema only)

`(Investigation)-[:STARTED_FROM]->(Transaction)`, `(Investigation)-[:HAS_FINDING]->(Finding)`,
`(Finding)-[:SUPPORTED_BY]->(Evidence)`, `(Evidence)-[:REFERENCES]->(Transaction)`. No node or relationship type and no
property was added, and no label is written to accounts or transactions.

| Entity | Identity (deterministic) |
|---|---|
| Investigation | hash(namespace, anchor transaction, fingerprint of the enabled rule versions) |
| detection | hash(namespace, pattern, detector rule version, canonical evidence set) |
| Evidence | hash(detection); one item per detection, referencing every supporting transaction; `produced_by_tool` = `money_trail.detector.<pattern>`, `query_ref` = rule version |
| Finding | hash(detection, kind): an `observation` (what the graph structure shows) plus, if `hypothesis_findings` is true, an unconfirmed `hypothesis` that it may warrant review |

Rerunning with unchanged data and rules creates nothing: nodes are created only when absent (never overwritten,
`created_at` is stable) and relationships are merged. Two anchors in one pattern create two investigations that share
the same findings and evidence. Changing a rule changes the rule version, so new findings and a new investigation are
created and the earlier ones stay as the record of the earlier evaluation. **Stale findings:** if the graph changes, old
findings are not edited or deleted; each finding carries `created_at`, and a rerun adds findings for evidence sets that
now exist. Evidence verification at rerun time reports detections that no longer hold, but earlier stored findings are not
invalidated automatically. Persistence is not atomic across statements; a failed run is repaired by rerunning.
Account and dataset anchors cannot be persisted because the schema links an Investigation to a starting Transaction and none
is invented; they are read-only.

Finding text avoids amounts and account identifiers and never states wrongdoing ("Structural observation from
configured rules only; it does not establish wrongdoing").

## CLI

```bash
money-trail detectors list [--detection-config FILE]       # offline
money-trail detectors validate --detection-config FILE     # offline; prints rule versions
money-trail investigate --detection-config FILE (--transaction-id ID | --account-id ID | --namespace NS) [--persist] [--json]
money-trail findings list [--investigation-id ID] [--transaction-id ID] --limit N [--json]
money-trail findings show INVESTIGATION_ID [--limit N] [--json]
money-trail evaluate --detection-config FILE --namespace NS --ground-truth FILE [--json]
```
Exit codes: 0 success, 1 failure, 3 anchor/investigation not found, 4 incomplete or truncated. Options may appear before
or after the nested action. `--limit` falls back to `MONEY_TRAIL_QUERY_MAX_RESULTS`.
