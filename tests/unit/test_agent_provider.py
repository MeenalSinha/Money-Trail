"""Provider adapter, retry policy and provider failure taxonomy.

The adapter tests use REAL anthropic SDK exception classes and response-shaped objects; they never make a network call.
They are unit tests of translation logic, not evidence that a model request works (see tests/integration/test_provider_live.py).
"""
import anthropic
import httpx2 as httpx
import pytest

from money_trail.agent import errors as E
from money_trail.agent.llm.anthropic_provider import AnthropicProvider, _to_wire_messages
from money_trail.agent.llm.base import (
    CompletionRequest, LLMResponse, Message, TextBlock, ToolResultBlock, ToolSpec, ToolUseBlock, Usage,
)
from money_trail.agent.llm.factory import SUPPORTED_PROVIDERS, build_provider
from money_trail.agent.llm.retry import RetryingProvider, RetryPolicy
from money_trail.agent.testing import ScriptedProvider, reply

REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
SECRET = "sk-ant-secret-123"


def status_exc(cls, code, msg="boom", headers=None):
    return cls(msg, response=httpx.Response(code, request=REQ, headers=headers or {}), body=None)


class FakeBlock:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeResp:
    def __init__(self, content, stop="end_turn", usage=(11, 7)):
        self.content, self.stop_reason = content, stop
        self.usage = FakeBlock(input_tokens=usage[0], output_tokens=usage[1]) if usage else None


class FakeClient:
    def __init__(self, result):
        self.calls, self._result = [], result
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def provider(result):
    client = FakeClient(result)
    return AnthropicProvider(model="m", api_key=SECRET, base_url=None, client=client), client


def request(**kw):
    base = dict(system="s", messages=(Message("user", (TextBlock("hi"),)),), tools=(ToolSpec("t", "d", {"type": "object"}),),
                max_output_tokens=100, timeout_seconds=9.0)
    base.update(kw)
    return CompletionRequest(**base)


@pytest.mark.parametrize("exc,expected,retryable", [
    (status_exc(anthropic.AuthenticationError, 401), E.ProviderAuthError, False),
    (status_exc(anthropic.PermissionDeniedError, 403), E.ProviderAuthError, False),
    (status_exc(anthropic.RateLimitError, 429, headers={"retry-after": "7"}), E.ProviderRateLimitError, True),
    (anthropic.APITimeoutError(request=REQ), E.ProviderTimeoutError, True),
    (anthropic.APIConnectionError(request=REQ), E.ProviderUnavailableError, True),
    (status_exc(anthropic.InternalServerError, 500), E.ProviderUnavailableError, True),
    (status_exc(anthropic.BadRequestError, 400, "prompt is too long: 300000 tokens"), E.ProviderContextError, False),
    (status_exc(anthropic.BadRequestError, 400, "bad field"), E.ProviderBadRequestError, False),
    (status_exc(anthropic.NotFoundError, 404), E.ProviderBadRequestError, False),
])
def test_sdk_errors_are_translated_with_correct_retryability(exc, expected, retryable):
    p, _ = provider(exc)
    with pytest.raises(expected) as e:
        p.complete(request())
    assert e.value.retryable is retryable


def test_retry_after_header_is_carried_and_secrets_are_scrubbed():
    p, _ = provider(status_exc(anthropic.RateLimitError, 429, f"key {SECRET} limited", {"retry-after": "7"}))
    with pytest.raises(E.ProviderRateLimitError) as e:
        p.complete(request())
    assert e.value.retry_after == 7.0
    p, _ = provider(status_exc(anthropic.BadRequestError, 400, f"invalid {SECRET}"))
    with pytest.raises(E.ProviderBadRequestError) as e:
        p.complete(request())
    assert SECRET not in str(e.value)


def test_unexpected_exceptions_do_not_leak_details():
    p, _ = provider(RuntimeError(f"internal {SECRET}"))
    with pytest.raises(E.ProviderError) as e:
        p.complete(request())
    assert SECRET not in str(e.value) and "RuntimeError" in str(e.value)


def test_request_shape_sdk_retries_disabled_and_forced_tool():
    p, c = provider(FakeResp([FakeBlock(type="text", text="ok")]))
    p.complete(request(forced_tool="t"))
    kw = c.calls[0]
    assert kw["tool_choice"] == {"type": "tool", "name": "t"} and kw["timeout"] == 9.0 and kw["max_tokens"] == 100
    assert kw["tools"][0]["input_schema"] == {"type": "object"} and "temperature" not in kw
    p.complete(request())
    assert "tool_choice" not in c.calls[1]


def test_sdk_client_is_built_without_hidden_retries():
    real = AnthropicProvider(model="m", api_key=SECRET, base_url=None)
    assert real._client.max_retries == 0


def test_parse_text_tool_use_usage_and_stop_reasons():
    resp = FakeResp([FakeBlock(type="text", text="thinking"), FakeBlock(type="tool_use", id="tu1", name="t", input={"a": 1})],
                    stop="tool_use")
    out = provider(resp)[0].complete(request())
    assert out.text == "thinking" and out.tool_calls == (ToolUseBlock("tu1", "t", {"a": 1}),)
    assert out.stop_reason == "tool_use" and out.usage == Usage(11, 7)
    assert provider(FakeResp([], stop="max_tokens"))[0].complete(request()).stop_reason == "max_tokens"
    assert provider(FakeResp([], stop="refusal"))[0].complete(request()).stop_reason == "refusal"
    assert provider(FakeResp([], stop="model_context_window_exceeded"))[0].complete(request()).stop_reason == "context_exceeded"
    assert provider(FakeResp([FakeBlock(type="text", text="x")], usage=None))[0].complete(request()).usage == Usage()


@pytest.mark.parametrize("resp", [FakeResp([], stop="pause_turn"), FakeResp([], stop=None),
                                  FakeResp([FakeBlock(type="tool_use", id="1", name="t", input="not a dict")], stop="tool_use")])
def test_unusable_provider_responses_are_errors(resp):
    with pytest.raises(E.ProviderResponseError):
        provider(resp)[0].complete(request())


def test_wire_messages_skip_empty_text_and_keep_tool_protocol():
    msgs = (Message("user", (TextBlock("q"),)),
            Message("assistant", (TextBlock("  "), ToolUseBlock("tu1", "t", {"a": 1}))),
            Message("user", (ToolResultBlock("tu1", "{}", True),)))
    w = _to_wire_messages(msgs)
    assert w[1]["content"] == [{"type": "tool_use", "id": "tu1", "name": "t", "input": {"a": 1}}]
    assert w[2]["content"][0] == {"type": "tool_result", "tool_use_id": "tu1", "content": "{}", "is_error": True}


def test_factory_rejects_unsupported_provider_and_missing_credential_before_any_request():
    assert SUPPORTED_PROVIDERS == ("anthropic",)
    with pytest.raises(E.ProviderConfigError, match="Unsupported provider"):
        build_provider("openai", model="m", api_key="k", base_url=None)
    with pytest.raises(E.ProviderConfigError, match="credential is not set"):
        build_provider("anthropic", model="m", api_key=None, base_url=None)
    with pytest.raises(E.ProviderConfigError):
        AnthropicProvider(model="m", api_key="", base_url=None)


# ---- retry policy ---------------------------------------------------------------------------------
POLICY = RetryPolicy(max_retries=3, initial_backoff_seconds=1, max_backoff_seconds=5)


def retrying(script, policy=POLICY, mono=lambda: 0.0):
    sleeps = []
    return RetryingProvider(ScriptedProvider(script), policy, sleep=sleeps.append, monotonic=mono), sleeps


def test_retries_transient_errors_with_exponential_capped_backoff_then_succeeds():
    p, sleeps = retrying([E.ProviderRateLimitError("r"), E.ProviderTimeoutError("t"), E.ProviderUnavailableError("u"), reply(text="ok")])
    assert p.complete(request()).text == "ok"
    assert sleeps == [1, 2, 4] and p.stats.requests == 4 and p.stats.retries == 3
    assert p.stats.failures == ["rate_limited", "timeout", "unavailable"]
    assert p.stats.input_tokens == 10 and p.stats.output_tokens == 5


def test_retries_are_bounded_and_the_last_error_is_raised():
    p, sleeps = retrying([E.ProviderTimeoutError("t")] * 10)
    with pytest.raises(E.ProviderTimeoutError):
        p.complete(request())
    assert p.stats.requests == 4 and len(sleeps) == 3            # first attempt + max_retries


@pytest.mark.parametrize("exc", [E.ProviderAuthError("a"), E.ProviderBadRequestError("b"), E.ProviderContextError("c"),
                                 E.ProviderConfigError("d"), E.ProviderResponseError("e")])
def test_permanent_errors_are_never_retried(exc):
    p, sleeps = retrying([exc, reply(text="never reached")])
    with pytest.raises(type(exc)):
        p.complete(request())
    assert p.stats.requests == 1 and sleeps == []


def test_retry_after_is_honoured_but_capped():
    p, sleeps = retrying([E.ProviderRateLimitError("r", retry_after=3), E.ProviderRateLimitError("r", retry_after=999), reply(text="ok")])
    p.complete(request())
    assert sleeps == [3, 5]


def test_no_retry_when_the_deadline_would_be_exceeded():
    p, sleeps = retrying([E.ProviderTimeoutError("t"), reply(text="x")], mono=lambda: 100.0)
    with pytest.raises(E.ProviderTimeoutError):
        p.complete(request(), deadline=100.5)                     # backoff of 1 s would pass the deadline
    assert sleeps == [] and p.stats.requests == 1


def test_zero_retries_means_a_single_attempt():
    p, sleeps = retrying([E.ProviderRateLimitError("r")], RetryPolicy(0, 1, 1))
    with pytest.raises(E.ProviderRateLimitError):
        p.complete(request())
    assert p.stats.requests == 1 and sleeps == []


def test_usage_is_none_when_provider_reports_nothing():
    p, _ = retrying([LLMResponse("x", (), "end_turn", Usage())])
    p.complete(request())
    assert p.stats.input_tokens is None and p.stats.output_tokens is None      # never invented
