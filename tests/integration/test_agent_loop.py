"""The agent loop against a REAL FalkorDB with a SCRIPTED model (orchestration tests; spec tests G, I, J).
The scripted provider is a test double: these tests prove the loop, limits, validation, verification and reporting behave,
not that any real model chooses well."""
import json
import subprocess
import sys

import pytest

from agent_fakes import conversation, submit
from money_trail.agent import errors as E
from money_trail.agent.report import validate_report
from money_trail.agent.testing import ScriptedProvider, reply, tool_call
from money_trail.investigation.engine import InvestigationEngine, InvestigationRequest

NS = "loop-ns"


@pytest.fixture
def loaded(ready, run_ingest, make_dataset):
    ds = make_dataset(NS, seed=3)
    assert run_ingest(ds)["status"] == "succeeded"
    return ds


def gt(ds, ptype):
    return [p for p in json.loads((ds.dir / "ground_truth.json").read_text())["patterns"] if p["type"] == ptype]


def anchor_of(ds):
    return gt(ds, "circular")[0]["transaction_ids"][0]


def run(agent, anchor, **kw):
    return agent.run({"anchor_type": "transaction", "anchor_id": anchor, "question": "Does this warrant review?"}, **kw)


def det_call(anchor, detector="circular"):
    return tool_call("run_pattern_detector", detector=detector, anchor_type="transaction", anchor_id=anchor)


OK_SUBMIT = lambda: submit(None, "Reviewed the retrieved results.")
SUBMIT_ONLY = ["submit_investigation_report"]


def ex_counts(rep):
    calls = rep["investigation_activity"]["tool_calls"]
    return sum(1 for c in calls if c["executed"]), sum(1 for c in calls if not c["executed"])


# ---- G: limits ------------------------------------------------------------------------------------
def test_g_tool_call_limit_stops_safely_and_forces_a_final_report(loaded, ready, make_agent):
    anchor = anchor_of(loaded)
    n = [0]

    def greedy(req):
        if [t.name for t in req.tools] == SUBMIT_ONLY:
            return reply(submit(anchor, "Stopped at the limit."))
        n[0] += 1
        return reply(tool_call("get_account_activity", account_id=ready.graph.ro_query("MATCH (a:Account) RETURN a.account_id LIMIT 1").result_set[0][0], limit=n[0]))
    agent, prov = make_agent(greedy, {"agent": {"max_tool_calls": 3, "max_steps": 20}})
    rep = run(agent, anchor)
    executed, rejected = ex_counts(rep)
    assert executed == 3 and rejected == 0                                    # seed + 2 model calls, never more
    assert "tool_call_limit_reached" in rep["metadata"]["completion_reasons"]
    assert rep["metadata"]["run_status"] == "incomplete" and rep["metadata"]["completed_within_limits"] is False
    assert any("tool-call limit" in q for q in rep["outstanding_questions"])
    assert prov.requests[-1].forced_tool == "submit_investigation_report" and [t.name for t in prov.requests[-1].tools] == SUBMIT_ONLY
    assert validate_report(rep) == []


def test_g_step_limit_bounds_model_requests(loaded, make_agent):
    anchor = anchor_of(loaded)
    n = [0]

    def loop_forever(req):
        if req.forced_tool:
            return reply(submit(anchor, "Out of steps."))
        n[0] += 1
        return reply(tool_call("get_account_activity", account_id="x:y", limit=n[0]))
    agent, prov = make_agent(loop_forever, {"agent": {"max_steps": 3, "max_tool_calls": 50}})
    rep = run(agent, anchor)
    assert len(prov.requests) == 3 and "step_limit_reached" in rep["metadata"]["completion_reasons"]
    assert rep["metadata"]["run_status"] == "incomplete" and len(rep["investigation_activity"]["model_steps"]) == 3


def test_g_request_limits_can_lower_but_not_raise(loaded, make_agent):
    anchor = anchor_of(loaded)
    agent, prov = make_agent([det_call(anchor)] and [reply(det_call(anchor)), reply(submit(anchor, "ok"))])
    rep = agent.run({"anchor_type": "transaction", "anchor_id": anchor, "limits": {"max_tool_calls": 100}})
    assert rep["metadata"]["run_status"] == "invalid_request" and "exceeds" in rep["outstanding_questions"][0] and not prov.requests
    agent, prov = make_agent([reply(det_call(anchor)), reply(det_call(anchor, "fan_in")), reply(submit(anchor, "x"))])
    rep = agent.run({"anchor_type": "transaction", "anchor_id": anchor, "limits": {"max_tool_calls": 2}})
    assert ex_counts(rep)[0] == 2 and "tool_call_limit_reached" in rep["metadata"]["completion_reasons"]


def test_g_time_limit_preserves_earlier_findings(loaded, make_agent):
    anchor = anchor_of(loaded)

    class Clock:
        t = 0.0

        def __call__(self):
            return self.t
    clock, n = Clock(), [0]

    def policy(req):
        n[0] += 1
        if n[0] == 1:
            return reply(det_call(anchor))                         # in time: executed, produces a finding
        clock.t += 1000                                             # the model is now slow: the deadline passes during this request
        return reply(tool_call("get_account_activity", account_id="x:y"))
    agent, prov = make_agent(policy, {"agent": {"max_duration_seconds": 60}}, monotonic=clock)
    rep = run(agent, anchor)
    assert "time_limit_reached" in rep["metadata"]["completion_reasons"] and rep["metadata"]["run_status"] == "incomplete"
    assert any(f["pattern_type"] == "circular" for f in rep["findings"])      # evidence collected before the limit is kept
    assert any("time limit" in q for q in rep["outstanding_questions"]) and len(prov.requests) == 2
    assert ex_counts(rep)[0] == 2                                              # the post-deadline call was not executed


# ---- invalid / malformed model behaviour -----------------------------------------------------------
def test_invalid_tool_calls_are_rejected_without_execution_and_the_loop_continues(loaded, make_agent):
    anchor = anchor_of(loaded)
    script = [reply(tool_call("drop_everything"), tool_call("get_transaction_context", transaction_id=anchor, cypher="MATCH (n) DETACH DELETE n")),
              reply(det_call(anchor)), reply(submit(anchor, "Done."))]
    agent, prov = make_agent(script)
    rep = run(agent, anchor)
    calls = rep["investigation_activity"]["tool_calls"]
    assert [(c["tool"], c["status"], c["executed"]) for c in calls[1:3]] == [("drop_everything", "invalid_arguments", False),
                                                                          ("get_transaction_context", "invalid_arguments", False)]
    msgs = prov.requests[1].messages[-1].blocks                                # the model was told why, per call id
    assert len(msgs) == 2 and all(b.is_error for b in msgs) and "unknown tool" in msgs[0].content
    assert rep["metadata"]["run_status"] == "completed" and any(f["pattern_type"] == "circular" for f in rep["findings"])
    assert ex_counts(rep)[0] == 2                                              # rejected calls consume no tool-call budget


def test_too_many_invalid_calls_stop_the_run(loaded, make_agent):
    anchor = anchor_of(loaded)
    agent, _ = make_agent([reply(tool_call("nope1")), reply(tool_call("nope2")), reply(tool_call("nope3"))],
                          {"agent": {"max_invalid_tool_calls": 1}})
    rep = run(agent, anchor)
    assert "too_many_invalid_tool_calls" in rep["metadata"]["completion_reasons"] and rep["metadata"]["run_status"] == "incomplete"


def test_malformed_report_gets_one_bounded_recovery_then_succeeds(loaded, make_agent):
    anchor = anchor_of(loaded)
    bad = tool_call("submit_investigation_report", summary="", question_assessment={"supported": True})
    agent, prov = make_agent([reply(det_call(anchor)), reply(bad), reply(submit(anchor, "Corrected report."))])
    rep = run(agent, anchor)
    assert rep["metadata"]["run_status"] == "completed" and rep["summary"]["narrative"] == "Corrected report."
    assert "rejected" in prov.requests[2].messages[-1].blocks[0].content


def test_repeated_malformed_reports_stop_without_inventing_a_summary(loaded, make_agent):
    anchor = anchor_of(loaded)
    bad = lambda: tool_call("submit_investigation_report", summary="x", question_assessment="yes")
    agent, _ = make_agent([reply(det_call(anchor)), reply(bad()), reply(bad())])
    rep = run(agent, anchor)
    assert "invalid_model_output" in rep["metadata"]["completion_reasons"] and rep["summary"]["narrative"] is None
    assert any(f["pattern_type"] == "circular" for f in rep["findings"])


@pytest.mark.parametrize("resp", [reply(text="I will just talk."), reply(text="cut off", stop="max_tokens")])
def test_prose_or_truncated_output_is_never_executed_and_recovers_once(loaded, make_agent, resp):
    anchor = anchor_of(loaded)
    agent, prov = make_agent([resp, reply(submit(anchor, "Recovered."))])
    rep = run(agent, anchor)
    assert rep["metadata"]["run_status"] == "completed" and ex_counts(rep) == (1, 0)      # only the system's anchor lookup ran
    assert "unusable" in prov.requests[1].messages[-1].blocks[0].text
    agent, _ = make_agent([resp, resp])
    assert "invalid_model_output" in run(agent, anchor)["metadata"]["completion_reasons"]


@pytest.mark.parametrize("stop,reason", [("refusal", "model_refusal"), ("context_exceeded", "context_limit")])
def test_refusal_and_context_overflow_are_reported(loaded, make_agent, stop, reason):
    anchor = anchor_of(loaded)
    agent, _ = make_agent([reply(det_call(anchor)), reply(text="", stop=stop)])
    rep = run(agent, anchor)
    assert reason in rep["metadata"]["completion_reasons"] and rep["metadata"]["run_status"] == "incomplete"
    assert any(f["pattern_type"] == "circular" for f in rep["findings"]) and any(reason in q for q in rep["outstanding_questions"])


# ---- provider failures ----------------------------------------------------------------------------
def test_provider_failure_after_evidence_preserves_it_and_discloses(loaded, make_agent):
    anchor = anchor_of(loaded)
    sleeps = []
    script = [reply(det_call(anchor))] + [E.ProviderRateLimitError("limited")] * 3
    agent, prov = make_agent(script, sleep=sleeps.append)
    rep = run(agent, anchor)
    u = rep["investigation_activity"]["provider_usage"]
    assert rep["metadata"]["run_status"] == "incomplete" and "provider_error:rate_limited" in rep["metadata"]["completion_reasons"]
    assert u["requests"] == 4 and u["retries"] == 2 and sleeps == [1, 2]       # 1 good + 3 failed attempts = max_retries(2)+1
    assert rep["summary"]["narrative"] is None and len(rep["findings"]) == 2   # no invented analysis; verified evidence kept
    assert any("provider failure (rate_limited)" in q for q in rep["outstanding_questions"])
    assert validate_report(rep) == []


def test_provider_failure_on_first_request_is_failed_and_not_retried_when_permanent(loaded, make_agent):
    anchor = anchor_of(loaded)
    agent, prov = make_agent([E.ProviderAuthError("rejected")])
    rep = run(agent, anchor)
    assert rep["metadata"]["run_status"] == "failed" and "provider_error:authentication_failed" in rep["metadata"]["completion_reasons"]
    assert rep["investigation_activity"]["provider_usage"]["requests"] == 1 and rep["findings"] == []
    assert rep["metadata"]["error"] and "rejected" in rep["metadata"]["error"]


def test_transient_provider_error_then_success(loaded, make_agent):
    anchor = anchor_of(loaded)
    agent, _ = make_agent([E.ProviderTimeoutError("t"), reply(det_call(anchor)), reply(submit(anchor, "Done."))])
    rep = run(agent, anchor)
    u = rep["investigation_activity"]["provider_usage"]
    assert rep["metadata"]["run_status"] == "completed" and u["retries"] == 1 and u["failed_attempts"] == ["timeout"]


# ---- I: unsupported conclusions cannot enter the report --------------------------------------------
def test_i_fabricated_records_and_conclusions_are_filtered_and_findings_unchanged(loaded, ready, make_agent):
    anchor = anchor_of(loaded)
    base_agent, _ = make_agent([reply(det_call(anchor)), reply(submit(anchor, "Neutral summary."))])
    base = run(base_agent, anchor)
    did = base["findings"][0]["detection_id"]
    lies = submit(anchor, f"Transaction {NS}:T999999999 moved 5000000 USD. This is money laundering by {NS}:ACC_FAKE. {anchor} is a cycle member.",
                  detections=[{"detection_id": did, "interpretation": "Proven fraud by the account owner.", "hypothesis": f"Funds may have returned to {NS}:ACC_FAKE."},
                              {"detection_id": "det:FABRICATED", "interpretation": "A pass-through chain exists."}],
                  outstanding=["Confirm that 9999 USD was lost.", "Review the timing gaps."])
    agent, _ = make_agent([reply(det_call(anchor)), reply(lies)])
    rep = run(agent, anchor)
    visible = json.dumps({k: v for k, v in rep.items() if k not in ("verification", "limitations")})   # everything the model could influence
    for bad in ("T999999999", "5000000", "ACC_FAKE", "FABRICATED", "money laundering", "Proven fraud", "9999 USD", "pass-through chain exists"):
        assert bad not in visible, bad
    rej = rep["verification"]["model_text"]["rejected"]
    assert rej and all(set(x) == {"field", "reasons", "chars", "sha256_12"} for x in rej)   # audited by reason/hash, content not echoed
    assert not any("Proven fraud" in json.dumps(x) or "money laundering" in json.dumps(x) for x in rej)
    assert f"{anchor} is a cycle member." in rep["summary"]["narrative"]       # the one grounded sentence survives
    assert "Review the timing gaps." in rep["outstanding_questions"]
    strip = lambda r: [(f["finding_id"], f["classification"], f["description"], f["supporting_transaction_ids"], f["evidence_ids"])
                       for f in r["findings"] if f["source"] == "deterministic_rule"]
    assert strip(rep) == strip(base)                                           # model text cannot alter or add findings
    assert rep["verification"]["model_text"]["sentences_rejected"] >= 6
    for f in rep["findings"]:                                                  # every cited transaction exists in the graph
        for t in f["supporting_transaction_ids"]:
            assert ready.graph.ro_query("MATCH (t:Transaction {transaction_id:$i}) RETURN 1", {"i": t}).result_set
    assert validate_report(rep) == []


def test_i_report_time_verification_marks_findings_unverified_when_the_graph_changes(loaded, ready, make_agent):
    anchor = anchor_of(loaded)
    victim = gt(loaded, "circular")[0]["transaction_ids"][1]

    def delete_then_submit(req):
        ready.graph.query("MATCH (t:Transaction {transaction_id:$i}) DETACH DELETE t", {"i": victim})
        return reply(submit(anchor, "Summary after the data changed."))
    agent, _ = make_agent([reply(det_call(anchor, "circular")), delete_then_submit])
    rep = run(agent, anchor)
    assert rep["findings"] == [] and len(rep["unverified_findings"]) == 1
    assert "no longer" in rep["unverified_findings"][0]["reason"] or "missing" in rep["unverified_findings"][0]["reason"].lower() or rep["unverified_findings"][0]["reason"]
    assert rep["summary"]["outcome"] == "not_determined" and rep["verification"]["detections_failed"] == 1
    assert any("failed verification" in q for q in rep["outstanding_questions"])


# ---- J: ground-truth isolation ---------------------------------------------------------------------
def test_j_no_ground_truth_reaches_tools_graph_or_report(loaded, ready, make_agent):
    anchor = anchor_of(loaded)
    agent, prov = make_agent([reply(tool_call("run_investigation", anchor_type="transaction", anchor_id=anchor)),
                              reply(submit(anchor, f"{anchor} appears in a detection."))])
    rep = run(agent, anchor)
    blob = json.dumps(rep) + json.dumps([[getattr(b, "content", getattr(b, "text", "")) for b in m.blocks] for r in prov.requests for m in r.messages])
    for marker in ("pattern_id", ":pattern:", "ground_truth", "evaluation_only", "format_version", "seed_alert_summary"):
        assert marker not in blob, marker
    keys = {r[0] for r in ready.graph.ro_query("MATCH (n) UNWIND keys(n) AS k RETURN DISTINCT k").result_set}
    assert not {"pattern_id", "pattern_type", "ground_truth", "is_suspicious", "label"} & keys
    out = subprocess.run([sys.executable, "-c", "import money_trail.agent.agent, money_trail.agent.tools.registry, sys; "
                          "sys.exit(1 if any(m.startswith('money_trail.evaluation') for m in sys.modules) else 0)"], capture_output=True)
    assert out.returncode == 0


# ---- request outcomes without a model ----------------------------------------------------------------
@pytest.mark.parametrize("raw,status,req_status", [
    ({}, "invalid_request", "missing_anchor"),
    ({"anchor_type": "transaction", "anchor_id": f"{NS}:NOPE"}, "anchor_not_found", "unknown_anchor"),
    ({"anchor_type": "account", "anchor_id": f"{NS}:NOPE"}, "anchor_not_found", "unknown_anchor"),
    ({"anchor_type": "bogus", "anchor_id": "x"}, "invalid_request", "invalid_request"),
    ({"anchor_type": "transaction", "anchor_id": f"{NS}:T000000001", "intent": "prove_fraud"}, "unsupported_request", "valid_unsupported_intent"),
])
def test_requests_that_cannot_run_never_reach_the_model_or_the_tools(loaded, make_agent, raw, status, req_status):
    agent, prov = make_agent([])
    rep = agent.run(raw)
    assert rep["metadata"]["run_status"] == status and rep["request"]["status"] == req_status
    assert prov.requests == [] and rep["investigation_activity"]["tool_calls"] == [] and rep["findings"] == []
    assert rep["summary"]["outcome"] == "not_determined" and validate_report(rep) == []


# ---- persistence ----------------------------------------------------------------------------------
def counts(conn):
    return tuple(conn.graph.ro_query(f"MATCH (n:{l}) RETURN count(n)").result_set[0][0] for l in ("Investigation", "Finding", "Evidence"))


def test_default_run_is_read_only_and_persist_is_explicit_idempotent_and_verified(loaded, ready, make_agent):
    anchor = anchor_of(loaded)
    script = lambda: [reply(tool_call("run_investigation", anchor_type="transaction", anchor_id=anchor)), reply(submit(anchor, "Done."))]
    assert counts(ready) == (0, 0, 0)
    agent, _ = make_agent(script())
    rep0 = run(agent, anchor)
    assert counts(ready) == (0, 0, 0) and rep0["metadata"]["investigation_id"] is None          # no --persist, no writes
    agent, _ = make_agent(script())
    rep1 = run(agent, anchor, persist=True)
    c1 = counts(ready)
    assert c1[0] == 1 and c1[1] >= 2 and c1[2] >= 1 and rep1["metadata"]["investigation_id"]
    pc = rep1["verification"]["persistence_check"]
    assert pc and all(v["evidence_present"] and v["references_match_detection"] for v in pc.values())
    assert all(f["persisted"] for f in rep1["findings"] if f["source"] == "deterministic_rule")
    agent, _ = make_agent(script())
    rep2 = run(agent, anchor, persist=True)
    assert counts(ready) == c1 and rep2["metadata"]["investigation_id"] == rep1["metadata"]["investigation_id"]   # rerun: no duplicates
    assert validate_report(rep1) == validate_report(rep2) == []


def test_persist_requires_a_transaction_anchor_and_does_not_touch_the_graph(loaded, ready, make_agent):
    acct = ready.graph.ro_query("MATCH (a:Account) RETURN a.account_id LIMIT 1").result_set[0][0]
    agent, prov = make_agent([])
    rep = agent.run({"anchor_type": "account", "anchor_id": acct}, persist=True)
    assert rep["metadata"]["run_status"] == "invalid_request" and "transaction anchor" in rep["outstanding_questions"][0]
    assert prov.requests == [] and counts(ready) == (0, 0, 0)


def test_later_run_sees_stored_findings_and_can_verify_them_through_the_tools(loaded, ready, make_agent):
    anchor = anchor_of(loaded)
    agent, _ = make_agent([reply(tool_call("run_investigation", anchor_type="transaction", anchor_id=anchor)), reply(submit(anchor, "First."))])
    run(agent, anchor, persist=True)

    def follow_stored(req):
        calls = conversation(req)
        if not calls:
            from agent_fakes import parse_seed
            stored = parse_seed(req)["stored_findings"]
            assert stored, "the seed context must expose findings stored by the earlier run"
            return reply(tool_call("get_finding_details", finding_id=stored[0]["finding_id"]))
        if calls[-1][0] == "get_finding_details":
            return reply(tool_call("get_evidence_details", evidence_id=calls[-1][2]["data"]["evidence"][0]["evidence"]["evidence_id"]))
        return reply(submit(anchor, "Stored records were retrieved."))
    agent, _ = make_agent(follow_stored)
    rep = agent.run({"anchor_type": "transaction", "anchor_id": anchor, "intent": "evidence_verification"})
    kinds = {r["kind"]: r for r in rep["stored_records_reviewed"]}
    assert rep["metadata"]["tools_executed"] == ["get_transaction_context", "get_finding_details", "get_evidence_details"]
    assert kinds["finding"]["references_valid"] is True and kinds["evidence"]["complete"] is True
    assert rep["metadata"]["run_status"] == "completed" and validate_report(rep) == []
