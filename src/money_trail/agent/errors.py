"""Provider error taxonomy. Messages never contain credentials."""
from __future__ import annotations

from typing import Optional

from money_trail.exceptions import AgentConfigError, AgentError


class ProviderError(AgentError):
    """A language-model provider request failed. `code` is stable and machine-readable."""
    code = "provider_error"
    retryable = False

    def __init__(self, message: str, *, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ProviderConfigError(ProviderError, AgentConfigError):
    code = "provider_config"


class ProviderAuthError(ProviderError):
    code = "authentication_failed"


class ProviderRateLimitError(ProviderError):
    code = "rate_limited"
    retryable = True


class ProviderTimeoutError(ProviderError):
    code = "timeout"
    retryable = True


class ProviderUnavailableError(ProviderError):
    code = "unavailable"
    retryable = True


class ProviderBadRequestError(ProviderError):
    code = "bad_request"


class ProviderContextError(ProviderError):
    code = "context_or_output_limit"


class ProviderResponseError(ProviderError):
    """The provider answered, but the answer cannot be used (refusal, malformed content, unknown stop reason)."""
    code = "invalid_response"
