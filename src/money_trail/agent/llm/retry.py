"""Bounded retries for retryable provider failures only; permanent failures are raised immediately."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from money_trail.agent.errors import ProviderError
from money_trail.agent.llm.base import CompletionRequest, LLMProvider, LLMResponse


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int                 # additional attempts after the first request
    initial_backoff_seconds: float
    max_backoff_seconds: float

    def delay(self, retry_number: int, retry_after: Optional[float]) -> float:
        base = min(self.max_backoff_seconds, self.initial_backoff_seconds * (2 ** (retry_number - 1)))
        if retry_after is not None:
            base = max(base, min(retry_after, self.max_backoff_seconds))
        return base


@dataclass
class ProviderStats:
    requests: int = 0
    retries: int = 0
    failures: List[str] = field(default_factory=list)       # error codes of failed attempts, in order
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None

    def add_usage(self, resp: LLMResponse) -> None:
        for attr, val in (("input_tokens", resp.usage.input_tokens), ("output_tokens", resp.usage.output_tokens)):
            if val is not None:
                setattr(self, attr, (getattr(self, attr) or 0) + val)


class RetryingProvider:
    """Wraps a provider. `deadline` (monotonic seconds) stops retrying when the investigation's time budget is spent."""

    def __init__(self, inner: LLMProvider, policy: RetryPolicy,
                 sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic) -> None:
        self.inner, self.policy, self._sleep, self._mono = inner, policy, sleep, monotonic
        self.name, self.model = inner.name, inner.model
        self.stats = ProviderStats()

    def complete(self, request: CompletionRequest, deadline: Optional[float] = None) -> LLMResponse:
        attempt = 0
        while True:
            self.stats.requests += 1
            try:
                resp = self.inner.complete(request)
            except ProviderError as exc:
                self.stats.failures.append(exc.code)
                if not exc.retryable or attempt >= self.policy.max_retries:
                    raise
                attempt += 1
                wait = self.policy.delay(attempt, exc.retry_after)
                if deadline is not None and self._mono() + wait >= deadline:
                    raise
                self.stats.retries += 1
                self._sleep(wait)
                continue
            self.stats.add_usage(resp)
            return resp
