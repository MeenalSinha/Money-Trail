"""Tool schema validation and registry integrity (no database)."""
import pytest

from money_trail.agent.config import parse_agent_config
from money_trail.agent.ledger import Ledger
from money_trail.agent.tools.base import Facts, Tool, ToolOutput, ToolResult
from money_trail.agent.tools.registry import ToolRegistry, build_registry
from money_trail.agent.tools.schema import Arg, ToolArgumentError, json_schema, validate_args
from money_trail.investigation.config import parse_detection_config
from support import agent_dict, detection_dict

LIM = parse_agent_config(agent_dict()).tools
DET = parse_detection_config(detection_dict())
EXPECTED = ["get_transaction_context", "get_account_activity", "trace_transaction_network", "run_pattern_detector",
            "get_finding_details", "get_evidence_details", "run_investigation"]


def test_registry_contains_exactly_the_seven_implemented_tools_all_read_only():
    reg = build_registry(DET, LIM)
    assert reg.names == EXPECTED
    for n in reg.names:
        t = reg.get(n)
        assert t.mutates_state is False and t.description and callable(t.handler) and t.output_keys


def test_schemas_are_closed_and_offer_no_way_to_pass_queries_code_or_paths():
    reg = build_registry(DET, LIM)
    forbidden = {"cypher", "query", "sql", "code", "function", "path", "file", "command", "script"}
    for spec in reg.specs():
        s = spec.input_schema
        assert s["additionalProperties"] is False and s["type"] == "object"
        assert not forbidden & set(s["properties"]), spec.name
        assert set(s["required"]) <= set(s["properties"])


def test_detector_enum_comes_from_the_detection_config_not_from_the_model():
    only = detection_dict()
    for k in only["detectors"]:
        only["detectors"][k]["enabled"] = k in ("circular", "fan_in")
    reg = build_registry(parse_detection_config(only), LIM)
    enum = reg.get("run_pattern_detector").spec().input_schema["properties"]["detector"]["enum"]
    assert enum == ["circular", "fan_in"]


def test_numeric_limits_in_schemas_match_configuration():
    reg = build_registry(DET, LIM)
    props = reg.get("trace_transaction_network").spec().input_schema["properties"]
    assert props["max_hops"]["maximum"] == LIM.max_hops and props["max_paths"]["maximum"] == LIM.max_paths
    assert reg.get("get_account_activity").spec().input_schema["properties"]["limit"]["maximum"] == LIM.max_results


ARGS = (Arg("id", "string", "x", is_id=True), Arg("n", "integer", "x", required=False, minimum=1, maximum=5),
        Arg("mode", "string", "x", required=False, enum=("a", "b")))


@pytest.mark.parametrize("raw,fragment", [
    ({}, "missing required argument 'id'"), ({"id": "x", "zzz": 1}, "unexpected argument 'zzz'"),
    ({"id": "x", "n": True}, "must be an integer"), ({"id": "x", "n": "3"}, "must be an integer"),
    ({"id": "x", "n": 0}, "at least 1"), ({"id": "x", "n": 6}, "at most 5"), ({"id": "x", "mode": "c"}, "one of"),
    ({"id": 5}, "must be a string"), ({"id": "a\"b"}, "well-formed"), ({"id": " lead"}, "well-formed"),
    ({"id": "x" * 300}, "well-formed"), ({"id": "a\nb"}, "well-formed"), ("not a dict", "JSON object"),
])
def test_argument_validation_rejects(raw, fragment):
    with pytest.raises(ToolArgumentError) as e:
        validate_args(ARGS, raw)
    assert fragment in str(e.value)


def test_argument_validation_accepts_and_drops_none():
    assert validate_args(ARGS, {"id": "ns:T 1", "n": 3, "mode": None}) == {"id": "ns:T 1", "n": 3}
    assert json_schema(ARGS)["required"] == ["id"]


def _tool(handler, name="t", keys=("k",), mutates=False):
    return Tool(name, "desc", (Arg("id", "string", "x", is_id=True),), handler, keys, mutates)


def test_registry_rejects_duplicates_mutators_and_incomplete_tools():
    ok = lambda c, a: ToolOutput(ToolResult("ok", {"k": 1}))
    with pytest.raises(ValueError, match="duplicate"):
        ToolRegistry([_tool(ok), _tool(ok)])
    with pytest.raises(ValueError, match="read-only"):
        ToolRegistry([_tool(ok, mutates=True)])
    with pytest.raises(ValueError, match="not fully specified"):
        ToolRegistry([Tool("t", "", (), ok, ("k",))])


def test_unknown_tool_and_bad_args_are_rejected_before_the_handler_runs():
    calls = []
    def handler(c, a):
        calls.append(a)
        return ToolOutput(ToolResult("ok", {"k": 1}))
    reg, L = ToolRegistry([_tool(handler)]), Ledger()
    e1 = reg.execute(None, "nope", {}, L, 1000)
    e2 = reg.execute(None, "t", {"id": "x", "extra": 1}, L, 1000)
    assert not e1.executed and not e2.executed and not calls
    assert e1.result.status == e2.result.status == "invalid_arguments" and "unknown tool" in e1.message


def test_handler_failures_become_error_results_without_leaking_internals_and_facts_not_recorded():
    def boom(c, a):
        raise RuntimeError("secret internal detail /etc/passwd")
    def wrong_shape(c, a):
        return ToolOutput(ToolResult("ok", {"unexpected": 1}), Facts(accounts={"ns:A"}))
    reg, L = ToolRegistry([_tool(boom, "boom"), _tool(wrong_shape, "bad")]), Ledger()
    e = reg.execute(None, "boom", {"id": "x"}, L, 1000)
    assert e.result.status == "error" and "passwd" not in e.payload and "RuntimeError" in e.payload
    e = reg.execute(None, "bad", {"id": "x"}, L, 1000)
    assert e.result.status == "error" and not L.accounts          # schema-violating output is never trusted


def test_oversized_results_are_not_delivered_and_do_not_enter_the_ledger():
    big = lambda c, a: ToolOutput(ToolResult("ok", {"k": "x" * 5000}), Facts(accounts={"ns:SEEN"}))
    reg, L = ToolRegistry([_tool(big)]), Ledger()
    e = reg.execute(None, "t", {"id": "x"}, L, 1000)
    assert e.result.status == "incomplete" and e.result.data["result_too_large"] and not L.accounts and len(e.payload) < 1000


def test_delivered_facts_enter_the_ledger_and_only_then_identifiers_are_citable():
    f = Facts(accounts={"ns:A"}, transactions={"ns:T": {"transaction_id": "ns:T", "amount": 5}})
    ok = lambda c, a: ToolOutput(ToolResult("ok", {"k": 1}), f)
    L = Ledger()
    ToolRegistry([_tool(ok)]).execute(None, "t", {"id": "x"}, L, 1000)
    assert {"ns:A", "ns:T"} <= L.identifiers() and "ns:Z" not in L.identifiers()
