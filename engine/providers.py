from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlparse

import httpx

from engine.models import AgentConfig, BUILTIN_PROVIDERS


class ProviderError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


async def post_json(client: "httpx.AsyncClient", url: str, *, label: str, **kwargs) -> Any:
    """POST, check the status, and decode — reporting every failure as ours.

    A refused connection, a DNS failure, or a timeout used to escape as a raw
    httpx exception and reached the goal as `internal_error` — a code that says
    "Codify is broken" about a provider that is merely unreachable. `json()`
    failing (a proxy's HTML error page) had the same problem. Both are named
    here, because the fallback path has to tell "this endpoint is down" from "our
    code is broken": only the first is worth trying somewhere else.
    """
    try:
        response = await client.post(url, **kwargs)
    except httpx.HTTPError as exc:
        raise ProviderError(
            "provider_unreachable", f"{label} unreachable: {type(exc).__name__}: {exc}"
        ) from exc
    if response.status_code >= 400:
        raise ProviderError("provider_http", f"{label} {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError(
            "provider_bad_response",
            f"{label} answered with a body that is not JSON (HTTP {response.status_code})",
        ) from exc


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
            data = await post_json(
                client,
                url,
                label="anthropic",
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
        return "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )


class OpenAICompatProvider(BaseProvider):
    def __init__(self, api_key: str | None, base_url: str):
        self._api_key = api_key or ""
        self._base_url = base_url.rstrip("/")
        # Three states: None = not probed yet, True/False = probed. Cached per
        # provider instance so the capability probe runs once, not per call.
        self._json_mode: bool | None = None

    async def complete(self, system_prompt, user_prompt, model, temperature, max_tokens) -> str:
        if not self._api_key:
            raise ProviderError("missing_api_key", "API key is not set")
        url = f"{self._base_url}/chat/completions"
        # Every role here answers JSON by contract (default_prompts.py). Asking
        # for structured output turns "please emit JSON" from prompt advice into
        # a wire guarantee on servers that support it.
        #
        # It is opt-in per server because a strict server rejects the unknown
        # field outright (some vLLM versions 400 on response_format) — one
        # broken endpoint must not take every role's replies down. On the first
        # 400 that mentions the field, remember it and retry plainly.
        if self._json_mode is None:
            self._json_mode = await self._supports_json_mode(url)
        payload = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if self._json_mode:
            payload["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=120) as client:
            try:
                data = await post_json(
                    client,
                    url,
                    label="openai_compat",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "content-type": "application/json",
                    },
                    json=payload,
                )
            except ProviderError as exc:
                if not (self._json_mode and exc.code == "provider_http"):
                    raise
                # This server rejects the field: stop sending it.
                self._json_mode = False
                payload.pop("response_format", None)
                data = await post_json(
                    client,
                    url,
                    label="openai_compat",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "content-type": "application/json",
                    },
                    json=payload,
                )
        return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""

    async def _supports_json_mode(self, url: str) -> bool:
        """Ask /v1/models whether this server advertises structured output.

        Conservative by design: any ambiguity (no route, odd body, no json
        schema capability anywhere) means "don't send the field", because the
        cost of a false positive is a 400 on every call, and the cost of a
        false negative is just the old prompt-level behavior.
        """
        root = self._base_url.rsplit("/", 1)[0] if self._base_url.endswith("/chat/completions") else self._base_url
        models_url = f"{root}/models"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    models_url,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
            if resp.status_code != 200:
                return False
            for m in (resp.json() or {}).get("data", []):
                caps = m.get("capabilities", {}) or {}
                if isinstance(caps, dict) and caps.get("json_schema") or caps.get("json_object"):
                    return True
            return False
        except Exception:
            return False


class OllamaProvider(BaseProvider):
    def __init__(self, base_url: str):
        validate_local_base_url(base_url)
        self._base_url = base_url.rstrip("/")

    async def complete(self, system_prompt, user_prompt, model, temperature, max_tokens) -> str:
        async with httpx.AsyncClient(timeout=180) as client:
            data = await post_json(
                client,
                f"{self._base_url}/api/generate",
                label="ollama",
                json={
                    "model": model,
                    "prompt": f"{system_prompt}\n\n{user_prompt}",
                    "options": {"temperature": temperature, "num_predict": max_tokens},
                    "stream": False,
                    "format": "json",
                },
            )
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
            data = await post_json(client, url, label="google", json=payload)
            candidates = data.get("candidates") or []
            if not candidates:
                return ""
            parts = candidates[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts)


import json
import os
from pathlib import Path
from typing import Any

from engine import home

ENV_KEY_MAP: dict[str, list[str]] = {
    "anthropic": ["ANTHROPIC_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "google": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
    "deepseek": ["DEEPSEEK_API_KEY"],
    "openrouter": ["OPENROUTER_API_KEY"],
    "groq": ["GROQ_API_KEY"],
}


def default_secrets_path() -> Path:
    """Where a file-backed credential lands, decided in `engine/home.py`."""
    return home.secrets_path()


class Keychain:
    """Where provider API keys live, with two backends.

    Preferred: the OS keyring (Secret Service, macOS Keychain, Windows
    Credential Manager) via the `keyring` package.

    Fallback: a `0600` JSON file under `~/.codify/secrets.json`.

    The fallback is not a nicety. A machine without a working keyring — headless
    Linux, no Secret Service running, or (the common case for a source checkout)
    `keyring` simply not installed in the interpreter that runs the engine —
    could otherwise never store a key at all. Every provider would report "no
    API key configured" forever and the only feedback would be an error from the
    settings screen, which reads as "Codify is broken" rather than "this box has
    no keyring". A local-first desktop app that cannot store a credential is not
    usable, so the file backend is the honest fallback, and `backend` is
    reported to the UI so the user is told which one is in force.

    Both backends are namespaced identically (`providers/<slug>`,
    `codify/agents/<role>`), so switching between them never changes which key a
    provider resolves to beyond what is actually stored.

    The keychain is skipped entirely when this process was handed its own store —
    an explicit `secrets_path`, or a redirected state directory (`CODIFY_HOME` /
    `CODIFY_SECRETS`). A run that names its own store must not be able to read or
    write the developer's real keychain; the file *is* the store in that case, and
    `describes_backend` says so.
    """

    def __init__(self, secrets_path: Path | None = None):
        self._secrets_path = secrets_path or default_secrets_path()
        # An explicitly injected store means "use this store", not "prefer it".
        self._explicit_store = secrets_path is not None
        self._keyring: Any = None
        self._keyring_checked = False

    # ── backends ────────────────────────────────────────────────────────────

    def _keyring_module(self) -> Any:
        """The `keyring` module if it actually works here, else None (cached)."""
        if not home.keyring_allowed(self._explicit_store):
            return None
        if not self._keyring_checked:
            self._keyring_checked = True
            try:
                import keyring
                from keyring.backends.fail import Keyring as FailKeyring

                # `fail.Keyring` accepts writes and raises on read; detect it now
                # rather than after a key has already been reported as saved.
                self._keyring = (
                    None if isinstance(keyring.get_keyring(), FailKeyring) else keyring
                )
            except Exception:
                self._keyring = None
        return self._keyring

    @property
    def backend(self) -> str:
        """"keyring" or "file" — which store a saved key actually lands in."""
        return "keyring" if self._keyring_module() else "file"

    def storage_reason(self) -> str:
        """Why keys land where they land — machine-readable, so the UI can say it.

        `file` has three different causes and only one of them is "this box has no
        keyring". A screen that guessed would tell an isolated verification run that
        its machine lacks a keychain, which is both false and the sort of claim that
        sends someone installing packages to fix a deliberate setting.
        """
        if self.backend == "keyring":
            return "keyring"
        return "isolated_run" if not home.keyring_allowed(self._explicit_store) else "no_keyring"

    def describes_backend(self) -> str:
        """*Where* a key goes. *Why* is `storage_reason` — one statement each."""
        if self.backend == "keyring":
            return "your OS keychain"
        return f"the local file {self._secrets_path} (permissions 0600)"

    def _read_file(self) -> dict[str, str]:
        try:
            data = json.loads(self._secrets_path.read_text())
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception:
            # A corrupted secret store must not take the engine down; acting as
            # if it were empty is recoverable (the key can be re-entered).
            return {}

    def _write_file(self, data: dict[str, str]) -> None:
        path = self._secrets_path
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp.chmod(0o600)
        tmp.replace(path)  # atomic: a crash mid-write cannot truncate the store

    # ── generic ref access ──────────────────────────────────────────────────

    def get(self, ref: str | None) -> str:
        if not ref:
            return ""
        keyring_mod = self._keyring_module()
        if keyring_mod:
            try:
                val = keyring_mod.get_password("codify", ref)
                if val:
                    return val
            except Exception:
                pass
        return self._read_file().get(ref, "")

    def set(self, role: str, api_key: str) -> str:
        ref = f"codify/agents/{role}"
        self._store(ref, api_key)
        return ref

    def _store(self, ref: str, api_key: str) -> None:
        keyring_mod = self._keyring_module()
        if keyring_mod:
            try:
                keyring_mod.set_password("codify", ref, api_key)
                return
            except Exception:
                # Keyring present but unusable at write time (locked, no session
                # bus). Fall through rather than failing the save.
                pass
        data = self._read_file()
        data[ref] = api_key
        try:
            self._write_file(data)
        except OSError as exc:
            raise ProviderError("secrets_unwritable", str(exc)) from exc

    def get_provider_key(self, provider: str) -> str:
        """Find an API key for this provider only.

        Sources, in order: the provider-scoped keyring entry
        (providers/<slug>), the local secrets file, then provider-specific
        environment variables. Role-scoped entries are deliberately NOT
        consulted: a key stored for one provider must never be sent to a
        different provider's endpoint.
        """
        val = self.get(f"providers/{provider}")
        if val:
            return val

        for var in ENV_KEY_MAP.get(provider.lower(), []):
            env_val = os.environ.get(var)
            if env_val:
                return env_val

        return ""

    def rename_role_key(self, old_role: str, new_role: str) -> bool:
        """Carry a role-scoped key onto a renamed role.

        Role ids changed once (coder→fixer, tester→verifier, ...). A key stored
        under the old id would otherwise be stranded: the role would look
        unconfigured forever and the only fix would be re-entering a secret the
        user already gave us.
        """
        old_ref = f"codify/agents/{old_role}"
        key = self.get(old_ref)
        if not key:
            return False
        self.set(new_role, key)
        self.forget(old_ref)
        return True

    def forget(self, ref: str) -> None:
        """Best-effort delete of one entry, from whichever backend holds it."""
        keyring_mod = self._keyring_module()
        if keyring_mod:
            try:
                keyring_mod.delete_password("codify", ref)
            except Exception:
                pass
        data = self._read_file()
        if ref in data:
            data.pop(ref)
            try:
                self._write_file(data)
            except OSError:
                # Nothing else to do: the entry is already unreachable.
                pass

    def set_provider_key(self, provider: str, api_key: str) -> None:
        """Store an API key for a provider (OS keychain, or the local file)."""
        self._store(f"providers/{provider}", api_key)

    def has_provider_key(self, provider: str) -> bool:
        if provider == "ollama":
            return True
        return bool(self.get_provider_key(provider))


class ProviderFactory:
    def __init__(self, keychain: Keychain):
        self._keychain = keychain

    def build(self, config: AgentConfig) -> BaseProvider:
        """The one way a provider is constructed.

        Wire format comes from the config's own `protocol`, which the registry
        normalises on save (`AgentRegistryService.set_config`): a provider slug
        and its protocol can never disagree here, and there is no second
        constructor resolving protocol from the built-in catalog instead.
        """
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
