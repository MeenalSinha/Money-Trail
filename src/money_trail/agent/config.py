"""Validated agent configuration (JSON), following the generator / ingestion / detection config conventions.

Every key is required and unknown keys are errors. Nothing here is a credential: the *name* of the environment
variable that holds the API key is configured, never its value. Time values: seconds (provider, agent) or integer
milliseconds (graph queries, as in the detection configuration).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from money_trail.agent.llm.factory import SUPPORTED_PROVIDERS
from money_trail.agent.llm.retry import RetryPolicy
from money_trail.config.settings import QUERY_CEILINGS
from money_trail.exceptions import AgentConfigError, DatasetConfigError
from money_trail.investigation.detectors.settings_util import no_extra, req_int

# Operational safety ceilings (not business rules): configured values may not exceed these.
AGENT_CEILINGS = {"steps": 50, "tool_calls": 100, "duration_s": 3600, "invalid_calls": 20, "recoveries": 5,
                  "result_chars": 200_000, "output_tokens": 64_000, "timeout_s": 600, "retries": 10,
                  "backoff_s": 300, "terms": 200, "text_chars": 20_000}
_SECTIONS = ("provider", "agent", "tool_limits", "grounding")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    model: str
    api_key_env: str
    base_url: Optional[str]
    request_timeout_seconds: float
    max_output_tokens: int
    retry: RetryPolicy


@dataclass(frozen=True)
class LoopConfig:
    prompt_version: str
    max_steps: int                    # model requests, including the final report request
    max_tool_calls: int               # executed tools, including the anchor-resolution call
    max_duration_seconds: float
    max_invalid_tool_calls: int       # rejected (unknown tool / bad arguments) calls before the run is stopped
    max_output_recoveries: int        # re-prompts after a response that is not a valid report submission
    max_tool_result_chars: int


@dataclass(frozen=True)
class ToolLimits:
    max_results: int
    max_hops: int
    max_paths: int
    max_gap_ms: Optional[int]         # None: no upper bound on a caller-supplied gap
    query_timeout_ms: int
    max_listed_findings: int


@dataclass(frozen=True)
class GroundingConfig:
    forbidden_conclusion_terms: Tuple[str, ...]
    hedging_markers: Tuple[str, ...]
    max_text_chars: int


@dataclass(frozen=True)
class AgentConfig:
    provider: ProviderConfig
    loop: LoopConfig
    tools: ToolLimits
    grounding: GroundingConfig
    path: Optional[Path] = None

    def safe_summary(self) -> Dict[str, Any]:
        """Non-secret description for reports and logs."""
        p, l, t = self.provider, self.loop, self.tools
        return {"provider": p.name, "model": p.model, "base_url": p.base_url or "default",
                "prompt_version": l.prompt_version, "max_steps": l.max_steps, "max_tool_calls": l.max_tool_calls,
                "max_duration_seconds": l.max_duration_seconds, "request_timeout_seconds": p.request_timeout_seconds,
                "max_retries": p.retry.max_retries, "max_output_tokens": p.max_output_tokens,
                "tool_limits": {"max_results": t.max_results, "max_hops": t.max_hops, "max_paths": t.max_paths,
                                "max_gap_ms": t.max_gap_ms, "query_timeout_ms": t.query_timeout_ms}}


def _num(raw: Mapping, key: str, where: str, lo: float, hi: float) -> float:
    if key not in raw:
        raise AgentConfigError(f"{where}.{key} is required.")
    v = raw[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo < v <= hi):
        raise AgentConfigError(f"{where}.{key} must be a number greater than {lo:g} and at most {hi:g}.")
    return float(v)


def _str(raw: Mapping, key: str, where: str, *, max_len: int = 200) -> str:
    v = raw.get(key)
    if not isinstance(v, str) or not v.strip() or not v.isprintable() or len(v) > max_len:
        raise AgentConfigError(f"{where}.{key} must be a non-empty printable string.")
    if v.startswith("<") and v.endswith(">"):
        raise AgentConfigError(f"{where}.{key} still contains a placeholder.")
    return v


def _terms(raw: Mapping, key: str, where: str, *, allow_empty: bool) -> Tuple[str, ...]:
    v = raw.get(key)
    if not isinstance(v, list) or (not v and not allow_empty) or len(v) > AGENT_CEILINGS["terms"]:
        raise AgentConfigError(f"{where}.{key} must be a list of strings"
                               f"{'' if allow_empty else ' (not empty)'}, at most {AGENT_CEILINGS['terms']} entries.")
    out = []
    for t in v:
        if not isinstance(t, str) or not t.strip() or len(t) > 80 or not t.isprintable():
            raise AgentConfigError(f"{where}.{key} entries must be short printable strings.")
        out.append(t.strip().lower())
    return tuple(dict.fromkeys(out))


def parse_agent_config(raw: Mapping[str, Any], path: Optional[Path] = None) -> AgentConfig:
    from money_trail.agent.prompts import PROMPTS      # late: prompts import nothing from config
    if not isinstance(raw, dict):
        raise AgentConfigError("Agent config must be a JSON object.")
    try:
        no_extra(raw, _SECTIONS, "agent config")
    except DatasetConfigError as exc:
        raise AgentConfigError(str(exc)) from None
    for k in _SECTIONS:
        if not isinstance(raw.get(k), dict):
            raise AgentConfigError(f"{k} is required and must be an object.")
    try:
        return _parse(raw, PROMPTS, path)
    except DatasetConfigError as exc:                  # the shared strict helpers raise this type
        raise AgentConfigError(str(exc)) from None


def _parse(raw: Mapping[str, Any], prompts: Mapping[str, str], path: Optional[Path]) -> AgentConfig:
    p = raw["provider"]
    no_extra(p, ("name", "model", "api_key_env", "base_url", "request_timeout_seconds", "max_retries",
                 "retry_initial_backoff_seconds", "retry_max_backoff_seconds", "max_output_tokens"), "provider")
    name = p.get("name")
    if name not in SUPPORTED_PROVIDERS:
        raise AgentConfigError(f"provider.name must be one of: {', '.join(SUPPORTED_PROVIDERS)}.")
    model = _str(p, "model", "provider")
    if any(c.isspace() for c in model):
        raise AgentConfigError("provider.model must not contain whitespace.")
    env_name = _str(p, "api_key_env", "provider", max_len=64)
    if not _ENV_NAME_RE.match(env_name):
        raise AgentConfigError("provider.api_key_env must be an upper-case environment variable name "
                               "(letters, digits, underscores; starts with a letter).")
    if "base_url" not in p:
        raise AgentConfigError("provider.base_url is required (use null for the provider default).")
    base_url = p["base_url"]
    if base_url is not None and (not isinstance(base_url, str) or not base_url.startswith(("https://", "http://"))
                                 or any(c.isspace() for c in base_url)):
        raise AgentConfigError("provider.base_url must be null or an http(s) URL.")
    timeout = _num(p, "request_timeout_seconds", "provider", 0, AGENT_CEILINGS["timeout_s"])
    retries = req_int(p, "max_retries", "provider", 0, AGENT_CEILINGS["retries"])
    b0 = _num(p, "retry_initial_backoff_seconds", "provider", 0, AGENT_CEILINGS["backoff_s"])
    b1 = _num(p, "retry_max_backoff_seconds", "provider", 0, AGENT_CEILINGS["backoff_s"])
    if b0 > b1:
        raise AgentConfigError("provider.retry_initial_backoff_seconds must not exceed retry_max_backoff_seconds.")
    provider = ProviderConfig(name, model, env_name, base_url, timeout,
                              req_int(p, "max_output_tokens", "provider", 256, AGENT_CEILINGS["output_tokens"]),
                              RetryPolicy(retries, b0, b1))

    a = raw["agent"]
    no_extra(a, ("prompt_version", "max_steps", "max_tool_calls", "max_duration_seconds", "max_invalid_tool_calls",
                 "max_output_recoveries", "max_tool_result_chars"), "agent")
    pv = a.get("prompt_version")
    if pv not in prompts:
        raise AgentConfigError(f"agent.prompt_version must be one of: {', '.join(sorted(prompts))}.")
    loop = LoopConfig(pv,
                      req_int(a, "max_steps", "agent", 2, AGENT_CEILINGS["steps"]),
                      req_int(a, "max_tool_calls", "agent", 1, AGENT_CEILINGS["tool_calls"]),
                      _num(a, "max_duration_seconds", "agent", 0, AGENT_CEILINGS["duration_s"]),
                      req_int(a, "max_invalid_tool_calls", "agent", 1, AGENT_CEILINGS["invalid_calls"]),
                      req_int(a, "max_output_recoveries", "agent", 0, AGENT_CEILINGS["recoveries"]),
                      req_int(a, "max_tool_result_chars", "agent", 1000, AGENT_CEILINGS["result_chars"]))

    t = raw["tool_limits"]
    no_extra(t, ("max_results", "max_hops", "max_paths", "max_gap_ms", "query_timeout_ms", "max_listed_findings"),
             "tool_limits")
    if "max_gap_ms" not in t:
        raise AgentConfigError("tool_limits.max_gap_ms is required (use null for no upper bound).")
    gap = t["max_gap_ms"]
    if gap is not None:
        gap = req_int(t, "max_gap_ms", "tool_limits", 1, 10 * 365 * 86_400_000)
    tools = ToolLimits(req_int(t, "max_results", "tool_limits", 1, QUERY_CEILINGS["results"]),
                       req_int(t, "max_hops", "tool_limits", 2, QUERY_CEILINGS["hops"]),
                       req_int(t, "max_paths", "tool_limits", 1, QUERY_CEILINGS["paths"]),
                       gap,
                       req_int(t, "query_timeout_ms", "tool_limits", 1, 3_600_000),
                       req_int(t, "max_listed_findings", "tool_limits", 1, QUERY_CEILINGS["results"]))

    g = raw["grounding"]
    no_extra(g, ("forbidden_conclusion_terms", "hedging_markers", "max_text_chars"), "grounding")
    grounding = GroundingConfig(_terms(g, "forbidden_conclusion_terms", "grounding", allow_empty=False),
                                _terms(g, "hedging_markers", "grounding", allow_empty=False),
                                req_int(g, "max_text_chars", "grounding", 100, AGENT_CEILINGS["text_chars"]))
    return AgentConfig(provider, loop, tools, grounding, path)


def load_agent_config(path) -> AgentConfig:
    p = Path(path)
    if not p.is_file():
        raise AgentConfigError(f"Agent config not found: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AgentConfigError(f"Agent config is not valid JSON ({type(exc).__name__}).") from None
    return parse_agent_config(raw, p.resolve())
