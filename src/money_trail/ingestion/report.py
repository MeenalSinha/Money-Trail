"""Ingestion reports: structured JSON (machine) plus a text summary (human). No secrets, no full records."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from money_trail.exceptions import DatasetConfigError

REPORT_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_report(cfg, run_id: str, graph_name: str) -> Dict[str, Any]:
    return {
        "report_version": REPORT_VERSION,
        "run_id": run_id,
        "dataset_namespace": cfg.namespace,
        "source": {"name": cfg.source_name, "version": cfg.source_version, "description": cfg.source_description},
        "config_fingerprint": cfg.fingerprint,
        "policies": {
            "invalid_policy": cfg.invalid_policy, "source_conflict_policy": cfg.source_conflict_policy,
            "graph_conflict_policy": cfg.graph_conflict_policy, "accounts_policy": cfg.accounts_policy,
            "batch_size": cfg.batch_size,
        },
        "graph_name": graph_name,
        "started_at": utc_now(), "completed_at": None,
        "status": "running", "failed_stage": None, "error": None,
        "writes_occurred": False,
        "files": {}, "records": {},
        "graph": {e: {"new": 0, "unchanged": 0, "updated": 0, "conflicting_rejected": 0, "conflicting_skipped": 0}
                  for e in ("accounts", "transactions", "alerts")},
        "accounts_derived": 0,
        "relationships": {r: {"created": 0, "already_present": 0} for r in ("SENT", "RECEIVED_BY", "FLAGS")},
        "batches": {"succeeded": 0, "failed": 0},
        "validation_errors": {"total": 0, "by_code": [], "samples": []},
        "quarantine": {"directory": None, "files": {}},
        "graph_conflicts": 0,
        "failure_detail": None,
        "verification": None,
        "graph_totals": {"before": None, "after": None},
    }


def finish(report: Dict[str, Any], status: str, **kw) -> Dict[str, Any]:
    report["status"] = status
    report["completed_at"] = utc_now()
    report.update(kw)
    return report


def write_report(cfg, report: Dict[str, Any]) -> Path:
    cfg.report_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.report_dir / f"{report['run_id']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return path


def list_reports(directory: Path) -> List[Dict[str, Any]]:
    rows = []
    for p in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            rows.append({"run_id": r["run_id"], "namespace": r["dataset_namespace"],
                         "status": r["status"], "started_at": r["started_at"]})
        except (ValueError, KeyError):
            continue
    return sorted(rows, key=lambda r: r["started_at"])


def load_report(directory: Path, run_id: str) -> Dict[str, Any]:
    if not run_id.replace("-", "").isalnum():
        raise DatasetConfigError("run id may contain only letters, digits and '-'.")
    path = directory / f"{run_id}.json"
    if not path.is_file():
        raise DatasetConfigError(f"No report for run id {run_id} in {directory}.")
    return json.loads(path.read_text(encoding="utf-8"))


def render_summary(r: Dict[str, Any]) -> str:
    L = [f"Ingestion run {r['run_id']}",
         f"  dataset namespace : {r['dataset_namespace']}  (source: {r['source']['name']})",
         f"  graph             : {r['graph_name']}",
         f"  status            : {r['status']}" + (f"  (failed stage: {r['failed_stage']})" if r["failed_stage"] else ""),
         f"  writes occurred   : {r['writes_occurred']}"]
    if r["error"]:
        L.append(f"  error             : {r['error']}")
    for e, s in r["records"].items():
        L.append(f"  {e:<13} examined {s['examined']}, accepted {s['accepted']}, rejected {s['rejected']}, "
                 f"identical repeats {s['duplicates_identical']}, conflicting repeats skipped "
                 f"{s['source_conflicts_skipped']}")
    for e, g in r["graph"].items():
        L.append(f"  graph {e:<13} new {g['new']}, unchanged {g['unchanged']}, updated {g['updated']}, "
                 f"conflicting (rejected {g['conflicting_rejected']}, skipped {g['conflicting_skipped']})")
    rel = r["relationships"]
    L.append("  relationships     : " + ", ".join(f"{k} created {v['created']} / existing {v['already_present']}"
                                                  for k, v in rel.items()))
    L.append(f"  batches           : {r['batches']['succeeded']} succeeded, {r['batches']['failed']} failed")
    if r["accounts_derived"]:
        L.append(f"  accounts derived from transactions: {r['accounts_derived']}")
    ve = r["validation_errors"]
    if ve["total"]:
        L.append(f"  validation errors : {ve['total']} (" + ", ".join(
            f"{x['entity']}:{x['code']}={x['count']}" for x in ve["by_code"]) + ")")
    if r["graph_conflicts"]:
        L.append(f"  graph conflicts   : {r['graph_conflicts']}")
    if r["quarantine"]["directory"]:
        L.append(f"  quarantine        : {r['quarantine']['directory']}")
    if r["failure_detail"]:
        L.append(f"  failure detail    : {json.dumps(r['failure_detail'], sort_keys=True)}")
    v = r["verification"]
    if v:
        L.append(f"  verification      : {'PASSED' if v['passed'] else 'FAILED'} "
                 f"{json.dumps(v['checks'], sort_keys=True)}")
    t = r["graph_totals"]
    if t["before"] is not None and t["after"] is not None:
        L.append(f"  namespace totals  : before {t['before']} -> after {t['after']}")
    return "\n".join(L)
