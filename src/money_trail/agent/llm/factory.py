"""Build the configured provider. Only providers that are implemented AND verified-by-test can be selected."""
from __future__ import annotations

from typing import Optional

from money_trail.agent.errors import ProviderConfigError
from money_trail.agent.llm.base import LLMProvider

SUPPORTED_PROVIDERS = ("anthropic",)


def build_provider(name: str, *, model: str, api_key: Optional[str], base_url: Optional[str]) -> LLMProvider:
    if name not in SUPPORTED_PROVIDERS:
        raise ProviderConfigError(f"Unsupported provider {name!r}. Supported: {', '.join(SUPPORTED_PROVIDERS)}.")
    if not api_key:
        raise ProviderConfigError("The provider credential is not set. Provide the environment variable named by "
                                  "provider.api_key_env (see .env.example); no request was made.")
    from money_trail.agent.llm.anthropic_provider import AnthropicProvider
    return AnthropicProvider(model=model, api_key=api_key, base_url=base_url)


class LazyProvider:
    """Builds the real provider on the first request, so runs that never reach the model (unknown anchor, invalid
    request, unsupported intent) need no credential. A missing credential fails here, before any network call."""

    def __init__(self, name: str, *, model: str, get_api_key, base_url: Optional[str]) -> None:
        self.name, self.model = name, model
        self._get_key, self._base_url, self._inner = get_api_key, base_url, None

    def complete(self, request):
        if self._inner is None:
            self._inner = build_provider(self.name, model=self.model, api_key=self._get_key(), base_url=self._base_url)
        return self._inner.complete(request)
