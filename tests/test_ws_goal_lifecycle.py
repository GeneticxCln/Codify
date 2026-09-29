"""A goal's WebSocket handler ends when its client does (audit of 2026-09-29, M9).

`ws_goal` never read its socket, so a client that had gone was noticed only when a `send` failed —
and a goal with no new events never sends. The UI closes the socket itself on every terminal status,
so every completed goal that was ever viewed left a poller behind: a coroutine waking four times a
second to re-read the goal for nobody, until the process was restarted. Measured on the audit
machine, idle engine CPU went 0.2 % → 3.6 % (50 views) → 7.0 % (200) → 13.9 % (500) of a core.
`ws_engine` handled this correctly; `ws_goal` did not.

This runs real uvicorn and a real `websockets` client, because the leak only exists there: Starlette's
`TestClient` cancels the handler when its portal closes, which is exactly the thing that hides it. The
observable is the handler's own behaviour — how often it reads the goal after the client is gone.
"""

from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

import uvicorn
import websockets

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.app import BOOT_TOKEN, app
from engine.db import connect
from engine.models import GoalCreate, WorkspaceCreate
from engine.services import GoalService, WorkspaceService


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class ServerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        (base / "ws").mkdir()
        conn = connect(base / "t.db")
        self.addCleanup(conn.close)
        self.goals = GoalService(conn)
        ws = WorkspaceService(conn).create(WorkspaceCreate(name="w", root_path=str(base / "ws")))
        goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="g"))
        # A goal with no events sends nothing, and a handler that has nothing to send is exactly
        # the state the leak lives in; PENDING gives the stream its first frame.
        self.goal = self.goals.update_status(goal.id, goal.version, "PENDING")
        app.state.goals = self.goals
        app.state.token = BOOT_TOKEN

        self.reads = 0
        real_get = self.goals.get

        def counting_get(goal_id: str) -> Any:
            self.reads += 1
            return real_get(goal_id)

        self.goals.get = counting_get  # type: ignore[method-assign]

        self.port = free_port()
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="error", lifespan="off",
            timeout_graceful_shutdown=1,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=lambda: asyncio.run(self.server.serve()), daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        self.assertTrue(self.server.started, "the test server never came up")
        self.addCleanup(self.stop_server)

    def stop_server(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    async def connect(self) -> Any:
        wire = await websockets.connect(f"ws://127.0.0.1:{self.port}/ws/goals/{self.goal.id}")
        await wire.send(json.dumps({"type": "auth", "token": BOOT_TOKEN}))
        return wire


class TestADepartedClientEndsItsHandler(ServerCase):
    async def test_after_the_client_closes_nothing_keeps_reading_the_goal(self) -> None:
        wire = await self.connect()
        await asyncio.wait_for(wire.recv(), 5)  # the goal's first event: the handler is up and polling
        await wire.close()

        # The handler has to notice the close and leave. Allowed a moment to do so, then it
        # must be silent: a poller for a client that is gone reads the goal four times a second.
        await asyncio.sleep(0.8)
        settled = self.reads
        await asyncio.sleep(1.2)

        self.assertEqual(
            settled, self.reads,
            f"the handler kept polling for a client that had gone ({self.reads - settled} reads in 1.2 s)",
        )

    async def test_many_views_of_finished_goals_leave_nothing_running(self) -> None:
        # The measured leak: one poller per view, accumulating. Ten is enough to see four times
        # a second become forty.
        for _ in range(10):
            wire = await self.connect()
            await asyncio.wait_for(wire.recv(), 5)
            await wire.close()
        await asyncio.sleep(0.8)
        settled = self.reads
        await asyncio.sleep(1.0)

        self.assertEqual(settled, self.reads, f"{self.reads - settled} reads in 1 s from views that were closed")


class TestALiveClientIsUnaffected(ServerCase):
    async def test_a_new_event_still_reaches_a_connected_client(self) -> None:
        wire = await self.connect()
        first = json.loads(await asyncio.wait_for(wire.recv(), 5))

        g = self.goals.get(self.goal.id)
        self.goals.update_status(g.id, g.version, "RUNNING")

        seen = []
        for _ in range(5):
            frame = json.loads(await asyncio.wait_for(wire.recv(), 5))
            seen.append(frame)
            if frame["type"] == "goal_status" and frame["payload"]["status"] == "RUNNING":
                break
        self.assertGreater(seen[-1]["sequence"], first["sequence"])
        self.assertEqual("RUNNING", seen[-1]["payload"]["status"])
        await wire.close()

    async def test_a_frame_from_the_client_is_ignored_and_the_stream_goes_on(self) -> None:
        wire = await self.connect()
        await asyncio.wait_for(wire.recv(), 5)

        await wire.send("hello from a client that has no business sending")
        g = self.goals.get(self.goal.id)
        self.goals.update_status(g.id, g.version, "RUNNING")

        statuses = []
        for _ in range(6):
            frame = json.loads(await asyncio.wait_for(wire.recv(), 5))
            if frame["type"] == "goal_status":
                statuses.append(frame["payload"]["status"])
            if "RUNNING" in statuses:
                break
        self.assertIn("RUNNING", statuses)
        await wire.close()

    async def test_a_goal_that_is_deleted_closes_the_socket_with_4404(self) -> None:
        wire = await self.connect()
        await asyncio.wait_for(wire.recv(), 5)

        self.goals._db.execute("DELETE FROM goals WHERE id = ?", (self.goal.id,))
        self.goals._db.commit()

        with self.assertRaises(websockets.ConnectionClosed) as caught:
            for _ in range(20):
                await asyncio.wait_for(wire.recv(), 5)
        self.assertEqual(4404, caught.exception.rcvd.code if caught.exception.rcvd else None)


if __name__ == "__main__":
    unittest.main()
