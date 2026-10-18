# Data contract, ingestion and investigation queries (Phase 2)

Phase 2 reuses the Phase 1 graph schema unchanged (see `architecture/graph-schema.md`). No node label, relationship,
property, index or constraint was added.

## 1. Canonical records

Source files are normalized into these records before any graph write. Identifiers, amounts and timestamps follow
the Phase 1 schema contract exactly.

| Entity | Graph node | Required | Optional | Edges created |
|---|---|---|---|---|
| Account | `Account` | `account_id` | `created_at`, `dataset` (set from the namespace) | none |
| Transaction | `Transaction` | `transaction_id`, `amount`, `currency`, `timestamp`; plus source and destination account (become edges) | `channel`, `reference`, `dataset` (set from the namespace) | `(Account)-[:SENT]->(Transaction)-[:RECEIVED_BY]->(Account)` |
| Alert | `Alert` | `alert_id`, `created_at`; plus the flagged transaction (becomes an edge) | `alert_type`, `severity`, `source`, `description` | `(Alert)-[:FLAGS]->(Transaction)` |

Account and Transaction nodes carry `dataset = <namespace>`. The Alert schema has no `dataset` property, so an alert's
namespace is visible only through its identifier prefix. Source columns that are not mapped are ignored.
Investigation, Finding, Evidence and Agent nodes are never created by ingestion.

### Identifier namespacing
Every identifier is stored as `<namespace>:<source id>`. The namespace comes from `dataset.namespace` in the dataset
config (letters, digits, `_ . -`, up to 64 characters, no `:`). A value that already starts with `<namespace>:` is
not prefixed again, so re-importing namespaced data is stable. A value starting with a different prefix is treated as
data (`other:x` becomes `ns:other:x`). Identifiers are trimmed, must be non-empty, free of control characters and at
most 200 characters in total.

### Amounts
Stored as non-negative integer minor units. Conversion uses exact decimal arithmetic, never binary floats:

* `unit: "minor"`: the source value must be an integer string.
* `unit: "major"`: the source value must match `digits[.digits]`; it is scaled by the currency's configured number of
  decimal places (`normalization.amount.currencies`, for example `{"USD": 2, "JPY": 0, "KWD": 3}`). More fractional
  digits than the currency allows is an error; nothing is ever rounded.
* Negative values, exponent notation, thousands separators, NaN/Infinity, empty values and values above the 64-bit
  integer range are rejected.
* Currency is upper-cased and must be a key of the configured table. Either map a currency column or set
  `fixed_currency`; setting both is an error.

### Timestamps
Stored as integer epoch milliseconds, UTC. Formats: `iso8601`, `epoch_ms`, `epoch_s`, `strptime` (needs `pattern`).
Values without a UTC offset are rejected (`AMBIGUOUS_TIMESTAMP`) unless `normalization.timestamp.timezone` is set
(`UTC`, a fixed offset such as `+05:30`, or an IANA name). With an IANA zone, wall-clock times that are repeated or
skipped by a daylight-saving change are rejected. Sub-millisecond precision is rejected (never truncated), as are
values before 1970. The machine's local timezone is never consulted. `iso8601` relies on Python's
`datetime.fromisoformat` (with a trailing `Z` accepted).

## 2. Dataset configuration

One JSON file per source. Relative paths resolve against the config file. Everything below is required unless marked
optional; unknown keys are errors, so typos fail loudly. See `examples/ingest.example.json`.

```text
dataset.namespace, dataset.source.name   (source.version / description optional)
format: "csv"
csv.delimiter, csv.encoding
files.accounts      optional, with accounts_policy
files.transactions  required
files.alerts        optional
    each: path, columns { canonical field -> source column header }
normalization.timestamp { format, pattern?, timezone? }
normalization.amount    { unit, currencies, fixed_currency? }
ingestion { batch_size (1-10000), invalid_policy, source_conflict_policy, graph_conflict_policy,
            accounts_policy, max_reported_errors, max_records_per_file (null = none), max_file_bytes (null = none) }
output { quarantine_dir, report_dir }
```

Required canonical columns: accounts `account_id`; transactions `transaction_id`, `source_account`,
`destination_account`, `amount`, `timestamp` (and `currency` unless `fixed_currency`); alerts `alert_id`,
`transaction_id`, `created_at`. Optional: accounts `created_at`; transactions `channel`, `reference`; alerts
`alert_type`, `severity`, `source`, `description`. A mapped source column missing from the file stops the run with a
message naming it. Blank lines are skipped; a UTF-8 BOM on the header is tolerated.

## 3. Policies

| Setting | Values | Meaning |
|---|---|---|
| `invalid_policy` | `strict` | any invalid record fails the run before the graph is touched |
| | `quarantine` | invalid records go to `<quarantine_dir>/<run_id>/<entity>.jsonl`; valid ones are imported; the run status is `succeeded_with_rejections` |
| `source_conflict_policy` | `reject` | the same id appearing again with different content is an error (`DUPLICATE_CONFLICT`); the first occurrence is kept |
| | `keep_first` | the later conflicting row is skipped and counted |
| `graph_conflict_policy` | `reject` | a record that differs from what the graph already holds is a conflict (strict: nothing is written; quarantine: the row is quarantined as `GRAPH_CONFLICT`) |
| | `skip` | the graph value is kept, the row counted as `conflicting_skipped` |
| | `update` | supplied properties overwrite the graph's; properties absent from the source are neither compared nor removed |
| `accounts_policy` | `require_file` | transactions must reference accounts in the accounts file |
| | `derive_from_transactions` | accounts are created from transaction endpoints; counted in `accounts_derived` |

Other rules, not configurable:

* A repeated record with identical content is skipped silently and counted (`duplicates_identical`); it is not an error.
* Transaction endpoint disagreements (the graph already has the transaction between other accounts) are always
  conflicts, even under `update`. Edges are never deleted or rewritten.
* A transaction whose account was rejected is rejected (`UNKNOWN_ACCOUNT`); an alert whose transaction was rejected
  is rejected (`UNKNOWN_TRANSACTION`). Accounts are never invented unless `derive_from_transactions` is set.
* Transfers from an account to itself are not rejected; the schema allows them and no rule was configured.
* In quarantine mode, alerts for a transaction that conflicts with the graph (and was skipped) still link to the
  existing graph transaction, because that node exists.

Error codes: `MALFORMED_ROW`, `MISSING_VALUE`, `INVALID_VALUE`, `INVALID_IDENTIFIER`, `INVALID_AMOUNT`,
`UNSUPPORTED_CURRENCY`, `INVALID_TIMESTAMP`, `AMBIGUOUS_TIMESTAMP`, `UNKNOWN_ACCOUNT`, `UNKNOWN_TRANSACTION`,
`DUPLICATE_CONFLICT`, `GRAPH_CONFLICT`. Each error records the entity, record number, line, code, field and a generic
message. Messages and reports never contain source values. Quarantine files do contain the raw rejected row (that is
their purpose), so treat them as data.

## 4. Pipeline stages

1. **Preflight**: `check_health` must report ok. Ingestion never initializes or repairs the schema (`failed_preflight`).
2. **Input and fingerprints**: size limits, SHA-256 of every file.
3. **Validation** (files only): one streaming pass per file, holding identifiers and content digests, not records.
   Strict failures stop here with nothing written.
4. **Plan** (strict + `graph_conflict_policy=reject` only): a read-only pass classifying every batch against the
   graph; any conflict stops the run with nothing written.
5. **Write**: accounts, then transactions (nodes, `SENT`, `RECEIVED_BY` per batch), then alerts (nodes, `FLAGS`).
   Each batch classifies against the live graph into new / unchanged / changed / conflicting, creates new nodes with
   all properties in one `CREATE` (FalkorDB checks mandatory constraints at creation), and `MERGE`s relationships for
   new and unchanged records alike, so a rerun completes relationships left over from an interrupted run.
6. **Verify**: re-reads the graph and checks that accounts exist, that each transaction exists between the expected
   accounts in the schema direction, and that each alert flags the expected transaction.

All values travel as query parameters; labels, relationship types and property names come from the schema registry.
The source files are re-hashed before writing; a file that changed during the run aborts it.

### Atomicity and recovery
Each Cypher statement is atomic (a constraint violation inside a batch rolls the whole statement back, verified on the
real server). A run is **not** atomic across statements or batches. If a batch fails, the report has status
`failed_write`, the failed entity and batch number, the statements of that batch that completed, whether any write
happened, and the counts so far. Rerunning the same dataset is safe: committed records classify as unchanged and the
rest is written.

### Batching and memory
`ingestion.batch_size` controls rows per statement. Files are streamed three times (validate, write, verify). Memory
holds identifier sets and content digests for the dataset, not the records. Measured once during development: about
60,000 transactions ingested in roughly 9 seconds, and about 17 seconds for the idempotent re-run on a local server.

## 5. Reports

Every ingestion writes `<report_dir>/<run_id>.json` and prints a summary. Fields: run id, namespace, source,
config fingerprint, policies, file names/sizes/SHA-256, start and finish times, status, failed stage and error,
`writes_occurred`, per-entity record counts (examined, accepted, rejected, identical repeats, conflicting repeats
skipped), per-entity graph counts (new, unchanged, updated, conflicting rejected/skipped), relationships created
versus already present, batches succeeded/failed, validation errors by code with capped samples, quarantine location,
verification results, and namespace totals before and after (Account and Transaction counted by `dataset`, Alert by
identifier prefix; other namespaces are not included). Statuses: `succeeded`, `succeeded_with_rejections`,
`failed_preflight`, `failed_validation`, `failed_conflict_check`, `failed_write`, `failed_verification`,
`failed_database`. Created and updated counts come from database results, not from the number of rows read.

## 6. Investigation queries

Read-only (`ro_query`), parameterized, bounded. Implemented in `money_trail/investigation/queries.py`.
Limits come from `MONEY_TRAIL_QUERY_MAX_RESULTS`, `_MAX_HOPS`, `_MAX_PATHS`, `_MAX_GAP_MS` or explicit arguments; a
query that needs a limit that is not supplied fails with a message naming the setting. Safety ceilings (results
10000, hops 10, paths 10000) bound worst-case work. The only value placed in query text is the validated integer hop
bound, because Cypher cannot parameterize path lengths.

Results carry `status`: `ok` (anchor found; results may be empty) or `not_found` (anchor does not exist), plus
`truncated` and `warnings`.

| Query | CLI | Returns |
|---|---|---|
| Transaction context | `query transaction ID` | the transaction, its source and destination accounts, directly flagging alerts; warns if it does not have exactly one of each endpoint |
| Account activity | `query account ID [--direction in\|out\|both] [--start-ms] [--end-ms] --limit N` | incoming and outgoing transactions with counterparty, ordered by timestamp then id, optionally time-windowed, capped at the limit (`truncated` set when more exist) |
| Network trace | `query trace ID [--direction forward\|backward] --max-hops N --max-paths N [--max-gap-ms N]` | time-respecting paths of up to N further transactions, the transactions, their source and destination accounts |
| Evidence path | `query evidence INVESTIGATION_ID` | investigation to finding to evidence to transaction paths (works with records created by later phases or tests) |

Trace semantics: one hop is one further transaction. Forward follows funds downstream (the destination account's later
outgoing transactions, timestamps non-decreasing); backward follows funds upstream (the source account's earlier
incoming transactions). The traversal and the time filter run inside FalkorDB as one bounded path query; paths do not
reuse relationships. When `truncated` is true the returned paths are a subset, and which subset FalkorDB produced is
not guaranteed; the returned set is sorted for stable output. Dense graphs can make a high hop bound expensive, which
is why hops and paths are mandatory and capped. These queries describe connectivity only; none classifies activity as
suspicious.

## 7. Ground-truth separation
See `evaluation-data.md`. The ingestion pipeline never reads the ground-truth file, the ingest config never names it,
the graph schema cannot hold extra properties, and tests verify that no label reaches the graph or any query result.

## 8. CLI exit codes
`0` success; `1` failure (validation failure, failed ingestion, configuration or query error); `3` query anchor not
found. `validate` exits 1 whenever any record would be rejected, including in quarantine mode. `ingest` exits 0 for
`succeeded` and `succeeded_with_rejections` (inspect the status), 1 for every failure status. Options common to a
subcommand (`--json`, `--env-file`) go before nested actions: `money-trail report --json list`.
