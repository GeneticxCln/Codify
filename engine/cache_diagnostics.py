"""Why a prompt cache missed, from facts that hold no prompt text.

A provider's prompt cache is keyed on a prefix. When a call that should have read from it reads nothing, one of a short
list of things changed between that call and the one before it: the model, the system prompt, the tools offered, the
history was rewritten by a compaction, or the cache simply expired. `docs/15` and `docs/16` record the token counts the
providers report; this module adds the *why*, from digests.

**Content-free by construction** (`docs/21` decision 6). What is kept about a call is a short digest of its system
prompt, a digest of its sorted tool names, the model and protocol names, and a length. Never the prompt, never a tool
description, never an argument. A digest of a long prompt cannot be reversed into it, and two prompts that differ in one
character have unrelated digests, which is all `explain` needs: it asks whether they are equal.

Pure, like `usage_audit`: no clock, no database, no model. The audit hands it the recorded facts.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

# What a miss can be attributed to, in the order the attribution is tried. `none` means that nothing the engine
# changed between the two calls explains it: the provider's own minimum prefix size, a different server, or a cache
# that was never asked for.
REASONS: tuple[str, ...] = (
    "first_request", "model_switch", "system_changed", "tools_changed", "compaction", "idle_ttl", "none",
)

# How long an unused cache entry is assumed to live. Anthropic's default ephemeral cache is five minutes; a provider
# with another lifetime makes `idle_ttl` an over- or under-claim, which is why the audit calls it a signal.
IDLE_TTL_S = 300.0

# Digests are the first 16 hex characters of a SHA-256: enough to tell two prompts apart, short enough that a
# diagnostics field cannot be mistaken for, or used as, a copy of anything.
DIGEST_CHARS = 16
# The two free-text parts are provider-chosen identifiers (a model id, a protocol name), not prompt text, and are
# still bounded so a pathological one cannot make a field a place to keep content.
MAX_NAME_CHARS = 100


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:DIGEST_CHARS]


def prompt_parts(system: str, tool_names: Iterable[str], model: str, protocol: str) -> dict[str, str]:
    """The parts a cache key is made of, as digests and short names. Tool order does not matter: it is sorted, so
    a reordering that the provider would also treat as a different prefix is caught by `tools` only if the *set*
    changed, and the prefix-stability test (`tests/test_prompt_prefix_stability.py`) is what pins the order."""
    return {
        "system": digest(system),
        "tools": digest("\n".join(sorted(tool_names))),
        "model": model[:MAX_NAME_CHARS],
        "protocol": protocol[:MAX_NAME_CHARS],
    }


def fingerprint(parts: dict[str, str]) -> str:
    """One digest over all the parts, in a fixed order. Changes exactly when one of them does."""
    return digest("|".join(parts.get(key, "") for key in ("system", "tools", "model", "protocol")))


@dataclass(frozen=True)
class CallFacts:
    """What `explain` needs to know about one model call."""

    parts: dict[str, str]
    timestamp: float
    cache_read: int = 0
    cache_write: int = 0


def explain(
    previous: CallFacts | None, current: CallFacts, *, compacted_between: bool = False,
    idle_ttl_s: float = IDLE_TTL_S,
) -> str:
    """The first reason, in `REASONS` order, that applies between two consecutive calls of one conversation."""
    if previous is None:
        return "first_request"
    if (
        previous.parts.get("model") != current.parts.get("model")
        or previous.parts.get("protocol") != current.parts.get("protocol")
    ):
        return "model_switch"
    if previous.parts.get("system") != current.parts.get("system"):
        return "system_changed"
    if previous.parts.get("tools") != current.parts.get("tools"):
        return "tools_changed"
    if compacted_between:
        return "compaction"
    if current.timestamp - previous.timestamp > idle_ttl_s:
        return "idle_ttl"
    return "none"
