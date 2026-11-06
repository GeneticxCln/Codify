"""`codify-cli`: a second surface onto the running engine, over the same HTTP API the window uses.

A client and nothing more. It starts no engine, reads no database, imports none of the engine's services and has no
route of its own: every command is one or two requests to an engine that is already running, carrying the same boot
token every request carries (docs/00 §6.3). What the window can do through those routes it can do, and what the routes
refuse it is refused, because the refusal is the engine's, not this file's.

* **Finding the engine.** `--port` / `CODIFY_ENGINE_PORT`, else the first port in the engine's range (7430-7440) that
  answers like a Codify engine. A port is asked *without* the token first and only trusted if it answers the engine's
  own `401 unauthorized`: the token is not sent to whatever happens to be listening in that range.
* **The token.** `--token`, else `CODIFY_BOOT_TOKEN`, else the engine's own `boot_token` file in its state directory.
  That file is read and never created: a client that minted one would be inventing a credential for an engine that has
  not run. (Importing `engine.app` would create it, which is why this module imports only `engine.home`.)
* **It does not approve a plan.** `ask` makes a turn the way the composer does (`POST /conversations/{id}/turns`, with
  the words and nothing else). A plan that comes back waits for a person to press Start in the window; nothing here
  presses it.

    codify-cli status
    codify-cli ask <workspace> "why does the build fail?" --wait
    codify-cli suggestions
    codify-cli profile export --workspace <id> -o me.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

import httpx

from engine import home

ENGINE_PORTS = range(7430, 7441)
ENV_PORT = "CODIFY_ENGINE_PORT"
ENV_TOKEN = home.ENV_BOOT_TOKEN
#: Goal statuses after which there is nothing more to wait for. PENDING is a plan waiting on a person; PAUSED is a run
#: waiting on one; the rest are over.
SETTLED = frozenset({"PENDING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED"})
WAIT_POLL_S = 1.0
DEFAULT_WAIT_S = 600.0

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3


class CliError(Exception):
    def __init__(self, message: str, code: int = EXIT_REFUSED) -> None:
        super().__init__(message)
        self.code = code


# ── finding the engine ────────────────────────────────────────────────────────────────────────────────────────────


def read_token(env: Mapping[str, str], flag: str | None, state_file: Path | None = None) -> str:
    """The boot token from the flag, the environment or the engine's own file. Never creates one."""
    if flag:
        return flag.strip()
    if env.get(ENV_TOKEN, "").strip():
        return env[ENV_TOKEN].strip()
    path = state_file if state_file is not None else home.boot_token_path()
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if not text:
        raise CliError(
            f"no boot token: pass --token, set {ENV_TOKEN}, or start the engine once so {path} exists",
            EXIT_UNREACHABLE,
        )
    return text


async def looks_like_the_engine(client: httpx.AsyncClient, base: str) -> bool:
    """Whether something on this address answers as a Codify engine, asked without the token."""
    try:
        res = await client.get(f"{base}/health", timeout=0.75)
        body = res.json()
    except (httpx.HTTPError, ValueError):
        return False
    return res.status_code == 401 and isinstance(body, dict) and body.get("code") == "unauthorized"


async def find_base(
    client: httpx.AsyncClient, env: Mapping[str, str], port_flag: int | None, ports: Sequence[int] = ENGINE_PORTS,
) -> str:
    chosen = port_flag if port_flag is not None else (int(env[ENV_PORT]) if env.get(ENV_PORT, "").isdigit() else None)
    if chosen is not None:
        return f"http://127.0.0.1:{chosen}"
    for port in ports:
        base = f"http://127.0.0.1:{port}"
        if await looks_like_the_engine(client, base):
            return base
    raise CliError(
        f"no Codify engine answered on 127.0.0.1:{ports[0]}-{ports[-1]}. Start the app (or `make run-engine`), "
        f"or pass --port / set {ENV_PORT}.",
        EXIT_UNREACHABLE,
    )


class Engine:
    """One authenticated connection. Every method is a request; an error body is turned into the engine's own words."""

    def __init__(self, client: httpx.AsyncClient, base: str, token: str) -> None:
        self.client, self.base, self.token = client, base, token

    async def call(self, method: str, path: str, *, json_body: Any = None, params: dict[str, Any] | None = None) -> Any:
        try:
            res = await self.client.request(
                method, f"{self.base}{path}", json=json_body, params=params,
                headers={"Authorization": f"Bearer {self.token}"}, timeout=30.0,
            )
        except httpx.HTTPError as exc:
            raise CliError(f"could not reach the engine at {self.base}: {exc}", EXIT_UNREACHABLE) from exc
        if res.status_code == 401:
            raise CliError("the engine refused the token. It changes if the engine's state directory does.", EXIT_REFUSED)
        if res.status_code >= 400:
            try:
                body = res.json()
                message = body.get("message") if isinstance(body, dict) else None
                if not message and isinstance(body, dict) and isinstance(body.get("detail"), list):
                    message = "; ".join(str(d.get("msg", d)) for d in body["detail"])[:300]
            except ValueError:
                message = None
            raise CliError(f"{res.status_code}: {message or res.text.strip()[:300] or 'refused'}")
        if res.status_code == 204 or not res.content:
            return None
        return res.json()


# ── commands ──────────────────────────────────────────────────────────────────────────────────────────────────────


def _emit(out: TextIO, value: Any, as_json: bool, text: str) -> None:
    out.write((json.dumps(value, indent=2) if as_json else text).rstrip("\n") + "\n")


async def cmd_status(engine: Engine, args: argparse.Namespace, out: TextIO) -> None:
    health = await engine.call("GET", "/health")
    workspaces = await engine.call("GET", "/workspaces")
    _emit(out, {"health": health, "workspaces": len(workspaces)}, args.json,
          f"engine at {engine.base}: {health.get('status', 'ok') if isinstance(health, dict) else 'ok'}; "
          f"{len(workspaces)} workspace(s)")


async def cmd_workspaces(engine: Engine, args: argparse.Namespace, out: TextIO) -> None:
    rows = await engine.call("GET", "/workspaces")
    _emit(out, rows, args.json, "\n".join(f"{w['id']}  {w['name']}  {w['root_path']}" for w in rows) or "no workspaces")


async def cmd_goals(engine: Engine, args: argparse.Namespace, out: TextIO) -> None:
    rows = await engine.call("GET", "/goals")
    if args.workspace:
        rows = [g for g in rows if g.get("workspace_id") == args.workspace]
    _emit(out, rows, args.json, "\n".join(f"{g['id']}  {g['status']:9}  {g['title']}" for g in rows) or "no goals")


async def cmd_ask(engine: Engine, args: argparse.Namespace, out: TextIO) -> None:
    """A turn, made the way the composer makes one: a thread, then the words and nothing else."""
    convo = await engine.call("POST", "/conversations", json_body={"workspace_id": args.workspace, "title": args.prompt[:60]})
    goal = await engine.call("POST", f"/conversations/{convo['id']}/turns", json_body={"prompt": args.prompt})
    if not args.wait:
        _emit(out, goal, args.json, f"turn {goal['id']} started in thread {convo['id']}")
        return
    deadline = time.monotonic() + args.timeout
    status = goal["status"]
    while status not in SETTLED:
        if time.monotonic() > deadline:
            raise CliError(f"still {status} after {args.timeout:.0f}s; turn {goal['id']} keeps running in the engine")
        await asyncio.sleep(WAIT_POLL_S)
        status = (await engine.call("GET", f"/goals/{goal['id']}"))["status"]
    events = await engine.call("GET", f"/goals/{goal['id']}/events")
    replies = [e for e in events if e.get("type") == "log" and (e.get("payload") or {}).get("turn") is True]
    reply = str(replies[-1]["payload"].get("message", "")) if replies else ""
    note = ""
    if status == "PENDING":
        note = "\n\n(A plan is waiting for your approval. Open the app to read it and press Start; this client does not.)"
    elif status == "PAUSED":
        note = "\n\n(The run is paused and waits for you in the app.)"
    _emit(out, {"goal_id": goal["id"], "status": status, "reply": reply}, args.json, f"[{status}] {reply}{note}")


async def cmd_suggestions(engine: Engine, args: argparse.Namespace, out: TextIO) -> None:
    action = args.action
    if action == "list":
        listing = await engine.call("GET", "/suggestions", params={"workspace_id": args.workspace} if args.workspace else None)
        lines = [f"{s['id']}  [{s['kind']}]  {s['title']}" for s in listing["suggestions"]]
        if listing["snoozed"]:
            lines.append(f"({listing['snoozed']} snoozed)")
        _emit(out, listing, args.json, "\n".join(lines) or "nothing to suggest")
    elif action == "scan":
        made = await engine.call("POST", "/suggestions/scan")
        _emit(out, made, args.json, f"{made['created']} new suggestion(s)")
    elif action == "dismiss":
        await engine.call("POST", f"/suggestions/{args.id}/dismiss")
        _emit(out, {"ok": True}, args.json, "dismissed")
    elif action == "snooze":
        await engine.call("POST", f"/suggestions/{args.id}/snooze", json_body={"hours": args.hours})
        _emit(out, {"ok": True}, args.json, f"snoozed for {args.hours} h")


async def cmd_notes(engine: Engine, args: argparse.Namespace, out: TextIO) -> None:
    if args.action == "list":
        got = await engine.call("GET", "/settings/notes")
        state = "on" if got["enabled"] else "off (the assistant is not shown them; turn them on in Settings → Agent Roles → Standing notes)"
        lines = [f"{n['id']}  {n['text']}" + ("  [imported]" if n["source"] == "imported" else "") for n in got["notes"]]
        _emit(out, got, args.json, "\n".join([f"notes are {state}", *lines]))
    elif args.action == "add":
        made = await engine.call("POST", "/settings/notes", json_body={"text": args.text})
        _emit(out, made, args.json, f"kept {made['id']}")
    elif args.action == "delete":
        await engine.call("DELETE", f"/settings/notes/{args.id}")
        _emit(out, {"ok": True}, args.json, "deleted")


async def cmd_profile(engine: Engine, args: argparse.Namespace, out: TextIO) -> None:
    if args.action == "export":
        params: dict[str, Any] = {"memory": str(args.memory).lower(), "notes": str(args.notes).lower()}
        if args.workspace:
            params["workspace_id"] = args.workspace
        document = await engine.call("GET", "/profile/export", params=params)
        text = json.dumps(document, indent=2) + "\n"
        if args.output:
            # Owner-only, like the state it came from: a profile holds prompts and, if asked for, what a workspace learned.
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            out.write(f"wrote {args.output}\n")
        else:
            out.write(text)
    else:
        try:
            profile = json.loads(Path(args.file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CliError(f"could not read {args.file}: {exc}", EXIT_USAGE) from exc
        body: dict[str, Any] = {
            "profile": profile,
            "apply": {
                "settings": not args.no_settings, "roles": not args.no_roles, "skills": args.skills,
                "memory": args.memory, "notes": args.notes,
            },
        }
        if args.workspace:
            body["workspace_id"] = args.workspace
        report = await engine.call("POST", "/profile/import", json_body=body)
        applied = report["applied"]
        lines = [
            f"settings: {', '.join(applied['settings']) or 'none'}",
            f"roles: {', '.join(applied['roles']) or 'none'}",
            f"skills: {', '.join(applied['skills']) or 'none'}",
            f"memory: {applied['memory']}   notes: {applied['notes']}",
            *(f"left alone: {s['what']} ({s['reason']})" for s in report["skipped"]),
        ]
        _emit(out, report, args.json, "\n".join(lines))


COMMANDS = {
    "status": cmd_status, "workspaces": cmd_workspaces, "goals": cmd_goals, "ask": cmd_ask,
    "suggestions": cmd_suggestions, "notes": cmd_notes, "profile": cmd_profile,
}


# ── the parser and the entry point ────────────────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codify-cli", description="A client for a running Codify engine.")
    parser.add_argument("--port", type=int, default=None, help=f"the engine's port (default: found in {ENGINE_PORTS[0]}-{ENGINE_PORTS[-1]})")
    parser.add_argument("--token", default=None, help=f"the boot token (default: ${ENV_TOKEN}, then the engine's boot_token file)")
    parser.add_argument("--json", action="store_true", help="print the engine's answer as JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="is the engine up, and how many workspaces it holds")
    sub.add_parser("workspaces", help="list workspaces")
    goals = sub.add_parser("goals", help="list goals and turns")
    goals.add_argument("--workspace", default=None)

    ask = sub.add_parser("ask", help="send a turn into a workspace, as the composer does")
    ask.add_argument("workspace", help="a workspace id (see `workspaces`)")
    ask.add_argument("prompt")
    ask.add_argument("--wait", action="store_true", help="wait for the turn to settle and print its reply")
    ask.add_argument("--timeout", type=float, default=DEFAULT_WAIT_S, help="seconds to wait (default 600)")

    sug = sub.add_parser("suggestions", help="the inbox of things the engine noticed")
    sug_sub = sug.add_subparsers(dest="action")
    sug.set_defaults(action="list", workspace=None)
    lst = sug_sub.add_parser("list")
    lst.add_argument("--workspace", default=None)
    sug_sub.add_parser("scan")
    dis = sug_sub.add_parser("dismiss")
    dis.add_argument("id")
    snz = sug_sub.add_parser("snooze")
    snz.add_argument("id")
    snz.add_argument("--hours", type=int, default=24)

    notes = sub.add_parser("notes", help="notes you keep for every project")
    notes_sub = notes.add_subparsers(dest="action")
    notes.set_defaults(action="list")
    notes_sub.add_parser("list")
    add = notes_sub.add_parser("add")
    add.add_argument("text")
    rm = notes_sub.add_parser("delete")
    rm.add_argument("id")

    profile = sub.add_parser("profile", help="export or import one JSON profile (no keys, no authority)")
    profile_sub = profile.add_subparsers(dest="action", required=True)
    exp = profile_sub.add_parser("export")
    exp.add_argument("--workspace", default=None, help="the workspace whose skills (and memory) to include")
    exp.add_argument("--memory", action="store_true", help="include what that workspace has learned")
    exp.add_argument("--notes", action="store_true", help="include your standing notes")
    exp.add_argument("-o", "--output", default=None, help="write here (owner-only) instead of printing")
    imp = profile_sub.add_parser("import")
    imp.add_argument("file")
    imp.add_argument("--workspace", default=None, help="where skills and memory land")
    imp.add_argument("--memory", action="store_true", help="apply the file's memory (off unless asked)")
    imp.add_argument("--notes", action="store_true", help="apply the file's notes (off unless asked)")
    imp.add_argument("--no-settings", action="store_true")
    imp.add_argument("--no-roles", action="store_true")
    imp.add_argument("--skills", action="store_true", help="apply the file's skills (off unless asked)")
    return parser


async def run(
    argv: Sequence[str], *, env: Mapping[str, str], out: TextIO, err: TextIO,
    transport: httpx.AsyncBaseTransport | None = None, base_url: str | None = None, state_file: Path | None = None,
) -> int:
    try:
        args = build_parser().parse_args(list(argv))
    except SystemExit as exit_:
        return EXIT_OK if exit_.code in (0, None) else EXIT_USAGE
    try:
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            token = read_token(env, args.token, state_file)
            base = base_url or await find_base(client, env, args.port)
            await COMMANDS[args.command](Engine(client, base, token), args, out)
    except CliError as exc:
        err.write(f"codify-cli: {exc}\n")
        return exc.code
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(sys.argv[1:] if argv is None else argv, env=os.environ, out=sys.stdout, err=sys.stderr))


if __name__ == "__main__":
    raise SystemExit(main())
