"""Reproducible synthetic banking data with deliberately constructed patterns.

Everything is derived from the validated GeneratorConfig and its explicit seed.
Account and transaction identifiers are assigned AFTER shuffling/sorting so that
identifier order carries no information about which records belong to a pattern.
Ground truth is written to a separate file and never enters the graph.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from money_trail.exceptions import DatasetConfigError
from money_trail.generation.config import GeneratorConfig
from money_trail.ingestion.contract import format_epoch_ms_iso, namespaced_id

GENERATOR_VERSION = "1.0.0"
GROUND_TRUTH_VERSION = 1
FILES = ("accounts.csv", "transactions.csv", "alerts.csv", "ground_truth.json",
         "dataset_manifest.json", "ingest_config.json")


@dataclass
class _Tx:
    src: Any      # account handle
    dst: Any
    amount: int
    ts: int
    channel: str
    pattern: Optional[int] = None   # index into patterns list
    order: int = 0


@dataclass
class GeneratedData:
    accounts: List[Tuple[str, int]] = field(default_factory=list)          # (source id, opened_at_ms)
    transactions: List[Dict[str, Any]] = field(default_factory=list)
    alerts: List[Dict[str, Any]] = field(default_factory=list)
    ground_truth: Dict[str, Any] = field(default_factory=dict)


def portable_config(cfg: GeneratorConfig) -> Dict[str, Any]:
    """Configuration worth recording as provenance: machine-specific output paths are left out,
    so the same logical configuration yields byte-identical files wherever it is run."""
    return {k: v for k, v in cfg.raw.items() if k not in ("output_dir", "ingest_output_dir")}


def _minor_to_text(minor: int, exponent: int) -> str:
    if exponent == 0:
        return str(minor)
    q, r = divmod(minor, 10 ** exponent)
    return f"{q}.{r:0{exponent}d}"


def generate(cfg: GeneratorConfig) -> GeneratedData:
    rng = random.Random(cfg.seed)
    ordinary = [("o", i) for i in range(cfg.ordinary_accounts)]
    fresh_count = 0
    txs: List[_Tx] = []
    patterns: List[Dict[str, Any]] = []
    window_end = cfg.window_start_ms + cfg.window_ms

    def new_fresh():
        nonlocal fresh_count
        fresh_count += 1
        return ("p", fresh_count - 1)

    def pick_accounts(k: int, shared_pct: int) -> List[Any]:
        chosen, used = [], set()
        for _ in range(k):
            if rng.randrange(100) < shared_pct:
                h = ordinary[rng.randrange(len(ordinary))]
                if h not in used:
                    chosen.append(h); used.add(h); continue
            chosen.append(new_fresh())
        return chosen

    def start_time(max_span_ms: int, label: str) -> int:
        latest = window_end - max_span_ms
        if latest < cfg.window_start_ms:
            raise DatasetConfigError(f"time_window is too short for the configured {label} delays.")
        return rng.randint(cfg.window_start_ms, latest)

    def jitter() -> int:
        return rng.randint(0, 999)

    for spec in cfg.patterns:
        P = spec.params
        for _ in range(spec.count):
            idx = len(patterns)
            amin, amax = P["amount_minor"]
            dmin, dmax = P["delay_seconds"]
            shared = P["shared_account_fraction_percent"]
            members: List[_Tx] = []
            roles: Dict[str, Any] = {}
            if spec.type == "circular":
                n = rng.randint(*P["cycle_length"])
                accts = pick_accounts(n, shared)
                amt, t = rng.randint(amin, amax), start_time(n * (dmax * 1000 + 999), "circular")
                for i in range(n):
                    members.append(_Tx(accts[i], accts[(i + 1) % n], amt, t, "", idx))
                    amt = max(1, amt * P["retention_bps"] // 10000)
                    t += rng.randint(dmin, dmax) * 1000 + jitter()
                roles["cycle_accounts"] = accts
            elif spec.type == "pass_through":
                hops = rng.randint(*P["hops"])
                accts = pick_accounts(hops + 1, shared)
                amt, t = rng.randint(amin, amax), start_time(hops * (dmax * 1000 + 999), "pass_through")
                for i in range(hops):
                    members.append(_Tx(accts[i], accts[i + 1], amt, t, "", idx))
                    amt = max(1, amt * P["retention_bps"] // 10000)
                    t += rng.randint(dmin, dmax) * 1000 + jitter()
                roles.update(origin=accts[0], mule_accounts=accts[1:-1], destination=accts[-1])
            elif spec.type == "fan_in":
                n = rng.randint(*P["sources"])
                accts = pick_accounts(n + 1, shared)
                t0 = start_time(dmax * 1000 + 999, "fan_in")
                for s in accts[:-1]:
                    members.append(_Tx(s, accts[-1], rng.randint(amin, amax),
                                       t0 + rng.randint(dmin, dmax) * 1000 + jitter(), "", idx))
                roles.update(collector=accts[-1], sources=accts[:-1])
            else:  # fan_out
                n = rng.randint(*P["recipients"])
                accts = pick_accounts(n + 1, shared)
                t0 = start_time(dmax * 1000 + 999, "fan_out")
                for r in accts[1:]:
                    members.append(_Tx(accts[0], r, rng.randint(amin, amax),
                                       t0 + rng.randint(dmin, dmax) * 1000 + jitter(), "", idx))
                roles.update(distributor=accts[0], recipients=accts[1:])
            for m in members:
                m.channel = cfg.channels[rng.randrange(len(cfg.channels))]
            txs.extend(members)
            patterns.append({"type": spec.type, "members": members, "roles": roles})

    for _ in range(cfg.ordinary_transactions):
        a = rng.randrange(len(ordinary))
        b = rng.randrange(len(ordinary) - 1)
        b += b >= a  # distinct endpoints
        txs.append(_Tx(ordinary[a], ordinary[b], rng.randint(*cfg.amount_minor),
                       rng.randint(cfg.window_start_ms, window_end - 1),
                       cfg.channels[rng.randrange(len(cfg.channels))], None))

    # --- identifiers are assigned after shuffling so ids reveal nothing about membership
    handles = ordinary + [("p", i) for i in range(fresh_count)]
    rng.shuffle(handles)
    acc_id = {h: f"{cfg.account_prefix}{n:08d}" for n, h in enumerate(handles, 1)}
    for i, t in enumerate(txs):
        t.order = i
    txs.sort(key=lambda t: (t.ts, t.order))
    tx_id = {id(t): f"{cfg.transaction_prefix}{n:09d}" for n, t in enumerate(txs, 1)}

    data = GeneratedData()
    for h in sorted(handles, key=lambda x: acc_id[x]):
        opened = max(0, cfg.window_start_ms - rng.randint(1, 3650) * 86_400_000)
        data.accounts.append((acc_id[h], opened))
    for t in txs:
        data.transactions.append({
            "id": tx_id[id(t)], "src": acc_id[t.src], "dst": acc_id[t.dst], "amount": t.amount,
            "ts": t.ts, "channel": t.channel,
        })

    # --- seed alerts: noisy by design; contents carry no pattern information
    pattern_txs = [t for t in txs if t.pattern is not None]
    ordinary_txs = [t for t in txs if t.pattern is None]
    if cfg.seed_alerts_on_pattern > len(pattern_txs) or cfg.seed_alerts_on_ordinary > len(ordinary_txs):
        raise DatasetConfigError("seed_alerts requests more alerts than there are eligible transactions.")
    chosen = rng.sample(pattern_txs, cfg.seed_alerts_on_pattern) + rng.sample(ordinary_txs, cfg.seed_alerts_on_ordinary)
    rng.shuffle(chosen)
    for n, t in enumerate(chosen, 1):
        data.alerts.append({"id": f"{cfg.alert_prefix}{n:07d}", "tx": tx_id[id(t)],
                            "ts": t.ts + rng.randint(0, 3600) * 1000})

    # --- ground truth (canonical, namespaced identifiers)
    ns = cfg.namespace
    cid = lambda raw, f: namespaced_id(raw, ns, f)
    gt_patterns = []
    for i, p in enumerate(patterns):
        def conv(v):
            return [cid(acc_id[x], "account") for x in v] if isinstance(v, list) else cid(acc_id[v], "account")
        accounts = sorted({cid(acc_id[x], "account") for m in p["members"] for x in (m.src, m.dst)})
        gt_patterns.append({
            "pattern_id": f"{ns}:pattern:{i + 1:06d}",
            "type": p["type"],
            "account_ids": accounts,
            "transaction_ids": [cid(tx_id[id(m)], "transaction") for m in sorted(p["members"], key=lambda m: (m.ts, m.order))],
            "roles": {k: conv(v) for k, v in p["roles"].items()},
        })
    gt_alerts = {"alerts_on_pattern_transactions": sum(1 for a in data.alerts
                                                       if any(a["tx"] == tx_id[id(m)] for p in patterns for m in p["members"]))}
    data.ground_truth = {
        "format_version": GROUND_TRUTH_VERSION,
        "namespace": ns,
        "identifier_format": "canonical '<namespace>:<source id>' as stored in the graph",
        "evaluation_only": "Never load into the investigation graph or expose to the investigation agent.",
        "generator": {"version": GENERATOR_VERSION, "seed": cfg.seed},
        "configuration": portable_config(cfg),
        "patterns": gt_patterns,
        "ordinary_note": "Transactions not listed in any pattern are ordinary random activity; random activity "
                         "can incidentally form structures that resemble patterns and is not labelled.",
        "seed_alert_summary": gt_alerts,
    }
    return data


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, path)


def write_dataset(cfg: GeneratorConfig, data: GeneratedData, *, overwrite: bool = False) -> Dict[str, Any]:
    out = cfg.output_dir / cfg.namespace
    if out.exists() and any(out.iterdir()):
        if not overwrite:
            raise DatasetConfigError(f"Output directory {out} already contains files; pass --overwrite to replace "
                                     "the generated files (other files are left untouched).")
    out.mkdir(parents=True, exist_ok=True)
    exp, fmt = cfg.currency_exponent, cfg.amount_format

    def csv_text(header, rows) -> str:
        import io
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
        return buf.getvalue()

    _write_text(out / "accounts.csv", csv_text(
        ["account_id", "opened_at"], [(a, format_epoch_ms_iso(t)) for a, t in data.accounts]))
    _write_text(out / "transactions.csv", csv_text(
        ["txn_id", "from_account", "to_account", "amount", "currency", "booked_at", "channel"],
        [(t["id"], t["src"], t["dst"], _minor_to_text(t["amount"], exp) if fmt == "major" else t["amount"],
          cfg.currency, format_epoch_ms_iso(t["ts"]), t["channel"]) for t in data.transactions]))
    _write_text(out / "alerts.csv", csv_text(
        ["alert_id", "txn_id", "raised_at", "alert_type", "severity", "source"],
        [(a["id"], a["tx"], format_epoch_ms_iso(a["ts"]), cfg.alert_type, cfg.alert_severity, "seed")
         for a in data.alerts]))
    _write_text(out / "ground_truth.json", json.dumps(data.ground_truth, indent=2, sort_keys=True) + "\n")

    rel = lambda p: os.path.relpath(p, out)
    ingest_cfg = {
        "dataset": {"namespace": cfg.namespace,
                    "source": {"name": "money_trail.generation", "version": GENERATOR_VERSION,
                               "description": "Synthetic data; no real financial records."}},
        "format": "csv", "csv": {"delimiter": ",", "encoding": "utf-8"},
        "files": {
            "accounts": {"path": "accounts.csv", "columns": {"account_id": "account_id", "created_at": "opened_at"}},
            "transactions": {"path": "transactions.csv", "columns": {
                "transaction_id": "txn_id", "source_account": "from_account", "destination_account": "to_account",
                "amount": "amount", "currency": "currency", "timestamp": "booked_at", "channel": "channel"}},
            "alerts": {"path": "alerts.csv", "columns": {
                "alert_id": "alert_id", "transaction_id": "txn_id", "created_at": "raised_at",
                "alert_type": "alert_type", "severity": "severity", "source": "source"}},
        },
        "normalization": {"timestamp": {"format": "iso8601", "timezone": None},
                          "amount": {"unit": fmt, "currencies": {cfg.currency: exp}, "fixed_currency": None}},
        "ingestion": {"batch_size": 500, "invalid_policy": "strict", "source_conflict_policy": "reject",
                      "graph_conflict_policy": "reject", "accounts_policy": "require_file",
                      "max_records_per_file": None, "max_file_bytes": None, "max_reported_errors": 50},
        "output": {"quarantine_dir": rel(cfg.ingest_output_dir / cfg.namespace / "quarantine"),
                   "report_dir": rel(cfg.ingest_output_dir / cfg.namespace / "reports")},
    }
    _write_text(out / "ingest_config.json", json.dumps(ingest_cfg, indent=2) + "\n")

    redacted = {k: v for k, v in portable_config(cfg).items() if k not in ("patterns", "seed_alerts")}
    manifest = {
        "dataset_namespace": cfg.namespace,
        "source": "money_trail.generation (synthetic)",
        "generator_version": GENERATOR_VERSION,
        "random_seed": cfg.seed,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "record_counts": {"accounts": len(data.accounts), "transactions": len(data.transactions),
                          "alerts": len(data.alerts)},
        "configuration": redacted,
        "configuration_redacted_sections": ["patterns", "seed_alerts"],
        "files": {n: {"sha256": _sha256(out / n)} for n in
                  ("accounts.csv", "transactions.csv", "alerts.csv", "ground_truth.json", "ingest_config.json")},
        "ground_truth": {"file": "ground_truth.json", "format_version": GROUND_TRUTH_VERSION,
                         "note": "Evaluation labels live only in this file; contents are not summarized here."},
        "known_limitations": [
            "Synthetic data; amounts are uniform random within configured ranges, not realistic distributions.",
            "Random activity can incidentally resemble patterns; only deliberate instances are labelled.",
            "All records of the dataset fit in memory during generation; intended for development-scale datasets.",
        ],
    }
    _write_text(out / "dataset_manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest
