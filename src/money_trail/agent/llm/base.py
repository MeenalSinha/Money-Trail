"""Provider-neutral conversation types and the minimal provider interface.

The agent talks only to `LLMProvider.complete`. Nothing outside `anthropic_provider.py` knows a vendor SDK.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol, Tuple, Union


@dataclass(frozen=True)
class TextBlock:
    text: str


@dataclass(frozen=True)
class ToolUseBlock:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass(frozen=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False


Block = Union[TextBlock, ToolUseBlock, ToolResultBlock]


@dataclass(frozen=True)
class Message:
    role: str                       # "user" | "assistant"
    blocks: Tuple[Block, ...]

    def __post_init__(self):
        if self.role not in ("user", "assistant"):
            raise ValueError("role must be 'user' or 'assistant'")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Dict[str, Any]


@dataclass(frozen=True)
class Usage:
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


STOP_REASONS = ("end_turn", "tool_use", "max_tokens", "refusal", "context_exceeded")


@dataclass(frozen=True)
class LLMResponse:
    text: str
    tool_calls: Tuple[ToolUseBlock, ...]
    stop_reason: str                # one of STOP_REASONS
    usage: Usage = field(default_factory=Usage)
    raw_stop_reason: Optional[str] = None

    def to_message(self) -> Message:
        blocks = ((TextBlock(self.text),) if self.text.strip() else ()) + tuple(self.tool_calls)
        return Message("assistant", blocks)


@dataclass(frozen=True)
class CompletionRequest:
    system: str
    messages: Tuple[Message, ...]
    tools: Tuple[ToolSpec, ...]
    max_output_tokens: int
    timeout_seconds: float
    forced_tool: Optional[str] = None     # the model must call exactly this tool


class LLMProvider(Protocol):
    name: str
    model: str

    def complete(self, request: CompletionRequest) -> LLMResponse: ...
