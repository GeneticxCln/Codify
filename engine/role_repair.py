"""Which roles cannot run, and what to point them at.

Fresh installs and installs that never added a key come up with roles that cannot
be called at all: no model chosen, or a provider with no credential. Fixing those
one card at a time in Settings is the tedium this module removes.

The restraint matters as much as the repair: a role that works is **never**
touched. Silently repointing a role someone deliberately chose is worse than the
tedium, and it would be the kind of quiet overwrite that makes an app untrustworthy.
So the rule is narrow, and every decision — including every decision *not* to act —
is returned with its reason.

Pure by design: no DB, no HTTP, no clock. The endpoint applies what this plans, and
the tests exercise the rule directly.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

# A failed discovery proves nothing, so it never makes a role repairable. Treating
# "the provider did not answer" as "your model is gone" would repoint roles over a
# temporary outage — which is exactly the false statement with the shape of the
# true one that the stale-model warning already guards against.


@dataclass
class RepairPlan:
    """What to change, what to leave, and why — for the caller to apply."""

    target: dict[str, Any] | None = None
    target_reason: str = ""
    to_repair: list[tuple[str, str]] = field(default_factory=list)  # (role, reason)
    left_alone: list[tuple[str, str]] = field(default_factory=list)  # (role, reason)
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.to_repair) and self.target is not None


def _keys_by_provider(key_status: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    # Rows without a provider are skipped rather than keyed under None: every lookup
    # is by provider name, so a None key is a key nobody can reach.
    return {str(row["provider"]): row for row in key_status or [] if row.get("provider")}


def _describe(provider: str, model: str, discovery: dict[str, dict[str, Any]]) -> tuple[str, str]:
    """How well a target is known to work, plus a caveat worth reporting once.

    "We checked and your model is there" is a different assurance from "we could
    not check", and the user deserves to know which one they have. The unverified
    wording deliberately does not claim the target is usable: the provider never
    said so, and a reason string that reads `usable:` would be a claim nobody made.
    """
    found = discovery.get(provider)
    if found and found.get("ok"):
        return f"usable: {provider}/{model} (verified in the catalog)", ""
    if found:
        return (
            f"left as configured: {provider}/{model} (not verified — {found.get('error')})",
            f"{provider} did not answer discovery ({found.get('error')}) — its roles were left "
            "alone rather than repointed on an unknown",
        )
    return f"left as configured: {provider}/{model} (provider not checked)", ""


def _fallback_verdict(
    config: dict[str, Any],
    problem: str,
    keys: dict[str, dict[str, Any]],
    discovery: dict[str, dict[str, Any]],
    catalog_ids: set[tuple[str, str]],
) -> tuple[str | None, str]:
    """Why the role's fallback saves it, why it does not, or neither.

    Returns `(reason_to_leave_alone, note)`. `reason_to_leave_alone` is None when
    the role really is broken — and then `note` explains the fallback, so the
    repair reason can name both halves of the problem instead of only the primary's.

    A fallback that is merely unverifiable is not a proven fault: the role may well
    run on it. Only a fallback the provider itself no longer serves makes the whole
    role broken, because then neither target can be called.
    """
    provider = config.get("fallback_provider") or ""
    model = (config.get("fallback_model_name") or "").strip()
    if not provider or not model:
        return None, ""
    fb_problem = target_problem(
        provider, model, config.get("fallback_protocol"), keys, discovery, catalog_ids
    )
    found = discovery.get(provider)
    if fb_problem is None or found is None or not found.get("ok"):
        assurance, note = _describe(provider, model, discovery)
        verified = found is not None and bool(found.get("ok"))
        lead = "runs on its fallback" if verified else "may run on its fallback"
        return f"{lead}: {problem} ({assurance})", note
    return None, f"its fallback {provider}/{model} is unusable too ({fb_problem})"


def _add_note(plan: RepairPlan, note: str) -> None:
    """Record a provider-level caveat once.

    Notes are reported per provider, not per role, so seven roles on an unreachable
    provider must not produce the same sentence seven times — a wall of identical
    warnings is how a real one gets skimmed past.
    """
    if note not in plan.notes:
        plan.notes.append(note)


def target_needs_key(provider: str, protocol: str | None, keys: dict[str, dict[str, Any]]) -> bool:
    """Does this provider require a credential?

    The provider's own answer wins when we have one. Falling back to the protocol
    treats anything that is not a local Ollama endpoint as needing a key — for a
    custom slug with no catalog entry that is the honest guess, because the engine
    has no record of a keyless custom provider.
    """
    known = keys.get(provider or "")
    if known is not None:
        return bool(known.get("needs_key"))
    return (protocol or "") != "ollama"


def needs_key(config: dict[str, Any], keys: dict[str, dict[str, Any]]) -> bool:
    return target_needs_key(config.get("provider") or "", config.get("protocol"), keys)


def target_problem(
    provider: str | None,
    model: str | None,
    protocol: str | None,
    keys: dict[str, dict[str, Any]],
    discovery: dict[str, dict[str, Any]],
    catalog_ids: set[tuple[str, str]],
) -> str | None:
    """Why this (provider, model) target cannot be called, or None if it can.

    Used for both of a role's targets: the primary and the fallback are the same
    question asked twice, and a second copy of these three checks is how the two
    answers end up disagreeing.
    """
    provider = provider or ""
    model = (model or "").strip()
    if not model:
        return "no model is chosen"
    if target_needs_key(provider, protocol, keys) and not (keys.get(provider) or {}).get("has_key"):
        return f"{provider} needs a credential and none is stored"
    found = discovery.get(provider)
    if found and found.get("ok") and (provider, model) not in catalog_ids:
        return f'{provider} no longer reports "{model}"'
    return None


def provider_is_keyless(provider: str, keys: dict[str, dict[str, Any]]) -> bool:
    known = keys.get(provider)
    if known is not None:
        return not bool(known.get("needs_key"))
    return False


def _usable_candidates(catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Discovered models that can actually answer.

    A provider's own report is the only reason a model is skipped here — the engine
    never guesses from a model's name (a name-based "skip the embeddings" list is a
    hardcoded list in disguise).
    """
    return [m for m in catalog or [] if m.get("supports_chat") is not False]


def choose_target(
    configs: list[dict[str, Any]],
    working_roles: set[str],
    catalog: list[dict[str, Any]],
    keys: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    """The model to point broken roles at, with the reason for choosing it.

    Order of preference:

    1. the model the *working* roles already use, most-used first — meeting the
       install where it is beats introducing a second model it never planned for;
    2. the first model on a provider that needs no credential, so the repair cannot
       fail for a reason the user has not fixed yet;
    3. the first discovered model at all.
    """
    candidates = _usable_candidates(catalog)
    if not candidates:
        return None, (
            "no model was discovered, so there is nothing to point these roles at: add a provider "
            "key or start a local model server, then load the model list again"
        )

    # How many *working* roles each discovered model is already serving. Counting
    # roles (not catalog entries) is what makes "the model your install already
    # runs" the model it actually runs.
    candidate_keys = {(m["provider"], m["id"]) for m in candidates}
    # A working role counts once, against whichever of its targets is actually in
    # the catalog: a role that runs on its fallback is already running that model,
    # so pointing the others at it introduces nothing new.
    in_use: Counter[tuple[str | None, str]] = Counter()
    for c in configs:
        if c.get("role") not in working_roles:
            continue
        for pair in (
            (c.get("provider"), (c.get("model_name") or "").strip()),
            (c.get("fallback_provider"), (c.get("fallback_model_name") or "").strip()),
        ):
            if pair[0] and pair[1] and pair in candidate_keys:
                in_use[pair] += 1
                break
    if in_use:
        (provider, model), count = in_use.most_common(1)[0]
        working = max(1, len(working_roles))
        return (
            {"provider": provider, "model": model},
            f"the model {count} of {working} working role{'s' if working != 1 else ''} already use",
        )

    keyless = next((m for m in candidates if provider_is_keyless(m["provider"], keys)), None)
    if keyless:
        return (
            {"provider": keyless["provider"], "model": keyless["id"]},
            f"the first model on {keyless['provider']}, which needs no credential",
        )

    first = candidates[0]
    return (
        {"provider": first["provider"], "model": first["id"]},
        f"the first discovered model ({first['provider']})",
    )


def plan_role_repair(
    configs: list[dict[str, Any]],
    key_status: list[dict[str, Any]],
    provider_status: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
) -> RepairPlan:
    """Decide which roles cannot run, and what to point them at.

    A role needs repair when it has no model, when its provider needs a credential
    the engine does not have, or when its provider answered and no longer lists the
    model it is set to. It is left alone otherwise — including when its provider
    could not be reached at all, which is an unknown, not a fault.
    """
    keys = _keys_by_provider(key_status)
    # Both are looked up by a real provider/id, so a row missing one is skipped
    # rather than keyed under None (which nothing can ever match).
    discovery = {
        str(row["provider"]): row for row in provider_status or [] if row.get("provider")
    }
    catalog_ids = {
        (str(m["provider"]), str(m["id"]))
        for m in _usable_candidates(catalog)
        if m.get("provider") and m.get("id")
    }

    plan = RepairPlan()
    for config in configs:
        role = config.get("role") or "?"
        provider = config.get("provider") or "?"
        model = (config.get("model_name") or "").strip()

        problem = target_problem(
            provider, model, config.get("protocol"), keys, discovery, catalog_ids
        )
        if problem is not None:
            # The primary cannot run. Before calling the role broken, ask its
            # fallback: a role whose fallback works is a role that keeps running,
            # and repointing its primary would overwrite the very choice holding
            # the goal up.
            verdict, detail = _fallback_verdict(
                config, problem, keys, discovery, catalog_ids
            )
            if verdict is not None:
                plan.left_alone.append((role, verdict))
                if detail:
                    _add_note(plan, detail)
                continue
            plan.to_repair.append(
                (role, f"{problem}; {detail}" if detail else problem)
            )
            continue

        # The role works, so it is left exactly as it is — with the assurance it
        # actually has.
        reason, note = _describe(provider, model, discovery)
        plan.left_alone.append((role, reason))
        if note:
            _add_note(plan, note)

    working = {role for role, _ in plan.left_alone}
    plan.target, plan.target_reason = choose_target(configs, working, catalog, keys)
    # Roles that needed repair and a repair nobody could perform are different
    # outcomes, but they need no note here: the caller has `to_repair` and
    # `target_reason`, and repeating the same sentence in both places is how a
    # screen ends up saying one thing twice and reading as none.
    return plan
