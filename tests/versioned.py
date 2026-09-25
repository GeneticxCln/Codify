"""Post a version-protected goal action without losing the race it invites.

Every mutating goal route takes an `expected_version` and compares it against the row
as it stands *when the request lands* — `GoalService.update_status` and
`update_step` in `engine/services.py`, plus the explicit checks in `engine/app.py`
for `/apply` and `/enable-execution`. That guard is what stops two clients acting on
one view of a goal. It is also why every client, this suite included, has to fetch a
version and then act on it, and anything that moves the goal in between turns that
fetch into a race the caller can lose. The engine reports a lost race in exactly two
ways, and telling them apart is the whole job:

* `409 version_conflict` — the version moved. The intent did not; only the number to
  quote went stale. Re-read and re-send.
* `409 illegal_status` — the *status* moved. Either the act is already in place (a
  `/start` on a goal something else started is moot, not a failure) or the goal went
  somewhere the act cannot follow and the caller's premise is gone.

So that reading lives here, once, instead of in every test that acts on a goal:

    post_versioned(client, goal_id, "start", diagnose=lambda: _diagnose(client, goal_id))

The one-line call site is the point. A test that hand-rolls the dance fails with
`assertEqual(200, response.status_code)` and a 409 body as its only clue. A test that
calls this is told which of the four things happened — a race worth retrying, an act
already in place, a goal that went terminal, or a route refusal no re-read can fix —
and, with `diagnose`, gets the goal's own event log attached to any of them.

Deliberately *not* retried: `plan_only`, `not_dry_run`, `nothing_to_apply`,
`goal_in_progress`, `step_not_pending`. Those are premise errors: the request cannot
succeed as written, so they are raised at once, with the body, rather than ground
into a timeout. For the same reason, a test whose subject *is* a refusal should post
directly instead of coming through here — the codes this module treats as benign
races are exactly the ones such a test exists to see.

The tables below are transcribed from the routes in `engine/app.py`;
`tests/test_versioned.py` re-derives them from the running engine, so an engine that
widens an action's legal statuses fails a test instead of quietly making this
module's idea of "legal" wrong.
"""

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

#: The only two 409 codes that mean "the goal moved under you". Everything else a
#: versioned route answers with is a premise error, not a race.
RACE_CODES = ("version_conflict", "illegal_status")

#: The statuses each action is legal from, as `engine/app.py` checks them.
LEGAL_FROM: dict[str, frozenset[str]] = {
    "start": frozenset({"PENDING", "PAUSED"}),
    "pause": frozenset({"RUNNING"}),
    "cancel": frozenset({"PLANNING", "PENDING", "RUNNING", "PAUSED"}),
    "apply": frozenset({"COMPLETED", "FAILED"}),
    "enable-execution": frozenset({"PENDING", "PAUSED"}),
}

#: Statuses the engine refuses *now* but leaves on its way to a legal one: a goal
#: still planning, or a dry run still running. Waiting these out is the difference
#: between a test that waits for a plan and one that asserts against a guess — the
#: e2e harness used to open-code exactly this loop, with the same 30s ceiling and
#: the same "planning never produced a startable goal" failure.
LEGAL_SOON: dict[str, frozenset[str]] = {
    "start": frozenset({"PLANNING"}),
    "pause": frozenset({"PENDING"}),
    "cancel": frozenset(),
    "apply": frozenset({"PLANNING", "PENDING", "RUNNING", "PAUSED"}),
    "enable-execution": frozenset({"PLANNING"}),
}

#: Statuses meaning the act's effect is already in place. Retrying would be wrong
#: even though the request is legal: `/start` on a RUNNING goal would launch a
#: second step run. Reported as moot so the caller can say whether that is
#: acceptable, which for a test that was the only mover it never is.
#:
#: `/apply` has none on purpose. Applying clears the stored proposals, so a lost
#: apply race surfaces as `nothing_to_apply` — a premise error, loudly, rather than a
#: second run of a deliverable the user already applied.
ALREADY_DONE: dict[str, frozenset[str]] = {
    "start": frozenset({"RUNNING"}),
    "pause": frozenset({"PAUSED"}),
    "cancel": frozenset({"CANCELLED"}),
    "apply": frozenset(),
    "enable-execution": frozenset(),
}


class PremiseBroken(AssertionError):
    """The act cannot succeed from where the goal is, or the route refused for a
    reason no re-read fixes. `response` is the engine's answer when there was one.

    An `AssertionError` because that is what it is, from a test's point of view: the
    premise this test set up does not hold, and retrying cannot change that.
    """

    def __init__(self, message: str, response: httpx.Response | None = None) -> None:
        super().__init__(message)
        self.response = response


class RaceLost(AssertionError):
    """The act is already in place, or a race outlived the deadline.

    Both mean the same thing to a caller that was the only mover: the test did not
    get to perform the act, and the message says which way it lost.
    """


@dataclass(frozen=True)
class ActionResult:
    """A versioned action the engine accepted."""

    response: httpx.Response
    attempts: int
    races: int


def _tables(action: str) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    if action not in LEGAL_FROM:
        raise KeyError(f"no versioned table for {action!r}; known actions: {sorted(LEGAL_FROM)}")
    return LEGAL_FROM[action], LEGAL_SOON[action], ALREADY_DONE[action]


def _note(diagnose: Callable[[], str] | None) -> str:
    """Whatever the caller can say about *why* — read only on a failure path, since
    diagnosing is two more requests the happy path should not pay for."""
    if diagnose is None:
        return ""
    return f" — {diagnose()}"


def _verdict(
    action: str, goal: Mapping[str, Any], diagnose: Callable[[], str] | None
) -> str:
    """What one read of a goal means for `action`: "send", or "wait".

    The other two answers raise instead of returning, because they end the attempt:
    an act already in place (`RaceLost` — the effect the caller wanted is there) and
    a goal somewhere the act cannot follow (`PremiseBroken` — the caller's premise is
    gone, and no number of retries will bring it back).
    """
    legal, soon, done = _tables(action)
    status = str(goal.get("status", ""))
    if status in legal:
        return "send"
    where = f"the goal is {status or 'missing a status'}"
    if status in done:
        raise RaceLost(f"{where}, so /{action} is already in place and this call is moot{_note(diagnose)}")
    if status in soon:
        return "wait"
    raise PremiseBroken(
        f"{where}, and /{action} is only legal from {' or '.join(sorted(legal))}{_note(diagnose)}"
    )


def _code(response: httpx.Response) -> str:
    """The refusal code the engine named, or the status code as a stand-in.

    `ApiError` renders as `{"code", "message"}` (`engine/services.py`), but a 409
    that somehow arrives without one is still a refusal and must not be retried as if
    it were a race.
    """
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(body, Mapping):
        return str(body.get("code") or f"HTTP {response.status_code}")
    return f"HTTP {response.status_code}"


def _waited_too_long(
    action: str, goal: Mapping[str, Any], deadline: float, diagnose: Callable[[], str] | None
) -> None:
    if time.monotonic() < deadline:
        return
    raise RaceLost(
        f"/{action} never became legal: the goal is still {goal.get('status')}"
        f"{_note(diagnose)}"
    )


def post_versioned(
    client: httpx.Client,
    goal_id: str,
    action: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 15.0,
    poll: float = 0.05,
    on_retry: Callable[[str], None] | None = None,
    diagnose: Callable[[], str] | None = None,
) -> ActionResult:
    """POST `/goals/{id}/{action}` with the version the engine currently holds.

    For a client talking to a real engine over a socket. `timeout` bounds the whole
    attempt, waiting included, so a goal that never becomes legal fails with what it
    was doing instead of hanging. `on_retry` is told about each race as it is lost,
    which is how a caller logs the flakiness it just absorbed rather than hiding it.
    """
    _tables(action)  # a typo'd action must not cost a request
    path = f"/goals/{goal_id}/{action}"
    deadline = time.monotonic() + timeout
    attempts = races = 0
    while True:
        goal = client.get(f"/goals/{goal_id}", headers=headers).json()
        if _verdict(action, goal, diagnose) == "wait":
            _waited_too_long(action, goal, deadline, diagnose)
            time.sleep(poll)
            continue
        attempts += 1
        response = client.post(
            path, json={"expected_version": goal.get("version")}, headers=headers
        )
        if response.status_code < 400:
            return ActionResult(response, attempts, races)
        code = _code(response)
        if code not in RACE_CODES:
            raise PremiseBroken(
                f"/{action} was refused: {response.status_code} {code}: {response.text}"
                f"{_note(diagnose)}",
                response,
            )
        races += 1
        if time.monotonic() >= deadline:
            raise RaceLost(
                f"/{action} lost {races} race(s) in {timeout:g}s and never landed"
                f" (last: {code} at version {goal.get('version')}){_note(diagnose)}"
            )
        if on_retry is not None:
            on_retry(f"{code} at version {goal.get('version')}")
        time.sleep(poll)


async def post_versioned_async(
    client: httpx.AsyncClient,
    goal_id: str,
    action: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 15.0,
    poll: float = 0.05,
    on_retry: Callable[[str], None] | None = None,
    diagnose: Callable[[], str] | None = None,
) -> ActionResult:
    """`post_versioned` for the async clients the in-process tests use.

    The same loop with awaits. Every decision lives in `_verdict`, so the two cannot
    drift apart in the part that matters — what a status or a 409 code means.
    """
    _tables(action)
    path = f"/goals/{goal_id}/{action}"
    deadline = time.monotonic() + timeout
    attempts = races = 0
    while True:
        goal = (await client.get(f"/goals/{goal_id}", headers=headers)).json()
        if _verdict(action, goal, diagnose) == "wait":
            _waited_too_long(action, goal, deadline, diagnose)
            await asyncio.sleep(poll)
            continue
        attempts += 1
        response = await client.post(
            path, json={"expected_version": goal.get("version")}, headers=headers
        )
        if response.status_code < 400:
            return ActionResult(response, attempts, races)
        code = _code(response)
        if code not in RACE_CODES:
            raise PremiseBroken(
                f"/{action} was refused: {response.status_code} {code}: {response.text}"
                f"{_note(diagnose)}",
                response,
            )
        races += 1
        if time.monotonic() >= deadline:
            raise RaceLost(
                f"/{action} lost {races} race(s) in {timeout:g}s and never landed"
                f" (last: {code} at version {goal.get('version')}){_note(diagnose)}"
            )
        if on_retry is not None:
            on_retry(f"{code} at version {goal.get('version')}")
        await asyncio.sleep(poll)
