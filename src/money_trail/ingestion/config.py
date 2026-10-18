"""Dataset (ingestion) configuration: a validated JSON file describing one source.

Nothing is defaulted silently: namespace, mappings, normalization, policies and
output directories must be stated. Relative paths resolve against the config file.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from money_trail.exceptions import DatasetConfigError
from money_trail.ingestion.contract import (
    AmountConverter, TimestampParser, parse_timezone, validate_namespace, CURRENCY_RE,
)

INVALID_POLICIES = ("strict", "quarantine")
SOURCE_CONFLICT_POLICIES = ("reject", "keep_first")
GRAPH_CONFLICT_POLICIES = ("reject", "skip", "update")
ACCOUNT_POLICIES = ("require_file", "derive_from_transactions")
TIMESTAMP_FORMATS = ("iso8601", "epoch_ms", "epoch_s", "strptime")

REQUIRED_COLUMNS = {
    "accounts": ("account_id",),
    "transactions": ("transaction_id", "source_account", "destination_account", "amount", "timestamp"),
    "alerts": ("alert_id", "transaction_id", "created_at"),
}
OPTIONAL_COLUMNS = {
    "accounts": ("created_at",),
    "transactions": ("currency", "channel", "reference"),
    "alerts": ("alert_type", "severity", "source", "description"),
}


@dataclass(frozen=True)
class FileSpec:
    entity: str
    path: Path
    columns: Mapping[str, str]  # canonical field -> source column header


@dataclass(frozen=True)
class DatasetConfig:
    namespace: str
    source_name: str
    source_version: Optional[str]
    source_description: Optional[str]
    config_path: Path
    delimiter: str
    encoding: str
    accounts: Optional[FileSpec]
    transactions: FileSpec
    alerts: Optional[FileSpec]
    timestamp: TimestampParser
    amounts: AmountConverter
    fixed_currency: Optional[str]
    batch_size: int
    invalid_policy: str
    source_conflict_policy: str
    graph_conflict_policy: str
    accounts_policy: str
    max_records_per_file: Optional[int]
    max_file_bytes: Optional[int]
    max_reported_errors: int
    quarantine_dir: Path
    report_dir: Path
    fingerprint: str  # sha256 of the canonical JSON of the config file

    def files(self):
        return [f for f in (self.accounts, self.transactions, self.alerts) if f is not None]


def _need(d: Mapping, key: str, where: str, types=None):
    if not isinstance(d, dict) or key not in d or d[key] is None:
        raise DatasetConfigError(f"{where}.{key} is required.")
    if types and not isinstance(d[key], types):
        raise DatasetConfigError(f"{where}.{key} has the wrong type.")
    return d[key]


def _no_extra(d: Mapping, allowed, where: str) -> None:
    extra = sorted(set(d) - set(allowed))
    if extra:
        raise DatasetConfigError(f"{where} has unknown keys: {', '.join(extra)}.")


def _opt_pos_int(d: Mapping, key: str, where: str) -> Optional[int]:
    v = d.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise DatasetConfigError(f"{where}.{key} must be a positive integer or null.")
    return v


def _choice(d: Mapping, key: str, options, where: str) -> str:
    v = _need(d, key, where, str)
    if v not in options:
        raise DatasetConfigError(f"{where}.{key} must be one of: {', '.join(options)}.")
    return v


def _file_spec(entity: str, raw: Mapping, base: Path, fixed_currency: Optional[str]) -> FileSpec:
    where = f"files.{entity}"
    _no_extra(raw, ("path", "columns"), where)
    path = Path(_need(raw, "path", where, str))
    path = path if path.is_absolute() else (base / path)
    cols = _need(raw, "columns", where, dict)
    allowed = REQUIRED_COLUMNS[entity] + OPTIONAL_COLUMNS[entity]
    _no_extra(cols, allowed, f"{where}.columns")
    required = [c for c in REQUIRED_COLUMNS[entity]]
    if entity == "transactions" and fixed_currency is None:
        required.append("currency")
    missing = [c for c in required if not isinstance(cols.get(c), str) or not cols[c].strip()]
    if missing:
        raise DatasetConfigError(f"{where}.columns must map: {', '.join(missing)}.")
    if entity == "transactions" and fixed_currency is not None and "currency" in cols:
        raise DatasetConfigError("transactions maps a currency column AND normalization.amount.fixed_currency "
                                 "is set; choose one.")
    for k, v in cols.items():
        if not isinstance(v, str) or not v.strip():
            raise DatasetConfigError(f"{where}.columns.{k} must be a non-empty column name.")
    values = list(cols.values())
    if len(set(values)) != len(values):
        raise DatasetConfigError(f"{where}.columns maps one source column to several canonical fields.")
    return FileSpec(entity, path, dict(cols))


def parse_dataset_config(raw: Mapping[str, Any], config_path: Path) -> DatasetConfig:
    base = config_path.parent
    _no_extra(raw, ("dataset", "format", "csv", "files", "normalization", "ingestion", "output"), "config")

    ds = _need(raw, "dataset", "config", dict)
    _no_extra(ds, ("namespace", "source"), "dataset")
    namespace = validate_namespace(_need(ds, "namespace", "dataset", str))
    src = _need(ds, "source", "dataset", dict)
    _no_extra(src, ("name", "version", "description"), "dataset.source")
    source_name = _need(src, "name", "dataset.source", str)

    if _need(raw, "format", "config", str) != "csv":
        raise DatasetConfigError("format must be 'csv' (the only supported format).")
    csv_cfg = _need(raw, "csv", "config", dict)
    _no_extra(csv_cfg, ("delimiter", "encoding"), "csv")
    delimiter = _need(csv_cfg, "delimiter", "csv", str)
    if len(delimiter) != 1 or delimiter in "\r\n\"":
        raise DatasetConfigError("csv.delimiter must be a single character other than quote or newline.")
    encoding = _need(csv_cfg, "encoding", "csv", str)
    try:
        "".encode(encoding)
    except LookupError:
        raise DatasetConfigError("csv.encoding is not a known encoding.") from None

    norm = _need(raw, "normalization", "config", dict)
    _no_extra(norm, ("timestamp", "amount"), "normalization")
    ts = _need(norm, "timestamp", "normalization", dict)
    _no_extra(ts, ("format", "pattern", "timezone"), "normalization.timestamp")
    fmt = _choice(ts, "format", TIMESTAMP_FORMATS, "normalization.timestamp")
    pattern = ts.get("pattern")
    if fmt == "strptime" and not (isinstance(pattern, str) and pattern):
        raise DatasetConfigError("normalization.timestamp.pattern is required when format is 'strptime'.")
    if fmt != "strptime" and pattern is not None:
        raise DatasetConfigError("normalization.timestamp.pattern is only valid with format 'strptime'.")
    tz = ts.get("timezone")
    if tz is not None and not isinstance(tz, str):
        raise DatasetConfigError("normalization.timestamp.timezone must be a string or null.")
    timestamp = TimestampParser(fmt, pattern, parse_timezone(tz))

    am = _need(norm, "amount", "normalization", dict)
    _no_extra(am, ("unit", "currencies", "fixed_currency"), "normalization.amount")
    unit = _choice(am, "unit", ("minor", "major"), "normalization.amount")
    cur = _need(am, "currencies", "normalization.amount", dict)
    if not cur:
        raise DatasetConfigError("normalization.amount.currencies must list at least one currency.")
    for code, exp in cur.items():
        if not CURRENCY_RE.match(code) or isinstance(exp, bool) or not isinstance(exp, int) or not (0 <= exp <= 6):
            raise DatasetConfigError("normalization.amount.currencies must map 3-letter upper-case codes "
                                     "to minor-unit decimal places between 0 and 6.")
    fixed = am.get("fixed_currency")
    if fixed is not None and fixed not in cur:
        raise DatasetConfigError("normalization.amount.fixed_currency must be a key of currencies.")
    amounts = AmountConverter(unit, dict(cur))

    files = _need(raw, "files", "config", dict)
    _no_extra(files, ("accounts", "transactions", "alerts"), "files")
    transactions = _file_spec("transactions", _need(files, "transactions", "files", dict), base, fixed)
    accounts = _file_spec("accounts", files["accounts"], base, fixed) if files.get("accounts") else None
    alerts = _file_spec("alerts", files["alerts"], base, fixed) if files.get("alerts") else None

    ing = _need(raw, "ingestion", "config", dict)
    _no_extra(ing, ("batch_size", "invalid_policy", "source_conflict_policy", "graph_conflict_policy",
                    "accounts_policy", "max_records_per_file", "max_file_bytes", "max_reported_errors"), "ingestion")
    batch = _need(ing, "batch_size", "ingestion", int)
    if isinstance(batch, bool) or not (1 <= batch <= 10000):
        raise DatasetConfigError("ingestion.batch_size must be between 1 and 10000.")
    accounts_policy = _choice(ing, "accounts_policy", ACCOUNT_POLICIES, "ingestion")
    if accounts_policy == "require_file" and accounts is None:
        raise DatasetConfigError("ingestion.accounts_policy 'require_file' needs files.accounts. "
                                 "Set 'derive_from_transactions' to derive accounts explicitly.")
    if accounts_policy == "derive_from_transactions" and accounts is not None:
        raise DatasetConfigError("files.accounts is configured but accounts_policy is 'derive_from_transactions'.")
    max_err = _need(ing, "max_reported_errors", "ingestion", int)
    if isinstance(max_err, bool) or not (0 <= max_err <= 1000):
        raise DatasetConfigError("ingestion.max_reported_errors must be between 0 and 1000.")

    out = _need(raw, "output", "config", dict)
    _no_extra(out, ("quarantine_dir", "report_dir"), "output")
    def _dir(key):
        p = Path(_need(out, key, "output", str))
        return p if p.is_absolute() else base / p

    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    return DatasetConfig(
        namespace=namespace, source_name=source_name,
        source_version=src.get("version"), source_description=src.get("description"),
        config_path=config_path, delimiter=delimiter, encoding=encoding,
        accounts=accounts, transactions=transactions, alerts=alerts,
        timestamp=timestamp, amounts=amounts, fixed_currency=fixed,
        batch_size=batch,
        invalid_policy=_choice(ing, "invalid_policy", INVALID_POLICIES, "ingestion"),
        source_conflict_policy=_choice(ing, "source_conflict_policy", SOURCE_CONFLICT_POLICIES, "ingestion"),
        graph_conflict_policy=_choice(ing, "graph_conflict_policy", GRAPH_CONFLICT_POLICIES, "ingestion"),
        accounts_policy=accounts_policy,
        max_records_per_file=_opt_pos_int(ing, "max_records_per_file", "ingestion"),
        max_file_bytes=_opt_pos_int(ing, "max_file_bytes", "ingestion"),
        max_reported_errors=max_err,
        quarantine_dir=_dir("quarantine_dir"), report_dir=_dir("report_dir"),
        fingerprint=hashlib.sha256(canonical).hexdigest(),
    )


def load_dataset_config(path) -> DatasetConfig:
    p = Path(path)
    if not p.is_file():
        raise DatasetConfigError(f"Dataset config not found: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DatasetConfigError(f"Dataset config is not valid JSON ({type(exc).__name__}).") from None
    if not isinstance(raw, dict):
        raise DatasetConfigError("Dataset config must be a JSON object.")
    return parse_dataset_config(raw, p.resolve())
