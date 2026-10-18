"""Canonical data contract: identifier namespacing, amount and timestamp normalization.

Canonical form (matches the Phase 1 schema):
  * identifiers: "<namespace>:<source id>" strings
  * amount: non-negative integer minor units (exact decimal arithmetic, no floats)
  * currency: upper-case code from the dataset's configured currency table
  * timestamp: integer epoch milliseconds, UTC
Nothing here guesses: ambiguous or lossy input is rejected with a RecordError.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, Context, Inexact
from typing import Mapping, Optional

from money_trail.exceptions import DatasetConfigError

NAMESPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
MAX_ID_LENGTH = 200      # keeps identity index entries small
MAX_TEXT_LENGTH = 2000   # bound on free-text fields
INT64_MAX = 2**63 - 1
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class RecordError(Exception):
    """A single source value failed normalization. Messages never contain the value."""

    def __init__(self, code: str, field: str, message: str) -> None:
        super().__init__(message)
        self.code, self.field, self.message = code, field, message

    def as_dict(self) -> dict:
        return {"code": self.code, "field": self.field, "message": self.message}


def validate_namespace(namespace: str) -> str:
    if not isinstance(namespace, str) or not NAMESPACE_RE.match(namespace):
        raise DatasetConfigError(
            "dataset.namespace must start with a letter or digit and contain only letters, digits, "
            "'_', '.', '-' (max 64 characters, no ':')."
        )
    return namespace


def clean_text(raw, field: str, required: bool = False) -> Optional[str]:
    if raw is None or not str(raw).strip():
        if required:
            raise RecordError("MISSING_VALUE", field, f"{field} is required but empty.")
        return None
    value = str(raw).strip()
    if _CONTROL.search(value):
        raise RecordError("INVALID_VALUE", field, f"{field} contains control characters.")
    if len(value) > MAX_TEXT_LENGTH:
        raise RecordError("INVALID_VALUE", field, f"{field} is longer than {MAX_TEXT_LENGTH} characters.")
    return value


def namespaced_id(raw, namespace: str, field: str) -> str:
    """Return "<namespace>:<id>". A value already carrying "<namespace>:" is not prefixed twice."""
    value = clean_text(raw, field, required=True)
    prefix = namespace + ":"
    if value.startswith(prefix):
        value = value[len(prefix):]
        if not value.strip():
            raise RecordError("INVALID_IDENTIFIER", field, f"{field} has a namespace prefix but no identifier.")
    if len(value) + len(prefix) > MAX_ID_LENGTH:
        raise RecordError("INVALID_IDENTIFIER", field, f"{field} exceeds {MAX_ID_LENGTH} characters.")
    return prefix + value


@dataclass(frozen=True)
class AmountConverter:
    unit: str                      # "minor" or "major"
    currencies: Mapping[str, int]  # currency code -> number of minor-unit decimal places

    _MINOR = re.compile(r"^[0-9]{1,19}$")
    _MAJOR = re.compile(r"^[0-9]{1,19}(\.[0-9]{1,18})?$")

    def currency(self, raw, field: str = "currency") -> str:
        value = clean_text(raw, field, required=True).upper()
        if not CURRENCY_RE.match(value) or value not in self.currencies:
            raise RecordError("UNSUPPORTED_CURRENCY", field, "currency is not in the configured currency table.")
        return value

    def to_minor(self, raw, currency: str, field: str = "amount") -> int:
        text = clean_text(raw, field, required=True)
        exponent = self.currencies[currency]
        if self.unit == "minor":
            if not self._MINOR.match(text):
                raise RecordError("INVALID_AMOUNT", field, "amount must be a non-negative integer in minor units.")
            value = int(text)
        else:
            if not self._MAJOR.match(text):
                raise RecordError("INVALID_AMOUNT", field, "amount must be a non-negative decimal number.")
            ctx = Context(prec=60, traps=[Inexact])
            d = Decimal(text)
            if d.as_tuple().exponent < -exponent:
                raise RecordError("INVALID_AMOUNT", field,
                                  f"amount has more than {exponent} decimal places for its currency; "
                                  "rounding is never applied.")
            value = int(ctx.scaleb(d, Decimal(exponent)))
        if value > INT64_MAX:
            raise RecordError("INVALID_AMOUNT", field, "amount exceeds the 64-bit integer range.")
        return value


def parse_timezone(spec: Optional[str]) -> Optional[tzinfo]:
    if spec is None:
        return None
    if spec.upper() in ("UTC", "Z"):
        return timezone.utc
    m = re.match(r"^([+-])(\d{2}):(\d{2})$", spec)
    if m:
        delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
        return timezone(delta if m.group(1) == "+" else -delta)
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(spec)
    except Exception:
        raise DatasetConfigError(
            "timestamp.timezone must be 'UTC', a fixed offset like '+05:30', or a valid IANA zone name."
        ) from None


@dataclass(frozen=True)
class TimestampParser:
    fmt: str                       # iso8601 | epoch_ms | epoch_s | strptime
    pattern: Optional[str] = None  # strptime pattern
    tz: Optional[tzinfo] = None    # interpretation for values WITHOUT an offset

    def to_epoch_ms(self, raw, field: str = "timestamp") -> int:
        text = clean_text(raw, field, required=True)
        if self.fmt in ("epoch_ms", "epoch_s"):
            if not re.match(r"^[0-9]{1,16}$", text):
                raise RecordError("INVALID_TIMESTAMP", field, "epoch timestamp must be a non-negative integer.")
            ms = int(text) * (1000 if self.fmt == "epoch_s" else 1)
        else:
            dt = self._parse(text, field)
            if dt.tzinfo is None:
                if self.tz is None:
                    raise RecordError("AMBIGUOUS_TIMESTAMP", field,
                                      "timestamp has no UTC offset and no timezone is configured.")
                dt = self._localize(dt, field)
            delta = dt.astimezone(timezone.utc) - _EPOCH
            if delta.microseconds % 1000:
                raise RecordError("INVALID_TIMESTAMP", field,
                                  "timestamp has sub-millisecond precision; truncation is never applied.")
            ms = (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000
        if not (0 <= ms <= INT64_MAX):
            raise RecordError("INVALID_TIMESTAMP", field, "timestamp is outside the supported range.")
        return ms

    def _parse(self, text: str, field: str) -> datetime:
        try:
            if self.fmt == "iso8601":
                return datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
            return datetime.strptime(text, self.pattern)
        except (ValueError, TypeError):
            raise RecordError("INVALID_TIMESTAMP", field, "timestamp does not match the configured format.") from None

    def _localize(self, dt: datetime, field: str) -> datetime:
        local = dt.replace(tzinfo=self.tz)
        # Wall times that are repeated or skipped by a DST change are ambiguous: reject.
        if local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset():
            raise RecordError("AMBIGUOUS_TIMESTAMP", field,
                              "local timestamp is ambiguous or non-existent in the configured timezone.")
        return local


def format_epoch_ms_iso(ms: int) -> str:
    """UTC ISO-8601 with millisecond precision (used by the generator)."""
    dt = _EPOCH + timedelta(milliseconds=ms)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
