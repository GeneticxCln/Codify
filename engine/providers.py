from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator
from urllib.parse import urlparse

import httpx

from engine.models import AgentConfig, BUILTIN_PROVIDERS


class ProviderError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_local_base_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        raise ProviderError("invalid_base_url", "Local provider base_url must point at localhost")
    if parsed.scheme != "http":
        raise ProviderError("invalid_base_url", "Local provider must use http (loopback only)")


class BaseProvider(ABC):
    @abstractmethod
    async def complete(
        self, system_prompt: str, user_prompt: str, model: str,
        temperature: float, max_tokens: int,
    ) -> str: ...

    async def stream(
        self, system_prompt: str, user_prompt: str, model: str,
        temperature: float, max_tokens: int,
    ) -> AsyncIterator[str]:
        yield await self.complete(system_prompt, user_prompt, model, temperature, max_tokens)

    async def test_connection(self, model: str) -> tuple[bool, str]:
        try:
            await self.complete("ping", "ping", model, 0.0, 8)
            return True, "ok"
        except ProviderError as exc:
            return False, exc.message
        except Exception as exc:
            return False, str(exc)


class AnthropicProvider(BaseProvider):
    def __init__(self, api_key: str, base_url: str):
        if not api_key:
            raise ProviderError("missing_api_key", "Anthropic API key is not set")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    async def complete(self, system_prompt, user_prompt, model, temperature, max_tokens) -> str:
        url = f"{self._base_url}/v1/messages"
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                url,
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_prompt}],
                },
            )
            if r.status_code >= 400:
                raise ProviderError("provider_http", f"anthropic {r.status_code}")
            data = r.json()
            return "".join(
                b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
            )


class OpenAICompatProvider(BaseProvider):
    def __init__(self, api_key: str | None, base_url: str):
        self._api_key = api_key or ""
        self._base_url = base_url.rstrip("/")

    async def complete(self, system_prompt, user_prompt, model, temperature, max_tokens) -> str:
        if not self._api_key:
            raise ProviderError("missing_api_key", "API key is not set")
        url = f"{self._base_url}/chat/completions"
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                },
            )
            if r.status_code >= 400:
                raise ProviderError("provider_http", f"openai_compat {r.status_code}")
            data = r.json()
            return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""


class OllamaProvider(BaseProvider):
    def __init__(self, base_url: str):
        validate_local_base_url(base_url)
        self._base_url = base_url.rstrip("/")

    async def complete(self, system_prompt, user_prompt, model, temperature, max_tokens) -> str:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{self._base_url}/api/generate",
                json={
                    "model": model,
                    "prompt": f"{system_prompt}\n\n{user_prompt}",
                    "options": {"temperature": temperature, "num_predict": max_tokens},
                    "stream": False,
                },
            )
            if r.status_code >= 400:
                raise ProviderError("provider_http", f"ollama {r.status_code}")
            data = r.json()
            if "response" not in data:
                raise ProviderError("provider_http", "ollama missing response")
            return data["response"]


class GoogleProvider(BaseProvider):
    def __init__(self, api_key: str, base_url: str):
        if not api_key:
            raise ProviderError("missing_api_key", "Google API key is not set")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    async def complete(self, system_prompt, user_prompt, model, temperature, max_tokens) -> str:
        url = f"{self._base_url}/models/{model}:generateContent?key={self._api_key}"
        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(url, json=payload)
            if r.status_code >= 400:
                raise ProviderError("provider_http", f"google {r.status_code}")
            data = r.json()
            candidates = data.get("candidates") or []
            if not candidates:
                return ""
            parts = candidates[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts)


import os

ENV_KEY_MAP: dict[str, list[str]] = {
    "anthropic": ["ANTHROPIC_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "google": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
    "deepseek": ["DEEPSEEK_API_KEY"],
    "openrouter": ["OPENROUTER_API_KEY"],
    "groq": ["GROQ_API_KEY"],
}


class Keychain:
    def get(self, ref: str | None) -> str:
        if not ref:
            return ""
        try:
            import keyring
            val = keyring.get_password("codify", ref)
            if val:
                return val
        except Exception:
            pass
        return ""

    def set(self, role: str, api_key: str) -> str:
        ref = f"codify/agents/{role}"
        try:
            import keyring
            keyring.set_password("codify", ref, api_key)
        except Exception as exc:
            raise ProviderError("keyring_unavailable", str(exc)) from exc
        return ref

    def get_provider_key(self, provider: str) -> str:
        """Find API key for provider in OS Keyring or fallback to environment variables."""
        # 1. Try keyring under providers/<provider>
        try:
            import keyring
            val = keyring.get_password("codify", f"providers/{provider}")
            if val:
                return val
        except Exception:
            pass

        # 2. Try role-based keys in keyring
        for role in ("coder", "planner", "tester", "reviewer", "summarizer"):
            val = self.get(f"codify/agents/{role}")
            if val:
                return val

        # 3. Try environment variables
        env_vars = ENV_KEY_MAP.get(provider.lower(), [])
        for var in env_vars:
            val = os.environ.get(var)
            if val:
                return val

        return ""

    def set_provider_key(self, provider: str, api_key: str) -> None:
        """Store API key for a provider in the OS Keyring."""
        try:
            import keyring
            keyring.set_password("codify", f"providers/{provider}", api_key)
        except Exception as exc:
            raise ProviderError("keyring_unavailable", str(exc)) from exc

    def has_provider_key(self, provider: str) -> bool:
        if provider == "ollama":
            return True
        return bool(self.get_provider_key(provider))


class ProviderFactory:
    def __init__(self, keychain: Keychain):
        self._keychain = keychain

    def build_for(
        self,
        provider: str,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> BaseProvider:
        """Instantiate a provider dynamically for any model or harness."""
        meta = BUILTIN_PROVIDERS.get(provider, {})
        protocol = meta.get("protocol", "openai_compat")
        key = api_key or self._keychain.get_provider_key(provider)
        base = base_url or meta.get("base_url", "")

        if protocol == "anthropic":
            return AnthropicProvider(key, base)
        if protocol == "openai_compat":
            return OpenAICompatProvider(key, base)
        if protocol == "ollama":
            return OllamaProvider(base or "http://127.0.0.1:11434")
        if protocol == "google":
            return GoogleProvider(key, base)
        raise ProviderError("unknown_protocol", f"Unknown protocol {protocol}")

    def build(self, config: AgentConfig) -> BaseProvider:
        key = self._keychain.get(config.api_key_ref) or self._keychain.get_provider_key(config.provider)
        base = config.base_url or BUILTIN_PROVIDERS.get(config.provider, {}).get("base_url") or ""
        if config.protocol == "anthropic":
            return AnthropicProvider(key, base)
        if config.protocol == "openai_compat":
            return OpenAICompatProvider(key, base)
        if config.protocol == "ollama":
            return OllamaProvider(base)
        if config.protocol == "google":
            return GoogleProvider(key, base)
        raise ProviderError("unknown_protocol", f"Unknown protocol {config.protocol}")
