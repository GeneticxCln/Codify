"""The orphan guard, proven by killing a *live* engine mid-process-tree.

`test_sandbox.py` proves the process-tree contract with a stand-in engine: a single
Python process that calls `SandboxService().run_command` directly — no uvicorn, no
API, no agent pipeline. `test_git.py` does the same for a commit's hook tree. What
neither can see is everything *between* a stand-in and a real engine: whether a goal's
verifier stage actually reaches `run_command`, whether the executor's scribe stage
reaches `GitService.commit`, whether the pid handed to the guard is the engine's own,
whether the process the window SIGKILLs is the one whose death the guard watches. This
module drives both paths for real, against a real `python3 -m engine` subprocess:

    POST /workspaces → POST /goals → (planning) → POST /goals/{id}/start

with a fake provider registered as the roles' Ollama `base_url` — real HTTP over real
sockets, no engine code patched or imported by the test. Three scenarios, one fixture:

* the **verifier** asks the sandbox to run a workspace script (the model-driven path);
* the workspace's **post-commit hook** execs the same script (the engine's own path:
  a commit runs hooks, git waits for them, and hooks are workspace content);
* a client calls `POST /workspaces/browse` while a goal runs (the engine's own GUI
  spawn: the native folder picker, `python3 -c <GTK source>`, which lives until a
  human answers it). A real dialog can neither be driven headlessly nor be allowed
  to pop on the user's screen, so the engine's `PATH` is prefixed with a shim whose
  `python3` *is* the probe: the route's own `shutil.which` resolves it, the shim
  answers exactly the invocation whose `-c` source carries the engine's picker
  marker (`engine/app.py::PICKER_MARKER`), and every other `python3` call — sandbox
  commands, git — is forwarded to the real interpreter unchanged. The hijack is
  scoped to the one spawn under test.

The script writes a heartbeat to disk and leaves a long-sleeping grandchild on the
process table. The moment that grandchild exists — proving the process is *running*,
not merely planned — the engine is SIGKILLed, exactly what closing the desktop window
does. Three things must then hold within seconds:

1. the grandchild is gone — the orphan this guard exists for;
2. the command or hook is gone, and the heartbeat stops growing — a survivor still
   writing is the half of the bug a user notices first;
3. the guard in front of whatever the engine ran is gone with it (one `pgrep` pattern
   names "the guard, with this argument", which nothing else in the tree matches).

Every assertion is made against the process table and the filesystem, never against
the engine's own reporting: the engine is dead by the time they run, and a dead engine
cannot report anything.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import IO, Any

import httpx

from tests.process_probe import (
    file_text,
    pids_matching,
    sigkill_matching,
    wait_for_text,
    wait_until,
)
from tests.versioned import post_versioned

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The grandchild's marker. `pgrep -f` reads command lines, and this string exists
# nowhere else on the process table, so a match can only be this test's grandchild.
ORPHAN_PROBE = "codify-live-engine-orphan-probe"
# The workspace script both scenarios end up running. Unique to this test, so the
# survivor checks cannot mistake another run's process for this one.
SLEEPER = "e2e_orphan_probe_sleeper.py"
HEARTBEAT = "heartbeat.txt"
# The commit the second scenario lets the engine make. A commit message stays in git's
# argv (`git commit … -m <message>`), so it names the git process — and the guard in
# front of it — without either of them carrying the repository path.
COMMIT_MARKER = "test: codify-live-engine-commit-marker"
# "The guard, with that argument": an ERE `pgrep -f` reads against the whole command
# line, and `guarded_argv` puts the guard's own path first, so each pattern matches the
# guard and nothing else in the tree.
GUARDED_SLEEPER = f"spawn_guard\\.py.*{SLEEPER}"
GUARDED_COMMIT = f"spawn_guard\\.py.*{COMMIT_MARKER}"

# The folder-picker scenario. The shim directory's *name* is the picker process's
# marker (the guard's argv carries the shim path, so one ERE names "the guard, with
# this picker"); the grandchild reuses ORPHAN_PROBE and the heartbeat reuses the
# same probe script as the other scenarios — one vocabulary, three spawns.
PICKER_PROBE = "codify-live-picker-probe"
GUARDED_PICKER = f"spawn_guard\\.py.*{PICKER_PROBE}"

# What the engine ends up running. The heartbeat is the "is anything still writing?"
# half; the grandchild is the "did anything outlive the engine?" half. Its 300s sleep is
# longer than every timeout in this test, so it can only stop by being killed.
SLEEPER_SOURCE = f"""\
import subprocess
import sys
import time
from pathlib import Path

subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)  # {ORPHAN_PROBE}"])

heartbeat = Path(__file__).resolve().with_name("{HEARTBEAT}")
while True:
    with heartbeat.open("a", encoding="utf-8") as handle:
        handle.write("beat\\n")
    time.sleep(0.2)
"""


# The picker scenario's shim: a `python3` the engine resolves instead of the real
# interpreter. The route's GTK source announces itself with the marker hardcoded in
# engine/app.py::PICKER_MARKER — if that marker ever changes, this scenario fails at
# its "the picker never opened" precondition rather than passing silently. Everything
# else is forwarded to the real python3, so the goal running alongside is unaffected.
_SHIM_SOURCE = """\
#!/bin/sh
case "$2" in
  *codify-folder-picker*) exec "{realpy}" "{workspace}/{sleeper}" ;;
  *) exec "{realpy}" "$@" ;;
esac
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def _pump_lines(pipe: IO[bytes]) -> queue.Queue[bytes]:
    """Drain a pipe on a thread, one line per queue entry, EOF as an empty entry.

    A plain `readline()` on the boot pipe blocks until a line arrives — fine for a
    healthy boot, a wedged test for a silent one. Reading on a thread lets the
    handshake loop below give up on a deadline instead of inheriting the engine's.
    """
    lines: queue.Queue[bytes] = queue.Queue()

    def pump() -> None:
        for line in pipe:
            lines.put(line)
        lines.put(b"")

    threading.Thread(target=pump, daemon=True).start()
    return lines


def _spawn_engine(
    home: Path, extra_env: dict[str, str] | None = None
) -> subprocess.Popen[bytes]:
    # A fresh port per attempt: the probe-release-rebind gap is a TOCTOU we cannot
    # remove, so a boot that loses it must be retried with a different port, not the
    # same losing one (the same shape as tests/test_concurrent_streams_ws.py).
    return subprocess.Popen(
        [sys.executable, "-m", "engine"],
        cwd=str(PROJECT_ROOT),
        env={
            **os.environ,
            "CODIFY_HOME": str(home),
            "CODIFY_PORT": str(_free_port()),
            **(extra_env or {}),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _kill_and_collect(engine: subprocess.Popen[bytes]) -> str:
    """SIGKILL a live engine, reap it, close its pipes, return its stderr tail.

    SIGKILL rather than TERM, here as in the tests: it is what a closed window does,
    and it is the death the guard has to notice on its own — nothing inside the engine
    gets to run another line.
    """
    if engine.poll() is None:
        engine.kill()
    engine.wait(timeout=10)
    stderr = engine.stderr.read() if engine.stderr else b""
    for stream in (engine.stdout, engine.stderr):
        if stream:
            stream.close()
    return stderr.decode(errors="replace")[-800:]


def _boot_engine(
    home: Path, timeout: float = 20.0, extra_env: dict[str, str] | None = None
) -> tuple[subprocess.Popen[bytes], str, int]:
    """Boot a real engine and read `CODIFY_ENGINE token=… port=…` off its stdout.

    A boot death — or a handshake that never arrives — is retried once with a fresh
    process and a fresh port; a second failure is reported with the engine's own
    stderr, which is the only thing a dead boot leaves behind.
    """
    for attempt in (1, 2):
        engine = _spawn_engine(home, extra_env)
        stdout = engine.stdout
        assert stdout is not None, "the engine is spawned with a stdout pipe"
        lines = _pump_lines(stdout)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                if engine.poll() is not None:
                    break  # died at boot: no line is coming
                continue
            if not line:  # EOF
                break
            text = line.decode(errors="replace").strip()
            if text.startswith("CODIFY_ENGINE"):
                fields = dict(p.split("=", 1) for p in text.split()[1:])
                return engine, fields["token"], int(fields["port"])
        detail = _kill_and_collect(engine)
        if attempt == 2:
            raise AssertionError(
                f"the engine never printed its boot handshake in {timeout:.0f}s:\n{detail}"
            )
    raise AssertionError("unreachable")


def _diagnose(
    client: httpx.Client, goal_id: str, engine: subprocess.Popen[bytes] | None = None
) -> str:
    """Why the goal never got as far as running its process.

    The ways these tests can fail before any kill are a goal that stopped somewhere in
    the agent pipeline and an engine that stopped answering; both are easier to read
    out of the goal's own event log than out of a bare timeout.
    """
    if engine is not None and engine.poll() is not None:
        return f"the engine itself exited with {engine.returncode} before the process ran"
    try:
        goal = client.get(f"/goals/{goal_id}").json()
        events = client.get(f"/goals/{goal_id}/events").json()
    except httpx.HTTPError as exc:
        return f"the engine stopped answering about the goal: {exc!r}"
    status = goal.get("status")
    steps = [(s.get("status"), s.get("title")) for s in goal.get("steps") or []]
    tail = ", ".join(str(e.get("type")) for e in events[-12:])
    return f"goal status={status} steps={steps} last events: {tail}"


class _FakeProvider(BaseHTTPRequestHandler):
    """A role-aware fake, registered as each role's Ollama `base_url`.

    One field of the reply is the whole payload and it differs per role (answers,
    steps, files, verdicts, …), so the shape is genuinely untyped here. The two
    scenarios subclass this and override only the replies that make them different.
    """

    def _reply_for(self, prompt: str) -> dict[str, Any]:
        prompt_l = prompt.lower()
        if "prompt_injection" in prompt_l:  # the Laya gate's typed-questions contract
            return {"answers": {
                "intent": "code_change", "risk": 0,
                "prompt_injection": 0.0, "needs_clarification": 0.0,
            }}
        if "you are codify planner" in prompt_l:
            return {"steps": [{
                "title": "run the long probe",
                "description": "start a process that must not outlive the engine",
                "suggested_paths": ["probe_target.txt"],
            }]}
        if "you are codify fixer" in prompt_l:
            return {"files": [{
                "path": "probe_target.txt", "action": "create", "content": "probe target\n",
            }]}
        if "you are codify verifier" in prompt_l:
            if "command ran" in prompt_l or "command output" in prompt_l:
                # Only reachable if the command ended before the engine did. The
                # command scenario never gets here: the engine dies while the probe is
                # still running.
                return {"argv": None, "verdict": "skip",
                        "explanation": "the probe command ended"}
            return self._verifier_reply()
        if "you are codify critic" in prompt_l:
            return {"decision": "approve", "reasons": []}
        if "you are codify scribe" in prompt_l:
            return {"summary": "ran the long probe", "commit_message": "test: run the long probe"}
        return {"enough": True, "files": []}  # librarian

    def _verifier_reply(self) -> dict[str, Any]:
        """The first verifier call's answer; subclasses say what it asks for."""
        return {"argv": None, "verdict": "pass", "explanation": "nothing to run"}

    def _post(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        # Ollama's /api/generate carries the whole prompt in one field; the chat route
        # carries messages instead.
        prompt = f"{body.get('system', '')}\n{body.get('prompt', body.get('messages', ''))}"
        payload = self._reply_for(prompt)
        # OllamaProvider reads the model's output from `response`. Asked to stream,
        # the fake answers NDJSON — the same reply, one JSON object per line, done=true
        # on the last — exactly what real Ollama does.
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            out = json.dumps(payload)
            half = max(1, len(out) // 2)
            for i, piece in enumerate((out[:half], out[half:])):
                line: dict[str, Any] = {"response": piece, "done": i == 1}
                if i == 1:
                    line["prompt_eval_count"] = 10
                    line["eval_count"] = 5
                self.wfile.write(json.dumps(line).encode() + b"\n")
            return
        data = json.dumps({"response": json.dumps(payload)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802 — httpx talks to /api/generate here
        self._post()

    def do_GET(self) -> None:
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:  # silence the request log
        pass


class _SandboxCommandFake(_FakeProvider):
    """The model-driven scenario: the verifier asks for the long-running script."""

    def _verifier_reply(self) -> dict[str, Any]:
        return {"argv": ["python3", SLEEPER], "verdict": "skip",
                "explanation": "running the long probe"}


class _CommitHookFake(_FakeProvider):
    """The engine-driven scenario: the verifier passes, so the goal reaches its commit
    — and the scribe's subject is the message git will be run with."""

    def _verifier_reply(self) -> dict[str, Any]:
        return {"argv": None, "verdict": "pass", "explanation": "nothing to run"}

    def _reply_for(self, prompt: str) -> dict[str, Any]:
        if "you are codify scribe" in prompt.lower():
            return {"summary": "wrote the probe target", "commit_message": COMMIT_MARKER}
        return super()._reply_for(prompt)


class TestOrphanGuardThroughALiveEngine(unittest.TestCase):
    """The stand-in's blind spot: the guard must hold through the real pipeline."""

    def test_killing_the_engine_takes_a_running_sandboxed_command_with_it(self) -> None:
        workspace = self._scenario_workspace()
        (workspace / SLEEPER).write_text(SLEEPER_SOURCE, encoding="utf-8")
        heartbeat = workspace / HEARTBEAT
        client, goal_id, engine = self._start_a_goal(workspace, _SandboxCommandFake)

        self.assertTrue(
            wait_until(ORPHAN_PROBE, matches=True, timeout=30),
            "the live engine never ran the sandboxed command: "
            + _diagnose(client, goal_id, engine),
        )
        # The process is up: this is the half-finished state the guard is claimed to
        # clean up. Both survivor patterns must be live *now*, or the "gone afterwards"
        # assertions below would pass for a process that was never there.
        before = wait_for_text(heartbeat)
        self.assertNotEqual("", before, "the running command never wrote a heartbeat")
        self.assertNotEqual(
            [], pids_matching(SLEEPER),
            "the command was not on the process table while its grandchild was",
        )
        # And the guard is in front of it. This one pattern can only match "the guard's
        # own argv, with this script as its argument": the command's command line has no
        # spawn_guard.py before the script name.
        self.assertNotEqual(
            [], pids_matching(GUARDED_SLEEPER),
            "the engine ran the command with no guard in front of it",
        )

        engine.kill()  # SIGKILL: exactly what closing the desktop window does
        engine.wait(timeout=10)

        self.assertTrue(
            wait_until(ORPHAN_PROBE, matches=False, timeout=15),
            "the grandchild outlived the engine that started its command",
        )
        self.assertTrue(
            wait_until(SLEEPER, matches=False, timeout=15),
            "the command, or the guard in front of it, outlived the engine",
        )
        self.assertTrue(
            wait_until(GUARDED_SLEEPER, matches=False, timeout=15),
            "the guard process itself outlived the engine it was watching",
        )
        self.assert_workspace_froze(heartbeat)

    def test_killing_the_engine_takes_a_running_commit_hook_with_it(self) -> None:
        """The engine's *own* processes, not ones a model asked for.

        Nothing asks the sandbox for anything here: the repository's post-commit hook
        execs the script, and the goal runs all the way to its commit to get there.
        That path is the last spawn the engine makes for itself, and the one a user
        would never connect to the closed window — a hook still rewriting the tree.
        """
        workspace = self._scenario_workspace()
        (workspace / SLEEPER).write_text(SLEEPER_SOURCE, encoding="utf-8")
        heartbeat = workspace / HEARTBEAT
        self._prepare_repo_with_hook(workspace)
        client, goal_id, engine = self._start_a_goal(workspace, _CommitHookFake)

        self.assertTrue(
            wait_until(ORPHAN_PROBE, matches=True, timeout=30),
            "the live engine never reached its commit hook: "
            + _diagnose(client, goal_id, engine),
        )
        before = wait_for_text(heartbeat)
        self.assertNotEqual("", before, "the running hook never wrote a heartbeat")
        self.assertNotEqual(
            [], pids_matching(SLEEPER),
            "the hook was not on the process table while its grandchild was",
        )
        # The guard is in front of *git* here, and git is mid-commit: its command line
        # carries the message the scribe wrote, which nothing else in the tree has.
        self.assertNotEqual(
            [], pids_matching(GUARDED_COMMIT),
            "the engine ran git with no guard in front of it",
        )

        engine.kill()
        engine.wait(timeout=10)

        self.assertTrue(
            wait_until(ORPHAN_PROBE, matches=False, timeout=15),
            "the hook's grandchild outlived the engine that ran the commit",
        )
        self.assertTrue(
            wait_until(SLEEPER, matches=False, timeout=15),
            "the commit hook outlived the engine that ran the commit",
        )
        self.assertTrue(
            wait_until(COMMIT_MARKER, matches=False, timeout=15),
            "git, or the guard in front of it, outlived the engine",
        )
        self.assert_workspace_froze(heartbeat)

    def test_killing_the_engine_takes_an_open_folder_picker_with_it(self) -> None:
        """The engine's own GUI spawn: the native folder picker behind /workspaces/browse.

        The picker is `python3 -c <GTK source>` resolved by `shutil.which` — a dialog
        that lives until a human answers it, so the 120 s timeout never fires in the
        case that matters, and nothing but the guard closes it when the engine dies.
        A real dialog can neither be driven headlessly nor be allowed to pop on the
        user's screen, so the engine's `PATH` is prefixed with a shim whose `python3`
        *is* the probe: the route's own `shutil.which` resolves it (the argv keeps its
        guarded shape — `[python, spawn_guard.py, <shim>, "-c", <GTK source>]`), and
        the shim answers only the invocation whose `-c` source carries the engine's
        own picker marker, forwarding every other `python3` call — sandbox commands,
        git — to the real interpreter, so the engine works exactly as it always does.
        The browse call runs on a thread, the way a user clicks it while a goal runs;
        the engine is SIGKILLed while the "dialog" is open.
        """
        workspace = self._scenario_workspace()
        (workspace / SLEEPER).write_text(SLEEPER_SOURCE, encoding="utf-8")
        heartbeat = workspace / HEARTBEAT

        # The shim: a directory whose `python3` is the probe. Its path carries the
        # picker marker (the guard's argv names it, so one ERE matches "the guard,
        # with this picker" and nothing else), and it hands off to the real
        # interpreter for every invocation that is not the picker itself.
        shim_tmp = tempfile.TemporaryDirectory(prefix=f"{PICKER_PROBE}-")
        self.addCleanup(shim_tmp.cleanup)
        shim_dir = Path(shim_tmp.name).resolve()
        real_python = subprocess.run(
            ["sh", "-c", "command -v python3"], capture_output=True, text=True, check=True
        ).stdout.strip()
        assert real_python, "a real python3 must exist for the shim to forward to"
        shim = shim_dir / "python3"
        shim.write_text(
            _SHIM_SOURCE.format(
                realpy=real_python, workspace=str(workspace), sleeper=SLEEPER
            ),
            encoding="utf-8",
        )
        shim.chmod(0o755)

        # The engine inherits the shim-prefixed PATH and nothing else unusual; it
        # boots, plans and runs goals exactly as in the other scenarios.
        home_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(home_tmp.cleanup)  # last: the engine's state lives here while it dies
        engine, token, port = _boot_engine(
            Path(home_tmp.name).resolve(),
            extra_env={"PATH": f"{shim_dir}:{os.environ['PATH']}"},
        )
        # Cleanups run last-registered-first, so the engine is SIGKILLed before the
        # sweeps — the sweeps only ever find what a *broken* guard left behind.
        self.addCleanup(sigkill_matching, PICKER_PROBE)  # the shim process
        self.addCleanup(sigkill_matching, GUARDED_PICKER)  # the guard
        self.addCleanup(sigkill_matching, ORPHAN_PROBE)  # the grandchild
        self.addCleanup(_kill_and_collect, engine)
        client = httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=10,
        )
        self.addCleanup(client.close)

        result: dict[str, Any] = {}

        def browse() -> None:
            try:
                response = client.post("/workspaces/browse", timeout=30)
                result["status"] = response.status_code
                result["body"] = response.json()
            except Exception as exc:  # the expected outcome is the request dying
                result["error"] = repr(exc)

        thread = threading.Thread(target=browse, daemon=True)
        thread.start()

        self.assertTrue(
            wait_until(PICKER_PROBE, matches=True, timeout=30),
            "the engine never opened the folder picker — the route's shutil.which must "
            f"resolve the shimmed python3 (engine alive: {engine.poll() is None}, "
            f"shim at {shim_dir}, browse answered: {result})",
        )
        # The "dialog" is open: the half-finished state the guard is claimed to clean
        # up. Every survivor pattern must be live *now*, or the "gone afterwards"
        # assertions below would pass for processes that were never there.
        self.assertNotEqual(
            [], pids_matching(GUARDED_PICKER),
            "the engine opened the picker with no guard in front of it",
        )
        self.assertNotEqual(
            [], pids_matching(ORPHAN_PROBE),
            "the picker never spawned its grandchild",
        )
        before = wait_for_text(heartbeat)
        self.assertNotEqual("", before, "the open picker never wrote a heartbeat")

        engine.kill()  # SIGKILL: exactly what closing the desktop window does
        engine.wait(timeout=10)

        self.assertTrue(
            wait_until(ORPHAN_PROBE, matches=False, timeout=15),
            "the picker's grandchild outlived the engine that opened the dialog",
        )
        self.assertTrue(
            wait_until(PICKER_PROBE, matches=False, timeout=15),
            "the picker process outlived the engine that opened it",
        )
        self.assertTrue(
            wait_until(GUARDED_PICKER, matches=False, timeout=15),
            "the guard process itself outlived the engine it was watching",
        )
        self.assert_workspace_froze(heartbeat)
        # The request half: the browse caller must not hang forever on a dialog whose
        # engine is gone. The guard's group kill closes the request one way or
        # another — a broken pipe here, an answered (or failed) response there — and
        # any of those is the caller returning; only a hang is the bug.
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), "the browse request never came back")

    def assert_workspace_froze(self, heartbeat: Path) -> None:
        """The filesystem half: a process that is gone cannot append another line.

        Sampled after the survivor checks, so the comparison is between two moments
        when nothing matching the patterns is still running.
        """
        frozen = file_text(heartbeat)
        time.sleep(1.0)
        self.assertEqual(
            frozen, file_text(heartbeat),
            "the workspace kept changing after the engine died",
        )

    # --- harness ---------------------------------------------------------

    def _scenario_workspace(self) -> Path:
        """A throwaway workspace, which is also the repository under test.

        Its own temp root, not under CODIFY_HOME: it is the thing the engine operates
        on, and the two must not be able to be confused for one another.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name).resolve()

    def _prepare_repo_with_hook(self, workspace: Path) -> None:
        """A real repository whose post-commit hook execs the workspace script.

        `git init` runs here rather than through GitService: this is the fixture, and
        the path under test must not be the thing that builds it. `--no-verify` skips
        pre-commit and commit-msg, never post-commit — and git waits for that hook, so
        a hook that never returns is a commit that never returns.
        """
        subprocess.run(
            ["git", "init", "-q"], cwd=str(workspace), capture_output=True, check=True
        )
        hook = workspace / ".git" / "hooks" / "post-commit"
        hook.write_text(f"#!/bin/sh\nexec python3 ./{SLEEPER}\n", encoding="utf-8")
        hook.chmod(0o755)

    def _start_a_goal(
        self, workspace: Path, handler: type[_FakeProvider]
    ) -> tuple[httpx.Client, str, subprocess.Popen[bytes]]:
        """Boot a real engine, register the fake, and start a goal over the API.

        Everything the two scenarios share: the engine (its own CODIFY_HOME, its own
        free port, the boot handshake parsed off stdout), the fake as every role's
        endpoint, the workspace, the goal, and the start call. The caller owns the
        assertions — and the kill — which is the only place the scenarios differ.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)  # last: the engine's state lives here while it dies
        home = Path(tmp.name).resolve()

        ai_port = _free_port()
        fake = ThreadingHTTPServer(("127.0.0.1", ai_port), handler)
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        self.addCleanup(fake.server_close)
        self.addCleanup(fake.shutdown)

        # Cleanups run last-registered-first, so the engine is SIGKILLed before the
        # client goes away and before the sweeps: the guard gets the same death every
        # real run gives it, and the sweeps only find what a *broken* guard left behind.
        self.addCleanup(sigkill_matching, ORPHAN_PROBE)
        self.addCleanup(sigkill_matching, SLEEPER)
        engine, token, port = _boot_engine(home)
        self.addCleanup(_kill_and_collect, engine)

        client = httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=10,
        )
        self.addCleanup(client.close)
        self._configure_roles(client, ai_port)
        workspace_id = self._create_workspace(client, workspace)
        goal_id = self._create_goal(client, workspace_id)
        self._start_goal(client, goal_id, engine)
        return client, goal_id, engine

    def _configure_roles(self, client: httpx.Client, ai_port: int) -> None:
        """Point every role at the fake, the way a user's local Ollama endpoint is
        registered. Nothing in the engine is patched or imported by the test."""
        for role in ("planner", "fixer", "verifier", "critic", "scribe", "librarian", "laya"):
            response = client.put(f"/settings/agents/{role}", json={
                "provider": "ollama",
                "base_url": f"http://127.0.0.1:{ai_port}",
                "model_name": "e2e-model",
            })
            self.assertEqual(200, response.status_code, f"configuring {role}: {response.text}")

    def _create_workspace(self, client: httpx.Client, workspace: Path) -> str:
        response = client.post("/workspaces", json={"name": "ws", "root_path": str(workspace)})
        self.assertEqual(200, response.status_code, response.text)
        return str(response.json()["id"])

    def _create_goal(self, client: httpx.Client, workspace_id: str) -> str:
        response = client.post("/goals", json={
            "workspace_id": workspace_id,
            "title": "run the long probe",
            "description": "start a process that must not outlive the engine",
        })
        self.assertEqual(200, response.status_code, response.text)
        return str(response.json()["id"])

    def _start_goal(
        self, client: httpx.Client, goal_id: str, engine: subprocess.Popen[bytes] | None = None
    ) -> None:
        """Wait for the plan, then start the goal exactly like the chat does.

        Both halves are the same problem. `/start` is version-protected, so the
        version has to be the one the engine holds *when the request lands* — and
        planning is still running when this is called, so the goal is PLANNING (which
        the engine answers 409 to) long before it is startable. `tests/versioned.py`
        owns that reading: it waits the goal out, quotes the version it just read,
        re-reads and re-sends if the version moved under it, and says which of the
        four ways this failed — with the engine's own stderr and the goal's event log
        attached — if it did not work. The hand-rolled loop this replaced reported
        the same two failures as a bare `assertEqual(200, ...)` with a 409 body.
        """
        post_versioned(
            client, goal_id, "start", timeout=30.0,
            diagnose=lambda: _diagnose(client, goal_id, engine),
        )


if __name__ == "__main__":
    unittest.main()
