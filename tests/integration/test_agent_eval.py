"""The agent-quality metrics are verified on runs whose correct values are known by construction."""
import json

import pytest

from agent_fakes import AdaptivePolicy, submit
from money_trail.agent.testing import reply, tool_call
from money_trail.cli import main
from money_trail.evaluation.agent_eval import Scenario, ScenarioRun, build_scenarios, evaluate_runs, run_scenarios
from money_trail.investigation.engine import InvestigationEngine, InvestigationRequest

NS = "eval-ns"


@pytest.fixture
def loaded(ready, run_ingest, make_dataset):
    ds = make_dataset(NS, seed=3)
    assert run_ingest(ds)["status"] == "succeeded"
    return ds


def quiet_fn(ready, det_config, ds):
    gt = json.loads((ds.dir / "ground_truth.json").read_text())
    labelled = {t for p in gt["patterns"] for t in p["transaction_ids"]}

    def quiet():
        eng = InvestigationEngine(ready, det_config)
        for (tid,) in ready.graph.ro_query("MATCH (t:Transaction) RETURN t.transaction_id ORDER BY t.transaction_id").result_set:
            if tid not in labelled and (lambda r: r.complete and not r.detections)(eng.investigate(InvestigationRequest("transaction", tid))):
                return tid
    return quiet


def test_metric_arithmetic_on_a_run_with_known_unnecessary_calls(loaded, ready, make_agent):
    gt = json.loads((loaded.dir / "ground_truth.json").read_text())["patterns"][0]
    a = gt["transaction_ids"][0]
    dup = dict(anchor_type="transaction", anchor_id=a)
    script = [reply(tool_call("run_investigation", **dup)),                                         # 1 useful
              reply(tool_call("run_investigation", **dup)),                                         # 2 exact repeat            -> unnecessary
              reply(tool_call("run_pattern_detector", detector="circular", **dup)),                 # 3 covered by the broad run -> unnecessary
              reply(tool_call("nonexistent_tool")),                                                 # 4 rejected                -> unnecessary
              reply(tool_call("get_account_activity", account_id=ready.graph.ro_query("MATCH (a:Account) RETURN a.account_id LIMIT 1").result_set[0][0], limit=3)),   # 5 useful
              reply(submit(a, f"{a} was examined. It moved 999999999 USD. Unknown {NS}:GHOST appears.", detections=[]))]
    agent, _ = make_agent(script, {"agent": {"max_tool_calls": 12, "max_steps": 12}})
    sc = Scenario("known", "metric arithmetic", {"anchor_type": "transaction", "anchor_id": a}, [["run_investigation"]], ["trace_transaction_network"],
                  ["completed"], ["patterns_identified"], list(gt["transaction_ids"]))
    m = evaluate_runs(ready, run_scenarios(agent, [sc]))
    assert m["model_tool_calls"] == 5 and m["unnecessary_tool_call_rate"] == 0.6                    # exactly calls 2, 3, 4
    assert m["tool_execution_success_rate"] == 0.8                                                   # the rejected call is the one failure
    assert m["tool_selection_accuracy"] == 1.0 and m["investigation_completion_rate"] == 1.0
    assert m["model_sentences"] == 3 and m["unsupported_claim_rate"] == round(2 / 3, 4)             # number and ghost id rejected, 1 grounded kept
    assert m["factual_sentences"] == 3 and m["evidence_grounding_rate"] == round(1 / 3, 4)
    assert m["post_filter_unresolved_ids"] == [] and m["findings_verified_independently"]["verified"] == m["findings_verified_independently"]["total"] > 0
    assert m["expected_pattern_reported"] == 1.0 and m["provider_usage"]["input_tokens"] == 60      # 6 scripted requests x 10, as reported
    assert set(m["latency_ms"]["tool_mean_by_name"]) >= {"get_transaction_context", "run_investigation"}


def test_forbidden_tool_and_missing_required_tool_fail_selection(loaded, ready, make_agent):
    a = json.loads((loaded.dir / "ground_truth.json").read_text())["patterns"][0]["transaction_ids"][0]
    agent, _ = make_agent([reply(tool_call("trace_transaction_network", anchor_type="transaction", anchor_id=a)), reply(submit(a, "Done."))])
    sc = Scenario("sel", "", {"anchor_type": "transaction", "anchor_id": a}, [["run_investigation", "run_pattern_detector"]], ["trace_transaction_network"])
    m = evaluate_runs(ready, run_scenarios(agent, [sc]))
    assert m["tool_selection_accuracy"] == 0.0


def test_scenario_set_from_ground_truth_and_full_adaptive_evaluation(loaded, ready, make_agent, det_config):
    scs = build_scenarios(ready, loaded.dir / "ground_truth.json", quiet_fn(ready, det_config, loaded))
    ids = [s.scenario_id for s in scs]
    assert ids == ["connected-circular", "connected-pass_through", "connected-fan_in", "connected-fan_out", "insufficient-evidence",
                   "missing-transaction", "unsupported-request"]
    for s in scs:                                                                   # the agent-facing request never carries expectations
        assert set(s.request) <= {"anchor_type", "anchor_id", "question"}
    agent, _ = make_agent(AdaptivePolicy(), {"agent": {"max_steps": 12, "max_tool_calls": 12}})
    runs = run_scenarios(agent, [s for s in scs if s.scenario_id != "unsupported-request"])
    m = evaluate_runs(ready, runs)
    by = {p["scenario"]: p for p in m["per_scenario"]}
    assert by["missing-transaction"]["run_status"] == "anchor_not_found" and by["missing-transaction"]["tools_executed"] == []
    for t in ("circular", "pass_through", "fan_in", "fan_out"):
        assert by[f"connected-{t}"]["outcome"] == "patterns_identified" and by[f"connected-{t}"]["expected_pattern_reported"], by[f"connected-{t}"]
    assert by["connected-fan_out"]["tools_executed"][:3] == ["get_transaction_context", "get_account_activity", "get_account_activity"]  # adapted to sparse data
    assert m["post_filter_unresolved_ids"] == [] and m["tool_execution_success_rate"] == 1.0


def test_live_evaluation_refuses_without_a_credential(capsys, tmp_path, monkeypatch, loaded):
    from support import agent_dict, detection_dict
    a, d = tmp_path / "a.json", tmp_path / "d.json"
    a.write_text(json.dumps(agent_dict())); d.write_text(json.dumps(detection_dict()))
    monkeypatch.delenv("TEST_AGENT_KEY", raising=False)
    code = main(["agent", "evaluate", "--agent-config", str(a), "--detection-config", str(d), "--ground-truth", str(loaded.dir / "ground_truth.json")])
    cap = capsys.readouterr()
    assert code == 1 and "nothing was run" in cap.err and cap.out == ""


def test_evaluator_independently_catches_a_fabricated_id_that_bypassed_the_filter(loaded, ready, make_agent):
    import copy
    a = json.loads((loaded.dir / "ground_truth.json").read_text())["patterns"][0]["transaction_ids"][0]
    agent, _ = make_agent([reply(tool_call("run_investigation", anchor_type="transaction", anchor_id=a)), reply(submit(a, f"{a} was reviewed."))])
    sc = Scenario("inject", "", {"anchor_type": "transaction", "anchor_id": a})
    run = run_scenarios(agent, [sc])[0]
    assert evaluate_runs(ready, [run])["post_filter_unresolved_ids"] == []
    tampered = copy.deepcopy(run.report)
    tampered["summary"]["narrative"] += f" It also touched {NS}:TXN_FABRICATED_999."
    assert evaluate_runs(ready, [ScenarioRun(sc, tampered, run.wall_ms)])["post_filter_unresolved_ids"] == [f"{NS}:TXN_FABRICATED_999"]
