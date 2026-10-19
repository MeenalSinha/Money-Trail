"""Anthropic Messages API adapter (SDK pinned in pyproject.toml).

Capabilities used, each confirmed against the installed SDK: `messages.create(model, max_tokens, system, messages,
tools, tool_choice, timeout)`, `tool_use` / `tool_result` blocks and `usage`. The pinned SDK exposes no sampling
temperature parameter, so none is configurable. SDK-level retries are disabled (`max_retries=0`): the bounded retry
policy lives in `retry.py`, so the configured retry count is the only one in effect.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from money_trail.agent.errors import (
    ProviderAuthError, ProviderBadRequestError, ProviderConfigError, ProviderContextError, ProviderError,
    ProviderRateLimitError, ProviderResponseError, ProviderTimeoutError, ProviderUnavailableError,
)
from money_trail.agent.llm.base import (
    CompletionRequest, LLMResponse, Message, TextBlock, ToolResultBlock, ToolUseBlock, Usage,
)

_STOP_MAP = {"end_turn": "end_turn", "stop_sequence": "end_turn", "tool_use": "tool_use", "max_tokens": "max_tokens",
             "refusal": "refusal", "model_context_window_exceeded": "context_exceeded"}
_CONTEXT_HINTS = ("prompt is too long", "context window", "too many tokens", "maximum context")


def _to_wire_messages(messages) -> List[Dict[str, Any]]:
    wire = []
    for m in messages:
        content: List[Dict[str, Any]] = []
        for b in m.blocks:
            if isinstance(b, TextBlock):
                if b.text.strip():
                    content.append({"type": "text", "text": b.text})
            elif isinstance(b, ToolUseBlock):
                content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.arguments})
            elif isinstance(b, ToolResultBlock):
                content.append({"type": "tool_result", "tool_use_id": b.tool_use_id, "content": b.content,
                                "is_error": b.is_error})
        if content:
            wire.append({"role": m.role, "content": content})
    return wire


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, *, model: str, api_key: str, base_url: Optional[str], client: Any = None) -> None:
        if not api_key:
            raise ProviderConfigError("No API key is available for the anthropic provider.")
        self.model = model
        self._api_key = api_key
        if client is not None:                     # injected for unit tests of the adapter only
            self._sdk = __import__("anthropic")
            self._client = client
            return
        try:
            import anthropic
        except ImportError:                        # pragma: no cover - dependency is declared
            raise ProviderConfigError("The 'anthropic' package is not installed.") from None
        self._sdk = anthropic
        kwargs: Dict[str, Any] = {"api_key": api_key, "max_retries": 0}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = anthropic.Anthropic(**kwargs)

    # -- helpers -----------------------------------------------------------------------------
    def _scrub(self, text: str) -> str:
        return (text or "").replace(self._api_key, "***")[:300]

    def _translate(self, exc: Exception) -> ProviderError:
        a = self._sdk
        if isinstance(exc, (a.AuthenticationError, a.PermissionDeniedError)):
            return ProviderAuthError("The provider rejected the credentials (authentication or permission failure).")
        if isinstance(exc, a.RateLimitError):
            ra = None
            try:
                ra = float(exc.response.headers.get("retry-after"))
            except (TypeError, ValueError, AttributeError):
                pass
            return ProviderRateLimitError("The provider rate-limited the request.", retry_after=ra)
        if isinstance(exc, a.APITimeoutError):
            return ProviderTimeoutError("The provider request timed out.")
        if isinstance(exc, a.APIConnectionError):
            return ProviderUnavailableError("Could not connect to the provider.")
        if isinstance(exc, a.APIStatusError):
            code = getattr(exc, "status_code", 0) or 0
            msg = self._scrub(str(getattr(exc, "message", "")))
            if code in (408,):
                return ProviderTimeoutError("The provider request timed out.")
            if code >= 500 or code in (409,):
                return ProviderUnavailableError(f"The provider is unavailable (HTTP {code}).")
            if code == 400 and any(h in msg.lower() for h in _CONTEXT_HINTS):
                return ProviderContextError(f"The request exceeded a context or output limit: {msg}")
            if code == 404:
                return ProviderBadRequestError("The provider did not find the configured model or endpoint (HTTP 404).")
            return ProviderBadRequestError(f"The provider rejected the request (HTTP {code}): {msg}")
        return ProviderError(f"Unexpected provider failure ({type(exc).__name__}).")

    # -- interface ---------------------------------------------------------------------------
    def complete(self, request: CompletionRequest) -> LLMResponse:
        kwargs: Dict[str, Any] = {
            "model": self.model, "max_tokens": request.max_output_tokens, "system": request.system,
            "messages": _to_wire_messages(request.messages), "timeout": request.timeout_seconds}
        if request.tools:
            kwargs["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                               for t in request.tools]
            if request.forced_tool:
                kwargs["tool_choice"] = {"type": "tool", "name": request.forced_tool}
        try:
            resp = self._client.messages.create(**kwargs)
        except self._sdk.APIError as exc:
            raise self._translate(exc) from None
        except Exception as exc:                  # never let an SDK internal leak a traceback with details
            raise ProviderError(f"Unexpected provider failure ({type(exc).__name__}).") from None
        return self._parse(resp)

    @staticmethod
    def _parse(resp: Any) -> LLMResponse:
        raw_stop = getattr(resp, "stop_reason", None)
        stop = _STOP_MAP.get(raw_stop)
        if stop is None:
            raise ProviderResponseError(f"The provider returned an unsupported stop reason: {raw_stop!r}.")
        texts: List[str] = []
        calls: List[ToolUseBlock] = []
        for block in getattr(resp, "content", None) or []:
            kind = getattr(block, "type", None)
            if kind == "text":
                texts.append(getattr(block, "text", "") or "")
            elif kind == "tool_use":
                args = getattr(block, "input", None)
                if not isinstance(args, dict):
                    raise ProviderResponseError("The provider returned a tool call whose arguments are not an object.")
                calls.append(ToolUseBlock(str(block.id), str(block.name), args))
        u = getattr(resp, "usage", None)
        usage = Usage(getattr(u, "input_tokens", None), getattr(u, "output_tokens", None)) if u is not None else Usage()
        return LLMResponse("\n".join(texts), tuple(calls), stop, usage, raw_stop)
