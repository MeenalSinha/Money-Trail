"""Tool, result and fact types shared by the registry and the handlers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from money_trail.agent.llm.base import ToolSpec
from money_trail.agent.tools.schema import Arg, json_schema
from money_trail.investigation.detectors.models import Detection

RESULT_STATUSES = ("ok", "not_found", "incomplete", "error", "invalid_arguments")


@dataclass
class ToolResult:
    """What the model sees. `not_found` and an empty `ok` are different from `error`."""
    status: str
    data: Dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "truncated": self.truncated, "warnings": self.warnings, "data": self.data}


@dataclass
class DetectorRun:
    detector: str
    anchor_type: str
    anchor_id: str
    complete: bool
    detections: int


@dataclass
class Facts:
    """Verified records delivered to the model by one tool call; they enter the ledger only if delivered."""
    transactions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    accounts: Set[str] = field(default_factory=set)
    alerts: Set[str] = field(default_factory=set)
    investigations: Set[str] = field(default_factory=set)
    detections: List[Tuple[Detection, str]] = field(default_factory=list)       # (detection, producing tool)
    findings: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    evidence: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    detector_runs: List[DetectorRun] = field(default_factory=list)


@dataclass
class ToolOutput:
    result: ToolResult
    facts: Facts = field(default_factory=Facts)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: Tuple[Arg, ...]
    handler: Callable[[Any, Dict[str, Any]], ToolOutput]
    output_keys: Tuple[str, ...]            # keys that data must contain when status is "ok"
    mutates_state: bool = False
    result_description: str = ""

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, json_schema(self.args))
