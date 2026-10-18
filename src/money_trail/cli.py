"""Command-line entry points: money-trail-check and money-trail-init."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

from money_trail.config import load_settings
from money_trail.database import FalkorDBConnection, check_health, initialize_graph
from money_trail.database.health import report_configuration_error
from money_trail.exceptions import ConfigurationError, MoneyTrailError, QueryError
from money_trail.logging_config import configure_logging


def _parser(prog: str, description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog, description=description)
    p.add_argument("--env-file", default=None,
                   help="Path to an env file. Defaults to ./.env when it exists.")
    p.add_argument("--json", action="store_true", help="Machine-readable output.")
    return p


def _resolve_env_file(arg: Optional[str]) -> Optional[str]:
    if arg:
        return arg
    return ".env" if Path(".env").is_file() else None


def check_main(argv: Optional[Sequence[str]] = None) -> int:
    """Exit 0 only when the database is reachable, the graph is accessible and the schema is ready."""
    args = _parser("money-trail-check", "Check FalkorDB connectivity and schema readiness.").parse_args(argv)
    try:
        settings = load_settings(_resolve_env_file(args.env_file))
    except ConfigurationError as exc:
        report = report_configuration_error(exc)
    else:
        configure_logging(settings.log_level)
        with FalkorDBConnection(settings) as conn:
            report = check_health(conn)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(f"status: {report.status}")
        print(f"  configuration valid : {report.configuration_valid}")
        print(f"  database reachable  : {report.database_reachable}")
        print(f"  graph accessible    : {report.graph_accessible}")
        print(f"  schema ready        : {report.schema_ready}")
        print(f"  {report.message}")
        for key in ("missing_indexes", "missing_constraints", "pending_constraints"):
            if report.details.get(key):
                print(f"  {key}: {', '.join(report.details[key])}")
    return 0 if report.healthy else 1


def init_main(argv: Optional[Sequence[str]] = None) -> int:
    """Create the configured graph if needed and add missing schema elements. Never deletes data."""
    args = _parser("money-trail-init", "Initialize the configured graph schema (idempotent, non-destructive).").parse_args(argv)
    try:
        settings = load_settings(_resolve_env_file(args.env_file))
        configure_logging(settings.log_level)
        with FalkorDBConnection(settings) as conn:
            result = initialize_graph(conn)
    except MoneyTrailError as exc:
        print(f"initialization failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({
            "graph": result.graph_name, "graph_created": result.graph_created,
            "indexes_created": result.indexes_created,
            "constraints_created": result.constraints_created,
            "already_present": result.already_present, "schema_version": result.schema_version,
        }, indent=2))
    else:
        print(f"graph: {result.graph_name} ({'created' if result.graph_created else 'already existed'})")
        print(f"indexes created: {len(result.indexes_created)}")
        print(f"constraints created: {len(result.constraints_created)}")
        print(f"schema elements already present: {result.already_present}")
        print("schema ready")
    return 0


# --- Phase 2: data commands (one `money-trail` command with subcommands) ---------

EXIT_NOT_FOUND = 3


def _emit(obj, as_json: bool, text: str) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True) if as_json else text)


def _cmd_generate(args) -> int:
    from money_trail.generation import generate, load_generator_config, write_dataset
    cfg = load_generator_config(args.config)
    manifest = write_dataset(cfg, generate(cfg), overwrite=args.overwrite)
    out = cfg.output_dir / cfg.namespace
    _emit({"output_dir": str(out), "manifest": manifest}, args.json,
          f"generated dataset '{cfg.namespace}' in {out}\n"
          f"  seed {manifest['random_seed']}, counts {manifest['record_counts']}\n"
          f"  ingest config: {out / 'ingest_config.json'}\n"
          f"  ground truth (evaluation only, never ingested): {out / 'ground_truth.json'}")
    return 0


def _cmd_validate(args) -> int:
    from money_trail.ingestion import load_dataset_config, validate_dataset
    cfg = load_dataset_config(args.dataset_config)
    out = validate_dataset(cfg)
    result = {"namespace": cfg.namespace, "fatal": out.fatal,
              "records": {e: s.as_dict() for e, s in out.stats.items()},
              "errors": {"total": out.error_total, "by_code": out.summary_errors(), "samples": out.error_samples}}
    lines = [f"validation of '{cfg.namespace}' (no database access, nothing written)"]
    if out.fatal:
        lines.append(f"  FATAL: {out.fatal}")
    for e, s in out.stats.items():
        lines.append(f"  {e:<13} examined {s.examined}, accepted {s.accepted}, rejected {s.rejected}, "
                     f"identical repeats {s.duplicates_identical}, conflicting repeats skipped {s.source_conflicts_skipped}")
    for x in out.summary_errors():
        lines.append(f"  error {x['entity']}:{x['code']} x{x['count']}")
    for x in out.error_samples:
        lines.append(f"    {x['entity']} record {x['record_number']} (line {x['line']}): {x['code']} - {x['message']}")
    lines.append("  result: " + ("VALID" if not out.fatal and not out.error_total else "INVALID"))
    _emit(result, args.json, "\n".join(lines))
    return 0 if not out.fatal and not out.error_total else 1


def _cmd_ingest(args) -> int:
    from money_trail.ingestion import ingest_dataset, load_dataset_config
    from money_trail.ingestion.report import render_summary
    cfg = load_dataset_config(args.dataset_config)
    settings = load_settings(_resolve_env_file(args.env_file))
    configure_logging(settings.log_level)
    with FalkorDBConnection(settings) as conn:
        report = ingest_dataset(conn, cfg)
    _emit(report, args.json, render_summary(report))
    return 0 if report["status"] in ("succeeded", "succeeded_with_rejections") else 1


def _report_dir(args) -> Path:
    if args.report_dir:
        return Path(args.report_dir)
    if args.dataset_config:
        from money_trail.ingestion import load_dataset_config
        return load_dataset_config(args.dataset_config).report_dir
    raise ConfigurationError("Provide --report-dir or --dataset-config.")


def _cmd_report(args) -> int:
    from money_trail.ingestion.report import list_reports, load_report, render_summary
    directory = _report_dir(args)
    if args.report_action == "list":
        rows = list_reports(directory)
        _emit(rows, args.json, "\n".join(f"{r['started_at']}  {r['run_id']}  {r['status']}  {r['namespace']}"
                                         for r in rows) or "no reports found")
        return 0
    report = load_report(directory, args.run_id)
    _emit(report, args.json, render_summary(report))
    return 0


def _cmd_query(args) -> int:
    from money_trail.investigation import InvestigationQueries, QueryLimits
    settings = load_settings(_resolve_env_file(args.env_file))
    configure_logging(settings.log_level)
    limits = QueryLimits.from_settings(settings).override(
        max_results=args.limit, max_hops=getattr(args, "max_hops", None),
        max_paths=getattr(args, "max_paths", None), max_gap_ms=getattr(args, "max_gap_ms", None))
    with FalkorDBConnection(settings) as conn:
        q = InvestigationQueries(conn, limits)
        if args.query_kind == "transaction":
            outcome = q.transaction_context(args.id)
        elif args.query_kind == "account":
            outcome = q.account_activity(args.id, args.direction, args.start_ms, args.end_ms)
        elif args.query_kind == "trace":
            outcome = q.trace_network(args.id, args.direction)
        else:
            outcome = q.evidence_paths(args.id)
    print(json.dumps(outcome.as_dict(), indent=2, sort_keys=True))
    return 0 if outcome.status == "ok" else EXIT_NOT_FOUND


# --- Phase 3: detection, investigation and evaluation ----------------------------------------

EXIT_INCOMPLETE = 4


def _load_detection(path):
    from money_trail.investigation.config import load_detection_config
    return load_detection_config(path)


def _cmd_detectors(args) -> int:
    from money_trail.investigation.detectors.registry import DETECTORS, ORDER
    cfg = _load_detection(args.detection_config) if args.detection_config else None
    if args.detectors_action == "validate" and cfg is None:
        raise ConfigurationError("detectors validate needs --detection-config.")
    rows = []
    for n in ORDER:
        doc = (DETECTORS[n].__module__.rsplit(".", 1)[-1], (__import__(DETECTORS[n].__module__, fromlist=["x"]).__doc__ or "").strip().splitlines()[0])
        row = {"detector": n, "summary": doc[1]}
        if cfg:
            row.update(enabled=cfg.enabled[n], rule_version=cfg.rule_version(n) if cfg.enabled[n] else None)
        rows.append(row)
    out = {"detectors": rows}
    if cfg:
        out.update(rule_set=f"{cfg.rule_set_name}@{cfg.rule_set_version}", rule_fingerprint=cfg.fingerprint(),
                   limits=cfg.limits.__dict__, hypothesis_findings=cfg.hypothesis_findings)
    text = []
    if cfg:
        text.append(f"detection config valid: rule set {out['rule_set']}, fingerprint {out['rule_fingerprint']}")
    for r in rows:
        extra = "" if cfg is None else f"  [{'enabled' if r['enabled'] else 'disabled'}]" + (f" {r['rule_version']}" if r["rule_version"] else "")
        text.append(f"  {r['detector']:<13} {r['summary']}{extra}")
    _emit(out, args.json, "\n".join(text))
    return 0


def _detection_text(res) -> str:
    L = [f"investigation status: {res.status} (complete: {res.complete}, {res.duration_ms} ms)",
         f"  anchor: {res.request.anchor_type} {res.request.anchor_id or res.request.namespace} (namespace {res.namespace})"]
    for n, r in res.detector_reports.items():
        if n.startswith("_"):
            continue
        L.append(f"  {n:<13} {r['detections']} detection(s), truncated={r['truncated']}, {r['queries']} queries, "
                 f"{r['rows_read']} rows, {r['query_seconds']}s")
        for w in r["warnings"]:
            L.append(f"    warning: {w}")
    for d in res.detections:
        L.append(f"  - {d.pattern_type} {d.detection_id}: {len(d.transaction_ids)} transactions, {len(d.account_ids)} accounts")
        L.append(f"      transactions: {', '.join(d.transaction_ids)}")
    for f in res.evidence_failures:
        L.append(f"  evidence validation failed for {f['detection_id']}: {f['reason']}")
    if res.persistence:
        pr = res.persistence
        L.append(f"  persisted investigation {pr['investigation_id']}: findings {pr['findings']}, evidence {pr['evidence']}")
    if res.error:
        L.append(f"  error: {res.error}")
    return "\n".join(L)


def _investigate_exit(res) -> int:
    if res.status == "anchor_not_found":
        return EXIT_NOT_FOUND
    if res.status in ("database_error", "evidence_validation_failed"):
        return 1
    return 0 if res.complete else EXIT_INCOMPLETE


def _cmd_investigate(args) -> int:
    from money_trail.investigation.engine import InvestigationEngine, InvestigationRequest
    chosen = [x for x in (args.transaction_id, args.account_id, args.namespace) if x]
    if len(chosen) != 1:
        raise ConfigurationError("give exactly one of --transaction-id, --account-id or --namespace (dataset-wide).")
    req = (InvestigationRequest("transaction", args.transaction_id) if args.transaction_id else
           InvestigationRequest("account", args.account_id) if args.account_id else
           InvestigationRequest("dataset", namespace=args.namespace))
    cfg = _load_detection(args.detection_config)
    settings = load_settings(_resolve_env_file(args.env_file))
    configure_logging(settings.log_level)
    with FalkorDBConnection(settings) as conn:
        res = InvestigationEngine(conn, cfg).investigate(req, persist=args.persist)
    _emit(res.as_dict(), args.json, _detection_text(res))
    return _investigate_exit(res)


def _cmd_findings(args) -> int:
    from money_trail.investigation import QueryLimits
    from money_trail.investigation.findings import get_investigation, list_findings
    settings = load_settings(_resolve_env_file(args.env_file))
    configure_logging(settings.log_level)
    limit = QueryLimits.from_settings(settings).override(max_results=args.limit).require("max_results", "results")
    with FalkorDBConnection(settings) as conn:
        if args.findings_action == "list":
            out = list_findings(conn, investigation_id=args.investigation_id, transaction_id=args.transaction_id, limit=limit)
            text = "\n".join(f"{f['finding_id']}  {f['kind']:<11} investigations={len(f['investigation_ids'])} "
                              f"evidence={len(f['evidence_ids'])}  {f['statement'][:90]}" for f in out["findings"]) or "no findings"
            _emit(out, args.json, text + ("\n(truncated)" if out["truncated"] else ""))
            return 0 if not out["truncated"] else EXIT_INCOMPLETE
        out = get_investigation(conn, args.investigation_id, limit=limit)
    if out is None:
        print(f"error: investigation not found: {args.investigation_id}", file=sys.stderr)
        return EXIT_NOT_FOUND
    lines = [f"investigation {args.investigation_id}: status {out['investigation']['status']}, "
             f"started from {', '.join(out['started_from'])}"]
    for f in out["findings"]:
        lines.append(f"  {f['kind']:<11} {f['finding_id']}: {f['statement'][:100]}")
        for e in f["evidence"]:
            lines.append(f"      evidence {e['evidence_id']} ({e['produced_by_tool']}): {', '.join(e['transaction_ids'])}")
    _emit(out, args.json, "\n".join(lines) + ("\n(truncated)" if out["truncated"] else ""))
    return 0 if not out["truncated"] else EXIT_INCOMPLETE


def _cmd_evaluate(args) -> int:
    from money_trail.evaluation.report import render_text, run_evaluation
    cfg = _load_detection(args.detection_config)
    settings = load_settings(_resolve_env_file(args.env_file))
    configure_logging(settings.log_level)
    with FalkorDBConnection(settings) as conn:
        report = run_evaluation(conn, cfg, args.namespace, args.ground_truth)
    _emit(report, args.json, render_text(report))
    status = report["detection"]["status"]
    if status in ("anchor_not_found", "database_error", "evidence_validation_failed"):
        return 1
    return 0 if report["detection"]["complete"] else EXIT_INCOMPLETE


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="money-trail", description="Money Trail data and investigation commands.")
    sub = p.add_subparsers(dest="command", required=True)
    env = argparse.ArgumentParser(add_help=False)
    env.add_argument("--env-file", default=None, help="Env file (default ./.env if present).")
    env.add_argument("--json", action="store_true", help="Machine-readable output.")

    after = argparse.ArgumentParser(add_help=False)      # same options, usable after a nested action
    after.add_argument("--env-file", default=argparse.SUPPRESS)
    after.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    g = sub.add_parser("generate", parents=[env], help="Generate a synthetic dataset (writes files only).")
    g.add_argument("--config", required=True, help="Generator config JSON.")
    g.add_argument("--overwrite", action="store_true", help="Replace previously generated files.")
    g.set_defaults(fn=_cmd_generate)

    v = sub.add_parser("validate", parents=[env], help="Validate a dataset offline (no database, writes nothing).")
    v.add_argument("--dataset-config", required=True)
    v.set_defaults(fn=_cmd_validate)

    i = sub.add_parser("ingest", parents=[env], help="Ingest a dataset into FalkorDB (the only command that writes the graph).")
    i.add_argument("--dataset-config", required=True)
    i.set_defaults(fn=_cmd_ingest)

    r = sub.add_parser("report", parents=[env], help="Inspect ingestion reports.")
    r.add_argument("--report-dir")
    r.add_argument("--dataset-config")
    rs = r.add_subparsers(dest="report_action", required=True)
    rs.add_parser("list")
    show = rs.add_parser("show")
    show.add_argument("run_id")
    r.set_defaults(fn=_cmd_report)

    q = sub.add_parser("query", help="Read-only investigation queries.")
    qs = q.add_subparsers(dest="query_kind", required=True)
    def qparser(name, helptext):
        sp = qs.add_parser(name, parents=[env], help=helptext)
        sp.add_argument("id")
        sp.add_argument("--limit", type=int, help="Max results (else MONEY_TRAIL_QUERY_MAX_RESULTS).")
        sp.set_defaults(fn=_cmd_query)
        return sp
    qparser("transaction", "Transaction context: accounts and alerts")
    a = qparser("account", "Incoming/outgoing activity of an account")
    a.add_argument("--direction", choices=("in", "out", "both"), default="both")
    a.add_argument("--start-ms", type=int)
    a.add_argument("--end-ms", type=int)
    t = qparser("trace", "Time-respecting transaction paths from a transaction")
    t.add_argument("--direction", choices=("forward", "backward"), default="forward")
    t.add_argument("--max-hops", type=int)
    t.add_argument("--max-paths", type=int)
    t.add_argument("--max-gap-ms", type=int)
    qparser("evidence", "Investigation -> finding -> evidence -> transaction paths")

    d = sub.add_parser("detectors", parents=[env], help="List detectors or validate a detection config (offline).")
    d.add_argument("--detection-config")
    ds = d.add_subparsers(dest="detectors_action", required=True)
    for action in ("list", "validate"):      # options are accepted before or after the action
        ap = ds.add_parser(action, parents=[after])
        ap.add_argument("--detection-config", default=argparse.SUPPRESS)
    d.set_defaults(fn=_cmd_detectors)

    iv = sub.add_parser("investigate", parents=[env], help="Run the enabled detectors from a transaction, account or whole dataset.")
    iv.add_argument("--detection-config", required=True)
    iv.add_argument("--transaction-id")
    iv.add_argument("--account-id")
    iv.add_argument("--namespace", help="Dataset-wide (bounded) analysis of this namespace; read-only.")
    iv.add_argument("--persist", action="store_true", help="Store findings and evidence (transaction anchors only).")
    iv.set_defaults(fn=_cmd_investigate)

    fd = sub.add_parser("findings", parents=[env], help="Read persisted findings and investigations.")
    fd.add_argument("--limit", type=int, help="Max results (else MONEY_TRAIL_QUERY_MAX_RESULTS).")
    fs = fd.add_subparsers(dest="findings_action", required=True)
    fl = fs.add_parser("list", parents=[after])
    fl.add_argument("--limit", type=int, default=argparse.SUPPRESS)
    fl.add_argument("--investigation-id")
    fl.add_argument("--transaction-id", help="Findings of investigations started from this transaction.")
    fsh = fs.add_parser("show", parents=[after], help="One investigation with its findings, evidence and transactions.")
    fsh.add_argument("--limit", type=int, default=argparse.SUPPRESS)
    fsh.add_argument("investigation_id")
    fd.set_defaults(fn=_cmd_findings)

    ev = sub.add_parser("evaluate", parents=[env], help="Run dataset-wide detection, then compare with the ground-truth file.")
    ev.add_argument("--detection-config", required=True)
    ev.add_argument("--namespace", required=True)
    ev.add_argument("--ground-truth", required=True, help="ground_truth.json of the generated dataset (read only after detection).")
    ev.set_defaults(fn=_cmd_evaluate)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except MoneyTrailError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
