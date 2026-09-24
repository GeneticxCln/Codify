"""Live model discovery — every model the UI shows is fetched from the provider.

There is no hardcoded model list anywhere in Codify. A provider gains, renames,
or retires models on its own schedule; a list compiled into the binary would be
wrong within weeks and would show users models they cannot call (and hide ones
they can). So the catalog is built by asking each *configured* provider what it
serves, using that provider's own credential, and the UI refreshes it every time
it opens.

Three consequences worth stating plainly:

* Adding an API key is enough to see a provider's models — the provider does not
  have to be assigned to an agent role first (`_targets` includes every builtin
  whose key resolves).
* Failures are per-provider, not global: one 401 or one unreachable host must not
  empty the whole picker. Each provider reports its own status.
* An empty catalog is a legitimate answer. If nothing is configured and nothing
  is reachable, the UI is told so instead of being handed a plausible-looking
  default list.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from engine.models import BUILTIN_PROVIDERS

DISCOVERY_TIMEOUT_S = 8.0
MAX_MODELS_PER_PROVIDER = 500
DEFAULT_TTL_S = 60.0
ANTHROPIC_VERSION = "2023-06-01"
GOOGLE_MAX_PAGES = 5


@dataclass
class ProviderTarget:
    """One provider to ask, with the credential and endpoint to ask it at."""

    provider: str
    protocol: str
    base_url: str
    api_key: str = ""
    needs_key: bool = True
    sources: list[str] = field(default_factory=list)


@dataclass
class DiscoveryResult:
    provider: str
    protocol: str
    ok: bool
    models: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "protocol": self.protocol,
            "ok": self.ok,
            "count": len(self.models),
            "error": self.error,
        }


def _iso_day(value: Any) -> float | None:
    """Parse a provider timestamp into epoch seconds (`created` / `created_at`)."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            from datetime import datetime

            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return None
    return None


def _entry(
    *,
    model_id: str,
    name: str | None = None,
    description: str = "",
    created: float | None = None,
    supports_chat: bool | None = None,
) -> dict[str, Any]:
    return {
        "id": model_id,
        "name": name or model_id,
        "description": description,
        "created": created,
        "supports_chat": supports_chat,
    }


# ── per-protocol discovery ──────────────────────────────────────────────────


async def discover_ollama(target: ProviderTarget, client: httpx.AsyncClient) -> list[dict[str, Any]]:
    r = await client.get(f"{target.base_url.rstrip('/')}/api/tags")
    r.raise_for_status()
    out = []
    for m in (r.json().get("models") or [])[:MAX_MODELS_PER_PROVIDER]:
        name = m.get("name")
        if not name:
            continue
        size = (m.get("details") or {}).get("parameter_size")
        out.append(
            _entry(
                model_id=name,
                description=f"Local Ollama model ({size})" if size else "Local Ollama model",
                created=_iso_day(m.get("modified_at")),
                # /api/tags reports no capability information. Leaving this
                # unknown is the honest answer — claiming True here would mark
                # an embedding model as chat-capable, which is exactly the kind
                # of invented metadata this module exists to avoid.
                supports_chat=None,
            )
        )
    return out


async def discover_openai_compat(
    target: ProviderTarget, client: httpx.AsyncClient
) -> list[dict[str, Any]]:
    """OpenAI, DeepSeek, Groq, OpenRouter and any OpenAI-compatible endpoint.

    The standard `GET /models` response carries no capability metadata, so
    nothing is filtered or relabelled here — guessing which ids are "chat"
    models from their names is how a hardcoded list sneaks back in.
    """
    r = await client.get(
        f"{target.base_url.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {target.api_key}"},
    )
    r.raise_for_status()
    data = r.json()
    rows = data.get("data") if isinstance(data, dict) else data
    out = []
    for m in (rows or [])[:MAX_MODELS_PER_PROVIDER]:
        if isinstance(m, str):
            out.append(_entry(model_id=m))
            continue
        model_id = m.get("id")
        if not model_id:
            continue
        owner = m.get("owned_by")
        out.append(
            _entry(
                model_id=model_id,
                description=f"{target.provider} · owned by {owner}" if owner else f"{target.provider} API model",
                created=_iso_day(m.get("created")),
            )
        )
    return out


async def discover_anthropic(
    target: ProviderTarget, client: httpx.AsyncClient
) -> list[dict[str, Any]]:
    # The Models API defaults to a small page size; ask for the lot in one call.
    r = await client.get(
        f"{target.base_url.rstrip('/')}/v1/models",
        params={"limit": MAX_MODELS_PER_PROVIDER},
        headers={"x-api-key": target.api_key, "anthropic-version": ANTHROPIC_VERSION},
    )
    r.raise_for_status()
    out = []
    for m in (r.json().get("data") or [])[:MAX_MODELS_PER_PROVIDER]:
        model_id = m.get("id")
        if not model_id:
            continue
        out.append(
            _entry(
                model_id=model_id,
                name=m.get("display_name") or model_id,
                description="Anthropic model",
                created=_iso_day(m.get("created_at")),
                supports_chat=True,
            )
        )
    return out


def _google_id(raw_name: str) -> str:
    """`models/gemini-2.5-flash` → `gemini-2.5-flash`.

    Google returns fully-qualified resource names. Passing one straight into
    `complete()` produced `.../models/models/gemini-…:generateContent`, which
    fails with a confusing 404 — the id has to be stripped at the boundary.
    """
    return raw_name.split("/", 1)[1] if raw_name.startswith("models/") else raw_name


async def discover_google(target: ProviderTarget, client: httpx.AsyncClient) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    page_token: str | None = None
    for _ in range(GOOGLE_MAX_PAGES):
        params: dict[str, Any] = {"key": target.api_key, "pageSize": 200}
        if page_token:
            params["pageToken"] = page_token
        r = await client.get(f"{target.base_url.rstrip('/')}/models", params=params)
        r.raise_for_status()
        body = r.json()
        for m in body.get("models") or []:
            raw = m.get("name") or ""
            if not raw:
                continue
            methods = m.get("supportedGenerationMethods") or []
            limit = m.get("inputTokenLimit")
            out.append(
                _entry(
                    model_id=_google_id(raw),
                    name=m.get("displayName") or _google_id(raw),
                    description=(
                        f"Google · {limit:,} token context" if isinstance(limit, int)
                        else "Google model"
                    ),
                    supports_chat=("generateContent" in methods) if methods else None,
                )
            )
        page_token = body.get("nextPageToken")
        if not page_token:
            break
    return out[:MAX_MODELS_PER_PROVIDER]


DISCOVERERS = {
    "ollama": discover_ollama,
    "openai_compat": discover_openai_compat,
    "anthropic": discover_anthropic,
    "google": discover_google,
}


async def discover_provider(
    target: ProviderTarget, *, client: httpx.AsyncClient | None = None
) -> DiscoveryResult:
    """Query one provider. Never raises: an unusable provider is a reported one."""
    if target.needs_key and not target.api_key:
        return DiscoveryResult(
            target.provider,
            target.protocol,
            ok=False,
            error="no API key configured for this provider",
        )
    if not target.base_url:
        return DiscoveryResult(
            target.provider, target.protocol, ok=False, error="no base_url configured"
        )
    discoverer = DISCOVERERS.get(target.protocol)
    if discoverer is None:
        return DiscoveryResult(
            target.provider, target.protocol, ok=False, error=f"unknown protocol {target.protocol!r}"
        )

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT_S)
    try:
        models = await asyncio.wait_for(
            discoverer(target, client), timeout=DISCOVERY_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        return DiscoveryResult(
            target.provider, target.protocol, ok=False, error=f"timed out after {DISCOVERY_TIMEOUT_S:.0f}s"
        )
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        hint = " (check the API key)" if status in (401, 403) else ""
        return DiscoveryResult(
            target.provider, target.protocol, ok=False, error=f"HTTP {status}{hint}"
        )
    except Exception as exc:
        return DiscoveryResult(
            target.provider, target.protocol, ok=False, error=f"{type(exc).__name__}: {exc}"
        )
    finally:
        if owns_client:
            await client.aclose()

    for m in models:
        m["provider"] = target.provider
        m["protocol"] = target.protocol
    return DiscoveryResult(target.provider, target.protocol, ok=True, models=models)


# ── target selection ────────────────────────────────────────────────────────


def _targets(registry: Any, keychain: Any) -> list[ProviderTarget]:
    """Which providers to ask, and with which credential.

    Union of:
      1. every provider assigned to an agent role (its configured endpoint/key),
      2. every builtin whose key resolves from the keychain or the environment,
         so a freshly added key surfaces models before any role uses it,
      3. every provider that needs no key (local servers).
    """
    by_provider: dict[str, ProviderTarget] = {}

    def add(target: ProviderTarget) -> None:
        existing = by_provider.get(target.provider)
        if existing is None:
            by_provider[target.provider] = target
            return
        # Prefer whichever candidate actually holds a credential/base_url —
        # and an endpoint a role actually configured beats the builtin default.
        # The first-wins rule used to let a role with no base_url (seeded laya)
        # pin the builtin endpoint while every later role's real endpoint was
        # dropped, so discovery polled a server the roles never talk to — and
        # the stale-model warning judged models against the wrong catalog.
        if not existing.api_key and target.api_key:
            existing.api_key = target.api_key
        if target.base_url and target.base_url != existing.base_url:
            # Both candidates here carry "agent:<role>" sources (every target in
            # the config loop does), so the discriminator is whether the
            # EXISTING base_url was role-set or fell back to the builtin default.
            existing_is_default = existing.base_url == BUILTIN_PROVIDERS.get(
                existing.provider, {}
            ).get("base_url")
            if not existing_is_default or existing.base_url == "":
                pass  # existing endpoint is deliberate; keep it
            else:
                existing.base_url = target.base_url
                existing.protocol = target.protocol
        existing.sources.extend(s for s in target.sources if s not in existing.sources)

    try:
        configs = registry.list_configs()
    except Exception:
        configs = []

    for cfg in configs:
        builtin = BUILTIN_PROVIDERS.get(cfg.provider, {})
        # A non-null api_key_ref belongs to this config's CURRENT provider:
        # `SettingsService.set_config` nulls the ref whenever the patch switches
        # provider, so a stored ref was always saved under the provider this row
        # names now. Keep that single-writer invariant true rather than
        # re-checking it here — the keychain has no provider stamp on role refs,
        # so a consumer-side check could only guess.
        add(
            ProviderTarget(
                provider=cfg.provider,
                protocol=cfg.protocol,
                base_url=cfg.base_url or builtin.get("base_url", ""),
                api_key=keychain.get(cfg.api_key_ref) or keychain.get_provider_key(cfg.provider),
                needs_key=bool(builtin.get("needs_key", True)),
                sources=[f"agent:{cfg.role}"],
            )
        )
        # A fallback target must be discoverable too: the settings screen flags a
        # stale fallback with the same live-catalog rule as a primary, and a
        # fallback whose provider was never queried reports "no models", which
        # would dress "we never asked" up as "your model is gone". Custom
        # endpoints are discovered with their own base_url; builtins inherit the
        # catalog endpoint.
        fb_provider = (cfg.fallback_provider or "").strip()
        if fb_provider and fb_provider not in by_provider:
            fb_builtin = BUILTIN_PROVIDERS.get(fb_provider, {})
            add(
                ProviderTarget(
                    provider=fb_provider,
                    protocol=cfg.fallback_protocol or fb_builtin.get("protocol", "openai_compat"),
                    base_url=cfg.fallback_base_url or fb_builtin.get("base_url", ""),
                    api_key=keychain.get_provider_key(fb_provider),
                    needs_key=bool(fb_builtin.get("needs_key", True)),
                    sources=[f"fallback:{cfg.role}"],
                )
            )

    for slug, meta in BUILTIN_PROVIDERS.items():
        if not meta.get("needs_key"):
            add(
                ProviderTarget(
                    provider=slug,
                    protocol=meta["protocol"],
                    base_url=meta["base_url"],
                    needs_key=False,
                    sources=["local"],
                )
            )
            continue
        if keychain.has_provider_key(slug):
            add(
                ProviderTarget(
                    provider=slug,
                    protocol=meta["protocol"],
                    base_url=meta["base_url"],
                    api_key=keychain.get_provider_key(slug),
                    needs_key=True,
                    sources=["key"],
                )
            )

    return sorted(by_provider.values(), key=lambda t: t.provider)


# ── service ─────────────────────────────────────────────────────────────────


class ModelCatalogService:
    """Discovers once, serves many — with an explicit `refresh` escape hatch."""

    def __init__(
        self,
        registry: Any,
        keychain: Any,
        ttl_s: float = DEFAULT_TTL_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._registry = registry
        self._keychain = keychain
        self._ttl_s = ttl_s
        self._transport = transport
        self._cache: dict[str, Any] | None = None
        self._cached_at = 0.0

    async def get(self, refresh: bool = False) -> dict[str, Any]:
        fresh = self._cache is not None and (time.time() - self._cached_at) < self._ttl_s
        if fresh and not refresh:
            return {**self._cache, "cached": True}  # type: ignore[dict-item]

        targets = _targets(self._registry, self._keychain)
        async with httpx.AsyncClient(
            timeout=DISCOVERY_TIMEOUT_S, transport=self._transport
        ) as client:
            results = await asyncio.gather(
                *(discover_provider(t, client=client) for t in targets)
            )

        models: list[dict[str, Any]] = []
        for result in results:
            models.extend(_sorted_models(result.models))

        payload = {
            "models": models,
            "providers": [r.to_payload() for r in sorted(results, key=lambda r: r.provider)],
            "fetched_at": time.time(),
            "cached": False,
        }
        self._cache = payload
        self._cached_at = time.time()
        return payload

    def invalidate(self) -> None:
        self._cache = None
        self._cached_at = 0.0


def _sorted_models(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Newest first when the provider dates its models, then by name.

    Providers add models on their own schedule; sorting by `created` is what
    puts a model released this morning at the top of the picker instead of
    wherever it happened to land in the API's alphabetical response.
    """
    dated = [m for m in models if m.get("created")]
    if not dated:
        return sorted(models, key=lambda m: m["id"])
    dated.sort(key=lambda m: (-float(m["created"]), m["id"]))
    undated = sorted((m for m in models if not m.get("created")), key=lambda m: m["id"])
    return dated + undated
