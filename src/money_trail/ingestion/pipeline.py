"""Ingestion orchestration: validate -> plan -> write -> verify -> report.

Writes go through GraphStore only. Not atomic across batches: each batch is a
short sequence of statements, and a failure leaves earlier batches committed.
The report says exactly what completed, and a rerun is safe because every step
classifies against the live graph and relationships are MERGEd.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, Iterator, List, Optional, Sequence

from redis import exceptions as redis_exc

from money_trail.database import FalkorDBConnection, GraphStore, check_health
from money_trail.exceptions import MoneyTrailError, SourceFormatError
from money_trail.ingestion import report as rpt
from money_trail.ingestion.config import DatasetConfig
from money_trail.ingestion.contract import RecordError
from money_trail.ingestion.reader import ParsedRow, check_file, file_sha256
from money_trail.ingestion.validation import (
    ENTITIES, QuarantineWriter, ValidationOutcome, iter_accepted, validate_dataset,
)

LABEL = {"accounts": "Account", "transactions": "Transaction", "alerts": "Alert"}
ID_FIELD = {"accounts": "account_id", "transactions": "transaction_id", "alerts": "alert_id"}
DB_ERRORS = (MoneyTrailError, redis_exc.RedisError)


class _Stop(Exception):
    """Internal: abort the run after the report has been filled in."""


def _batches(rows: Iterator[ParsedRow], size: int) -> Iterator[List[ParsedRow]]:
    batch: List[ParsedRow] = []
    for r in rows:
        batch.append(r)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _derived_rows(cfg: DatasetConfig, outcome: ValidationOutcome) -> Iterator[ParsedRow]:
    for i, aid in enumerate(sorted(outcome.derived_accounts), 1):
        yield ParsedRow("accounts", i, 0, record={"account_id": aid, "dataset": cfg.namespace})


def _source_rows(cfg, entity: str, outcome: ValidationOutcome) -> Iterator[ParsedRow]:
    if entity == "accounts" and cfg.accounts_policy == "derive_from_transactions":
        return _derived_rows(cfg, outcome)
    spec = getattr(cfg, entity)
    return iter_accepted(cfg, spec, outcome) if spec else iter(())


def classify(store: GraphStore, cfg: DatasetConfig, entity: str, rows: Sequence[ParsedRow]) -> Dict[str, list]:
    """Compare a batch to the live graph. Read-only.

    Returns buckets: new, unchanged, changed, conflict (endpoint disagreement; never auto-resolved).
    Properties absent from the source are neither compared nor removed.
    """
    label, idf = LABEL[entity], ID_FIELD[entity]
    existing = store.fetch_nodes(label, [r.record[idf] for r in rows])
    endpoints = store.fetch_transaction_endpoints([r.record[idf] for r in rows]) if entity == "transactions" else {}
    out = {"new": [], "unchanged": [], "changed": [], "conflict": []}
    for r in rows:
        cur = existing.get(r.record[idf])
        if cur is None:
            out["new"].append(r)
            continue
        if entity == "transactions":
            senders, receivers = endpoints.get(r.record[idf], (set(), set()))
            bad_src = senders and senders != {r.extra["source_account"]}
            bad_dst = receivers and receivers != {r.extra["destination_account"]}
            if bad_src or bad_dst:
                out["conflict"].append(r)
                continue
        differs = any(cur.get(k) != v for k, v in r.record.items())
        out["changed" if differs else "unchanged"].append(r)
    return out


def _fail(report, cfg, status, stage, message, **kw):
    rpt.finish(report, status, failed_stage=stage, error=message, **kw)
    return report


def ingest_dataset(conn: FalkorDBConnection, cfg: DatasetConfig, *, run_id: Optional[str] = None) -> Dict[str, Any]:
    run_id = run_id or uuid.uuid4().hex[:16]
    report = rpt.new_report(cfg, run_id, conn.settings.graph_name)
    store = GraphStore(conn)
    quarantine: Optional[QuarantineWriter] = None
    graph_skipped: Dict[str, set] = {e: set() for e in ENTITIES}

    try:
        # --- preflight: never initializes or repairs anything
        health = check_health(conn)
        if not health.healthy:
            return _fail(report, cfg, "failed_preflight", "preflight", f"{health.status}: {health.message}")

        # --- input discovery and fingerprints
        try:
            for spec in cfg.files():
                check_file(cfg, spec)
                report["files"][spec.entity] = {"name": spec.path.name, "bytes": spec.path.stat().st_size,
                                                "sha256": file_sha256(spec.path)}
        except SourceFormatError as exc:
            return _fail(report, cfg, "failed_validation", "input", str(exc))

        # --- stage 1: validation (file only)
        if cfg.invalid_policy == "quarantine":
            quarantine = QuarantineWriter(cfg.quarantine_dir / run_id)
        outcome = validate_dataset(cfg, quarantine)
        report["records"] = {e: outcome.stats[e].as_dict() for e in ENTITIES}
        report["accounts_derived"] = len(outcome.derived_accounts)
        report["validation_errors"] = {"total": outcome.error_total, "by_code": outcome.summary_errors(),
                                       "samples": outcome.error_samples}
        if quarantine:
            quarantine.close()
            report["quarantine"] = {"directory": str(cfg.quarantine_dir / run_id),
                                    "files": {e: p.name for e, p in quarantine.paths.items()}}
        if outcome.fatal:
            return _fail(report, cfg, "failed_validation", "validation", outcome.fatal)
        if outcome.error_total and cfg.invalid_policy == "strict":
            return _fail(report, cfg, "failed_validation", "validation",
                         f"{outcome.error_total} invalid record(s); strict mode writes nothing.")
        for spec in cfg.files():  # sources must not change between validation and writing
            if file_sha256(spec.path) != report["files"][spec.entity]["sha256"]:
                return _fail(report, cfg, "failed_validation", "validation",
                             f"{spec.entity} file changed during validation.")

        report["graph_totals"]["before"] = _totals(store, cfg)

        # --- stage 2: plan (read-only) gates strict runs on graph conflicts
        if cfg.invalid_policy == "strict" and cfg.graph_conflict_policy == "reject":
            conflicts, changed = _plan(store, cfg, outcome)
            if conflicts or changed:
                report["graph_conflicts"] = conflicts + changed
                return _fail(report, cfg, "failed_conflict_check", "plan",
                             f"{conflicts + changed} record(s) conflict with existing graph data; "
                             "strict mode with graph_conflict_policy=reject writes nothing.")

        # --- stage 3: write
        quarantine = QuarantineWriter(cfg.quarantine_dir / run_id) if quarantine is None else quarantine
        try:
            _write_all(store, cfg, outcome, report, quarantine, graph_skipped)
        except _Stop:
            return report
        finally:
            quarantine.close()
        if quarantine.paths:
            report["quarantine"] = {"directory": str(cfg.quarantine_dir / run_id),
                                    "files": {e: p.name for e, p in quarantine.paths.items()}}

        # --- stage 4: verify against the database itself
        report["verification"] = _verify(store, cfg, outcome, graph_skipped)
        report["graph_totals"]["after"] = _totals(store, cfg)
        if not report["verification"]["passed"]:
            return _fail(report, cfg, "failed_verification", "verify",
                         "Database contents do not match what was written.")

        g = report["graph"]
        rejected = sum(s["rejected"] for s in report["records"].values())
        conflicted = sum(x["conflicting_rejected"] + x["conflicting_skipped"] for x in g.values())
        rpt.finish(report, "succeeded" if not (rejected or conflicted) else "succeeded_with_rejections")
        return report
    except DB_ERRORS as exc:  # unexpected database error outside a tracked batch
        return _fail(report, cfg, "failed_database", "database", f"{type(exc).__name__}: {str(exc)[:200]}",
                     writes_occurred=report["writes_occurred"])
    finally:
        if quarantine:
            quarantine.close()
        rpt.write_report(cfg, report)


def _plan(store, cfg, outcome):
    """Read-only pass over every batch: (endpoint conflicts, content conflicts) against the live graph."""
    conflicts = changed = 0
    for entity in ENTITIES:
        for batch in _batches(_source_rows(cfg, entity, outcome), cfg.batch_size):
            cls = classify(store, cfg, entity, batch)
            conflicts += len(cls["conflict"])
            changed += len(cls["changed"])
    return conflicts, changed


def _totals(store: GraphStore, cfg: DatasetConfig) -> Dict[str, int]:
    return {
        "Account": store.count_matching("Account", "dataset", cfg.namespace),
        "Transaction": store.count_matching("Transaction", "dataset", cfg.namespace),
        "Alert": store.count_id_prefix("Alert", cfg.namespace + ":"),
    }


def _write_all(store, cfg, outcome, report, quarantine, graph_skipped) -> None:
    batch_no = 0
    for entity in ENTITIES:
        if entity == "alerts" and not cfg.alerts:
            continue
        for batch in _batches(_source_rows(cfg, entity, outcome), cfg.batch_size):
            batch_no += 1
            done: List[str] = []
            try:
                _write_batch(store, cfg, entity, batch, report, quarantine, graph_skipped, done)
                report["batches"]["succeeded"] += 1
            except DB_ERRORS as exc:
                report["batches"]["failed"] += 1
                rpt.finish(report, "failed_write", failed_stage="write", error=f"{type(exc).__name__}: {str(exc)[:200]}",
                           failure_detail={"entity": entity, "batch_number": batch_no, "records_in_batch": len(batch),
                                           "completed_before_failure": done,
                                           "note": "Statements are individually atomic; earlier statements of "
                                                   "this batch may have committed. Rerun to reconcile."})
                raise _Stop()


def _write_batch(store, cfg, entity, batch, report, quarantine, graph_skipped, done: List[str]) -> None:
    label, idf = LABEL[entity], ID_FIELD[entity]
    cls = classify(store, cfg, entity, batch)
    g = report["graph"][entity]

    def conflict(rows, reason):
        for r in rows:
            graph_skipped[entity].add(r.record[idf])
            if cfg.graph_conflict_policy == "skip":
                g["conflicting_skipped"] += 1
            else:
                g["conflicting_rejected"] += 1
                report["graph_conflicts"] += 1
                if quarantine is not None:
                    r.errors = [RecordError("GRAPH_CONFLICT", "*", reason)]
                    quarantine.write(r)

    if cfg.graph_conflict_policy == "update":
        to_update, changed_conflicts = cls["changed"], []
    else:
        to_update, changed_conflicts = [], cls["changed"]
    conflict(changed_conflicts, "record differs from existing graph data.")
    conflict(cls["conflict"], "transaction endpoints differ from existing graph relationships.")

    created = store.create_nodes(label, [r.record for r in cls["new"]])
    if cls["new"]:
        report["writes_occurred"] = report["writes_occurred"] or created > 0
    done.append(f"create_{label}:{created}")
    g["new"] += created
    if to_update:
        store.update_nodes(label, [r.record for r in to_update])
        report["writes_occurred"] = True
        done.append(f"update_{label}:{len(to_update)}")
        g["updated"] += len(to_update)
    g["unchanged"] += len(cls["unchanged"])

    linkable = cls["new"] + cls["unchanged"] + to_update
    if entity == "transactions" and linkable:
        pairs = [(r.extra["source_account"], r.record["transaction_id"]) for r in linkable]
        m, c = store.link_batch("SENT", pairs)
        _rel(report, "SENT", m, c); done.append(f"SENT:{c}")
        pairs = [(r.record["transaction_id"], r.extra["destination_account"]) for r in linkable]
        m, c = store.link_batch("RECEIVED_BY", pairs)
        _rel(report, "RECEIVED_BY", m, c); done.append(f"RECEIVED_BY:{c}")
    if entity == "alerts" and linkable:
        pairs = [(r.record["alert_id"], r.extra["transaction_id"]) for r in linkable]
        m, c = store.link_batch("FLAGS", pairs)
        _rel(report, "FLAGS", m, c); done.append(f"FLAGS:{c}")


def _rel(report, name: str, matched: int, created: int) -> None:
    report["relationships"][name]["created"] += created
    report["relationships"][name]["already_present"] += matched - created
    report["writes_occurred"] = report["writes_occurred"] or created > 0


def _verify(store, cfg, outcome, graph_skipped) -> Dict[str, Any]:
    expected = {e: 0 for e in ENTITIES}
    found = {e: 0 for e in ENTITIES}
    for entity in ENTITIES:
        if entity == "alerts" and not cfg.alerts:
            continue
        for batch in _batches(_source_rows(cfg, entity, outcome), cfg.batch_size):
            rows = [r for r in batch if r.record[ID_FIELD[entity]] not in graph_skipped[entity]]
            expected[entity] += len(rows)
            if entity == "accounts":
                found[entity] += len(store.fetch_nodes("Account", [r.record["account_id"] for r in rows]))
            elif entity == "transactions":
                found[entity] += store.verify_transactions([
                    {"id": r.record["transaction_id"], "src": r.extra["source_account"],
                     "dst": r.extra["destination_account"]} for r in rows])
            else:
                found[entity] += store.verify_alert_links([
                    {"id": r.record["alert_id"], "tx": r.extra["transaction_id"]} for r in rows])
    return {"passed": expected == found,
            "checks": {"expected": expected, "found_in_graph": found,
                       "meaning": "accounts exist; transactions exist with SENT and RECEIVED_BY to the "
                                  "expected accounts; alerts exist with FLAGS to the expected transaction"}}
