"""The documented Phase 4 commands against a disposable real graph. Model-dependent runs patch only the provider factory with a
scripted double (orchestration through the CLI); the missing-credential test uses the real, unpatched failure path."""
import json

import pytest

from agent_fakes import submit
from money_trail.agent.report import validate_report
from money_trail.agent.testing import ScriptedProvider, reply, tool_call
from money_trail.cli import main
from support import agent_dict, detection_dict
from test_cli import env_file  # noqa: F401  (fixture reuse)


@pytest.fixture
def cfgs(tmp_path):
    a, d = tmp_path / "agent.json", tmp_path / "detection.json"
    a.write_text(json.dumps(agent_dict()))
    d.write_text(json.dumps(detection_dict()))
    return str(a), str(d)


@pytest.fixture
def loaded_cli(env_file, make_dataset, capsys, ready):
    ds = make_dataset("cli-agent", seed=21)
    assert main(["ingest", "--dataset-config", str(ds.ingest_config), "--env-file", str(env_file)]) == 0
    capsys.readouterr()
    gt = json.loads((ds.dir / "ground_truth.json").read_text())["patterns"]
    return ["--env-file", str(env_file)], [p for p in gt if p["type"] == "circular"][0]["transaction_ids"][0]


def run(capsys, *argv):
    code = main(list(argv))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


@pytest.fixture
def scripted(monkeypatch):
    """Replace the provider factory (not the loop) with a scripted model; returns a setter."""
    holder = {}
    import money_trail.agent.llm.factory as factory
    monkeypatch.setattr(factory, "build_provider", lambda *a, **k: holder["p"])
    return lambda script: holder.__setitem__("p", ScriptedProvider(script)) or holder["p"]


def test_offline_commands_validate_config_and_list_tools(capsys, cfgs, tmp_path, monkeypatch):
    agent, det = cfgs
    monkeypatch.delenv("TEST_AGENT_KEY", raising=False)
    code, out, _ = run(capsys, "agent", "validate-config", "--agent-config", agent, "--json")
    assert code == 0 and json.loads(out)["valid"] is True and "TEST_AGENT_KEY" not in out
    code, out, _ = run(capsys, "agent", "validate-config", "--agent-config", agent, "--check-credentials", "--json")
    assert code == 1 and json.loads(out)["credential_set"] is False
    monkeypatch.setenv("TEST_AGENT_KEY", "present-but-never-printed")
    code, out, _ = run(capsys, "agent", "validate-config", "--agent-config", agent, "--check-credentials", "--json")
    assert code == 0 and json.loads(out)["credential_set"] is True and "never-printed" not in out
    code, out, _ = run(capsys, "agent", "tools", "--agent-config", agent, "--detection-config", det, "--json")
    tools = json.loads(out)["tools"]
    assert code == 0 and len(tools) == 7 and all(t["mutates_state"] is False for t in tools)
    bad = tmp_path / "bad.json"
    raw = agent_dict()
    raw["provider"]["name"] = "openai"
    bad.write_text(json.dumps(raw))
    code, _, err = run(capsys, "agent", "validate-config", "--agent-config", str(bad))
    assert code == 1 and "provider.name" in err


def test_missing_credential_fails_clearly_before_any_model_request(capsys, cfgs, loaded_cli, monkeypatch):
    env, anchor = loaded_cli
    monkeypatch.delenv("TEST_AGENT_KEY", raising=False)
    agent, det = cfgs
    code, out, err = run(capsys, "agent", "investigate", "--agent-config", agent, "--detection-config", det, "--transaction-id", anchor, "--json", *env)
    rep = json.loads(out)
    assert code == 1 and rep["metadata"]["run_status"] == "failed" and rep["findings"] == []
    assert "provider_error:provider_config" in rep["metadata"]["completion_reasons"]
    assert "credential" in err and "no request was made" in err
    assert rep["investigation_activity"]["provider_usage"]["requests"] == 1       # the single attempt that failed validation, no network


def test_ai_investigation_all_formats_trace_and_exit_codes(capsys, cfgs, loaded_cli, scripted):
    env, anchor = loaded_cli
    agent, det = cfgs
    base = ["agent", "investigate", "--agent-config", agent, "--detection-config", det, "--transaction-id", anchor, "--question", "Any patterns?", *env]
    script = lambda: [reply(tool_call("run_investigation", anchor_type="transaction", anchor_id=anchor)), reply(submit(anchor, f"{anchor} is in a pattern."))]
    scripted(script())
    code, out, _ = run(capsys, *base, "--trace")
    assert code == 0 and "Investigation report  [completed]" in out and "Trace:" in out and "run_investigation" in out and "origin" not in out
    scripted(script())
    code, out, _ = run(capsys, *base, "--format", "markdown")
    assert code == 0 and out.startswith("# Investigation report") and "## Findings" in out
    scripted(script())
    code, out, _ = run(capsys, *base, "--format", "json")
    rep = json.loads(out)
    assert code == 0 and validate_report(rep) == [] and rep["metadata"]["tools_executed"] == ["get_transaction_context", "run_investigation"]
    assert [c["origin"] for c in rep["investigation_activity"]["tool_calls"]] == ["system_anchor_resolution", "model"]
    scripted(script())
    code, out, _ = run(capsys, *base, "--json")                                   # global --json is the same as --format json
    assert code == 0 and json.loads(out)["schema_version"] == 1


def test_exit_codes_for_not_found_unsupported_invalid_incomplete(capsys, cfgs, loaded_cli, scripted, tmp_path, ready):
    env, anchor = loaded_cli
    agent, det = cfgs
    common = ["agent", "investigate", "--agent-config", agent, "--detection-config", det, *env]
    scripted([])
    code, out, _ = run(capsys, *common, "--transaction-id", "cli-agent:NOPE")
    assert code == 3 and "anchor_not_found" in out
    code, out, _ = run(capsys, *common, "--transaction-id", anchor, "--intent", "prove_fraud")
    assert code == 5 and "unsupported_request" in out
    code, _, err = run(capsys, *common)
    assert code == 1 and "exactly one of" in err
    code, _, err = run(capsys, *common, "--transaction-id", anchor, "--account-id", "x")
    assert code == 1 and "exactly one of" in err
    rf = tmp_path / "req.json"
    rf.write_text(json.dumps({"anchor_type": "transaction", "anchor_id": anchor}))
    code, _, err = run(capsys, *common, "--request-file", str(rf), "--transaction-id", anchor)
    assert code == 1 and "cannot be combined" in err
    code, out, _ = run(capsys, *common, "--account-id", "cli-agent:ACC_NOPE", "--persist")
    assert code == 3 and "anchor_not_found" in out                                 # existence is checked before persistence rules
    acct = ready.graph.ro_query("MATCH (a:Account) RETURN a.account_id LIMIT 1").result_set[0][0]
    code, out, _ = run(capsys, *common, "--account-id", acct, "--persist")
    assert code == 1 and "invalid_request" in out and "transaction anchor" in out
    scripted([reply(tool_call("get_account_activity", account_id="x:y", limit=i + 1)) for i in range(5)] + [reply(submit(anchor, "Stopped."))])
    code, out, _ = run(capsys, *common, "--transaction-id", anchor, "--max-tool-calls", "2", "--max-steps", "3")
    assert code == 4 and "[incomplete]" in out and "limit" in out


def test_request_file_namespace_and_persist_through_the_cli(capsys, cfgs, loaded_cli, scripted, ready):
    env, anchor = loaded_cli
    agent, det = cfgs
    local = anchor.split(":", 1)[1]
    rf = ready  # keep fixture alive
    import pathlib, tempfile
    p = pathlib.Path(tempfile.mkdtemp()) / "req.json"
    p.write_text(json.dumps({"anchor_type": "transaction", "anchor_id": local, "namespace": "cli-agent", "question": "Review please."}))
    counts = lambda: tuple(ready.graph.ro_query(f"MATCH (n:{l}) RETURN count(n)").result_set[0][0] for l in ("Investigation", "Finding", "Evidence"))
    scripted([reply(tool_call("run_investigation", anchor_type="transaction", anchor_id=anchor)), reply(submit(anchor, "Done."))])
    code, out, _ = run(capsys, "agent", "investigate", "--agent-config", agent, "--detection-config", det, "--request-file", str(p), "--json", *env)
    assert code == 0 and json.loads(out)["metadata"]["anchor"]["id"] == anchor and counts() == (0, 0, 0)    # no --persist: nothing written
    scripted([reply(tool_call("run_investigation", anchor_type="transaction", anchor_id=anchor)), reply(submit(anchor, "Done."))])
    code, out, _ = run(capsys, "agent", "investigate", "--agent-config", agent, "--detection-config", det, "--request-file", str(p), "--persist", "--json", *env)
    rep = json.loads(out)
    assert code == 0 and rep["metadata"]["investigation_id"] and counts()[0] == 1 and validate_report(rep) == []
