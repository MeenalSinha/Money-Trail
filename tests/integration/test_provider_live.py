"""PROVIDER integration tests (marker: provider). They classify themselves honestly:
  * test_real_model_request_and_tool_use: needs a real credential. Without one it is BLOCKED (skipped with a reason).
    It is never replaced by a mock and never reported as passed.
  * test_invalid_credential_is_rejected_by_the_real_endpoint: a real network request with a deliberately invalid key; proves
    the authentication-failure path against the real service. It does NOT show that a model request succeeds."""
import os
import pathlib

import pytest

from money_trail.agent.agent import InvestigationAgent
from money_trail.agent.config import load_agent_config, parse_agent_config
from money_trail.agent.errors import ProviderAuthError, ProviderUnavailableError
from money_trail.agent.llm.base import CompletionRequest, Message, TextBlock, ToolSpec
from money_trail.agent.llm.factory import build_provider
from money_trail.agent.report import validate_report
from money_trail.config.settings import load_secret
from support import agent_dict

pytestmark = pytest.mark.provider
EXAMPLE = pathlib.Path("examples/agent.example.json")


def real_key():
    cfg = load_agent_config(EXAMPLE)
    return cfg, load_secret(cfg.provider.api_key_env, ".env" if pathlib.Path(".env").is_file() else None)


def test_invalid_credential_is_rejected_by_the_real_endpoint():
    cfg = load_agent_config(EXAMPLE)
    p = build_provider("anthropic", model=cfg.provider.model, api_key="sk-ant-invalid-key-for-auth-failure-test", base_url=None)
    req = CompletionRequest("s", (Message("user", (TextBlock("hi"),)),), (), 16, 20.0)
    try:
        p.complete(req)
    except ProviderUnavailableError:
        pytest.skip("BLOCKED: the provider endpoint is not reachable from this environment")
    except ProviderAuthError as exc:
        assert "rejected the credentials" in str(exc)
        return
    pytest.fail("an invalid key was accepted")


def test_real_model_request_and_tool_use(ready, run_ingest, make_dataset, det_config):
    cfg, key = real_key()
    if not key:
        pytest.skip(f"BLOCKED: no credential in {cfg.provider.api_key_env}; real provider integration is UNVERIFIED")
    import json
    ds = make_dataset("prov-ns", seed=3)
    assert run_ingest(ds)["status"] == "succeeded"
    anchor = [p for p in json.loads((ds.dir / "ground_truth.json").read_text())["patterns"] if p["type"] == "circular"][0]["transaction_ids"][0]
    # Real provider with the real configured model; only the loop limits are tightened for the test.
    raw = json.loads(EXAMPLE.read_text())
    raw["agent"].update(max_steps=8, max_tool_calls=8, max_duration_seconds=120)
    agent = InvestigationAgent(ready, det_config, parse_agent_config(raw), build_provider(
        cfg.provider.name, model=cfg.provider.model, api_key=key, base_url=cfg.provider.base_url))
    rep = agent.run({"anchor_type": "transaction", "anchor_id": anchor,
                     "question": "Investigate this flagged transaction and determine whether the connected activity contains patterns that warrant further review."})
    u = rep["investigation_activity"]["provider_usage"]
    assert u["requests"] >= 1 and rep["metadata"]["run_status"] in ("completed", "incomplete")
    assert validate_report(rep) == []
    model_calls = [c for c in rep["investigation_activity"]["tool_calls"] if c["origin"] == "model"]
    assert model_calls, "the real model selected no registered tool"
    assert all(c["tool"] in {"get_transaction_context", "get_account_activity", "trace_transaction_network", "run_pattern_detector",
                             "get_finding_details", "get_evidence_details", "run_investigation"} for c in model_calls)
