"""A watch loop that tells open clients when a provider's model list moves.

Discovery is live (`model_catalog.py`), which is exactly why this exists. A
provider that released a model has it in the catalogue the instant somebody asks,
and every part of this app that shows models — the settings provider rows, the
role cards, the command bar — used to find out only by asking, on a timer it
owned, with nothing to say so. So the engine watches instead, and the only thing
it sends is the *fact of a change*: which provider gained and lost which ids, and
when the providers were last actually asked. A client that receives that reads
`GET /models` — which is a cache hit, because this loop is what warmed it — and
nothing else has to change to stay current.

**The frame is a diff, never a catalogue.** Eight providers at five hundred
models each is a payload no screen asked for, sent on every sweep. The diff is
the part a reader can act on ("openrouter gained two"), and the catalogue is one
cached request away.

**A sweep that finds nothing still says it checked.** Not a change announcement —
a time. Without it, a screen that no longer polls cannot report the age of its own
list, and the honest alternative is either a number nobody can check or a timer
back on the client that spends a provider's rate limit to keep the number fresh.

**No subscribers, no traffic.** The loop asks nobody anything unless a client is
connected. A desktop app left open on the settings screen for an evening would
otherwise poll eight providers every minute forever, spending a provider's
rate limit on a list nobody is reading — and the rate limit is the thing that
makes a user's key stop working.

**The first sweep after a subscriber arrives is a baseline, not a broadcast.**
The alternative is worse than useless: someone opens Codify after an hour away
and every model on every provider is announced as new at once, which is a
notification that teaches its reader to ignore it. So the snapshot that
establishes what "current" means is taken silently, and only the sweeps after it
can say anything changed.

**This is not a goal event.** A catalogue belongs to no goal, and pushing it
into `/ws/goals/{id}` would put a frame with no `goal_id` and no `sequence` into
a durable, sequenced, per-goal log — the cross-stream contamination
`tests/stream_isolation.py` exists to prevent. So it travels on its own channel,
`/ws/engine`, and the goal event union is left alone.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, Literal

import httpx

# The catalogue's own cache TTL (`model_catalog.DEFAULT_TTL_S`). Sweeping on the
# same period means every sweep is a real answer from the providers rather than
# an echo of the last one, and a sweep can never be more than one period stale.
CATALOG_WATCH_INTERVAL_S = 60.0

#: The frames this channel carries. A union, not a string, so the engine and the
#: UI name the same closed set and adding a member is a decision in two files
#: rather than a typo in one.
EngineEventType = Literal["model_catalog_changed", "model_catalog_checked"]


def catalog_changes(
    previous: dict[str, list[str]] | None,
    current: dict[str, list[str]],
) -> dict[str, dict[str, list[str]]]:
    """What moved between two `provider -> ids` snapshots, per provider.

    Empty when nothing moved, which is the common case and the reason the loop
    can be cheap: an unchanged sweep sends nothing at all.

    `previous` of `None` means "nothing to compare against" — the caller is
    establishing a baseline — and reports every current id as added, so the
    caller has one rule for both cases and the decision of *whether* to announce
    it stays with the caller, where the subscriber history lives.

    A provider present in one snapshot and not the other is reported with
    everything it had added or removed. That is a provider that stopped
    answering, which is a fact a reader of a model list needs: a provider going
    quiet is not the same as a provider with nothing new.
    """
    if previous is None:
        return {
            provider: {"added": list(ids), "removed": []}
            for provider, ids in current.items()
            if ids
        }
    changes: dict[str, dict[str, list[str]]] = {}
    for provider in set(previous) | set(current):
        before = set(previous.get(provider, []))
        after = set(current.get(provider, []))
        added = sorted(after - before)
        removed = sorted(before - after)
        if added or removed:
            changes[provider] = {"added": added, "removed": removed}
    return changes


def ids_by_provider(models: list[dict[str, Any]]) -> dict[str, list[str]]:
    """`[{provider, id}, …]` → `{provider: [id, …]}`, order preserved.

    Order comes from the catalogue, which already sorts newest-first where a
    provider dates its models, so a first-sight baseline keeps the order a reader
    will see rather than an arbitrary one.
    """
    out: dict[str, list[str]] = {}
    for model in models:
        provider = str(model.get("provider") or "")
        if not provider:
            continue
        out.setdefault(provider, []).append(str(model.get("id") or ""))
    return out


class CatalogWatch:
    """Re-ask the providers while somebody is listening, and announce a change.

    `publish` is called with one frame per real change and must not block: it is
    the endpoint's fan-out, which hands the frame to each connection's queue
    without waiting for a client to read it.
    """

    def __init__(
        self,
        catalog: Any,
        publish: Callable[[dict[str, Any]], None],
        interval_s: float = CATALOG_WATCH_INTERVAL_S,
    ) -> None:
        self._catalog = catalog
        self._publish = publish
        self._interval_s = interval_s
        self._subscribers = 0
        # The snapshot the next sweep compares against, and whether there is one.
        # Dropped when the last subscriber leaves, so the reconnecting client's
        # first sweep is a baseline rather than an hour of accumulated "news".
        self._last: dict[str, list[str]] | None = None

    @property
    def subscribers(self) -> int:
        return self._subscribers

    def subscribe(self) -> None:
        self._subscribers += 1

    def unsubscribe(self) -> None:
        self._subscribers = max(0, self._subscribers - 1)
        if self._subscribers == 0:
            self._last = None

    async def sweep(self) -> dict[str, dict[str, list[str]]]:
        """One pass: ask, diff, announce. Returns the change it announced.

        Public because a test that has to wait a minute for a timer is a test
        that stops being run, and because "one pass" is the unit that means
        something: the loop is a scheduler around this.

        Two frames, because a client needs to know two different things. A
        **change** frame says a provider's list moved and carries the diff. A
        **checked** frame says only that the providers were asked, and carries the
        time — which is what lets a screen report the age of its list honestly
        without asking the providers itself. A screen showing "checked 40s ago"
        that nobody is keeping true is a screen showing a number nobody can check.
        """
        if self._subscribers == 0:
            return {}
        catalog = await self._catalog.get(refresh=True)
        current = ids_by_provider(catalog.get("models") or [])
        changes = catalog_changes(self._last, current)
        baseline = self._last is None
        self._last = current
        fetched_at = catalog.get("fetched_at") or time.time()
        if baseline or not changes:
            # Nothing moved. The client is still owed the *time*: its list is
            # seconds old, and that is a fact worth carrying on a channel this
            # cheap to send one frame on.
            self._publish({"type": "model_catalog_checked", "payload": {"fetched_at": fetched_at}})
            return {}
        self._publish(
            {
                "type": "model_catalog_changed",
                "payload": {
                    "added": {p: c["added"] for p, c in changes.items() if c["added"]},
                    "removed": {p: c["removed"] for p, c in changes.items() if c["removed"]},
                    "fetched_at": fetched_at,
                },
            }
        )
        return changes

    async def run(self) -> None:
        """The loop. Stops when cancelled, which is how the engine shuts down."""
        while True:
            await asyncio.sleep(self._interval_s)
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, OSError, asyncio.TimeoutError):
                # A sweep that fails this way is a provider that did not answer or
                # a socket that went away, and the catalogue already carries that
                # as a per-provider reason. A watch loop that died on the first
                # network blip would stop announcing releases for the rest of the
                # session, which is the failure nobody ever sees.
                #
                # Deliberately narrow. Anything else escaping here is a bug in this
                # module, and a loop that retries a bug every minute forever hides
                # it: the traceback that would name it never arrives. Letting it
                # propagate ends the watcher loudly instead.
                continue
