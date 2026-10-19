"""End-to-end scenarios: real FalkorDB, real generated dataset, real tools, real verification; the model is SCRIPTED
(AdaptivePolicy decides each next call from the actual tool results). Spec scenarios 1-5."""
import json

import pytest
from redis import exceptions as redis_exc

from agent_fakes import AdaptivePolicy, submit
from money_trail.agent.report import render_text, validate_report
from money_trail.agent.testing import ScriptedProvider, reply, tool_call
from money_trail.investigation.engine import InvestigationEngine, InvestigationRequest

NS = "e2e-ns"


@pytest.fixture
def loaded(ready, run_ingest, make_dataset):
    ds = make_dataset(NS, seed=3)
    assert run_ingest(ds)["status"] == "succeeded"
    return ds


def patterns(ds):
    return json.loads((ds.dir / "ground_truth.json").read_text())["patterns"]


def graph_has(conn, tid):
    return bool(conn.graph.ro_query("MATCH (t:Transaction {transaction_id:$i}) RETURN 1", {"i": tid}).result_set)


def ordinary_transaction_without_detections(ready, ds, det_config):
    labelled = {t for p in patterns(ds) for t in p["transaction_ids"]}
    eng = InvestigationEngine(ready, det_config)
    for (tid,) in ready.graph.ro_query("MATCH (t:Transaction) RETURN t.transaction_id ORDER BY t.transaction_id").result_set:
        if tid in labelled:
            continue
        r = eng.investigate(InvestigationRequest("transaction", tid))
        if r.complete and not r.detections:
            return tid
    pytest.fail("no ordinary transaction without detections in the dataset")


def test_scenario_1_connected_suspicious_activity(loaded, ready, make_agent, det_config):
    gt = patterns(loaded)[0]
    anchor = gt["transaction_ids"][0]
    policy = AdaptivePolicy()
    agent, _ = make_agent(policy)
    rep = agent.run({"anchor_type": "transaction", "anchor_id": anchor, "question": "Investigate this flagged transaction and determine whether "
                                                                                    "the connected activity contains patterns that warrant further review."})
    assert policy.sequence == [["get_account_activity"], ["run_investigation"], ["submit_investigation_report"]]
    assert rep["metadata"]["run_status"] == "completed" and rep["summary"]["outcome"] == "patterns_identified"
    assert rep["metadata"]["tools_executed"] == ["get_transaction_context", "get_account_activity", "run_investigation"]
    full = InvestigationEngine(ready, det_config).investigate(InvestigationRequest("transaction", anchor))
    expected = {d.detection_id: d for d in full.detections}
    got = {f["detection_id"] for f in rep["findings"] if f["source"] == "deterministic_rule"}
    assert got == set(expected)                                                # exactly the Phase 3 detections, nothing added or dropped
    circ = [f for f in rep["findings"] if f["pattern_type"] == gt["type"] and f["classification"] == "observation"]
    assert any(set(gt["transaction_ids"]) <= set(f["supporting_transaction_ids"]) for f in circ)
    for f in rep["findings"]:
        assert f["verified"] and f["evidence_ids"] and all(graph_has(ready, t) for t in f["supporting_transaction_ids"])
    assert rep["summary"]["narrative"] and anchor in rep["summary"]["narrative"]
    assert rep["verification"]["detections_failed"] == 0 and validate_report(rep) == []
    assert "not a finding of wrongdoing" in rep["summary"]["headline"]
    assert "Investigation report" in render_text(rep, trace=True)


def test_scenario_2_insufficient_evidence_takes_a_different_path(loaded, ready, make_agent, det_config):
    a1 = patterns(loaded)[0]["transaction_ids"][0]
    quiet = ordinary_transaction_without_detections(ready, loaded, det_config)
    p1, p2 = AdaptivePolicy(), AdaptivePolicy()
    agent1, _ = make_agent(p1)
    agent1.run({"anchor_type": "transaction", "anchor_id": a1})
    agent2, _ = make_agent(p2)
    rep = agent2.run({"anchor_type": "transaction", "anchor_id": quiet, "question": "Is there anything here worth a closer look?"})
    assert p1.sequence != p2.sequence                                          # same agent code, different evidence, different steps
    assert rep["findings"] == [] and rep["summary"]["outcome"] in ("no_pattern_identified_under_tested_criteria", "not_determined")
    if rep["summary"]["outcome"] == "no_pattern_identified_under_tested_criteria":
        h = rep["summary"]["headline"]
        assert "tested criteria" in h and "does not establish that the activity is legitimate" in h
    text = json.dumps({k: v for k, v in rep.items() if k != "limitations"}).lower()
    assert "is legitimate" not in text.replace("does not establish that the activity is legitimate", "")
    assert rep["metadata"]["run_status"] in ("completed", "incomplete") and validate_report(rep) == []


def test_scenario_3_missing_transaction_stops_without_fabrication(loaded, make_agent):
    agent, prov = make_agent([])
    rep = agent.run({"anchor_type": "transaction", "anchor_id": f"{NS}:TXN_NOT_THERE", "question": "Investigate this."})
    assert rep["metadata"]["run_status"] == "anchor_not_found" and prov.requests == []
    assert rep["findings"] == [] and rep["investigation_activity"]["tool_calls"] == [] and rep["observations"]["retrieved_counts"]["transactions"] == 0
    assert "does not exist" in rep["summary"]["headline"] and validate_report(rep) == []


def test_scenario_4a_truncated_search_keeps_findings_and_discloses(loaded, make_agent):
    anchor = patterns(loaded)[0]["transaction_ids"][0]
    script = [reply(tool_call("run_investigation", anchor_type="transaction", anchor_id=anchor)),
              reply(tool_call("trace_transaction_network", anchor_type="transaction", anchor_id=anchor, max_hops=4, max_paths=1)),
              reply(submit(anchor, f"Examined {anchor}."))]
    agent, _ = make_agent(script)
    rep = agent.run({"anchor_type": "transaction", "anchor_id": anchor})
    assert rep["metadata"]["run_status"] == "incomplete" and rep["metadata"]["completed_within_limits"] is False
    assert any(f["pattern_type"] for f in rep["findings"]) and rep["summary"]["outcome"] == "patterns_identified"
    assert any("truncated or incomplete" in q and "trace_transaction_network" in q for q in rep["outstanding_questions"])
    trace = rep["investigation_activity"]["tool_calls"]
    assert trace[-1]["truncated"] is True and trace[-1]["status"] == "incomplete"
    assert validate_report(rep) == []


def test_scenario_4b_tool_failure_keeps_earlier_findings(loaded, ready, make_agent, spy_graph):
    _, add_hook = spy_graph
    anchor = patterns(loaded)[0]["transaction_ids"][0]
    script = [reply(tool_call("run_investigation", anchor_type="transaction", anchor_id=anchor)),
              reply(tool_call("trace_transaction_network", anchor_type="transaction", anchor_id=anchor)),
              reply(submit(anchor, "Summary."))]
    agent, _ = make_agent(script)

    def fail_trace(q):
        if "RECEIVED_BY|SENT" in q:
            raise redis_exc.ResponseError("Query failed: simulated server fault")
    add_hook(fail_trace)
    rep = agent.run({"anchor_type": "transaction", "anchor_id": anchor})
    t = rep["investigation_activity"]["tool_calls"][-1]
    assert t["tool"] == "trace_transaction_network" and t["status"] == "error" and "simulated server fault" in t["error"]
    assert rep["metadata"]["run_status"] == "incomplete" and rep["findings"]
    assert any("trace_transaction_network" in q and "failed" in q for q in rep["outstanding_questions"])


def test_scenario_5_unsupported_request_is_explained_without_a_conclusion(loaded, make_agent):
    anchor = patterns(loaded)[0]["transaction_ids"][0]
    q = "Prove that the account owner is a money launderer and freeze the account."
    script = [reply(tool_call("run_investigation", anchor_type="transaction", anchor_id=anchor)),
              reply(submit(anchor, f"{anchor} is part of a structural pattern. The owner is a criminal money launderer.",
                           supported=False, limitation="Graph patterns cannot prove a legal conclusion, and no tool can freeze or block accounts."))]
    agent, _ = make_agent(script)
    rep = agent.run({"anchor_type": "transaction", "anchor_id": anchor, "question": q})
    assert rep["request"]["question_assessment"]["supported"] is False
    assert any("cannot establish" in x for x in rep["outstanding_questions"])
    assert "launderer" not in json.dumps({k: v for k, v in rep.items() if k not in ("verification", "request")})
    assert rep["findings"] and "not a finding of wrongdoing" in rep["summary"]["headline"]
    assert not any(c["tool"] not in ("get_transaction_context", "run_investigation") for c in rep["investigation_activity"]["tool_calls"])
    assert validate_report(rep) == []
