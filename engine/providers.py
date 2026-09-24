from __future__ import annotations

import asyncio
import json
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
from collections.abc import AsyncIterator, Callable
from urllib.parse import urlparse

import httpx

from engine import home
from engine.models import AgentConfig, BUILTIN_PROVIDERS


class ProviderError(Exception):
    # Which role was being called when this failed. Declared rather than left to the
    # assignment in `engine/executor.py`: without it the attribute exists only on
    # errors that happened to be routed through that one path, which is why readers
    # had to reach for `getattr(exc, "role", None)` to stay safe.
    role: str | None = None

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


async def post_json(
    client: httpx.AsyncClient, url: str, *, label: str, **kwargs: Any
) -> Any:
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


# The liveness-probe deadline, from `01` §3: max_tokens=8, prompt `ping`, 15s.
# Deliberately far below the generation clients' timeouts — a probe that hangs
# for minutes is the settings screen hanging, not a test.
TEST_CONNECTION_TIMEOUT_S = 15


def normalize_usage(provider: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the token-usage block out of a provider response, normalized.

    Every provider names these fields differently and most code just drops
    them — which is why nobody can answer "what did this goal cost in
    tokens". Returns None when the response carries no usage at all (some
    local servers do not).
    """
    # Runtime guard: normalize_usage is called with parsed JSON, and the
    # isinstance keeps a malformed payload from crashing the recorder.
    if not isinstance(data, dict):
        return None  # type: ignore[unreachable]
    if provider == "anthropic":
        u = data.get("usage") or {}
        return _mk_usage(u.get("input_tokens"), u.get("output_tokens"))
    if provider == "openai_compat":
        u = data.get("usage") or {}
        return _mk_usage(u.get("prompt_tokens"), u.get("completion_tokens"))
    if provider == "google":
        u = data.get("usageMetadata") or {}
        return _mk_usage(u.get("promptTokenCount"), u.get("candidatesTokenCount"))
    if provider == "ollama":
        return _mk_usage(data.get("prompt_eval_count"), data.get("eval_count"))
    return None


def _mk_usage(inp: Any, out: Any) -> dict[str, Any] | None:
    try:
        i = int(inp) if inp is not None else 0
        o = int(out) if out is not None else 0
    except (TypeError, ValueError):
        return None
    if i <= 0 and o <= 0:
        return None
    return {"input_tokens": i, "output_tokens": o, "total_tokens": i + o}


def validate_local_base_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().strip("[]")
    # All of 127/8 is loopback, plus IPv6 ::1 and the name forms.
    is_loopback = (
        host in ("localhost", "::1")
        or host.startswith("127.")
    )
    if not is_loopback:
        # Plain IP without brackets already handled; dotted check above covers
        # 127.0.0.1 through 127.255.255.255.
        try:
            import ipaddress
            if ipaddress.ip_address(host).is_loopback:
                is_loopback = True
        except ValueError:
            pass
    if not is_loopback:
        raise ProviderError("invalid_base_url", "Local provider base_url must point at localhost")
    if parsed.scheme != "http":
        raise ProviderError("invalid_base_url", "Local provider must use http (loopback only)")


class BaseProvider(ABC):
    # Optional callback: providers that receive token usage in their responses
    # invoke it with a normalized dict {input_tokens, output_tokens,
    # total_tokens} after each successful call. The orchestrator attaches a
    # recorder; anything else (health probes, settings tests) leaves it None.
    usage_sink: Callable[[dict[str, Any]], None] | None = None
    # Optional callback for streamed replies: invoked with the full text
    # accumulated so far, whenever the server yields more. The orchestrator
    # throttles it into chat-visible snapshots; leaving it None (health
    # probes, settings tests) keeps the plain blocking behavior.
    on_delta: Callable[[str], None] | None = None

    @abstractmethod
    async def complete(
        self, system_prompt: str, user_prompt: str, model: str,
        temperature: float, max_tokens: int,
    ) -> str: ...

    @staticmethod
    async def _stream_lines(response: httpx.Response) -> AsyncIterator[str]:
        """Yield decoded SSE data payloads from a streaming response body.

        Every OpenAI-descended dialect (OpenAI-compat, Google's alt=sse,
        Anthropic) frames chunks as `data: {json}` lines separated by blank
        lines; `[DONE]` closes the OpenAI-style streams. Shared so each
        provider only owns its own payload unwrapping. (Ollama speaks raw
        NDJSON instead and parses its body directly.)
        """
        async for line in response.aiter_lines():
            line = (line or "").strip()
            if not line.startswith("data:"):
                continue
            chunk = line[len("data:"):].strip()
            if chunk and chunk != "[DONE]":
                yield chunk

    def _report_usage(self, provider: str, data: dict[str, Any]) -> None:
        """Hand the response's token usage to the sink, when one is attached.

        Best-effort by design: accounting must never be able to fail a call —
        a sink raising, or a response shaped unexpectedly, is swallowed here
        and the completion already succeeded.
        """
        sink = getattr(self, "usage_sink", None)
        if sink is None:
            return
        try:
            usage = normalize_usage(provider, data)
            if usage:
                sink(usage)
        except Exception:
            pass

    async def test_connection(self, model: str) -> tuple[bool, str]:
        """One tiny completion, on a deadline: this is a liveness probe, not a
        generation run.

        The probe rides the provider's own `complete`, whose HTTP client is
        sized for real replies (120s here, 180s for a local Ollama loading a
        model from disk). Left unbounded it inherited that budget, and a hung
        endpoint held the settings screen for minutes before reporting what the
        user only needed to know after 15: the provider is not answering.
        `asyncio.wait_for` draws the documented line and reports the timeout in
        the same shape as any other failure, so the button always comes back.
        """
        try:
            await asyncio.wait_for(
                self.complete("ping", "ping", model, 0.0, 8), timeout=TEST_CONNECTION_TIMEOUT_S
            )
            return True, "ok"
        except asyncio.TimeoutError:
            return False, f"no answer within {TEST_CONNECTION_TIMEOUT_S}s"
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

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str,
        temperature: float, max_tokens: int,
    ) -> str:
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
        self._report_usage("anthropic", data)
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

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str,
        temperature: float, max_tokens: int,
    ) -> str:
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
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }
        if self.on_delta is not None:
            # SSE streaming (`stream: true`): each chunk carries a delta to
            # append; the final chunk (finish_reason set) carries the usage
            # block when the server includes it. A server that rejects the
            # streaming request itself (4xx) is retried once the blocking way
            # — the reply matters more than the delivery.
            try:
                return await self._complete_streaming(payload, headers)
            except ProviderError as exc:
                if exc.code != "provider_http":
                    raise
                saved = self.on_delta
                self.on_delta = None
                try:
                    return await self.complete(system_prompt, user_prompt, model, temperature, max_tokens)
                finally:
                    self.on_delta = saved
        async with httpx.AsyncClient(timeout=120) as client:
            try:
                data = await post_json(
                    client,
                    url,
                    label="openai_compat",
                    headers=headers,
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
                    headers=headers,
                    json=payload,
                )
        self._report_usage("openai_compat", data)
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

    async def _complete_streaming(self, payload: dict[str, Any], headers: dict[str, Any]) -> str:
        """One streaming pass over the SSE body; returns the full reply text."""
        url = f"{self._base_url}/chat/completions"
        text = ""
        data: dict[str, Any] = {}
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream("POST", url, headers=headers, json={**payload, "stream": True}) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        raise ProviderError("provider_http", f"openai_compat {response.status_code}")
                    async for chunk in self._stream_lines(response):
                        try:
                            obj = json.loads(chunk)
                        except ValueError:
                            continue
                        choices = obj.get("choices") or [{}]
                        text += choices[0].get("delta", {}).get("content") or ""
                        if self.on_delta is not None:
                            try:
                                self.on_delta(text)
                            except Exception:
                                pass
                        # Usage arrives in whichever chunk carries it, the
                        # finish chunk in another. Overwriting `data` wholesale
                        # loses one when the two don't coincide — merge so the
                        # usage report sees both.
                        if obj.get("usage"):
                            data = {**data, "usage": obj["usage"]}
                        if choices[0].get("finish_reason"):
                            # obj wins per-key: the finish chunk's usage (the
                            # complete one) overrides an earlier partial.
                            data = {**data, **obj} if data else obj
        except httpx.HTTPError as exc:
            # A refused/dropped connection mid-stream is unreachability, not a
            # bug — same normalization post_json applies to non-streaming calls.
            raise ProviderError(
                "provider_unreachable", f"openai_compat unreachable: {type(exc).__name__}: {exc}"
            ) from exc
        if not data:
            raise ProviderError(
                "provider_bad_response",
                "openai_compat stream ended without a finish_reason chunk",
            )
        self._report_usage("openai_compat", data)
        return text


class OllamaProvider(BaseProvider):
    def __init__(self, base_url: str):
        validate_local_base_url(base_url)
        self._base_url = base_url.rstrip("/")

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str,
        temperature: float, max_tokens: int,
    ) -> str:
        stream = self.on_delta is not None
        async with httpx.AsyncClient(timeout=180) as client:
            if not stream:
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
            else:
                # NDJSON streaming: one JSON object per line until the final
                # one (done=true) carries the full text and the usage counts.
                # Same reply, just delivered in pieces — the accumulated text
                # is byte-identical to what stream=false returns.
                text = ""
                data = {}
                try:
                    async with client.stream(
                        "POST",
                        f"{self._base_url}/api/generate",
                        json={
                            "model": model,
                            "prompt": f"{system_prompt}\n\n{user_prompt}",
                            "options": {"temperature": temperature, "num_predict": max_tokens},
                            "stream": True,
                            "format": "json",
                        },
                    ) as response:
                        if response.status_code >= 400:
                            raise ProviderError("provider_http", f"ollama {response.status_code}")
                        async for line in response.aiter_lines():
                            line = (line or "").strip()
                            if not line:
                                continue
                            piece = json.loads(line)
                            text += piece.get("response", "")
                            try:
                                self.on_delta(text)  # type: ignore[misc]
                            except Exception:
                                pass
                            if piece.get("done"):
                                data = piece
                except json.JSONDecodeError as exc:
                    raise ProviderError(
                        "provider_bad_response", f"ollama streamed a line that is not JSON: {exc}"
                    ) from exc
                except httpx.HTTPError as exc:
                    # A refused/dropped connection is unreachability, not a bug —
                    # the streaming twin of post_json's normalization; without it
                    # a dead endpoint escapes the fallback machinery entirely.
                    raise ProviderError(
                        "provider_unreachable", f"ollama unreachable: {type(exc).__name__}: {exc}"
                    ) from exc
                if not data.get("done"):
                    raise ProviderError(
                        "provider_bad_response", "ollama stream ended without a done=true chunk"
                    )
        if "response" not in data and not stream:
            raise ProviderError("provider_http", "ollama missing response")
        self._report_usage("ollama", data)
        # In streaming mode the reply is the accumulated text — the done
        # chunk's own "response" field is just the last piece, not the whole.
        return text if stream else data["response"]


class GoogleProvider(BaseProvider):
    def __init__(self, api_key: str, base_url: str):
        if not api_key:
            raise ProviderError("missing_api_key", "Google API key is not set")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    async def complete(
        self, system_prompt: str, user_prompt: str, model: str,
        temperature: float, max_tokens: int,
    ) -> str:
        url = f"{self._base_url}/models/{model}:generateContent"
        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        # The key travels in a header, not the URL: a query-string credential
        # lands in proxy/server access logs, and Google accepts the header form.
        async with httpx.AsyncClient(timeout=120) as client:
            data = await post_json(
                client, url, label="google", json=payload,
                headers={"x-goog-api-key": self._api_key},
            )
            candidates = data.get("candidates") or []
            if not candidates:
                return ""
            parts = candidates[0].get("content", {}).get("parts", [])
            self._report_usage("google", data)
            return "".join(p.get("text", "") for p in parts)


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
        # Until a save happens, report the configured preference as the last
        # write — there has been no write to disagree with it yet.
        self._last_write_backend = self.backend

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

    def _read_file(self, *, strict: bool = False) -> dict[str, str]:
        try:
            data = json.loads(self._secrets_path.read_text())
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as exc:
            if strict:
                # A write is about to REPLACE this store. Silently treating a
                # corrupt/unreadable file as empty would wipe every other key
                # in it (the old bytes are discarded by the atomic replace even
                # though they might have been recoverable, e.g. a transient
                # EACCES). Fail the save loudly instead; reads stay lenient so
                # a corrupt store degrades to "no key" rather than a dead app.
                raise ProviderError(
                    "secrets_unreadable",
                    f"the local secret store at {self._secrets_path} exists but "
                    f"cannot be read ({exc.__class__.__name__}); refusing to "
                    "overwrite it — fix or remove the file, then save again",
                ) from exc
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
        # Create the temp file already 0600: write_text creates it with the
        # process umask (typically 0644), leaving a window where the plaintext
        # secrets sit world-readable before the chmod below runs.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            # Best-effort inter-process lock: concurrent saves last-writer-wins
            # without it, and the loser drops the winner's key.
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(data, indent=2, sort_keys=True))
            tmp.replace(path)  # atomic: a crash mid-write cannot truncate the store
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    # ── generic ref access ──────────────────────────────────────────────────

    def get(self, ref: str | None) -> str:
        if not ref:
            return ""
        keyring_mod = self._keyring_module()
        if keyring_mod:
            try:
                val = keyring_mod.get_password("codify", ref)
                if isinstance(val, str) and val:
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
                self._last_write_backend = "keyring"
                return
            except Exception:
                # Keyring present but unusable at write time (locked, no session
                # bus). Fall through rather than failing the save.
                pass
        # Strict read: saving must not wipe a store it merely failed to read
        # (the empty-dict default here would make the replace drop every other
        # key the file held).
        data = self._read_file(strict=True)
        data[ref] = api_key
        try:
            self._write_file(data)
        except OSError as exc:
            raise ProviderError("secrets_unwritable", str(exc)) from exc
        self._last_write_backend = "file"

    def last_write_backend(self) -> str:
        """"keyring" or "file" — where the most recent save ACTUALLY landed.

        `backend` is the configured preference; a keyring that throws at write
        time silently falls through to the file, and a UI that trusted the
        preference would tell the user "stored in your OS keychain" about a key
        sitting in a JSON file. Call this right after a save to report the
        truth.
        """
        return self._last_write_backend

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
