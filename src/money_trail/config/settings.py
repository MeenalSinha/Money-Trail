"""Centralized, validated configuration.

This is the only module that reads environment variables or .env files.
Every other component receives a :class:`Settings` instance.
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional
import os

from dotenv import dotenv_values

from money_trail.exceptions import ConfigurationError

ENVIRONMENTS = ("development", "test", "production")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_GRAPH_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
# Safety ceilings for read-only investigation queries. These bound worst-case work on the
# database; they are not business rules. Configured limits must not exceed them.
QUERY_CEILINGS = {"results": 10000, "hops": 10, "paths": 10000}
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def validate_graph_name(name: str, key: str = "FALKORDB_GRAPH_NAME") -> str:
    """Graph names are restricted so they are safe to log and to use as keys."""
    if not _GRAPH_NAME_RE.match(name or ""):
        raise ConfigurationError(
            f"{key} must start with a letter and contain only letters, digits and "
            "underscores (max 63 characters)."
        )
    return name


@dataclass(frozen=True)
class Settings:
    environment: str
    log_level: str
    host: str
    port: int
    username: Optional[str]
    password: Optional[str] = dataclasses.field(repr=False)
    tls: bool
    tls_ca_cert: Optional[str]
    connect_timeout: float
    socket_timeout: float
    graph_name: str
    test_graph_prefix: Optional[str] = None
    # Optional limits for investigation queries; None means "not configured".
    query_max_results: Optional[int] = None
    query_max_hops: Optional[int] = None
    query_max_paths: Optional[int] = None
    query_max_gap_ms: Optional[int] = None

    def __repr__(self) -> str:  # explicit: never include the password
        return (
            f"Settings(environment={self.environment!r}, host={self.host!r}, "
            f"port={self.port}, username={'set' if self.username else 'unset'}, "
            f"password={'set' if self.password else 'unset'}, tls={self.tls}, "
            f"graph_name={self.graph_name!r})"
        )

    def with_graph_name(self, graph_name: str) -> "Settings":
        """Return a copy bound to another graph (used for isolated test graphs)."""
        validate_graph_name(graph_name, "graph_name")
        return dataclasses.replace(self, graph_name=graph_name)

    def safe_summary(self) -> dict:
        """Non-sensitive description suitable for logs and health output."""
        return {
            "environment": self.environment,
            "graph_name": self.graph_name,
            "tls": self.tls,
            "authentication": "configured" if self.password else "none",
        }


def _require(env: Mapping[str, str], key: str, problems: list) -> str:
    value = (env.get(key) or "").strip()
    if not value or (value.startswith("<") and value.endswith(">")):
        problems.append(f"{key} is required (see .env.example).")
        return ""
    return value


def _optional(env: Mapping[str, str], key: str) -> Optional[str]:
    value = (env.get(key) or "").strip()
    return value or None


def _parse_bool(env: Mapping[str, str], key: str, problems: list) -> bool:
    raw = (env.get(key) or "false").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    problems.append(f"{key} must be one of true/false (got an unrecognized value).")
    return False


def _parse_float(env, key: str, upper: float, problems: list) -> float:
    raw = _require(env, key, problems)
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        problems.append(f"{key} must be a number of seconds.")
        return 0.0
    if not (0 < value <= upper):
        problems.append(f"{key} must be greater than 0 and at most {upper:g} seconds.")
    return value


def _optional_int(env: Mapping[str, str], key: str, upper: Optional[int], problems: list) -> Optional[int]:
    raw = _optional(env, key)
    if raw is None:
        return None
    if not raw.isdigit() or int(raw) < 1:
        problems.append(f"{key} must be a positive integer.")
        return None
    if upper is not None and int(raw) > upper:
        problems.append(f"{key} must not exceed {upper} (safety ceiling).")
        return None
    return int(raw)


def build_settings(env: Mapping[str, str]) -> Settings:
    """Validate a mapping of configuration values and build Settings.

    All problems are collected and reported together. Values are never echoed
    back in messages, so secrets cannot leak through errors.
    """
    problems: list = []

    environment = _require(env, "MONEY_TRAIL_ENV", problems).lower()
    if environment and environment not in ENVIRONMENTS:
        problems.append(f"MONEY_TRAIL_ENV must be one of {', '.join(ENVIRONMENTS)}.")

    log_level = (env.get("MONEY_TRAIL_LOG_LEVEL") or "INFO").strip().upper()
    if log_level not in LOG_LEVELS:
        problems.append(f"MONEY_TRAIL_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}.")

    host = _require(env, "FALKORDB_HOST", problems)

    port = 0
    port_raw = _require(env, "FALKORDB_PORT", problems)
    if port_raw:
        if not port_raw.isdigit() or not (1 <= int(port_raw) <= 65535):
            problems.append("FALKORDB_PORT must be an integer between 1 and 65535.")
        else:
            port = int(port_raw)

    username = _optional(env, "FALKORDB_USERNAME")
    password = _optional(env, "FALKORDB_PASSWORD")
    if password and password.startswith("<") and password.endswith(">"):
        problems.append("FALKORDB_PASSWORD still contains the .env.example placeholder.")
    if username and not password:
        problems.append("FALKORDB_USERNAME is set but FALKORDB_PASSWORD is missing.")

    tls = _parse_bool(env, "FALKORDB_TLS", problems)
    tls_ca_cert = _optional(env, "FALKORDB_TLS_CA_CERT")
    if tls_ca_cert and not tls:
        problems.append("FALKORDB_TLS_CA_CERT is set but FALKORDB_TLS is not enabled.")
    if tls_ca_cert and not Path(tls_ca_cert).is_file():
        problems.append("FALKORDB_TLS_CA_CERT does not point to an existing file.")

    connect_timeout = _parse_float(env, "FALKORDB_CONNECT_TIMEOUT", 120, problems)
    socket_timeout = _parse_float(env, "FALKORDB_SOCKET_TIMEOUT", 600, problems)

    graph_name = _require(env, "FALKORDB_GRAPH_NAME", problems)
    if graph_name:
        try:
            validate_graph_name(graph_name)
        except ConfigurationError as exc:
            problems.append(str(exc))

    test_prefix = _optional(env, "MONEY_TRAIL_TEST_GRAPH_PREFIX")
    if test_prefix:
        if test_prefix.startswith("<") and test_prefix.endswith(">"):
            problems.append("MONEY_TRAIL_TEST_GRAPH_PREFIX still contains the placeholder.")
        else:
            try:
                validate_graph_name(test_prefix, "MONEY_TRAIL_TEST_GRAPH_PREFIX")
            except ConfigurationError as exc:
                problems.append(str(exc))
            if graph_name and test_prefix == graph_name:
                problems.append(
                    "MONEY_TRAIL_TEST_GRAPH_PREFIX must differ from FALKORDB_GRAPH_NAME."
                )

    query_max_results = _optional_int(env, "MONEY_TRAIL_QUERY_MAX_RESULTS", QUERY_CEILINGS["results"], problems)
    query_max_hops = _optional_int(env, "MONEY_TRAIL_QUERY_MAX_HOPS", QUERY_CEILINGS["hops"], problems)
    query_max_paths = _optional_int(env, "MONEY_TRAIL_QUERY_MAX_PATHS", QUERY_CEILINGS["paths"], problems)
    query_max_gap_ms = _optional_int(env, "MONEY_TRAIL_QUERY_MAX_GAP_MS", None, problems)

    if environment == "production":
        if not password:
            problems.append("Production requires FALKORDB_PASSWORD.")
        if not tls:
            problems.append("Production requires FALKORDB_TLS=true.")

    if problems:
        raise ConfigurationError(
            "Invalid configuration:\n  - " + "\n  - ".join(problems)
        )

    return Settings(
        environment=environment,
        log_level=log_level,
        host=host,
        port=port,
        username=username,
        password=password,
        tls=tls,
        tls_ca_cert=tls_ca_cert,
        connect_timeout=connect_timeout,
        socket_timeout=socket_timeout,
        graph_name=graph_name,
        test_graph_prefix=test_prefix,
        query_max_results=query_max_results,
        query_max_hops=query_max_hops,
        query_max_paths=query_max_paths,
        query_max_gap_ms=query_max_gap_ms,
    )


def load_settings(
    env_file: Optional[os.PathLike] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Settings:
    """Load settings from an optional env file overlaid by process environment.

    Process environment wins over the file. No implicit fallbacks exist for
    host, port, graph name or environment.
    """
    values: dict = {}
    if env_file is not None:
        path = Path(env_file)
        if not path.is_file():
            raise ConfigurationError(f"Environment file not found: {path}")
        values.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    source = os.environ if environ is None else environ
    values.update({k: v for k, v in source.items() if k.startswith(("FALKORDB_", "MONEY_TRAIL_"))})
    return build_settings(values)
