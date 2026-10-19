"""Scripted model behaviour for ORCHESTRATION tests. This is a test double, not an LLM: it decides each next call from the
tool results actually present in the conversation so that end-to-end tests exercise different paths for different evidence.
It demonstrates that the agent loop, tools, verification and report work together; it says nothing about how well a real
model selects tools (that is only measurable with a real provider; see docs/PHASE4 verification report)."""
import json

from money_trail.agent.llm.base import CompletionRequest, ToolResultBlock, ToolUseBlock
from money_trail.agent.testing import reply, tool_call


def parse_seed(request: CompletionRequest) -> dict:
    text = request.messages[0].blocks[0].text
    payload = text.split("Result:\n", 1)[1].split("\n\nDecide", 1)[0]
    return json.loads(payload)["data"]


def conversation(request: CompletionRequest):
    """[(tool_name, arguments, parsed_result_or_None)] for every model-chosen call so far."""
    results = {}
    for m in request.messages:
        for b in m.blocks:
            if isinstance(b, ToolResultBlock):
                try:
                    results[b.tool_use_id] = json.loads(b.content)
                except ValueError:
                    results[b.tool_use_id] = None
    out = []
    for m in request.messages:
        for b in m.blocks:
            if isinstance(b, ToolUseBlock):
                out.append((b.name, b.arguments, results.get(b.id)))
    return out


def submit(anchor, summary, detections=(), supported=True, limitation=None, outstanding=()):
    qa = {"supported": supported}
    if limitation:
        qa["limitation"] = limitation
    return tool_call("submit_investigation_report", summary=summary, question_assessment=qa, detections=list(detections),
                     outstanding_questions=list(outstanding))


class AdaptivePolicy:
    """probe the destination account; if it is sparse probe the source account; if either is connected run the broad
    investigation, otherwise stop; if the broad investigation finds nothing, follow up with a trace; then submit a report
    grounded in what was returned."""

    def __init__(self):
        self.sequence = []

    def __call__(self, request):
        seed = parse_seed(request)
        tx = seed["transaction"]["transaction_id"]
        calls = conversation(request)
        names = [c[0] for c in calls]
        if not calls:
            return self._go(reply(tool_call("get_account_activity", account_id=seed["destination_accounts"][0], limit=10)))
        last_name, _, last = calls[-1]
        if last_name == "get_account_activity":
            connected = len(last["data"]["activity"]) >= 2
            probed = [c[1]["account_id"] for c in calls if c[0] == "get_account_activity"]
            if not connected and seed["source_accounts"][0] not in probed:          # sparse destination: look at the source side too
                return self._go(reply(tool_call("get_account_activity", account_id=seed["source_accounts"][0], limit=10)))
            if connected:
                return self._go(reply(tool_call("run_investigation", anchor_type="transaction", anchor_id=tx)))
            return self._go(reply(submit(tx, f"Transaction {tx} has limited connected activity. No detector was run.")))
        if last_name == "run_investigation":
            dets = last["data"]["detections"]
            if dets:
                d = dets[0]
                return self._go(reply(submit(tx, f"Transaction {tx} is part of {len(dets)} structural detection(s), including "
                                                 f"{d['pattern_type']} {d['detection_id']}.",
                                             detections=[{"detection_id": d["detection_id"],
                                                          "interpretation": f"The {d['pattern_type']} structure through "
                                                                            f"{d['account_ids'][0]} may warrant review."}])))
            return self._go(reply(tool_call("trace_transaction_network", anchor_type="transaction", anchor_id=tx, direction="forward", max_hops=3)))
        if last_name == "trace_transaction_network":
            if "path_count" not in last["data"]:                  # the size guard withheld an oversized result
                return self._go(reply(submit(tx, f"No configured pattern was identified for {tx}; the trace result was too large to review.")))
            n = last["data"]["path_count"]
            return self._go(reply(submit(tx, f"No configured pattern was identified for {tx}; the forward trace found {n} path(s).")))
        return self._go(reply(submit(tx, "Done.")))

    def _go(self, resp):
        self.sequence.append([c.name for c in resp.tool_calls])
        return resp
