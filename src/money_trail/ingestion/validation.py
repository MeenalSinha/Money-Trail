"""Stage 1: file-only validation. Never touches the database.

Single streaming pass per file. Memory holds identifiers and content digests,
not whole records. Produces the set of record numbers that must not be written
(rejected rows and skipped duplicates) so the write pass can re-stream the file.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

from money_trail.exceptions import SourceFormatError
from money_trail.ingestion.config import DatasetConfig, FileSpec
from money_trail.ingestion.contract import RecordError
from money_trail.ingestion.reader import ParsedRow, read_rows

ENTITIES = ("accounts", "transactions", "alerts")


@dataclass
class EntityStats:
    examined: int = 0
    accepted: int = 0
    rejected: int = 0
    duplicates_identical: int = 0       # repeated record with identical content: skipped, not an error
    source_conflicts_skipped: int = 0   # conflicting repeat skipped under source_conflict_policy=keep_first

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class ValidationOutcome:
    stats: Dict[str, EntityStats] = field(default_factory=dict)
    skip: Dict[str, Set[int]] = field(default_factory=dict)
    error_counts: Counter = field(default_factory=Counter)  # (entity, code) -> n
    error_samples: List[dict] = field(default_factory=list)
    derived_accounts: Set[str] = field(default_factory=set)
    fatal: Optional[str] = None

    @property
    def error_total(self) -> int:
        return sum(self.error_counts.values())

    def summary_errors(self) -> List[dict]:
        return [{"entity": e, "code": c, "count": n} for (e, c), n in sorted(self.error_counts.items())]


class QuarantineWriter:
    """Appends rejected rows (raw source values plus reasons) to <dir>/<entity>.jsonl."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._files = {}
        self.paths: Dict[str, Path] = {}
        self.paths_opened: Set[str] = set()

    def write(self, row: ParsedRow) -> None:
        if row.entity not in self._files:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / f"{row.entity}.jsonl"
            self.paths[row.entity] = path
            mode = "a" if row.entity in self.paths_opened else "w"
            self.paths_opened.add(row.entity)
            self._files[row.entity] = open(path, mode, encoding="utf-8")
        self._files[row.entity].write(json.dumps({
            "entity": row.entity, "record_number": row.index, "line": row.line,
            "errors": [e.as_dict() for e in row.errors], "raw": row.raw,
        }, sort_keys=True) + "\n")

    def close(self) -> None:
        for f in self._files.values():
            f.close()
        self._files.clear()


def _reject(out: ValidationOutcome, cfg: DatasetConfig, row: ParsedRow, quarantine) -> None:
    out.stats[row.entity].rejected += 1
    out.skip[row.entity].add(row.index)
    for err in row.errors:
        out.error_counts[(row.entity, err.code)] += 1
        if len(out.error_samples) < cfg.max_reported_errors:
            out.error_samples.append({"entity": row.entity, "record_number": row.index, "line": row.line,
                                      **err.as_dict()})
    if quarantine is not None:
        quarantine.write(row)


def validate_dataset(cfg: DatasetConfig, quarantine: Optional[QuarantineWriter] = None) -> ValidationOutcome:
    out = ValidationOutcome()
    for e in ENTITIES:
        out.stats[e], out.skip[e] = EntityStats(), set()
    account_digest: Dict[str, str] = {}
    tx_digest: Dict[str, str] = {}
    alert_digest: Dict[str, str] = {}

    def duplicate(row: ParsedRow, seen: Dict[str, str], key: str) -> bool:
        """True if the row is a repeat that must not be written (already counted/rejected)."""
        if key not in seen:
            seen[key] = row.digest
            return False
        stats = out.stats[row.entity]
        if seen[key] == row.digest:
            stats.duplicates_identical += 1
            out.skip[row.entity].add(row.index)
        elif cfg.source_conflict_policy == "keep_first":
            stats.source_conflicts_skipped += 1
            out.skip[row.entity].add(row.index)
        else:
            row.errors.append(RecordError("DUPLICATE_CONFLICT", "*",
                                          "same identifier appears earlier with different content."))
            _reject(out, cfg, row, quarantine)
        return True

    try:
        if cfg.accounts:
            for row in read_rows(cfg, cfg.accounts):
                out.stats["accounts"].examined += 1
                if row.errors:
                    _reject(out, cfg, row, quarantine); continue
                if duplicate(row, account_digest, row.record["account_id"]):
                    continue
                out.stats["accounts"].accepted += 1

        for row in read_rows(cfg, cfg.transactions):
            out.stats["transactions"].examined += 1
            if not row.errors:
                for field_name in ("source_account", "destination_account"):
                    acc = row.extra[field_name]
                    if cfg.accounts_policy == "require_file" and acc not in account_digest:
                        row.errors.append(RecordError("UNKNOWN_ACCOUNT", field_name,
                                                      f"{field_name} is not an accepted account of this dataset."))
                        break
            if row.errors:
                _reject(out, cfg, row, quarantine); continue
            if duplicate(row, tx_digest, row.record["transaction_id"]):
                continue
            out.stats["transactions"].accepted += 1
            if cfg.accounts_policy == "derive_from_transactions":
                out.derived_accounts.add(row.extra["source_account"])
                out.derived_accounts.add(row.extra["destination_account"])

        if cfg.accounts_policy == "derive_from_transactions":
            out.stats["accounts"].examined = out.stats["accounts"].accepted = len(out.derived_accounts)

        if cfg.alerts:
            for row in read_rows(cfg, cfg.alerts):
                out.stats["alerts"].examined += 1
                if not row.errors and row.extra["transaction_id"] not in tx_digest:
                    row.errors.append(RecordError("UNKNOWN_TRANSACTION", "transaction_id",
                                                  "alert refers to a transaction that is not accepted in this dataset."))
                if row.errors:
                    _reject(out, cfg, row, quarantine); continue
                if duplicate(row, alert_digest, row.record["alert_id"]):
                    continue
                out.stats["alerts"].accepted += 1
    except SourceFormatError as exc:
        out.fatal = str(exc)
    return out


def iter_accepted(cfg: DatasetConfig, spec: FileSpec, outcome: ValidationOutcome) -> Iterator[ParsedRow]:
    """Re-stream a file, yielding only rows that passed validation and are not skipped."""
    skip = outcome.skip[spec.entity]
    for row in read_rows(cfg, spec):
        if row.index in skip:
            continue
        if row.errors:
            raise SourceFormatError(f"{spec.entity} source changed between validation and writing.")
        yield row
