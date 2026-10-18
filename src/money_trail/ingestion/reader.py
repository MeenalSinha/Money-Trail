"""Streaming CSV reader: maps source columns to canonical records and normalizes them."""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from money_trail.exceptions import SourceFormatError
from money_trail.ingestion.config import DatasetConfig, FileSpec
from money_trail.ingestion.contract import RecordError, clean_text, namespaced_id


@dataclass
class ParsedRow:
    entity: str
    index: int                    # 1-based data-record number (header excluded)
    line: int                     # physical line where the record ended
    record: Optional[Dict[str, Any]] = None   # canonical node properties (when valid)
    extra: Dict[str, str] = field(default_factory=dict)  # endpoints / references (canonical ids)
    digest: Optional[str] = None  # content hash over properties + endpoints
    errors: List[RecordError] = field(default_factory=list)
    raw: Any = None               # original source values, used only for quarantine files


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_file(cfg: DatasetConfig, spec: FileSpec) -> None:
    if not spec.path.is_file():
        raise SourceFormatError(f"{spec.entity} file not found: {spec.path.name}")
    if cfg.max_file_bytes is not None and spec.path.stat().st_size > cfg.max_file_bytes:
        raise SourceFormatError(f"{spec.entity} file exceeds ingestion.max_file_bytes.")


def _digest(record: Dict[str, Any], extra: Dict[str, str]) -> str:
    payload = json.dumps([record, extra], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _optional(cols: Dict[str, str], row: Dict[str, str], name: str) -> Optional[str]:
    return clean_text(row.get(cols[name]), name) if name in cols else None


def normalize(cfg: DatasetConfig, spec: FileSpec, row: Dict[str, str]):
    """Return (record, extra) or raise RecordError for the first problem found."""
    cols, ns = spec.columns, cfg.namespace
    get = lambda name: row.get(cols[name])
    if spec.entity == "accounts":
        rec = {"account_id": namespaced_id(get("account_id"), ns, "account_id"), "dataset": ns}
        if "created_at" in cols and (row.get(cols["created_at"]) or "").strip():
            rec["created_at"] = cfg.timestamp.to_epoch_ms(get("created_at"), "created_at")
        return rec, {}
    if spec.entity == "transactions":
        tid = namespaced_id(get("transaction_id"), ns, "transaction_id")
        src = namespaced_id(get("source_account"), ns, "source_account")
        dst = namespaced_id(get("destination_account"), ns, "destination_account")
        currency = cfg.fixed_currency or cfg.amounts.currency(get("currency"))
        rec = {
            "transaction_id": tid, "dataset": ns, "currency": currency,
            "amount": cfg.amounts.to_minor(get("amount"), currency),
            "timestamp": cfg.timestamp.to_epoch_ms(get("timestamp")),
        }
        for opt in ("channel", "reference"):
            v = _optional(cols, row, opt)
            if v is not None:
                rec[opt] = v
        return rec, {"source_account": src, "destination_account": dst}
    rec = {
        "alert_id": namespaced_id(get("alert_id"), ns, "alert_id"),
        "created_at": cfg.timestamp.to_epoch_ms(get("created_at"), "created_at"),
    }
    for opt in ("alert_type", "severity", "source", "description"):
        v = _optional(cols, row, opt)
        if v is not None:
            rec[opt] = v
    return rec, {"transaction_id": namespaced_id(get("transaction_id"), ns, "transaction_id")}


def read_rows(cfg: DatasetConfig, spec: FileSpec) -> Iterator[ParsedRow]:
    """Yield one ParsedRow per data record. Raises SourceFormatError for file-level problems."""
    check_file(cfg, spec)
    try:
        f = open(spec.path, "r", encoding=cfg.encoding, newline="")
    except OSError as exc:
        raise SourceFormatError(f"{spec.entity} file cannot be opened ({type(exc).__name__}).") from None
    with f:
        reader = csv.reader(f, delimiter=cfg.delimiter, strict=True)
        try:
            header = next(reader)
        except StopIteration:
            raise SourceFormatError(f"{spec.entity} file is empty (no header row).") from None
        except (csv.Error, UnicodeDecodeError) as exc:
            raise SourceFormatError(f"{spec.entity} header cannot be parsed ({type(exc).__name__}).") from None
        header = [h.strip().lstrip("\ufeff") for h in header]
        if len(set(header)) != len(header):
            raise SourceFormatError(f"{spec.entity} file has duplicate column headers.")
        missing = sorted(set(spec.columns.values()) - set(header))
        if missing:
            raise SourceFormatError(
                f"{spec.entity} file is missing mapped source columns: {', '.join(missing)}."
            )
        index = 0
        while True:
            try:
                values = next(reader)
            except StopIteration:
                return
            except csv.Error:
                index += 1
                yield ParsedRow(spec.entity, index, reader.line_num,
                                errors=[RecordError("MALFORMED_ROW", "*", "row is not valid CSV.")])
                continue
            except UnicodeDecodeError:
                raise SourceFormatError(f"{spec.entity} file is not valid {cfg.encoding}.") from None
            if not values:       # blank line
                continue
            index += 1
            if cfg.max_records_per_file is not None and index > cfg.max_records_per_file:
                raise SourceFormatError(f"{spec.entity} file exceeds ingestion.max_records_per_file.")
            row = ParsedRow(spec.entity, index, reader.line_num, raw=values)
            if len(values) != len(header):
                row.errors.append(RecordError("MALFORMED_ROW", "*", "row has the wrong number of fields."))
                yield row
                continue
            data = dict(zip(header, values))
            row.raw = data
            try:
                row.record, row.extra = normalize(cfg, spec, data)
                row.digest = _digest(row.record, row.extra)
            except RecordError as err:
                row.errors.append(err)
            yield row
