"""Test doubles for the provider interface. NOT a provider: nothing here talks to a model, and the CLI never uses it.

Tests built on these classes verify the agent's orchestration (limits, validation, failure handling, grounding). They say
nothing about how well any real model chooses tools; real-provider behaviour is covered only by the `provider` tests.
"""
from __future__ import annotations

import itertools
from typing import Any, Callable, List, Optional, Union

from money_trail.agent.llm.base import CompletionRequest, LLMResponse, ToolUseBlock, Usage

_ids = itertools.count(1)


def tool_call(name: str, **arguments: Any) -> ToolUseBlock:
    return ToolUseBlock(f"toolu_test_{next(_ids)}", name, arguments)


def reply(*calls: ToolUseBlock, text: str = "", stop: Optional[str] = None, usage: Usage = Usage(10, 5)) -> LLMResponse:
    return LLMResponse(text, tuple(calls), stop or ("tool_use" if calls else "end_turn"), usage)


class ScriptedProvider:
    """Returns the next scripted item per request: an LLMResponse, an exception to raise, or a callable(request)."""
    name = "scripted"
    model = "scripted-test-double"

    def __init__(self, script: List[Union[LLMResponse, Exception, Callable[[CompletionRequest], LLMResponse]]]) -> None:
        self._script = list(script)
        self.requests: List[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> LLMResponse:
        self.requests.append(request)
        if not self._script:
            raise AssertionError("scripted provider has no more responses")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item(request) if callable(item) else item


class PolicyProvider:
    """Wraps a callable(request) -> LLMResponse as a provider, for tests whose scripted model decides from the conversation."""
    name = "scripted-policy"
    model = "scripted-test-double"

    def __init__(self, policy: Callable[[CompletionRequest], LLMResponse]) -> None:
        self._policy = policy
        self.requests: List[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> LLMResponse:
        self.requests.append(request)
        return self._policy(request)
