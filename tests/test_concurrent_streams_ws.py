from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
"""The same isolation proof, over the wire.

`test_concurrent_streams.py` proves the service layer never crosses two
concurrent goals' event streams. But the chat does not call the service layer —
it holds a WebSocket to `/ws/goals/{goal_id}` and renders what arrives. This
test boots a *real engine subprocess* (real uvicorn, real sockets, real HTTP
auth), drives two goals concurrently through the REST API with a real fake-AI
server on the network, and holds one genuine WebSocket connection per goal,
asserting what arrives on each wire:

1. Purity     — every frame on a connection belongs to that connection's goal.
2. Integrity  — sequences arrive dense and ascending from 1 (no gaps that a
                shared counter, a dropped frame, or a replay collision cause).
3. Separation — no byte of one goal's content appears on the other's wire.
4. Replay     — a connection opened *after* completion receives exactly the
                events the live connection saw, in order.
5. Mid-run    — a second connection to the same goal attaches partway through
                (when the goal's diff is already written). The engine replays
                from 0 on every connect and the client dedups by sequence
                (`goalStream.ts`'s reconnect contract), so the mid-run
                connection's frames must exactly equal the live connection's:
                a dense replay flowing into the live tail with no seam.
6. Pause      — a goal paused mid-run and resumed keeps a dense stream: the
                PAUSED spell is on the wire, before the goal's last completed
                step, and the work still finishes.
7. Cancel     — a goal cancelled mid-run ends its stream at the cancel:
                nothing dribbles out afterwards, no other status follows, and
                the cancel tore no hole in the sequence run.

All isolation guarantees live in `tests/stream_isolation.py` exactly once,
shared with the service-layer twin (`test_concurrent_streams.py`) so the two
layers cannot drift. Skipped (not failed) when the `websockets` client is
unavailable, so the suite still runs in bare environments.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    import websockets
except ImportError:  # pragma: no cover - environment-dependent
    websockets = None

from tests.stream_isolation import (
    assert_cancelled_stream_ends_cleanly,
    assert_content_separated,
    assert_dense_from_one,
    assert_midrun_resume_is_seamless,
    assert_pause_spell_is_midstream,
    assert_replay_equals_live,
    assert_stream_pure,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeAIHandler(BaseHTTPRequestHandler):
    """Role-aware fake provider: every reply carries the asking goal's marker.

    The marker is read from the prompt exactly as a real provider would see it,
    so content crossing the wire is detectable by text, not ids.
    """

    def _post(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        # Ollama's /api/generate carries the whole prompt in one field.
        prompt = f"{body.get('system', '')}\n{body.get('prompt', body.get('messages', ''))}"
        marker = (
            "alpha" if "alpha" in prompt
            else "beta" if "beta" in prompt
            else "gamma" if "gamma" in prompt
            else "none"
        )
        prompt_l = prompt.lower()
        if "prompt_injection" in prompt:  # the Laya gate's typed-questions contract
            payload = {"answers": {
                "intent": "code_change", "risk": 0,
                "prompt_injection": 0.0, "needs_clarification": 0.0,
            }}
        elif "you are codify planner" in prompt_l:
            payload = {"steps": [{
                "title": f"{marker} step", "description": f"the {marker} goal",
                "suggested_paths": [f"{marker}.txt"],
            }]}
        elif "you are codify fixer" in prompt_l:
            payload = {"files": [{
                "path": f"{marker}.txt", "action": "create", "content": f"{marker} body\n",
            }]}
        elif "you are codify verifier" in prompt_l:
            payload = {"argv": None, "verdict": "pass", "explanation": f"the {marker} change trivially passes"}
        elif "you are codify critic" in prompt_l:
            payload = {"decision": "approve", "reasons": []}
        elif "you are codify scribe" in prompt_l:
            payload = {"summary": f"the {marker} file was created", "commit_message": f"feat: add {marker} file"}
        else:  # librarian
            payload = {"enough": True, "files": []}
        # OllamaProvider reads the model's output from `response`.
        data = json.dumps({"response": json.dumps(payload)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802 — httpx talks to /chat/completions here
        self._post()

    def do_GET(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # silence the request log
        pass


class TestConcurrentGoalsOverRealWebSockets(unittest.IsolatedAsyncioTestCase):
    @unittest.skipIf(websockets is None, "websockets client not installed")
    async def test_concurrent_goals_isolated_over_real_websockets(self):
        ai_port = _free_port()
        fake = HTTPServer(("127.0.0.1", ai_port), _FakeAIHandler)
        threading.Thread(target=fake.serve_forever, daemon=True).start()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "ws").mkdir()

            def _spawn_engine():
                # A fresh port per attempt: the probe-release-rebind gap is a
                # TOCTOU we cannot remove, so a boot that loses it must be
                # retried with a different port, not the same losing one.
                return subprocess.Popen(
                    [sys.executable, "-m", "engine"],
                    cwd=str(PROJECT_ROOT),
                    env={**os.environ, "CODIFY_HOME": str(home), "CODIFY_PORT": str(_free_port())},
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )

            engine = _spawn_engine()
            try:
                token, bound_port, engine = await self._handshake(engine, spawn=_spawn_engine)
                api = f"http://127.0.0.1:{bound_port}"
                headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

                import httpx
                async with httpx.AsyncClient(base_url=api, headers=headers, timeout=10) as client:
                    # The engine's Ollama base_url is a config field, so the fake
                    # AI server is registered like a user's local endpoint would
                    # be — no code is patched, the engine talks over real HTTP.
                    for role in ("planner", "fixer", "verifier", "critic", "scribe", "librarian", "laya"):
                        r = await client.put(f"/settings/agents/{role}", json={
                            "provider": "ollama",
                            "base_url": f"http://127.0.0.1:{ai_port}",
                            "model_name": "wire-model",
                        })
                        self.assertEqual(r.status_code, 200, f"configuring {role}")

                    r = await client.post("/workspaces", json={"name": "ws", "root_path": str(home / "ws")})
                    self.assertEqual(r.status_code, 200)
                    ws_id = r.json()["id"]

                    r1 = await client.post("/goals", json={"workspace_id": ws_id, "title": "goal-alpha", "description": "alpha"})
                    r2 = await client.post("/goals", json={"workspace_id": ws_id, "title": "goal-beta", "description": "beta"})
                    r3 = await client.post("/goals", json={"workspace_id": ws_id, "title": "goal-gamma", "description": "gamma"})
                    r4 = await client.post("/goals", json={"workspace_id": ws_id, "title": "goal-delta", "description": "delta"})
                    self.assertEqual(r1.status_code, 200)
                    self.assertEqual(r2.status_code, 200)
                    self.assertEqual(r3.status_code, 200)
                    self.assertEqual(r4.status_code, 200)
                    goal_a, goal_b, goal_c, goal_d = (
                        r1.json()["id"], r2.json()["id"], r3.json()["id"], r4.json()["id"],
                    )

                    async def _open(gid: str):
                        """Connect and authenticate like the UI does — the engine
                        takes the token as an auth *message* (browsers cannot set
                        headers), then replays from 0."""
                        wire = await websockets.connect(f"ws://127.0.0.1:{bound_port}/ws/goals/{gid}")
                        await wire.send(json.dumps({"type": "auth", "token": token}))
                        return wire

                    wire_a, wire_b, wire_g, wire_d = await asyncio.gather(
                        _open(goal_a), _open(goal_b), _open(goal_c), _open(goal_d)
                    )
                    async with wire_a, wire_b, wire_g, wire_d:
                        # The mid-run subscriber watches REST until gamma's diff
                        # exists, then opens a SECOND connection to the same
                        # goal — the chat's re-subscribe-after-Apply path.
                        midrun = asyncio.create_task(
                            self._midrun_attach(client, goal_c, token, bound_port)
                        )
                        # Beta is paused the moment its first step starts,
                        # held briefly while the other goals keep running, then
                        # resumed — its stream must survive a mid-run PAUSED
                        # spell.
                        pauser = asyncio.create_task(self._pause_resume(client, goal_b))
                        # Delta is cancelled the moment its first step starts:
                        # its stream must end at the cancel, cleanly, while the
                        # others finish around it.
                        canceller = asyncio.create_task(self._cancel_midrun(client, goal_d))
                        live = asyncio.gather(
                            self._drain(wire_a, "alpha"), self._drain(wire_b, "beta"),
                            self._drain(wire_g, "gamma"), self._drain(wire_d, "delta"),
                        )
                        # Start through the same endpoint the chat uses; the
                        # engine plans and runs all four goals concurrently in
                        # the background while the wires are open. Beta's pause
                        # and delta's cancel are owned by their own tasks, so
                        # they are deliberately left out of the gather here.
                        await asyncio.gather(
                            self._run_to_completion(client, goal_a),
                            self._run_to_completion(client, goal_c),
                        )
                        await asyncio.gather(pauser, canceller)
                        frames_a, frames_b, frames_g, frames_d = await asyncio.wait_for(
                            live, timeout=60
                        )
                        floor, frames_mid = await asyncio.wait_for(midrun, timeout=60)

                    # Replay connection: opened after everything is done.
                    wire_replay = await _open(goal_a)
                    async with wire_replay:
                        replay = await self._drain_until_quiet(wire_replay, "alpha")

                await self._assert_isolated(frames_a, frames_b, goal_a, goal_b, replay)
                self._assert_midrun(frames_g, frames_mid, goal_c, floor)
                self._assert_pause_survivable(frames_b, goal_b)
                assert_cancelled_stream_ends_cleanly(self, frames_d, goal_d, "delta")
                # The cancelled goal's content may be partially written (its
                # fixer may have landed first), but nothing may carry *other*
                # goals' content.
                text_d = "\n".join(json.dumps(ev) for ev in frames_d)
                self.assertNotIn("alpha", text_d)
                self.assertNotIn("beta", text_d)
                self.assertNotIn("gamma", text_d)
            finally:
                # Reap unconditionally: a graceful SIGTERM can stall on an
                # uvicorn that still sees an open connection, and an unreaped
                # Popen warns "subprocess still running" at collection time.
                engine.kill()
                engine.wait()
                for stream in (engine.stdout, engine.stderr):
                    if stream:
                        stream.close()
                fake.shutdown()
                fake.server_close()

    # --- harness ---------------------------------------------------------

    async def _handshake(self, engine: subprocess.Popen, spawn, timeout: float = 15.0):
        """Read the boot line `CODIFY_ENGINE token=… port=…` from stdout.

        A boot death is retried once with a fresh process and a fresh port:
        `_free_port` releases the port before the engine binds it, so an
        unrelated ephemeral socket can occasionally win the race — the one
        TOCTOU this harness cannot remove. The spawner picks a new port per
        attempt; the handshake returns the port the engine actually bound
        (which is also what real clients parse). A genuinely broken engine
        fails both boots with the stderr in the message.
        """
        loop = asyncio.get_running_loop()
        for attempt in (1, 2):
            deadline = loop.time() + timeout
            died = False
            while loop.time() < deadline:
                if engine.poll() is not None:
                    out, err = engine.communicate()
                    detail = err.decode()[-800:]
                    if attempt == 2:
                        raise AssertionError(f"engine died at boot:\n{detail}")
                    died = True  # retry with a fresh process and port
                    break
                line = await loop.run_in_executor(None, engine.stdout.readline)
                text = line.decode(errors="replace").strip()
                if text.startswith("CODIFY_ENGINE"):
                    fields = dict(p.split("=", 1) for p in text.split()[1:])
                    return fields["token"], int(fields["port"]), engine
                await asyncio.sleep(0.05)
            if not died:
                raise AssertionError(f"engine never printed its boot handshake within {timeout:.0f}s")
            engine = spawn()
        raise AssertionError("unreachable")

    async def _run_to_completion(self, client: "httpx.AsyncClient", goal_id: str) -> None:
        """Start through the API the way the chat does: planning runs in the
        background after POST /goals, so poll until the plan exists (PENDING),
        then start with the version the server reports — /start is
        version-protected and a stale version is a 409."""
        deadline = time.monotonic() + 30
        while True:
            g = (await client.get(f"/goals/{goal_id}")).json()
            if g["status"] == "PENDING":
                break
            if time.monotonic() > deadline:
                raise AssertionError(f"goal never got a plan (status={g['status']})")
            await asyncio.sleep(0.05)
        r = await client.post(f"/goals/{goal_id}/start", json={"expected_version": g["version"]})
        self.assertEqual(r.status_code, 200, "goal must start")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            g = (await client.get(f"/goals/{goal_id}")).json()
            if g["status"] in ("COMPLETED", "FAILED"):
                self.assertEqual(g["status"], "COMPLETED", "goal must succeed over the wire")
                return
            await asyncio.sleep(0.05)
        raise AssertionError("goal did not reach a terminal state in 60s")

    async def _midrun_attach(self, client: "httpx.AsyncClient", goal_id: str, token: str, port: int):
        """Open a second WS to a running goal the moment its diff exists.

        Watches the REST goal detail until a step has a diff on disk (the
        mid-run moment), then connects and authenticates exactly like the
        chat's re-subscribe does. Returns (floor, frames): the floor is the
        highest sequence visible via REST at attach time — the resume point a
        reconnecting client would dedup against.
        """
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            detail = (await client.get(f"/goals/{goal_id}")).json()
            steps = detail.get("steps") or []
            if any((s.get("review_notes") or "").strip() for s in steps) or any(
                s.get("status") != "PENDING" for s in steps
            ):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("gamma never reached a mid-run moment (no step progressed)")

        events = (await client.get(f"/goals/{goal_id}/events")).json()
        floor = max((e["sequence"] for e in events), default=0)
        self.assertGreaterEqual(floor, 5, "attached too early to count as mid-run")

        wire = await websockets.connect(f"ws://127.0.0.1:{port}/ws/goals/{goal_id}")
        await wire.send(json.dumps({"type": "auth", "token": token}))
        async with wire:
            frames = await self._drain_until_quiet(wire, "gamma")
        return floor, frames

    async def _cancel_midrun(self, client: "httpx.AsyncClient", goal_id: str) -> None:
        """Own delta's lifecycle: start it, cancel it the moment its first
        step begins, and never resume it.

        Cancel goes through POST /cancel (legal from RUNNING); the step runner
        checks the status between steps and stops, so the stream must end at
        the cancel with nothing dribbling out afterwards.
        """
        deadline = time.monotonic() + 30
        while True:
            g = (await client.get(f"/goals/{goal_id}")).json()
            if g["status"] == "PENDING":
                break
            if time.monotonic() > deadline:
                raise AssertionError(f"goal never got a plan (status={g['status']})")
            await asyncio.sleep(0.05)
        r = await client.post(f"/goals/{goal_id}/start", json={"expected_version": g["version"]})
        self.assertEqual(r.status_code, 200, "goal must start")

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            detail = (await client.get(f"/goals/{goal_id}")).json()
            steps = detail.get("steps") or []
            if any(s.get("status") != "PENDING" for s in steps):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("delta never started a step — nothing to cancel")

        r = await client.post(
            f"/goals/{goal_id}/cancel",
            json={"expected_version": (await client.get(f"/goals/{goal_id}")).json()["version"]},
        )
        self.assertEqual(r.status_code, 200, "cancel must be accepted mid-run")

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            g = (await client.get(f"/goals/{goal_id}")).json()
            if g["status"] == "CANCELLED":
                return
            await asyncio.sleep(0.01)
        raise AssertionError("goal never reached CANCELLED")

    async def _pause_resume(self, client: "httpx.AsyncClient", goal_id: str) -> None:
        """Own the goal's whole lifecycle: start it, pause it at its first
        step, resume it, and let it finish.

        Pause goes through POST /pause (RUNNING→PAUSED); resume is POST /start
        again, which is legal from PAUSED and re-spawns the step runner. The
        paused spell overlaps the other goals' runs, which keep going the
        whole time.
        """
        # Start it (this task owns beta's lifecycle; the main flow deliberately
        # leaves beta out of its completion gather).
        deadline = time.monotonic() + 30
        while True:
            g = (await client.get(f"/goals/{goal_id}")).json()
            if g["status"] == "PENDING":
                break
            if time.monotonic() > deadline:
                raise AssertionError(f"goal never got a plan (status={g['status']})")
            await asyncio.sleep(0.05)
        r = await client.post(f"/goals/{goal_id}/start", json={"expected_version": g["version"]})
        self.assertEqual(r.status_code, 200, "goal must start")

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            detail = (await client.get(f"/goals/{goal_id}")).json()
            steps = detail.get("steps") or []
            if any(s.get("status") != "PENDING" for s in steps):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("beta never started a step — nothing to pause")

        r = await client.post(
            f"/goals/{goal_id}/pause",
            json={"expected_version": (await client.get(f"/goals/{goal_id}")).json()["version"]},
        )
        self.assertEqual(r.status_code, 200, "pause must be accepted mid-run")
        paused_at = time.monotonic()
        while time.monotonic() - paused_at < 20:
            g = (await client.get(f"/goals/{goal_id}")).json()
            if g["status"] == "PAUSED":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("goal never reached PAUSED")

        # Hold the pause briefly so the PAUSED spell is real on the wire.
        await asyncio.sleep(0.3)

        r = await client.post(
            f"/goals/{goal_id}/start",
            json={"expected_version": (await client.get(f"/goals/{goal_id}")).json()["version"]},
        )
        self.assertEqual(r.status_code, 200, "resume from PAUSED must be accepted")

        # And let it finish, like any run.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            g = (await client.get(f"/goals/{goal_id}")).json()
            if g["status"] in ("COMPLETED", "FAILED"):
                self.assertEqual(g["status"], "COMPLETED", "goal must succeed over the wire")
                return
            await asyncio.sleep(0.05)
        raise AssertionError("goal did not reach a terminal state in 60s")

    def _assert_pause_survivable(self, frames_b, goal_b) -> None:
        """A mid-run PAUSED spell leaves the stream complete and coherent."""
        assert_pause_spell_is_midstream(self, frames_b, "beta")
        # And the goal still finished its actual work.
        text = "\n".join(json.dumps(ev) for ev in frames_b)
        self.assertIn("beta body", text)

    def _assert_midrun(self, frames_g, frames_mid, goal_c, floor) -> None:
        """The mid-run connection's frames equal the live connection's.

        The server replays from 0 on every connect and the client dedups by
        sequence (goalStream.ts), so "resuming" is proven by equality with the
        from-the-start stream: same dense sequence run, no seam at the floor.
        """
        assert_midrun_resume_is_seamless(self, frames_g, frames_mid, floor, goal_c, "gamma")
        text = "\n".join(json.dumps(ev) for ev in frames_mid)
        self.assertIn("gamma body", text)

    async def _drain(self, wire, marker: str) -> list[dict]:
        """Read frames until the goal is terminal, then a quiet spell for stragglers."""
        frames: list[dict] = []
        terminal = False
        quiet = 0
        while quiet < 6:
            try:
                raw = await asyncio.wait_for(wire.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                if terminal:
                    quiet += 1
                continue
            event = json.loads(raw)
            frames.append(event)
            # Same terminal set as the UI (goalStream.ts): a cancelled goal's
            # stream ends too, or this drain would spin on it forever.
            if event.get("type") == "goal_status" and event.get("payload", {}).get("status") in (
                "COMPLETED", "FAILED", "CANCELLED",
            ):
                terminal = True
        return frames

    async def _drain_until_quiet(self, wire, marker: str) -> list[dict]:
        frames: list[dict] = []
        quiet = 0
        while quiet < 6:
            try:
                frames.append(json.loads(await asyncio.wait_for(wire.recv(), timeout=0.5)))
                quiet = 0
            except asyncio.TimeoutError:
                quiet += 1
        return frames

    async def _assert_isolated(self, frames_a, frames_b, goal_a, goal_b, replay) -> None:
        for name, frames, goal_id in (
            ("alpha", frames_a, goal_a), ("beta", frames_b, goal_b),
        ):
            self.assertGreaterEqual(len(frames), 10, f"{name}'s wire stream is suspiciously thin")
            # 1–2. The shared guarantees, asserted exactly as the service-layer
            # twin does (see tests/stream_isolation.py).
            assert_stream_pure(self, frames, goal_id, name)
            assert_dense_from_one(self, frames, name)

        # 3. Separation by content over the wire.
        assert_content_separated(
            self, {"alpha": frames_a, "beta": frames_b},
            body_markers={"alpha": "alpha body", "beta": "beta body"},
        )

        # 4. Replay equality: the late connection got exactly the live frames.
        assert_replay_equals_live(
            self, [e["sequence"] for e in replay], [e["sequence"] for e in frames_a], "alpha",
        )


if __name__ == "__main__":
    unittest.main()
