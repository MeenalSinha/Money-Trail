"""Central registry: the ONLY way the model can cause anything to execute."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from redis import exceptions as redis_exc

from money_trail.agent.config import ToolLimits
from money_trail.agent.ledger import Ledger
from money_trail.agent.llm.base import ToolSpec
from money_trail.agent.tools.base import RESULT_STATUSES, Tool, ToolOutput, ToolResult
from money_trail.agent.tools.handlers import ToolContext, build_tools
from money_trail.agent.tools.schema import ToolArgumentError, validate_args
from money_trail.exceptions import MoneyTrailError, QueryTimeoutError, ValidationError
from money_trail.investigation.config import DetectionConfig


@dataclass
class ExecutedTool:
    """One tool-call record (the unit of the investigation trace)."""
    tool: str
    arguments: Dict[str, Any]
    result: ToolResult
    duration_ms: int
    executed: bool                        # False: rejected before any query ran
    payload: str                          # exact JSON text delivered to the model
    message: Optional[str] = None
    origin: str = "model"                 # "model" or "system_anchor_resolution"

    @property
    def is_error(self) -> bool:
        return self.result.status in ("error", "invalid_arguments")


class ToolRegistry:
    def __init__(self, tools: Sequence[Tool]) -> None:
        names = [t.name for t in tools]
        if len(set(names)) != len(names):
            raise ValueError("duplicate tool names")
        for t in tools:
            if t.mutates_state:
                raise ValueError(f"tool {t.name} mutates state; model-callable tools must be read-only")
            if not t.description or not callable(t.handler) or not t.output_keys:
                raise ValueError(f"tool {t.name} is not fully specified")
        self._tools = {t.name: t for t in tools}

    @property
    def names(self) -> List[str]:
        return list(self._tools)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def specs(self) -> List[ToolSpec]:
        return [t.spec() for t in self._tools.values()]

    @staticmethod
    def _payload(result: ToolResult) -> str:
        return json.dumps(result.as_dict(), sort_keys=True, separators=(",", ":"), default=str)

    def _reject(self, name: str, raw: Any, message: str, t0: float) -> ExecutedTool:
        res = ToolResult("invalid_arguments", {"error": message})
        args = dict(raw) if isinstance(raw, dict) else {}
        return ExecutedTool(name, args, res, int((time.perf_counter() - t0) * 1000), False, self._payload(res), message)

    def execute(self, ctx: ToolContext, name: str, raw_args: Any, ledger: Ledger, max_chars: int) -> ExecutedTool:
        t0 = time.perf_counter()
        tool = self._tools.get(name)
        if tool is None:
            return self._reject(name, raw_args, f"unknown tool '{name}'; available: {', '.join(self._tools)}", t0)
        try:
            args = validate_args(tool.args, raw_args)
        except ToolArgumentError as exc:
            return self._reject(name, raw_args, f"invalid arguments: {exc}", t0)
        message = None
        try:
            out: ToolOutput = tool.handler(ctx, args)
            res = out.result
            if res.status not in RESULT_STATUSES:
                raise ValueError("bad status")
            if res.status in ("ok", "incomplete") and any(k not in res.data for k in tool.output_keys):
                raise ValueError("output schema")
        except QueryTimeoutError:
            out = ToolOutput(ToolResult("incomplete", {}, True, ["the query timed out; no result is available. This does "
                                                                 "not mean nothing exists. Retry with tighter bounds."]))
            res, message = out.result, "query timeout"
        except ValidationError as exc:
            out = ToolOutput(ToolResult("invalid_arguments", {"error": str(exc)[:300]}))
            res, message = out.result, str(exc)[:300]
        except (MoneyTrailError, redis_exc.RedisError) as exc:
            msg = f"{type(exc).__name__}: {str(exc)[:200]}"
            out = ToolOutput(ToolResult("error", {"error": msg}))
            res, message = out.result, msg
        except Exception as exc:                                   # defect guard: never leak internals to the model
            out = ToolOutput(ToolResult("error", {"error": f"internal tool failure ({type(exc).__name__})"}))
            res, message = out.result, f"internal failure {type(exc).__name__}"
        payload = self._payload(res)
        if len(payload) > max_chars:
            res = ToolResult("incomplete", {"result_too_large": True, "size_chars": len(payload), "limit_chars": max_chars},
                             True, ["the result exceeded the maximum size and was not delivered; repeat with a smaller "
                                    "limit, fewer hops or fewer paths"])
            payload, out = self._payload(res), ToolOutput(res)
            message = "result too large"
        elif res.status in ("ok", "incomplete"):
            ledger.apply(out.facts)                                # only what was delivered becomes citable
            ledger.add_payload_numbers(res.data)
        return ExecutedTool(name, args, res, int((time.perf_counter() - t0) * 1000), True, payload, message)


def build_registry(detection: DetectionConfig, limits: ToolLimits) -> ToolRegistry:
    return ToolRegistry(build_tools(detection, limits))
