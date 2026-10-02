"""Drive a real turn against a real local model, through the real engine.

The unit tests use test doubles for the model, which is the only way to make
them deterministic and the only way they can assert things like "the planner was
never called". What they cannot tell you is whether the whole thing works when
the thing on the other end is a real LLM on a real socket.

This does that, and nothing else:

    python3 -m scripts.drive_a_turn "hi"

It starts the engine in-process against a throwaway CODIFY_HOME, points the
turn's roles at a local Ollama, posts a turn through the ordinary HTTP route,
and prints the events that came back. No test doubles anywhere: the gate, the
conductor's tool loop and the provider are all real.

It is a development tool, not part of `make check` — it needs a model, and a
gate that depends on somebody's machine is the thing docs/00 is against.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


def turn_tally(events: list[dict[str, Any]], status: str) -> dict[str, Any]:
    """What one turn's events say a real model did, counted rather than read.

    A transcript tells a person whether a turn went well; a baseline is a rate, and rates come from
    counting: which tools the conductor called, how many calls failed and why, how many were asked for
    again, and how the turn ended. `status` is the goal's status once the run stopped — an answer is a
    turn that completed *and said something*, a plan is a turn that handed over for approval.
    """
    tools: dict[str, int] = {}
    failed: dict[str, int] = {}
    reasks = fallbacks = 0
    errors: list[str] = []
    answered = False
    for e in events:
        payload = e.get("payload") or {}
        kind = e.get("type")
        if kind == "log":
            message = str(payload.get("message", ""))
            if payload.get("turn"):
                answered = True
            elif message.startswith("conductor called "):
                name = message[len("conductor called "):].split("(", 1)[0]
                tools[name] = tools.get(name, 0) + 1
        elif kind == "agent_call_failed":
            code = str(payload.get("code"))
            failed[code] = failed.get(code, 0) + 1
            if payload.get("retrying"):
                reasks += 1
        elif kind == "provider_fallback":
            fallbacks += 1
        elif kind == "error":
            errors.append(str(payload.get("code")))
    if status == "PENDING":
        outcome = "planned"
    elif status == "COMPLETED":
        outcome = "answered" if answered else "silent"
    else:
        outcome = status.lower()
    return {
        "outcome": outcome, "tools": tools, "failed_calls": failed,
        "reasks": reasks, "fallbacks": fallbacks, "errors": errors,
    }


def run_tally(events: list[dict[str, Any]], status: str) -> dict[str, Any]:
    """How a run that was *started* ended, counted from its events (what `--approve` reports).

    A turn ends in an answer or a plan; a run ends finished, paused with a reason, or failed. The pause code is
    the part worth counting, because a model that cannot finish a step is the rate a baseline is after and the
    code says which way it fell short (`docs/09` §10.14). Built on `turn_tally` for the calls and errors; only
    the outcome and the pause are the run's own. `events` should be the ones published after Start, or the
    turn's own `recon` and `plan` are counted as the run's.
    """
    tally = turn_tally(events, status)
    pause: dict[str, Any] | None = None
    for e in events:
        payload = e.get("payload") or {}
        if e.get("type") != "goal_status":
            continue
        if payload.get("status") == "PAUSED" and payload.get("reason_code"):
            pause = {"code": str(payload["reason_code"]), "reason": str(payload.get("reason") or "")}
        elif payload.get("status") == "RUNNING":
            pause = None
    if status == "COMPLETED":
        outcome = "completed"
    elif status == "PAUSED":
        outcome = f"paused:{pause['code']}" if pause else "paused"
    else:
        outcome = status.lower()
    tally["outcome"] = outcome
    tally["pause"] = pause
    return tally


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "prompt", nargs="+",
        help="one or more turns; each is posted to the same thread",
    )
    parser.add_argument("--model", default=os.environ.get("CODIFY_TEST_MODEL", "qwen2.5-coder:7b"))
    parser.add_argument("--provider", default="ollama")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--approve",
        action="store_true",
        help=(
            "press Start on the plan a turn makes and report how the run ended: finished, paused with the "
            "engine's reason, or failed. Without it a turn that plans stops there, as it always did. The "
            "exit status is non-zero unless the run completed"
        ),
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=(
            "override the provider endpoint. Without this a provider runs against "
            "its real API; with it you can drive a dialect at any compatible "
            "endpoint (e.g. --provider openai pointed at Ollama's /v1)"
        ),
    )
    parser.add_argument(
        "--api-key-env",
        default=None,
        metavar="VAR",
        help=(
            "name of an environment variable holding the API key. Named rather "
            "than valued on purpose: a key in argv is a key in the process list "
            "and in the shell history, and this script prints everything it is "
            "given"
        ),
    )
    args = parser.parse_args()

    # A throwaway home before anything reads one. The engine must never see the
    # developer's real ~/.codify from a script like this.
    scratch = tempfile.mkdtemp(prefix="codify-drive-")
    os.environ["CODIFY_HOME"] = scratch
    os.environ["CODIFY_DB"] = str(Path(scratch) / "drive.db")
    os.environ["CODIFY_SECRETS"] = str(Path(scratch) / "secrets.json")

    from httpx import ASGITransport
    import httpx

    from engine.app import BOOT_TOKEN, app
    from engine.db import connect
    from engine.executor import ExecutorService
    from engine.laya import LayaService
    from engine.models import (
        ROLES,
        AgentConfigUpdate,
        ConversationCreate,
        WorkspaceCreate,
    )
    from engine.providers import Keychain, ProviderFactory
    from engine.sandbox import SandboxService
    from engine.services import (
        AgentRegistryService,
        ConversationService,
        GoalService,
        SettingsService,
        WorkspaceService,
    )

    conn = connect(Path(scratch) / "drive.db")
    keychain = Keychain(secrets_path=Path(scratch) / "secrets.json")
    factory = ProviderFactory(keychain)
    registry = AgentRegistryService(conn, factory, keychain)
    workspaces = WorkspaceService(conn)
    conversations = ConversationService(conn)
    goals = GoalService(conn)
    settings = SettingsService(conn)
    laya = LayaService(registry=registry)
    executor = ExecutorService(
        goals, workspaces, registry, SandboxService(), laya=laya
    )
    executor.settings = settings

    # Wired onto the app, so the request goes through the real route rather than
    # reaching past it: `_spawn` is what runs `run_chat` in the background, and
    # a script that bypassed it would be testing something the client never uses.
    app.state.conn = conn
    app.state.registry = registry
    app.state.workspaces = workspaces
    app.state.conversations = conversations
    app.state.goals = goals
    app.state.sandbox = SandboxService()
    app.state.settings = settings
    app.state.executor = executor
    app.state.token = BOOT_TOKEN

    # Every role at the same target. The point is that the *whole* path runs —
    # a real gate, a real conductor, a real provider. Whatever dialect
    # `--provider` selects is the one exercised end to end, which is the only
    # way a wire-format mistake shows up: a converter that is wrong compiles,
    # passes its own tests, and then returns someone else's 400.
    api_key: str | None = None
    if args.api_key_env:
        api_key = os.environ.get(args.api_key_env) or None
        if api_key is None:
            print(f"{args.api_key_env} is not set (or is empty)")
            return 2
    patch = AgentConfigUpdate(provider=args.provider, model_name=args.model)
    if args.base_url:
        patch.base_url = args.base_url
    if api_key is not None:
        patch.api_key = api_key
    for role in ROLES:
        registry.set_config(role, patch)

    repo = Path(scratch) / "workspace"
    repo.mkdir()
    (repo / "greeter.py").write_text(
        "def greet(name):\n"
        '    """Return a greeting for `name`."""\n'
        '    return f"Hello, {name}!"\n'
    )
    ws = workspaces.create(WorkspaceCreate(name="demo", root_path=str(repo)))
    thread = conversations.create(ConversationCreate(workspace_id=ws.id, title="drive"))

    # The real route, over ASGI, with the boot token — invariant 3. Everything
    # from here is the client's path: POST the turn, poll the ordinary goal
    # events, read the answer off the log. Several prompts become several turns
    # in one thread, which is also the only way to see memory and a mid-thread
    # escalation.
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {BOOT_TOKEN}"}
    print(f"workspace: {repo}")
    print(f"thread:    {thread.id}")
    print(f"model:     {args.provider}/{args.model}")
    print("-" * 68)

    failed = False
    tallies: list[dict[str, Any]] = []
    run_tallies: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", headers=headers,
        timeout=60.0,
    ) as client:
        for index, prompt in enumerate(args.prompt, start=1):
            started = time.monotonic()
            posted = await client.post(
                f"/conversations/{thread.id}/turns", json={"prompt": prompt}
            )
            if posted.status_code != 200:
                print(f"POST failed: {posted.status_code} {posted.text}")
                return 2
            goal_id: str = posted.json()["id"]
            print(f"\n--- turn {index}: {prompt[:96]!r}")
            print(f"    goal: {goal_id}")

            events: list[dict[str, Any]] = []
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                resp = await client.get(f"/goals/{goal_id}/events")
                events = resp.json()
                terminal = [
                    e for e in events
                    if e["type"] == "goal_status"
                    and (e["payload"] or {}).get("status") in (
                        "COMPLETED", "FAILED", "CANCELLED", "PENDING"
                    )
                ]
                if terminal:
                    break
                await asyncio.sleep(0.25)

            status = goals.get(goal_id).status
            steps = goals.steps(goal_id)
            print(f"    status: {status}   ({time.monotonic() - started:.1f}s)   "
                  f"steps: {len(steps)}")

            reply = ""
            for event in events:
                payload = event["payload"] or {}
                kind = event["type"]
                if kind == "log":
                    text = str(payload.get("message", ""))
                    if payload.get("turn"):
                        reply = text
                        print(f"    [ANSWER] {text}")
                    elif not payload.get("level") == "info" or "conductor" in text:
                        print(f"    [{payload.get('level', 'log')}] {text[:200]}")
                elif kind == "agent_assigned":
                    suffix = " (conductor)" if payload.get("conductor") else ""
                    print(
                        f"    [agent] {payload.get('role')} -> "
                        f"{payload.get('provider')}/{payload.get('model')}{suffix}"
                    )
                elif kind == "laya_decision":
                    answers = payload.get("answers") or {}
                    print(
                        f"    [gate]  engine={payload.get('engine')} "
                        f"intent={answers.get('intent')} "
                        f"injection={(answers.get('prompt_injection') or {}).get('noul')}"
                    )
                elif kind == "error":
                    print(f"    [ERROR] {payload.get('code')}: {payload.get('message')}")

            tally = turn_tally(events, goals.get(goal_id).status)
            tallies.append(tally)
            print(f"    [tally] {tally}")

            # PENDING means the pipeline took over and is waiting for a person
            # to approve the plan — the expected end of a code change, and not
            # something that has "answered" yet.
            final_status = goals.get(goal_id).status
            if final_status != "PENDING" and not reply:
                print("    (no answer)")
                failed = True

            if args.approve:
                if final_status != "PENDING" or not steps:
                    print("    (nothing to start: this turn made no plan)")
                    continue
                # The real route, with the version the plan was made at: the same press a person makes.
                before = max((e["sequence"] for e in events), default=0)
                started_at = time.monotonic()
                pressed = await client.post(
                    f"/goals/{goal_id}/start", json={"expected_version": goals.get(goal_id).version}
                )
                if pressed.status_code != 200:
                    print(f"    Start refused: {pressed.status_code} {pressed.text}")
                    failed = True
                    continue
                print(f"\n--- start: goal {goal_id}")
                run_status = "RUNNING"
                deadline = time.monotonic() + args.timeout
                while time.monotonic() < deadline:
                    run_status = goals.get(goal_id).status
                    if run_status not in ("RUNNING", "PENDING"):
                        break
                    await asyncio.sleep(0.25)
                after = (await client.get(f"/goals/{goal_id}/events")).json()
                run_events = [e for e in after if e["sequence"] > before]
                listed = ", ".join(f"{step.title}: {step.status}" for step in goals.steps(goal_id))
                print(f"    status: {run_status}   ({time.monotonic() - started_at:.1f}s)   steps: {listed}")
                for event in run_events:
                    payload = event["payload"] or {}
                    if event["type"] == "log" and str(payload.get("message", "")).startswith("conductor called "):
                        print(f"    [call] {str(payload['message'])[len('conductor called '):][:120]}")
                    elif event["type"] == "error":
                        print(f"    [ERROR] {payload.get('code')}: {payload.get('message')}")
                outcome = run_tally(run_events, run_status)
                if outcome["pause"]:
                    print(f"    [PAUSED] {outcome['pause']['code']}: {outcome['pause']['reason']}")
                print(f"    [run] {outcome}")
                run_tallies.append(outcome)
                if run_status != "COMPLETED":
                    failed = True

    conn.close()
    outcomes: dict[str, int] = {}
    for tally in tallies:
        outcomes[tally["outcome"]] = outcomes.get(tally["outcome"], 0) + 1
    print("-" * 68)
    print(f"turns: {len(tallies)}   outcomes: {outcomes}   "
          f"failed calls: {sum(sum(t['failed_calls'].values()) for t in tallies)}   "
          f"re-asks: {sum(t['reasks'] for t in tallies)}   "
          f"fallbacks: {sum(t['fallbacks'] for t in tallies)}")
    if run_tallies:
        ended: dict[str, int] = {}
        for tally in run_tallies:
            ended[tally["outcome"]] = ended.get(tally["outcome"], 0) + 1
        print(f"runs: {len(run_tallies)}   outcomes: {ended}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
