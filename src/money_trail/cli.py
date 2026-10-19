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


# --- Phase 4: AI-assisted investigation ------------------------------------------------------
EXIT_UNSUPPORTED = 5
_AGENT_EXIT = {"completed": 0, "incomplete": EXIT_INCOMPLETE, "anchor_not_found": EXIT_NOT_FOUND,
               "unsupported_request": EXIT_UNSUPPORTED, "invalid_request": 1, "failed": 1}


def _cmd_agent(args) -> int:
    from money_trail.agent.config import load_agent_config
    cfg = load_agent_config(args.agent_config)
    env_file = _resolve_env_file(args.env_file)
    if args.agent_action == "validate-config":
        from money_trail.config.settings import load_secret
        out = {"valid": True, "agent": cfg.safe_summary()}
        if args.check_credentials:
            out["credential_set"] = load_secret(cfg.provider.api_key_env, env_file) is not None
            out["credential_variable"] = cfg.provider.api_key_env
        _emit(out, args.json, "agent config valid\n" + json.dumps(out["agent"], indent=2) +
              (f"\ncredential {cfg.provider.api_key_env}: {'set' if out['credential_set'] else 'NOT set'}"
               if args.check_credentials else ""))
        return 0 if out.get("credential_set", True) else 1
    det = _load_detection(args.detection_config) if args.detection_config else None
    if det is None:
        raise ConfigurationError("--detection-config is required.")
    if args.agent_action == "tools":
        from money_trail.agent.tools.registry import build_registry
        reg = build_registry(det, cfg.tools)
        rows = [{"name": t.name, "description": t.description, "mutates_state": t.mutates_state, "input_schema": t.spec().input_schema,
                 "returns": t.result_description} for t in (reg.get(n) for n in reg.names)]
        _emit({"tools": rows}, args.json, "\n".join(f"{r['name']:<27} reads graph only  returns: {r['returns']}\n    {r['description']}" for r in rows))
        return 0
    if args.agent_action == "evaluate":
        return _agent_evaluate(args, cfg, det, env_file)
    return _agent_investigate(args, cfg, det, env_file)


def _agent_evaluate(args, cfg, det, env_file) -> int:
    """Run the scenario set against the REAL configured provider and print agent-quality metrics. Needs a credential."""
    from money_trail.agent.agent import InvestigationAgent
    from money_trail.agent.llm.factory import LazyProvider
    from money_trail.config.settings import load_secret
    from money_trail.evaluation.agent_eval import build_scenarios, evaluate_runs, run_scenarios
    from money_trail.investigation.engine import InvestigationEngine, InvestigationRequest
    if load_secret(cfg.provider.api_key_env, env_file) is None:
        print(f"error: {cfg.provider.api_key_env} is not set; a live evaluation needs a real provider credential "
              "(nothing was run).", file=sys.stderr)
        return 1
    settings = load_settings(env_file)
    configure_logging(settings.log_level)
    with FalkorDBConnection(settings) as conn:
        gt = json.loads(Path(args.ground_truth).read_text(encoding="utf-8"))
        labelled = {t for p in gt["patterns"] for t in p["transaction_ids"]}
        engine = InvestigationEngine(conn, det)

        def quiet():
            for (tid,) in conn.graph.ro_query("MATCH (t:Transaction {dataset: $ns}) RETURN t.transaction_id ORDER BY t.transaction_id LIMIT 200",
                                              {"ns": gt["namespace"]}).result_set:
                if tid not in labelled:
                    r = engine.investigate(InvestigationRequest("transaction", tid))
                    if r.complete and not r.detections:
                        return tid
            return None
        provider = LazyProvider(cfg.provider.name, model=cfg.provider.model, base_url=cfg.provider.base_url,
                                get_api_key=lambda: load_secret(cfg.provider.api_key_env, env_file))
        agent = InvestigationAgent(conn, det, cfg, provider)
        result = evaluate_runs(conn, run_scenarios(agent, build_scenarios(conn, Path(args.ground_truth), quiet)))
    result["note"] = ("Live provider run on a small synthetic scenario set; not evidence of real-world detection performance.")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _agent_request(args) -> dict:
    if args.request_file:
        if any((args.transaction_id, args.account_id, args.question, args.intent, args.namespace)):
            raise ConfigurationError("--request-file cannot be combined with --transaction-id, --account-id, --question, --intent or --namespace.")
        try:
            raw = json.loads(Path(args.request_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigurationError(f"Request file is not readable JSON ({type(exc).__name__}).") from None
    else:
        if bool(args.transaction_id) == bool(args.account_id):
            raise ConfigurationError("give exactly one of --transaction-id, --account-id or --request-file.")
        raw = {"anchor_type": "transaction" if args.transaction_id else "account",
               "anchor_id": args.transaction_id or args.account_id}
        for k, v in (("question", args.question), ("intent", args.intent), ("namespace", args.namespace)):
            if v is not None:
                raw[k] = v
    limits = {k: v for k, v in (("max_steps", args.max_steps), ("max_tool_calls", args.max_tool_calls),
                                ("max_duration_seconds", args.max_duration_seconds)) if v is not None}
    if limits and isinstance(raw, dict):
        raw = {**raw, "limits": {**(raw.get("limits") or {}), **limits}}
    return raw


def _agent_investigate(args, cfg, det, env_file) -> int:
    from money_trail.agent.agent import InvestigationAgent
    from money_trail.agent.llm.factory import LazyProvider
    from money_trail.agent.report import render_markdown, render_text
    from money_trail.config.settings import load_secret
    raw = _agent_request(args)
    settings = load_settings(env_file)
    configure_logging(settings.log_level)
    provider = LazyProvider(cfg.provider.name, model=cfg.provider.model, base_url=cfg.provider.base_url,
                            get_api_key=lambda: load_secret(cfg.provider.api_key_env, env_file))
    with FalkorDBConnection(settings) as conn:
        health = check_health(conn)
        if not health.healthy:
            print(f"error: database not ready ({health.status}): {health.message}", file=sys.stderr)
            return 1
        report = InvestigationAgent(conn, det, cfg, provider).run(raw, persist=args.persist)
    fmt = "json" if args.json else args.format
    print(json.dumps(report, indent=2, sort_keys=True) if fmt == "json" else
          render_markdown(report, trace=args.trace) if fmt == "markdown" else render_text(report, trace=args.trace))
    err = report["metadata"].get("error")
    if err and report["metadata"]["run_status"] in ("failed", "incomplete") and any(
            r.startswith("provider_error") for r in report["metadata"]["completion_reasons"]):
        print(f"error: model provider failure: {err}", file=sys.stderr)
    return _AGENT_EXIT.get(report["metadata"]["run_status"], 1)


# --- Phase 5: Persistent memory and coordination -----------------------------

def _cmd_migrate(args) -> int:
    from money_trail.database.migration import migrate_schema
    settings = load_settings(_resolve_env_file(args.env_file))
    configure_logging(settings.log_level)
    with FalkorDBConnection(settings) as conn:
        res = migrate_schema(conn)
    out = {
        "graph_name": res.graph_name,
        "from_version": res.from_version,
        "to_version": res.to_version,
        "applied": res.applied,
        "indexes_created": res.indexes_created,
        "constraints_created": res.constraints_created,
        "already_current": res.already_current,
    }
    text = (
        f"Graph {res.graph_name}: schema already current at version {res.to_version}."
        if res.already_current
        else f"Graph {res.graph_name}: migrated v{res.from_version} -> v{res.to_version} "
             f"({len(res.indexes_created)} indexes, {len(res.constraints_created)} constraints)."
    )
    _emit(out, args.json, text)
    return 0


def _cmd_schema_version(args) -> int:
    from money_trail.database.migration import get_active_schema_version, get_applied_migrations
    settings = load_settings(_resolve_env_file(args.env_file))
    configure_logging(settings.log_level)
    with FalkorDBConnection(settings) as conn:
        ver = get_active_schema_version(conn)
        applied = get_applied_migrations(conn)
    out = {"active_version": ver, "applied_migrations": applied}
    text = (
        f"Active schema version: {ver}\nApplied migrations:\n"
        + "\n".join(f"  v{m['version']}: {m['description']} ({m['status']})" for m in applied)
        if applied
        else f"Active schema version: {ver} (no migration nodes recorded)"
    )
    _emit(out, args.json, text)
    return 0


def _cmd_memory(args) -> int:
    from money_trail.memory.store import MemoryStore
    settings = load_settings(_resolve_env_file(args.env_file))
    configure_logging(settings.log_level)
    with FalkorDBConnection(settings) as conn:
        store = MemoryStore(conn)
        if args.memory_action == "list":
            if not args.scope:
                raise ConfigurationError("--scope (investigation or dataset) is required.")
            mems = store.query_memories(
                scope=args.scope,
                dataset=args.dataset,
                investigation_id=args.investigation_id,
                memory_type=args.type,
                status=args.status,
                limit=args.limit or 50,
            )
            out = {"memories": [m.to_dict() for m in mems]}
            text = "\n".join(
                f"{m.memory_id}  {m.memory_type:<9} {m.status:<10}  {m.statement[:90]}"
                for m in mems
            ) or "no memories found"
            _emit(out, args.json, text)
            return 0
        elif args.memory_action == "show":
            m = store.get_memory(args.memory_id)
            if not m:
                print(f"error: memory not found: {args.memory_id}", file=sys.stderr)
                return EXIT_NOT_FOUND
            out = m.to_dict()
            text = (
                f"Memory {m.memory_id} [{m.memory_type}, {m.status}]\n"
                f"Scope: {m.scope} (investigation={m.investigation_id}, dataset={m.dataset})\n"
                f"Statement: {m.statement}\n"
                f"Provenance tool: {m.provenance_tool}\n"
                f"Grounding evidence: {', '.join(m.evidence_ids) or 'none'}"
            )
            _emit(out, args.json, text)
            return 0
        elif args.memory_action == "provenance":
            m = store.get_memory(args.memory_id)
            if not m:
                print(f"error: memory not found: {args.memory_id}", file=sys.stderr)
                return EXIT_NOT_FOUND
            ev_records = []
            for eid in m.evidence_ids:
                tx_rows = conn.graph.ro_query(
                    "MATCH (e:Evidence {evidence_id: $eid})-[:REFERENCES]->(t:Transaction) "
                    "RETURN t.transaction_id, t.amount, t.currency",
                    {"eid": eid},
                ).result_set
                ev_records.append({
                    "evidence_id": eid,
                    "transactions": [{"transaction_id": r[0], "amount": r[1], "currency": r[2]} for r in tx_rows],
                })
            out = {"memory_id": m.memory_id, "provenance_tool": m.provenance_tool, "evidence": ev_records}
            text = f"Memory {m.memory_id} provenance ({m.provenance_tool}):\n" + "\n".join(
                f"  Evidence {e['evidence_id']} -> {len(e['transactions'])} transactions: {', '.join(t['transaction_id'] for t in e['transactions'])}"
                for e in ev_records
            )
            _emit(out, args.json, text)
            return 0
        elif args.memory_action == "history":
            m = store.get_memory(args.memory_id)
            if not m:
                print(f"error: memory not found: {args.memory_id}", file=sys.stderr)
                return EXIT_NOT_FOUND
            out = {
                "memory_id": m.memory_id,
                "status": m.status,
                "superseded_by": m.superseded_by,
                "supersedes": m.supersedes_id,
                "contradictions": m.contradicting_memory_ids,
            }
            text = (
                f"Memory {m.memory_id} history:\n"
                f"  Status: {m.status}\n"
                f"  Superseded by: {m.superseded_by or 'none'}\n"
                f"  Supersedes: {m.supersedes_id or 'none'}\n"
                f"  Contradictions: {', '.join(m.contradicting_memory_ids) or 'none'}"
            )
            _emit(out, args.json, text)
            return 0
    return 0


def _cmd_task(args) -> int:
    from money_trail.coordination.store import CoordinationStore
    settings = load_settings(_resolve_env_file(args.env_file))
    configure_logging(settings.log_level)
    with FalkorDBConnection(settings) as conn:
        cstore = CoordinationStore(conn)
        if args.task_action == "list":
            if not args.investigation_id:
                raise ConfigurationError("--investigation-id is required.")
            tasks = cstore.list_tasks(args.investigation_id, status=args.status, role=args.role)
            out = {"tasks": [t.__dict__ for t in tasks]}
            text = "\n".join(
                f"{t.task_id}  {t.task_type:<18} {t.status:<11} role={t.assigned_role or 'none':<13} deps={len(t.dependencies)}"
                for t in tasks
            ) or "no tasks found"
            _emit(out, args.json, text)
            return 0
        elif args.task_action == "show":
            t = cstore.get_task(args.task_id)
            if not t:
                print(f"error: task not found: {args.task_id}", file=sys.stderr)
                return EXIT_NOT_FOUND
            out = t.__dict__
            text = (
                f"Task {t.task_id} [{t.task_type}]\n"
                f"Status: {t.status}\n"
                f"Investigation: {t.investigation_id}\n"
                f"Assigned: role={t.assigned_role}, agent={t.assigned_agent_id}\n"
                f"Dependencies: {', '.join(t.dependencies) or 'none'}\n"
                f"Summary: {t.result_summary or 'none'}\n"
                f"Error: {t.error_detail or 'none'}"
            )
            _emit(out, args.json, text)
            return 0
        elif args.task_action == "blockers":
            t = cstore.get_task(args.task_id)
            if not t:
                print(f"error: task not found: {args.task_id}", file=sys.stderr)
                return EXIT_NOT_FOUND
            unmet = []
            for dep_id in t.dependencies:
                dep = cstore.get_task(dep_id)
                if not dep or dep.status != "completed":
                    unmet.append({"task_id": dep_id, "status": dep.status if dep else "missing"})
            out = {"task_id": t.task_id, "status": t.status, "unmet_dependencies": unmet, "error_detail": t.error_detail}
            text = (
                f"Task {t.task_id} blockers (status: {t.status}):\n"
                + ("\n".join(f"  Unmet dependency: {u['task_id']} (status: {u['status']})" for u in unmet) if unmet else "  No unmet dependencies.")
                + (f"\n  Error detail: {t.error_detail}" if t.error_detail else "")
            )
            _emit(out, args.json, text)
            return 0
    return 0


def _cmd_handoff(args) -> int:
    from money_trail.coordination.store import CoordinationStore
    settings = load_settings(_resolve_env_file(args.env_file))
    configure_logging(settings.log_level)
    with FalkorDBConnection(settings) as conn:
        cstore = CoordinationStore(conn)
        if args.handoff_action == "list":
            if not args.investigation_id:
                raise ConfigurationError("--investigation-id is required.")
            handoffs = cstore.list_handoffs(args.investigation_id)
            out = {"handoffs": [h.__dict__ for h in handoffs]}
            text = "\n".join(
                f"{h.handoff_id}  task={h.task_id}  {h.source_agent_id} -> {h.target_role}  [{h.status}]"
                for h in handoffs
            ) or "no handoffs found"
            _emit(out, args.json, text)
            return 0
        elif args.handoff_action == "show":
            h = cstore.get_handoff(args.handoff_id)
            if not h:
                print(f"error: handoff not found: {args.handoff_id}", file=sys.stderr)
                return EXIT_NOT_FOUND
            out = h.__dict__
            text = (
                f"Handoff {h.handoff_id} [{h.status}]\n"
                f"Investigation: {h.investigation_id}\n"
                f"Task: {h.task_id}\n"
                f"Transfer: {h.source_agent_id} -> {h.target_role} (target agent: {h.target_agent_id or 'any'})\n"
                f"Reason: {h.reason or 'unspecified'}\n"
                f"Context: {h.context_summary or 'none'}"
            )
            _emit(out, args.json, text)
            return 0
    return 0


def _cmd_session(args) -> int:
    from money_trail.coordination.orchestrator import MultiAgentOrchestrator
    from money_trail.coordination.store import CoordinationStore
    settings = load_settings(_resolve_env_file(args.env_file))
    configure_logging(settings.log_level)
    with FalkorDBConnection(settings) as conn:
        cstore = CoordinationStore(conn)
        if args.session_action == "list":
            if not args.investigation_id:
                raise ConfigurationError("--investigation-id is required.")
            rows = conn.graph.ro_query(
                "MATCH (s:AgentSession {investigation_id: $inv_id}) RETURN s.session_id, s.status, s.started_at, s.completed_at ORDER BY s.started_at DESC",
                {"inv_id": args.investigation_id},
            ).result_set
            sessions = [{"session_id": r[0], "status": r[1], "started_at": r[2], "completed_at": r[3]} for r in rows]
            out = {"sessions": sessions}
            text = "\n".join(f"{s['session_id']}  status={s['status']:<11} started={s['started_at']}" for s in sessions) or "no sessions"
            _emit(out, args.json, text)
            return 0
        elif args.session_action == "show":
            s = cstore.get_session(args.session_id)
            if not s:
                print(f"error: session not found: {args.session_id}", file=sys.stderr)
                return EXIT_NOT_FOUND
            rec = cstore.recover_session(args.session_id)
            out = {
                "session": s.__dict__,
                "tasks_count": len(rec["tasks"]),
                "ready_tasks": [t.task_id for t in rec["ready_tasks"]],
                "unresolved_tasks": [t.task_id for t in rec["unresolved_tasks"]],
                "memories_count": len(rec["memory_ids"]),
                "handoffs_count": len(rec["handoffs"]),
            }
            text = (
                f"Session {s.session_id} [{s.status}]\n"
                f"Investigation: {s.investigation_id}\n"
                f"Tasks: {len(rec['tasks'])} (ready: {len(rec['ready_tasks'])}, unresolved: {len(rec['unresolved_tasks'])})\n"
                f"Memories: {len(rec['memory_ids'])}, Handoffs: {len(rec['handoffs'])}"
            )
            _emit(out, args.json, text)
            return 0
        elif args.session_action == "resume":
            orch = MultiAgentOrchestrator(conn)
            res = orch.resume_investigation(args.session_id, question=args.question)
            _emit(res, args.json, f"Resumed session {args.session_id}: status={res['status']}, executed {res['resumed_steps']} steps.")
            return 0 if res["status"] == "completed" else EXIT_INCOMPLETE
    return 0


def _cmd_audit(args) -> int:
    from money_trail.coordination.roles.auditor import EvidenceAuditorRole
    settings = load_settings(_resolve_env_file(args.env_file))
    configure_logging(settings.log_level)
    with FalkorDBConnection(settings) as conn:
        auditor = EvidenceAuditorRole(conn)
        rows = conn.graph.ro_query(
            "MATCH (i:Investigation {investigation_id: $inv_id})-[:HAS_FINDING]->(f:Finding) RETURN f.finding_id",
            {"inv_id": args.investigation_id},
        ).result_set
        fids = [r[0] for r in rows]
        res = [auditor.audit_finding(fid) for fid in fids]
        verified_count = sum(1 for r in res if r["verified"])
        out = {
            "investigation_id": args.investigation_id,
            "total_findings": len(res),
            "verified_count": verified_count,
            "rejected_count": len(res) - verified_count,
            "findings": res,
        }
        text = f"Audit of investigation {args.investigation_id}:\n" + (
            "\n".join(
                f"  Finding {r['finding_id']}: {'VERIFIED' if r['verified'] else 'REJECTED'} - {r['reason']}"
                for r in res
            )
            if res
            else "  No findings recorded for this investigation."
        )
        _emit(out, args.json, text)
        return 0 if verified_count == len(res) else 1


def _cmd_dashboard(args) -> int:
    import subprocess
    import sys
    from pathlib import Path

    app_path = Path(__file__).parent / "dashboard" / "app.py"
    cmd = [sys.executable, "-m", "streamlit", "run", str(app_path)]
    if getattr(args, "port", None):
        cmd.extend(["--server.port", str(args.port)])
    if getattr(args, "host", None):
        cmd.extend(["--server.address", str(args.host)])
    if getattr(args, "headless", False):
        cmd.append("--server.headless=true")
    try:
        return subprocess.call(cmd)
    except KeyboardInterrupt:
        return 0


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

    ag = sub.add_parser("agent", parents=[env], help="AI-assisted investigation (Phase 4); `money-trail investigate` stays the graph-only engine.")
    ags = ag.add_subparsers(dest="agent_action", required=True)

    def agent_common(sp):
        sp.add_argument("--agent-config", required=True, help="Agent configuration JSON (examples/agent.example.json).")
        return sp
    avc = agent_common(ags.add_parser("validate-config", parents=[after], help="Validate the agent configuration (offline)."))
    avc.add_argument("--check-credentials", action="store_true", help="Also report whether the API-key variable is set (never prints it).")
    avc.set_defaults(fn=_cmd_agent)
    atl = agent_common(ags.add_parser("tools", parents=[after], help="List the registered tools and their schemas (offline)."))
    atl.add_argument("--detection-config", required=True)
    atl.set_defaults(fn=_cmd_agent)
    ev = agent_common(ags.add_parser("evaluate", parents=[after], help="Live agent-quality evaluation against the real provider (needs a credential)."))
    ev.add_argument("--detection-config", required=True)
    ev.add_argument("--ground-truth", required=True, help="Generated ground_truth.json (used by the evaluator only).")
    ev.set_defaults(fn=_cmd_agent)
    ai = agent_common(ags.add_parser("investigate", parents=[after], help="Run the agent from a transaction or account."))
    ai.add_argument("--detection-config", required=True)
    ai.add_argument("--transaction-id")
    ai.add_argument("--account-id")
    ai.add_argument("--request-file", help="JSON investigation request (anchor_type, anchor_id, question, intent, ...).")
    ai.add_argument("--question", help="The investigative question (guidance only; the anchor decides what is investigated).")
    ai.add_argument("--intent", help="Structured intent; unsupported values are refused.")
    ai.add_argument("--namespace", help="Dataset namespace used to qualify a bare anchor id.")
    ai.add_argument("--persist", action="store_true", help="Persist verified findings and evidence (transaction anchors only).")
    ai.add_argument("--format", choices=("text", "json", "markdown"), default="text")
    ai.add_argument("--trace", action="store_true", help="Include the tool-call trace in text/markdown output.")
    ai.add_argument("--max-steps", type=int)
    ai.add_argument("--max-tool-calls", type=int)
    ai.add_argument("--max-duration-seconds", type=float)
    ai.set_defaults(fn=_cmd_agent)

    # Phase 5: Schema Migration and Version
    mig = sub.add_parser("migrate", parents=[env], help="Run additive schema migration to the latest version.")
    mig.set_defaults(fn=_cmd_migrate)

    sv = sub.add_parser("schema-version", parents=[env], help="Print currently applied schema version and migration history.")
    sv.set_defaults(fn=_cmd_schema_version)

    # Phase 5: Persistent Graph Memory
    mem = sub.add_parser("memory", parents=[env], help="Inspect persistent graph memory, provenance, history, and supersession.")
    mems = mem.add_subparsers(dest="memory_action", required=True)
    ml = mems.add_parser("list", parents=[after], help="List memories with scope isolation.")
    ml.add_argument("--scope", choices=("investigation", "dataset"), required=True)
    ml.add_argument("--dataset")
    ml.add_argument("--investigation-id")
    ml.add_argument("--type", choices=("episodic", "semantic"))
    ml.add_argument("--status", choices=("active", "superseded", "invalidated", "revalidation_needed"), default="active")
    ml.add_argument("--limit", type=int)
    ms = mems.add_parser("show", parents=[after], help="Show details of a specific memory.")
    ms.add_argument("memory_id")
    mp = mems.add_parser("provenance", parents=[after], help="Inspect grounding evidence chain for a memory.")
    mp.add_argument("memory_id")
    mh = mems.add_parser("history", parents=[after], help="Inspect supersession and contradiction history.")
    mh.add_argument("memory_id")
    mem.set_defaults(fn=_cmd_memory)

    # Phase 5: Task Coordination
    tsk = sub.add_parser("task", parents=[env], help="Inspect durable agent tasks, dependencies, and execution state.")
    tsks = tsk.add_subparsers(dest="task_action", required=True)
    tl = tsks.add_parser("list", parents=[after], help="List tasks in an investigation.")
    tl.add_argument("--investigation-id", required=True)
    tl.add_argument("--status")
    tl.add_argument("--role")
    ts = tsks.add_parser("show", parents=[after], help="Show task details.")
    ts.add_argument("task_id")
    tb = tsks.add_parser("blockers", parents=[after], help="View task dependency blockers.")
    tb.add_argument("task_id")
    tsk.set_defaults(fn=_cmd_task)

    # Phase 5: Agent Handoffs
    hnd = sub.add_parser("handoff", parents=[env], help="Inspect multi-agent handoffs between specialized roles.")
    hnds = hnd.add_subparsers(dest="handoff_action", required=True)
    hl = hnds.add_parser("list", parents=[after], help="List handoffs in an investigation.")
    hl.add_argument("--investigation-id", required=True)
    hs = hnds.add_parser("show", parents=[after], help="Show handoff details.")
    hs.add_argument("handoff_id")
    hnd.set_defaults(fn=_cmd_handoff)

    # Phase 5: Persistent Sessions and Resumption
    ses = sub.add_parser("session", parents=[env], help="Manage and inspect persistent investigation sessions.")
    sess = ses.add_subparsers(dest="session_action", required=True)
    sl = sess.add_parser("list", parents=[after], help="List sessions for an investigation.")
    sl.add_argument("--investigation-id", required=True)
    ss = sess.add_parser("show", parents=[after], help="Show session details.")
    ss.add_argument("session_id")
    sr = sess.add_parser("resume", parents=[after], help="Resume an interrupted investigation session from FalkorDB.")
    sr.add_argument("session_id")
    sr.add_argument("--question")
    ses.set_defaults(fn=_cmd_session)

    # Phase 5: Evidence Audit
    aud = sub.add_parser("audit", parents=[env], help="Audit agent participation and evidence grounding for an investigation.")
    aud.add_argument("investigation_id")
    aud.set_defaults(fn=_cmd_audit)

    # Phase 6: Interactive Dashboard
    dash = sub.add_parser("dashboard", parents=[env], help="Launch the Money Trail interactive Streamlit dashboard.")
    dash.add_argument("--port", type=int, default=8501, help="Port to bind the Streamlit server.")
    dash.add_argument("--host", default="localhost", help="Host address to bind.")
    dash.add_argument("--headless", action="store_true", help="Run in headless mode (no browser popup).")
    dash.set_defaults(fn=_cmd_dashboard)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except MoneyTrailError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
