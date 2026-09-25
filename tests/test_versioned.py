"""The versioned-action helper, and proof its idea of "legal" is the engine's.

`tests/versioned.py` exists so a test acting on a goal does not lose the race its own
`expected_version` invites. Two things have to hold for that to be worth having, and
only one of them is about the retry loop:

1. the loop does the right thing with each answer the engine can give — a race worth
   retrying, an act already in place, a goal that went terminal, a route refusal no
   re-read fixes, and a race that never settles;
2. the tables that decide those cases are the engine's tables, not this repo's
   memory of them.

(1) is proved here against a scripted engine: a `httpx.MockTransport` that answers
exactly the reads and writes each test describes and *fails loudly* if the helper asks
for one more than the script says — so a retry that should not happen is a failure,
not a silently longer test. (2) is proved against the running FastAPI app, one goal
per status, asking the real routes which statuses they refuse. A route that starts
accepting a status this module calls illegal fails a test here instead of quietly
turning a wait into a hang.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import json
import tempfile
import time
import typing
import unittest
from pathlib import Path
from typing import Any, cast

import httpx
from httpx import ASGITransport

from engine.app import BOOT_TOKEN, app
from engine.db import connect
from engine.models import GoalCreate, GoalStatus, WorkspaceCreate
from engine.services import GoalService, WorkspaceService

from tests.versioned import (
    ALREADY_DONE,
    LEGAL_FROM,
    LEGAL_SOON,
    PremiseBroken,
    RaceLost,
    post_versioned,
    post_versioned_async,
)

#: One read of a goal, as the helper sees it.
_Read = tuple[int, str]
#: One write's answer: the status code and the body the engine sent with it.
_Write = tuple[int, dict[str, Any]]


class _Script:
    """A goal that moves when this test says it does, and says so if asked twice.

    Queued rather than stateful on purpose: the helper is supposed to make a
    *specific* number of round trips, and a stateful double would let a helper that
    retries too eagerly pass by simply having more state to offer.
    """

    def __init__(self, reads: list[_Read], writes: list[_Write]) -> None:
        self.reads = list(reads)
        self.writes = list(writes)
        self.sent: list[int | None] = []

    def _next(self, items: list[Any], what: str) -> Any:
        if not items:
            raise AssertionError(
                f"the helper made a request the script does not describe: {what}"
            )
        return items.pop(0)

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                version, status = self._next(self.reads, f"read #{len(self.sent) + 1}")
                return httpx.Response(200, json={"id": "g1", "version": version, "status": status})
            body = json.loads(request.content)
            quoted = body.get("expected_version")
            self.sent.append(quoted if isinstance(quoted, int) else None)
            code, payload = self._next(
                self.writes, f"POST quoting version {quoted!r} (read #{len(self.sent)})"
            )
            return httpx.Response(code, json=payload)

        return httpx.MockTransport(handle)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport(), base_url="http://engine")

    def unasked(self) -> tuple[int, int]:
        """How many reads and writes the script had left over: a helper that
        under-asked has skipped work it should have done."""
        return len(self.reads), len(self.writes)


class TestVersionedAction(unittest.IsolatedAsyncioTestCase):
    """The loop, against a scripted engine."""

    def test_a_lost_version_race_is_retried_rather_than_reported(self) -> None:
        """The case this whole module exists for: the version moved between the read
        and the write, so the first POST is a 409 the test never asked for."""
        script = _Script(
            reads=[(4, "PENDING"), (5, "PENDING")],
            writes=[
                (409, {"code": "version_conflict", "message": "version mismatch"}),
                (200, {"id": "g1", "status": "RUNNING", "version": 6}),
            ],
        )
        retries: list[str] = []
        result = post_versioned(
            script.client(), "g1", "start", poll=0.0, timeout=5.0, on_retry=retries.append
        )
        self.assertEqual(200, result.response.status_code)
        self.assertEqual(2, result.attempts, "the retry is a second attempt, not a re-send")
        self.assertEqual(1, result.races)
        # The retry quoted the *fresh* version, not the one that lost.
        self.assertEqual([4, 5], script.sent)
        self.assertEqual(1, len(retries), "a lost race is reported, not swallowed")
        self.assertIn("version_conflict", retries[0])
        self.assertEqual((0, 0), script.unasked(), "no round trip beyond the script")

    def test_a_goal_still_planning_is_waited_for_rather_than_called_illegal(self) -> None:
        """/start on a PLANNING goal is refused today and legal in a moment. The
        helper waits, then acts — which is what the e2e harness used to open-code."""
        script = _Script(
            reads=[(0, "PLANNING"), (0, "PLANNING"), (1, "PENDING")],
            writes=[(200, {"id": "g1", "status": "RUNNING", "version": 2})],
        )
        result = post_versioned(script.client(), "g1", "start", poll=0.0, timeout=5.0)
        self.assertEqual(200, result.response.status_code)
        self.assertEqual(1, result.attempts, "waiting is not an attempt")
        self.assertEqual([1], script.sent, "the act quoted the version planning left")
        self.assertEqual((0, 0), script.unasked())

    def test_a_goal_that_went_terminal_is_a_broken_premise_not_a_race(self) -> None:
        """CANCELLED cannot become startable, so no amount of re-reading helps. This
        must not be retried, and must not be reported as a lost race: the test's
        premise is gone, which is a different bug with a different fix."""
        script = _Script(reads=[(3, "CANCELLED")], writes=[])
        with self.assertRaises(PremiseBroken) as ctx:
            post_versioned(script.client(), "g1", "start", poll=0.0, timeout=5.0)
        self.assertIn("CANCELLED", str(ctx.exception))
        for legal in ("PENDING", "PAUSED"):
            self.assertIn(legal, str(ctx.exception), "the message must say what was possible")
        self.assertEqual([], script.sent, "a goal that went terminal is never acted on")
        self.assertEqual((0, 0), script.unasked())

    def test_an_act_already_in_place_is_reported_as_moot(self) -> None:
        """A RUNNING goal is one /start away from a second step run. The effect the
        caller wanted is already there, so this is neither a success to assert on nor
        a premise to rebuild — it is a race lost in the one direction that does not
        matter, and it is named as such."""
        script = _Script(reads=[(9, "RUNNING")], writes=[])
        with self.assertRaises(RaceLost) as ctx:
            post_versioned(script.client(), "g1", "start", poll=0.0, timeout=5.0)
        self.assertIn("already in place", str(ctx.exception))
        self.assertIn("moot", str(ctx.exception))
        self.assertEqual([], script.sent)
        self.assertEqual((0, 0), script.unasked())

    def test_a_route_refusal_is_raised_at_once_and_never_retried(self) -> None:
        """`plan_only` cannot be re-read away. Retrying it would turn a precise 409
        into a 15-second timeout and lose the code that says what went wrong."""
        script = _Script(
            reads=[(0, "PENDING")],
            writes=[(409, {"code": "plan_only", "message": "goal is plan-only"})],
        )
        with self.assertRaises(PremiseBroken) as ctx:
            post_versioned(script.client(), "g1", "start", poll=0.0, timeout=5.0)
        self.assertIn("plan_only", str(ctx.exception))
        refusal = cast(httpx.Response, ctx.exception.response)
        self.assertEqual("plan_only", refusal.json()["code"], "the engine's own body is kept")
        self.assertEqual([0], script.sent, "one refusal, one request")
        self.assertEqual((0, 0), script.unasked())

    def test_a_race_that_never_settles_gives_up_at_the_deadline(self) -> None:
        """A goal whose version moves forever must fail, not spin. The bound is the
        point: an unbounded retry is how a test suite hangs instead of reporting."""
        started = time.monotonic()
        seen = {"version": 0, "posted": 0}

        def handle(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                # Every read reports a newer version than the last, so the helper
                # never wins.
                seen["version"] += 1
                return httpx.Response(
                    200, json={"id": "g1", "version": seen["version"], "status": "PENDING"}
                )
            seen["posted"] += 1
            return httpx.Response(409, json={"code": "version_conflict"})

        client = httpx.Client(transport=httpx.MockTransport(handle), base_url="http://engine")
        with self.assertRaises(RaceLost) as ctx:
            post_versioned(client, "g1", "start", poll=0.002, timeout=0.1)
        elapsed = time.monotonic() - started
        self.assertGreater(seen["posted"], 1, "it must have retried before giving up")
        self.assertLess(elapsed, 5.0, "the deadline, not the test runner, stops it")
        self.assertIn("version_conflict", str(ctx.exception))
        self.assertIn("never landed", str(ctx.exception))

    def test_the_diagnosis_is_attached_to_a_failure_and_skipped_on_success(self) -> None:
        """The point of `diagnose` is the goal's own event log, which is two more
        requests — worth it exactly when something is wrong, and not otherwise."""
        asked = 0

        def diagnose() -> str:
            nonlocal asked
            asked += 1
            return "goal status=FAILED last events: error, goal_status"

        failing = _Script(reads=[(0, "FAILED")], writes=[])
        with self.assertRaises(PremiseBroken) as ctx:
            post_versioned(failing.client(), "g1", "start", poll=0.0, timeout=5.0, diagnose=diagnose)
        self.assertIn("last events: error, goal_status", str(ctx.exception))
        self.assertEqual(1, asked)

        asked = 0
        winning = _Script(reads=[(0, "PENDING")], writes=[(200, {"status": "RUNNING"})])
        post_versioned(winning.client(), "g1", "start", poll=0.0, timeout=5.0, diagnose=diagnose)
        self.assertEqual(0, asked, "a pass must not pay for a diagnosis")

    def test_an_unknown_action_is_refused_before_any_request(self) -> None:
        """A typo in the action name would otherwise 404 on a goal that was fine."""
        script = _Script(reads=[(0, "PENDING")], writes=[])
        with self.assertRaises(KeyError) as ctx:
            post_versioned(script.client(), "g1", "begin", poll=0.0, timeout=5.0)
        self.assertIn("begin", str(ctx.exception))
        self.assertEqual((1, 0), script.unasked(), "not one request was spent")

    async def test_the_async_twin_retries_the_way_the_sync_one_does(self) -> None:
        """The in-process tests use async clients. The twin is the same loop with
        awaits, and the decisions it shares are `_verdict` — this is the check that
        the copy kept them."""
        script = _Script(
            reads=[(7, "PAUSED"), (8, "PAUSED")],
            writes=[
                (409, {"code": "illegal_status", "message": "cannot start from PAUSED"}),
                (200, {"id": "g1", "status": "RUNNING", "version": 9}),
            ],
        )
        client = httpx.AsyncClient(transport=script.transport(), base_url="http://engine")
        try:
            result = await post_versioned_async(client, "g1", "start", poll=0.0, timeout=5.0)
        finally:
            await client.aclose()
        self.assertEqual(200, result.response.status_code)
        self.assertEqual(1, result.races)
        self.assertEqual([7, 8], script.sent)
        self.assertEqual((0, 0), script.unasked())

    async def test_the_async_twin_raises_the_same_failures(self) -> None:
        script = _Script(reads=[(3, "CANCELLED")], writes=[])
        client = httpx.AsyncClient(transport=script.transport(), base_url="http://engine")
        try:
            with self.assertRaises(PremiseBroken):
                await post_versioned_async(client, "g1", "start", poll=0.0, timeout=5.0)
        finally:
            await client.aclose()
        self.assertEqual([], script.sent)


class TestTablesMatchTheEngine(unittest.IsolatedAsyncioTestCase):
    """`tests/versioned.py` claims to know which statuses each action accepts. That
    claim is only worth something if the engine still agrees, so this asks the real
    routes rather than re-reading the same source twice.

    Only the *refusal* direction is probed for a spawn, because acting on a legal
    status for real would start a step run. But a stale version is enough to tell
    the two apart: every route checks the status before the version, and quotes the
    version before spawning or mutating anything, so a status the table calls legal
    answers `version_conflict` — proof it got past the status check — and a status it
    calls illegal answers `illegal_status`. The whole matrix is therefore derivable
    here with no side effects at all.
    """

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name).resolve()
        conn = connect(root / "test.db")
        self.addCleanup(conn.close)
        self.goals = GoalService(conn)
        self.workspaces = WorkspaceService(conn)
        # Only `goals` and the boot token are on the refusal path; anything further
        # and the fixture would be asserting against its own stubs.
        app.state.goals = self.goals
        app.state.workspaces = self.workspaces
        app.state.token = BOOT_TOKEN
        self.client = httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        )
        self.addAsyncCleanup(self.client.aclose)
        self.headers = {"Authorization": f"Bearer {BOOT_TOKEN}"}
        # A workspace root has to exist: the service refuses a path it cannot see,
        # which is the same guard a real registration goes through.
        ws_root = root / "ws"
        ws_root.mkdir()
        self.ws_id = str(
            self.workspaces.create(WorkspaceCreate(name="ws", root_path=str(ws_root))).id
        )

    def _goal_in(self, status: str, *, dry_run: bool = False) -> str:
        """A fresh goal forced into `status`, by the same service the route reads."""
        goal = self.goals.create(
            GoalCreate(workspace_id=self.ws_id, title=f"goal {status}", dry_run=dry_run)
        )
        # A goal is created PLANNING at version 0; the move below is the one bump
        # that puts it where the probe needs it.
        self.goals.update_status(goal.id, goal.version, status)
        self.assertEqual(status, self.goals.get(goal.id).status)
        return goal.id

    async def _code_for(self, action: str, status: str) -> str:
        """The code the real route answers with, for one action against one status.

        The probe quotes a version no goal has, so a status the route considers legal
        is refused at the version check instead of being acted on: the answer proves
        which of the two guards the request reached without letting anything run.
        """
        # /start answers plan_only first, and /apply answers not_dry_run first, so
        # each probe needs a goal that clears the earlier guard — otherwise it would
        # prove the wrong refusal.
        goal_id = self._goal_in(status, dry_run=action == "apply")
        response = await self.client.post(
            f"/goals/{goal_id}/{action}",
            headers=self.headers,
            # Stale but in range: `expected_version` is `ge=0`, so a negative one
            # would be answered by the model and prove nothing about the route.
            json={"expected_version": 10_000},
        )
        self.assertEqual(409, response.status_code, f"/{action} on a {status} goal: {response.text}")
        return str(response.json()["code"])

    async def test_the_legal_table_is_the_engines_own(self) -> None:
        """Every action, every status: legal exactly where the table says so.

        Both directions, because the failure modes are opposite. A status the table
        calls legal that the route refuses is a helper that would report a benign
        lost race as a broken premise. A status the table calls illegal that the
        route accepts is worse and quieter: the helper would refuse a call the
        engine would have made, and a test asserting the refusal would keep passing.
        """
        for action, legal in LEGAL_FROM.items():
            for status in typing.get_args(GoalStatus):
                expected = "version_conflict" if status in legal else "illegal_status"
                with self.subTest(action=action, status=status):
                    self.assertEqual(expected, await self._code_for(action, status))

    async def test_a_waiting_status_really_is_illegal_today(self) -> None:
        """`LEGAL_SOON` claims a status the engine refuses *now* and leaves on its way
        to a legal one — a goal still planning, a dry run still running. The claim is
        only useful while it is true: an entry the engine has started accepting would
        make the helper wait for something already here, and would make the matrix
        above pass for the wrong reason."""
        for action, soon in LEGAL_SOON.items():
            for status in sorted(soon):
                with self.subTest(action=action, status=status):
                    self.assertNotIn(status, LEGAL_FROM[action], "both legal and waiting?")
                    self.assertEqual("illegal_status", await self._code_for(action, status))

    def test_the_three_tables_never_overlap(self) -> None:
        """`_verdict` asks "send", then "already done", then "soon": the order the
        tables are read in. Overlap would make the verdict depend on that order, so a
        status that is both legal and done would start a second run of a goal that is
        already running."""
        for action in LEGAL_FROM:
            legal, soon, done = LEGAL_FROM[action], LEGAL_SOON[action], ALREADY_DONE[action]
            with self.subTest(action=action):
                self.assertEqual(set(), set(legal) & set(soon))
                self.assertEqual(set(), set(legal) & set(done))
                self.assertEqual(set(), set(soon) & set(done))

    def test_every_action_in_the_tables_is_a_real_route(self) -> None:
        """A renamed or removed route must not leave a table entry pointing at a 404
        that no test reaches, since every test using it would fail on the rename."""
        paths = {getattr(route, "path", "") for route in app.routes}
        for action in LEGAL_FROM:
            with self.subTest(action=action):
                self.assertIn(f"/goals/{{goal_id}}/{action}", paths)


if __name__ == "__main__":
    unittest.main()
