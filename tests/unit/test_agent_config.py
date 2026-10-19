"""Agent configuration, credential loading and request validation (no database, no provider)."""
import copy
import json

import pytest

from money_trail.agent.config import parse_agent_config, load_agent_config
from money_trail.agent.request import RequestStatus, validate_request, AgentRequest
from money_trail.config.settings import load_secret
from money_trail.exceptions import AgentConfigError, ConfigurationError, ValidationError
from support import agent_dict


def bad(mutate, match=None):
    raw = agent_dict()
    mutate(raw)
    with pytest.raises(AgentConfigError, match=match):
        parse_agent_config(raw)


def test_example_file_is_valid_and_has_no_secret():
    cfg = load_agent_config("examples/agent.example.json")
    assert cfg.provider.name == "anthropic" and cfg.provider.api_key_env == "ANTHROPIC_API_KEY"
    text = open("examples/agent.example.json").read()
    assert "sk-" not in text and "api_key\"" not in text          # only the variable NAME is configured


def test_valid_config_round_trips_and_summary_has_no_secret():
    cfg = parse_agent_config(agent_dict())
    s = json.dumps(cfg.safe_summary())
    assert "TEST_AGENT_KEY" not in s and cfg.loop.max_steps == 8 and cfg.provider.retry.max_retries == 2


@pytest.mark.parametrize("mutate,match", [
    (lambda r: r.pop("provider"), "provider is required"),
    (lambda r: r.update(extra={}), "unknown keys"),
    (lambda r: r["provider"].update(name="openai"), "provider.name"),
    (lambda r: r["provider"].update(model="<model>"), "placeholder"),
    (lambda r: r["provider"].update(model="two words"), "whitespace"),
    (lambda r: r["provider"].update(api_key_env="lower_case"), "upper-case"),
    (lambda r: r["provider"].update(base_url="ftp://x"), "base_url"),
    (lambda r: r["provider"].pop("base_url"), "base_url is required"),
    (lambda r: r["provider"].update(request_timeout_seconds=0), "request_timeout_seconds"),
    (lambda r: r["provider"].update(request_timeout_seconds=10_000), "request_timeout_seconds"),
    (lambda r: r["provider"].update(max_retries=-1), "max_retries"),
    (lambda r: r["provider"].update(max_retries=1000), "max_retries"),
    (lambda r: r["provider"].update(max_retries=True), "max_retries"),
    (lambda r: r["provider"].update(retry_initial_backoff_seconds=9, retry_max_backoff_seconds=1), "must not exceed"),
    (lambda r: r["provider"].update(max_output_tokens=10), "max_output_tokens"),
    (lambda r: r["provider"].update(temperature=0.2), "unknown keys"),         # not supported by the pinned SDK
    (lambda r: r["agent"].update(max_steps=1), "max_steps"),
    (lambda r: r["agent"].update(max_tool_calls=0), "max_tool_calls"),
    (lambda r: r["agent"].update(max_tool_calls=10_000), "max_tool_calls"),
    (lambda r: r["agent"].update(prompt_version="v999"), "prompt_version"),
    (lambda r: r["agent"].update(max_duration_seconds=0), "max_duration_seconds"),
    (lambda r: r["tool_limits"].update(max_hops=1), "max_hops"),
    (lambda r: r["tool_limits"].update(max_hops=99), "max_hops"),
    (lambda r: r["tool_limits"].update(max_results=0), "max_results"),
    (lambda r: r["tool_limits"].pop("max_gap_ms"), "max_gap_ms is required"),
    (lambda r: r["grounding"].update(forbidden_conclusion_terms=[]), "forbidden_conclusion_terms"),
    (lambda r: r["grounding"].update(hedging_markers="may"), "hedging_markers"),
])
def test_invalid_config_is_rejected_with_actionable_message(mutate, match):
    bad(mutate, match)


def test_config_errors_are_configuration_errors():
    with pytest.raises(ConfigurationError):
        parse_agent_config({})


def test_missing_and_malformed_files(tmp_path):
    with pytest.raises(AgentConfigError, match="not found"):
        load_agent_config(tmp_path / "nope.json")
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(AgentConfigError, match="not valid JSON"):
        load_agent_config(p)


# ---- credential loading -------------------------------------------------------------------------
def test_secret_precedence_placeholder_and_no_echo(tmp_path):
    f = tmp_path / ".env"
    f.write_text("TEST_AGENT_KEY=from-file-value\n")
    assert load_secret("TEST_AGENT_KEY", f, {}) == "from-file-value"
    assert load_secret("TEST_AGENT_KEY", f, {"TEST_AGENT_KEY": "from-process"}) == "from-process"
    assert load_secret("TEST_AGENT_KEY", None, {}) is None
    assert load_secret("TEST_AGENT_KEY", None, {"TEST_AGENT_KEY": "<set-your-key>"}) is None
    assert load_secret("TEST_AGENT_KEY", None, {"TEST_AGENT_KEY": "  "}) is None
    with pytest.raises(ConfigurationError, match="upper-case"):
        load_secret("bad name", None, {})
    # the value of a secret never appears in any error
    with pytest.raises(ConfigurationError) as e:
        load_secret("lower", None, {"lower": "TOPSECRETVALUE"})
    assert "TOPSECRETVALUE" not in str(e.value)
    with pytest.raises(ConfigurationError):
        load_secret("TEST_AGENT_KEY", tmp_path / "missing.env", {})


# ---- request validation ------------------------------------------------------------------------
def v(**kw):
    return validate_request({"anchor_type": "transaction", "anchor_id": "ns:T1", **kw})


def test_valid_request_defaults():
    r = v()
    assert r.status == RequestStatus.VALID and r.request.intent == "pattern_review" and r.runnable


@pytest.mark.parametrize("raw", [{}, {"anchor_type": "transaction"}, {"anchor_type": "account", "anchor_id": "  "},
                                 {"question": "what is going on?"}])
def test_missing_anchor_is_distinct(raw):
    assert validate_request(raw).status == RequestStatus.MISSING_ANCHOR


@pytest.mark.parametrize("raw", [
    "not an object", {"anchor_type": "dataset", "anchor_id": "x"}, {"anchor_type": "transaction", "anchor_id": 5},
    {"anchor_type": "transaction", "anchor_id": "a'b"}, {"anchor_type": "transaction", "anchor_id": "x", "bogus": 1},
    {"anchor_type": "transaction", "anchor_id": "x", "question": ""},
    {"anchor_type": "transaction", "anchor_id": "x", "question": "q" * 5000},
    {"anchor_type": "transaction", "anchor_id": "x", "namespace": "bad ns"},
    {"anchor_type": "transaction", "anchor_id": "x", "request_id": "has space"},
    {"anchor_type": "transaction", "anchor_id": "x", "limits": {"max_steps": 0}},
    {"anchor_type": "transaction", "anchor_id": "x", "limits": {"max_steps": 2.5}},
    {"anchor_type": "transaction", "anchor_id": "x", "limits": {"max_graph_depth": 3}},
])
def test_invalid_requests(raw):
    assert validate_request(raw).status == RequestStatus.INVALID


def test_unsupported_structured_intent_is_distinct_and_keeps_the_request():
    r = v(intent="prove_fraud")
    assert r.status == RequestStatus.UNSUPPORTED_INTENT and r.request is not None and not r.runnable


def test_precedence_anchor_wins_and_conflicting_ids_are_warned_not_investigated():
    r = v(question="Also look at ns:OTHER1 and ns:T1 please")
    assert r.status == RequestStatus.VALID and r.request.anchor_id == "ns:T1"
    assert len(r.warnings) == 1 and "ns:OTHER1" in r.warnings[0] and "takes precedence" in r.warnings[0]


def test_namespace_qualifies_bare_ids_once():
    assert validate_request({"anchor_type": "transaction", "anchor_id": "T1", "namespace": "ns"}).request.anchor_id == "ns:T1"
    assert validate_request({"anchor_type": "transaction", "anchor_id": "ns:T1", "namespace": "ns"}).request.anchor_id == "ns:T1"


def test_request_limits_can_only_lower_configured_limits():
    loop = parse_agent_config(agent_dict()).loop
    lower = AgentRequest("transaction", "ns:T1", limits={"max_steps": 3, "max_duration_seconds": 5})
    eff = lower.effective_loop(loop)
    assert eff.max_steps == 3 and eff.max_duration_seconds == 5 and eff.max_tool_calls == loop.max_tool_calls
    with pytest.raises(ValidationError, match="exceeds"):
        AgentRequest("transaction", "ns:T1", limits={"max_tool_calls": loop.max_tool_calls + 1}).effective_loop(loop)
