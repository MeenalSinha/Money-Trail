"""Validated configuration for the synthetic data generator (JSON). Every parameter is explicit."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

from money_trail.exceptions import DatasetConfigError
from money_trail.ingestion.contract import CURRENCY_RE, parse_timezone, validate_namespace

PATTERN_TYPES = ("circular", "pass_through", "fan_in", "fan_out")


def _int(d: Mapping, key: str, where: str, lo: int, hi: int = 10**12) -> int:
    v = d.get(key)
    if isinstance(v, bool) or not isinstance(v, int) or not (lo <= v <= hi):
        raise DatasetConfigError(f"{where}.{key} must be an integer between {lo} and {hi}.")
    return v


def _range(d: Mapping, key: str, where: str, lo: int, hi: int = 10**12) -> Tuple[int, int]:
    r = d.get(key)
    if not (isinstance(r, dict) and set(r) == {"min", "max"}):
        raise DatasetConfigError(f"{where}.{key} must be an object with 'min' and 'max'.")
    a, b = _int(r, "min", f"{where}.{key}", lo, hi), _int(r, "max", f"{where}.{key}", lo, hi)
    if a > b:
        raise DatasetConfigError(f"{where}.{key}.min must not exceed max.")
    return a, b


def _extra(d: Mapping, allowed, where: str) -> None:
    extra = sorted(set(d) - set(allowed))
    if extra:
        raise DatasetConfigError(f"{where} has unknown keys: {', '.join(extra)}.")


@dataclass(frozen=True)
class PatternSpec:
    type: str
    count: int
    params: Mapping[str, Any]


@dataclass(frozen=True)
class GeneratorConfig:
    namespace: str
    seed: int
    output_dir: Path
    ingest_output_dir: Path
    currency: str
    currency_exponent: int
    amount_format: str               # "major" or "minor" in the written CSV
    window_start_ms: int
    window_days: int
    ordinary_accounts: int
    ordinary_transactions: int
    amount_minor: Tuple[int, int]
    channels: Tuple[str, ...]
    account_prefix: str
    transaction_prefix: str
    alert_prefix: str
    patterns: Tuple[PatternSpec, ...]
    seed_alerts_on_pattern: int
    seed_alerts_on_ordinary: int
    alert_type: str
    alert_severity: str
    raw: Mapping[str, Any]

    @property
    def window_ms(self) -> int:
        return self.window_days * 86_400_000


def parse_generator_config(raw: Mapping[str, Any], path: Path) -> GeneratorConfig:
    base = path.parent
    _extra(raw, ("namespace", "seed", "output_dir", "ingest_output_dir", "currency", "amount_format", "time_window",
                 "ordinary_activity", "id_prefixes", "patterns", "seed_alerts"), "config")
    ns = validate_namespace(raw.get("namespace"))
    seed = raw.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise DatasetConfigError("seed is required and must be an integer (no implicit seeding).")
    for key in ("output_dir", "ingest_output_dir"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise DatasetConfigError(f"{key} is required.")
    out = Path(raw["output_dir"]); out = out if out.is_absolute() else base / out
    ing = Path(raw["ingest_output_dir"]); ing = ing if ing.is_absolute() else base / ing

    cur = raw.get("currency")
    if not (isinstance(cur, dict) and set(cur) == {"code", "exponent"}):
        raise DatasetConfigError("currency must be {code, exponent}.")
    if not CURRENCY_RE.match(str(cur["code"])):
        raise DatasetConfigError("currency.code must be a 3-letter upper-case code.")
    exponent = _int(cur, "exponent", "currency", 0, 6)
    fmt = raw.get("amount_format")
    if fmt not in ("major", "minor"):
        raise DatasetConfigError("amount_format must be 'major' or 'minor'.")

    from datetime import datetime, timezone
    tw = raw.get("time_window") or {}
    _extra(tw, ("start", "days"), "time_window")
    try:
        start = datetime.fromisoformat(str(tw.get("start")).replace("Z", "+00:00"))
    except ValueError:
        raise DatasetConfigError("time_window.start must be an ISO-8601 timestamp with a UTC offset.") from None
    if start.tzinfo is None:
        raise DatasetConfigError("time_window.start must include a UTC offset (for example 'Z').")
    start_ms = int(start.astimezone(timezone.utc).timestamp()) * 1000
    days = _int(tw, "days", "time_window", 1, 3650)

    oa = raw.get("ordinary_activity") or {}
    _extra(oa, ("accounts", "transactions", "amount_minor", "channels"), "ordinary_activity")
    accounts = _int(oa, "accounts", "ordinary_activity", 2, 10_000_000)
    txs = _int(oa, "transactions", "ordinary_activity", 0, 100_000_000)
    amount = _range(oa, "amount_minor", "ordinary_activity", 0, 10**15)
    channels = oa.get("channels")
    if not (isinstance(channels, list) and channels and all(isinstance(c, str) and c.strip() for c in channels)):
        raise DatasetConfigError("ordinary_activity.channels must be a non-empty list of strings.")

    ip = raw.get("id_prefixes") or {}
    _extra(ip, ("account", "transaction", "alert"), "id_prefixes")
    prefixes = []
    for k in ("account", "transaction", "alert"):
        v = ip.get(k)
        if not (isinstance(v, str) and v and ":" not in v and v.isprintable() and " " not in v):
            raise DatasetConfigError(f"id_prefixes.{k} must be a non-empty string without ':' or spaces.")
        prefixes.append(v)

    pats: List[PatternSpec] = []
    for i, p in enumerate(raw.get("patterns") or []):
        w = f"patterns[{i}]"
        if not isinstance(p, dict) or p.get("type") not in PATTERN_TYPES:
            raise DatasetConfigError(f"{w}.type must be one of: {', '.join(PATTERN_TYPES)}.")
        t = p["type"]
        count = _int(p, "count", w, 0, 1_000_000)
        common = {"count", "type", "amount_minor", "delay_seconds", "shared_account_fraction_percent"}
        specific = {"circular": {"cycle_length", "retention_bps"},
                    "pass_through": {"hops", "retention_bps"},
                    "fan_in": {"sources"}, "fan_out": {"recipients"}}[t]
        _extra(p, common | specific, w)
        params: Dict[str, Any] = {
            "amount_minor": _range(p, "amount_minor", w, 1, 10**15),
            "delay_seconds": _range(p, "delay_seconds", w, 0, 10**7),
            "shared_account_fraction_percent": _int(p, "shared_account_fraction_percent", w, 0, 100),
        }
        if t == "circular":
            params["cycle_length"] = _range(p, "cycle_length", w, 3, 1000)
            params["retention_bps"] = _int(p, "retention_bps", w, 1, 10000)
        elif t == "pass_through":
            params["hops"] = _range(p, "hops", w, 2, 1000)
            params["retention_bps"] = _int(p, "retention_bps", w, 1, 10000)
        elif t == "fan_in":
            params["sources"] = _range(p, "sources", w, 2, 100000)
        else:
            params["recipients"] = _range(p, "recipients", w, 2, 100000)
        pats.append(PatternSpec(t, count, params))

    sa = raw.get("seed_alerts") or {}
    _extra(sa, ("on_pattern_transactions", "on_ordinary_transactions", "alert_type", "severity"), "seed_alerts")
    on_p = _int(sa, "on_pattern_transactions", "seed_alerts", 0, 10**9)
    on_o = _int(sa, "on_ordinary_transactions", "seed_alerts", 0, 10**9)
    atype, sev = sa.get("alert_type", ""), sa.get("severity", "")
    if (on_p or on_o) and not (isinstance(atype, str) and atype and isinstance(sev, str) and sev):
        raise DatasetConfigError("seed_alerts.alert_type and seed_alerts.severity are required when alerts are requested.")

    cfg = GeneratorConfig(
        namespace=ns, seed=seed, output_dir=out, ingest_output_dir=ing, currency=cur["code"], currency_exponent=exponent,
        amount_format=fmt, window_start_ms=start_ms, window_days=days, ordinary_accounts=accounts,
        ordinary_transactions=txs, amount_minor=amount, channels=tuple(c.strip() for c in channels),
        account_prefix=prefixes[0], transaction_prefix=prefixes[1], alert_prefix=prefixes[2],
        patterns=tuple(pats), seed_alerts_on_pattern=on_p, seed_alerts_on_ordinary=on_o,
        alert_type=atype, alert_severity=sev, raw=raw,
    )
    return cfg


def load_generator_config(path) -> GeneratorConfig:
    p = Path(path)
    if not p.is_file():
        raise DatasetConfigError(f"Generator config not found: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DatasetConfigError(f"Generator config is not valid JSON ({type(exc).__name__}).") from None
    if not isinstance(raw, dict):
        raise DatasetConfigError("Generator config must be a JSON object.")
    return parse_generator_config(raw, p.resolve())
