"""The engine's half of the bridge to the surfaces the app window owns, and the part of it that does not change from surface to surface.

The conductor has eyes and hands on one surface the engine does not hold: the browser pages, which the Tauri shell owns and
`webview_bridge.py` reaches. The editor is the second, and it lives in the UI process, so the shell cannot be the go-between.
The next surfaces (a terminal, a diff, whatever the app grows) will want the same two things, a way to *look* and a way to
*act*, so this file is the part of that which is written once:

  * **The engine asks, the window polls and answers.** `GET /surfaces/next` is a long poll and `POST /surfaces/answer`
    is the reply; there is no socket, for the reason `webview_bridge.py` gives (every WebSocket here carries one goal's
    events, and a second long-lived one is how two streams interleave). A poll is also the liveness signal: a window
    that stops polling is a window that is gone, and the next question says so instead of waiting out a timeout.
  * **An operation is a fixed string from a table**, `{surface: {op: Op}}`, never the model's to invent. An `Op` pairs
    the shape of what the engine may send with the shape of what may come back. Adding a surface is adding one entry and
    its vocabulary, and nothing here changes.
  * **Both shapes are fixed and checked on the engine's side.** Arguments are validated *before* they cross, so a
    refusal costs no round trip and a field the op does not name never leaves the engine. An answer is validated into
    its result model, which drops every field the model does not name, caps everything that can be long, and refuses a
    wrong type rather than repairing it. What the window holds can be third-party text (a workspace file says whatever it
    says), so nothing it returns is trusted past those shapes.
  * **A question that is not answered is a sentence.** The wait is bounded, and an answer for a question that is not
    waiting (an id the engine never issued, a second answer, one that arrived after the timeout) is dropped on the floor.

**What this carries is questions and answers, never authority.** There is no path from here to the filesystem, the
sandbox, the boot token or a Tauri command, and nothing the window says becomes argv or a path the engine acts on. In
particular the editor's `edit` changes the text in an open buffer and nothing else: a person's own Save is the only way
that text reaches the disk (docs/00 §6.9), and `tests/test_invariants_at_their_boundary.py` fails if this module, or any
conductor module, so much as names the route's writer.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError

#: How long a question waits for the window. An editor answers in milliseconds, so this is long enough for a window
#: that is busy painting and short enough that a window which is not there costs one conductor call, not its budget.
ASK_TIMEOUT_S = 15.0

#: The longest the window's poll is held open: short enough that the window notices a dead engine between polls, long
#: enough that an idle engine is not woken hundreds of times a minute.
POLL_WAIT_S = 20.0

#: How long a poll has to have been seen before the bridge stops claiming a window is there. Three polls of silence is a
#: window that is not coming back, and a sticky flag would outlive the app that set it.
ATTACHED_WINDOW_S = POLL_WAIT_S * 3.0

#: The longest reason from the window that is passed on to the model.
MAX_REFUSAL_CHARS = 500


class SurfaceUnavailable(RuntimeError):
    """Nothing is polling, so there is nobody to ask. The message says so in a sentence the model can act on."""


class SurfaceRefused(RuntimeError):
    """The question was not asked, or was not done, and the message says why."""


class SurfaceAnswer(BaseModel):
    """What the window posts back: one id, one verdict, one payload.

    `extra: "forbid"` like every other body here. (The browser bridge's answer is a plain model and ignores extras
    silently; a body that cannot be closed would be the one place a field could be smuggled past the shapes below.)
    `id` is a plain string because it is an opaque handle the engine issued, not a fact about the world: an id that was
    never issued is dropped by the bridge, and a validation error here would only turn a stale answer into a 422.
    """

    model_config = {"extra": "forbid"}
    id: str
    ok: bool = True
    result: Any = None
    error: str | None = Field(None, description="the window's reason, when `ok` is false")


@dataclass(frozen=True)
class Op:
    """One thing the engine may ask a surface to do: its name, what it may send, and what may come back."""

    name: str
    args: type[BaseModel]
    result: type[BaseModel]


@dataclass
class _Pending:
    id: str
    surface: str
    op: Op
    args: dict[str, Any]
    workspace_id: str
    future: asyncio.Future[BaseModel]


def default_surfaces() -> dict[str, dict[str, Op]]:
    """Every surface the engine knows. Imported late, because each surface's vocabulary imports `Op` from here."""
    from engine.surface_editor import EDITOR_OPS

    return {"editor": dict(EDITOR_OPS)}


class SurfaceBridge:
    """A queue of questions for the app window, and the answers to them.

    One instance per engine, on `app.state`, so the executor's tools and the three routes talk to the same pending set.
    Every method is for the event loop only, which is every caller.
    """

    def __init__(self, surfaces: Mapping[str, Mapping[str, Op]] | None = None) -> None:
        self._surfaces: dict[str, dict[str, Op]] = (
            {name: dict(ops) for name, ops in surfaces.items()} if surfaces is not None else default_surfaces()
        )
        self._pending: dict[str, _Pending] = {}
        # Questions already handed to the window, so a second poll does not hand the same one out again. Unlike the
        # browser bridge, several questions may be in flight, because an editor answers instantly and the conductor
        # may ask while a person is typing; they come out in the order they went in.
        self._handed: set[str] = set()
        self._last_seen: float | None = None
        self._wake = asyncio.Event()

    # ── the engine's side ──────────────────────────────────────────────────

    async def ask(self, surface: str, op: str, args: dict[str, Any], *, workspace_id: str) -> BaseModel:
        """Put one question to the window and wait for its answer, as the op's result model.

        Raises `SurfaceRefused` (an op that does not exist, arguments that do not fit it, an answer of the wrong shape,
        a refusal from the window, or silence) or `SurfaceUnavailable` (no window is polling). Every caller is a tool
        dispatcher, whose contract is to turn either into a sentence the model is shown.
        """
        spec = self._surfaces.get(surface, {}).get(op)
        if spec is None:
            raise SurfaceRefused(f"There is no {op!r} on the {surface!r} surface.")
        if not self.attached:
            raise SurfaceUnavailable(
                "The app window is not connected to this engine, so there is nothing to ask. This is the normal answer "
                "outside the desktop app: a command-line engine, a benchmark and a headless test have no window. "
                "Answer from the workspace instead, or ask the person to open the app."
            )
        try:
            cleaned = spec.args.model_validate(args).model_dump()
        except ValidationError as exc:
            raise SurfaceRefused(f"{surface} {op} was not asked: {_why(exc)}") from None
        loop = asyncio.get_running_loop()
        pending = _Pending(
            id=uuid.uuid4().hex, surface=surface, op=spec, args=cleaned, workspace_id=workspace_id,
            future=loop.create_future(),
        )
        self._pending[pending.id] = pending
        self._wake.set()
        try:
            return await asyncio.wait_for(pending.future, ASK_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise SurfaceRefused(
                f"The app window did not answer {surface} {op} within {ASK_TIMEOUT_S:.0f}s. It may be closed, or busy; "
                "do not ask again straight away. Say what you wanted and let the person do it."
            ) from None
        finally:
            self._pending.pop(pending.id, None)
            self._handed.discard(pending.id)

    @property
    def attached(self) -> bool:
        """Whether the window has polled recently enough to believe."""
        return self._last_seen is not None and (time.monotonic() - self._last_seen) <= ATTACHED_WINDOW_S

    def state(self) -> dict[str, Any]:
        """What the window may want to know before it polls, and what a tool may be asked."""
        return {
            "attached": self.attached,
            "inflight": len(self._pending),
            "timeout_s": ASK_TIMEOUT_S,
            "surfaces": {name: sorted(ops) for name, ops in self._surfaces.items()},
        }

    # ── the window's side ──────────────────────────────────────────────────

    def note_ui(self) -> None:
        """A window polled. That is the whole liveness signal there is, stamped rather than latched."""
        self._last_seen = time.monotonic()

    async def next_request(self, wait_s: float = POLL_WAIT_S) -> dict[str, Any] | None:
        """The oldest question not yet handed out, or None if none arrives in time."""
        self.note_ui()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, wait_s)
        while True:
            item = self._oldest()
            if item is not None:
                return self._hand_out(item)
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            # Cleared before the re-check, never after: a question asked between the check and the clear would set
            # the event, the wait would eat the set, and the question would sit until the poll expired.
            self._wake.clear()
            item = self._oldest()
            if item is not None:
                return self._hand_out(item)
            try:
                await asyncio.wait_for(self._wake.wait(), remaining)
            except asyncio.TimeoutError:
                return None

    def answer(self, request_id: str, ok: bool, payload: Any, error: str | None = None) -> bool:
        """Land an answer on a waiting question. False if nothing is waiting under that id.

        Popped, not left for `ask`'s `finally`: between resolving a future and the awaiting coroutine resuming, the entry
        would still look outstanding, and a second answer (or a poll) landing in that window would act on a question that
        has already been answered.
        """
        pending = self._pending.pop(str(request_id or ""), None)
        if pending is None or pending.future.done():
            return False
        self._handed.discard(pending.id)
        label = f"{pending.surface} {pending.op.name}"
        if not ok:
            pending.future.set_exception(SurfaceRefused(_refusal(error, payload)))
            return True
        try:
            result = pending.op.result.model_validate(payload)
        except ValidationError as exc:
            pending.future.set_exception(
                SurfaceRefused(f"The app window's answer to {label} was not in a shape the engine accepts: {_why(exc)}")
            )
            return True
        pending.future.set_result(result)
        return True

    def abandon(self, request_id: str) -> bool:
        """The window gave up on a question rather than answering it."""
        pending = self._pending.pop(str(request_id or ""), None)
        if pending is None or pending.future.done():
            return False
        self._handed.discard(pending.id)
        pending.future.set_exception(SurfaceRefused("The app window gave up on that before it had an answer."))
        return True

    def _oldest(self) -> _Pending | None:
        """The question asked first that the window has not been given, so answers come back in asking order."""
        for pending in self._pending.values():
            if pending.id not in self._handed:
                return pending
        return None

    def _hand_out(self, pending: _Pending) -> dict[str, Any]:
        self._handed.add(pending.id)
        return {
            "id": pending.id,
            "surface": pending.surface,
            "op": pending.op.name,
            "workspace_id": pending.workspace_id,
            "args": pending.args,
        }


def _why(exc: ValidationError) -> str:
    """The first thing wrong, in a line, from a validation error: a field name and what was expected of it."""
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first.get("loc", ())) or "the answer"
    return f"{where}: {first.get('msg', 'invalid')}"


def _refusal(error: str | None, payload: Any) -> str:
    """The window's own reason, capped, or a plain sentence when it gave none."""
    for candidate in (error, payload if isinstance(payload, str) else None):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()[:MAX_REFUSAL_CHARS]
    return "The app window could not do that and did not say why."
