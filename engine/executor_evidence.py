"""The evidence layer: the librarian's reconnaissance and the pack it hands the planner and the fixer.

The second layer of `ExecutorService` (see `engine/executor.py`); it calls only `executor_core`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from engine.fs import FileSystemService, PathEscapeError
from engine.library import (
    LibraryService,
    MAX_ROUND_CHARS,
    format_command,
    format_knowledge,
    format_read,
    format_search,
    read_knowledge,
)
from engine.models import Goal
from engine.sandbox import CommandNotAllowed
from engine.executor_core import _ExecutorCore


# Reconnaissance bounds. The librarian answers in rounds: it asks for material,
# the engine fetches it, and it asks again — so a goal spends at most this many
# model calls on looking before anything is planned. A cap the model cannot raise
# is what keeps "look around" from becoming an unbounded crawl.
MAX_LIBRARY_ROUNDS = 3
MAX_LIBRARY_READS_PER_ROUND = 12
MAX_LIBRARY_SEARCHES_PER_ROUND = 6
MAX_LIBRARY_GIT_PER_ROUND = 6
MAX_LIBRARY_RUNS_PER_ROUND = 4


def _as_read_int(value: Any, default: int | None) -> int | None:
    """A model-supplied read offset/limit coerced to a sane int, or the default.

    Models send "2", 2, 2.0, occasionally "two". Junk falls back to the default
    rather than raising — a malformed window is a wasted round, not a defect.
    """
    if value is None:
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n >= 1 else default


class _Evidence(_ExecutorCore):
    """The librarian's stage and the evidence pack it builds.

    What a goal is told about the workspace before anything is planned: the tree, the files the engine
    opened, the matches it found, the prior a workspace wrote down for itself. Calls only the core layer."""

    # ── librarian ──────────────────────────────────────────────────────────────

    def _evidence_for(self, goal_id: str) -> dict[str, Any]:
        """The evidence pack this goal's librarian produced, read back from events.

        From the event log rather than memory: a goal resumed in another process
        must still hand its fixer the same material its planner planned from.
        """
        latest: dict[str, Any] = {}
        for ev in self.goals.events_after(goal_id, 0):
            if ev.type == "library_evidence":
                latest = ev.payload or {}
        return latest

    async def _librarian(
        self, goal_id: str, goal: Goal, ws: Any, task: str = "",
    ) -> dict[str, Any]:
        """Look around before anything is planned or changed.

        Rounds: the librarian asks for material (reads, searches, read-only git or
        inspect commands), the engine fetches it, and it asks again — capped at
        MAX_LIBRARY_ROUNDS so "look around" cannot become an unbounded crawl. It
        finishes early by setting `enough`, or by asking for nothing.

        Everything it claims is checked against what it was actually shown: a file
        it never opened is dropped and logged rather than passed on as fact.

        `task` is what the conductor said it wanted found out, and it exists for
        the same reason `_fixer`'s `guidance` does: the conductor is the one
        dispatching this run, so a "look around the workspace" that ignored what
        it was sent to look for was not doing its job. It is placed *beside* the
        goal and labelled as an addition, never in place of it — the goal is what
        the user asked for, and a conductor that could rewrite it could send the
        librarian after something the user never requested. Empty (the engine's
        own path, with no conductor) leaves the prompt byte-identical to before.
        """
        lib = LibraryService(ws.root_path)
        tree = lib.tree()
        listed: set[str] = set(tree["files"])
        opened: set[str] = set()
        matched: set[str] = set()

        listing = "\n".join(tree["files"]) or "(no files)"
        # A workspace that wrote down what it learned gets that read first. It
        # is a prior, never evidence: it enters the prompt and the pack's
        # `knowledge` block, and it does NOT enter `files`, so it cannot borrow
        # the pack's "this path was actually opened" guarantee (docs/04 §4.9).
        knowledge = read_knowledge(ws.root_path, listed)
        if knowledge and knowledge["stale_paths"]:
            self._log(
                goal_id, None, "warn",
                f"{knowledge['path']} names {len(knowledge['stale_paths'])} path(s) this "
                f"workspace does not have ({', '.join(knowledge['stale_paths'][:3])}) — "
                "told the librarian to distrust them rather than to follow them",
            )
        prior = format_knowledge(knowledge)
        # The conductor's ask, beside the goal rather than instead of it, and
        # ahead of the tree so it is read as an instruction about the material
        # that follows rather than as another thing to look at.
        ask_section = ""
        if task.strip():
            ask_section = (
                "The conductor asked for this specifically, in addition to the "
                f"goal above:\n{task.strip()}\n\n"
            )
        prompt = (
                f"Goal: {goal.title}\nDescription:\n{goal.description}\n\n"
                f"{ask_section}"
                f"Workspace tree (depth 2, {len(tree['files'])} entries"
                f"{', TRUNCATED' if tree['truncated'] else ''}):\n{listing}\n\n"
                f"{prior}"
            )

        last: dict[str, Any] = {}
        rounds_used = 0
        capped = False
        for round_no in range(1, MAX_LIBRARY_ROUNDS + 1):
            rounds_used = round_no
            out = await self.orchestrator.run_agent("librarian", goal_id, None, prompt)
            last = out if isinstance(out, dict) else {}
            requests = self._library_requests(last)
            if last.get("enough") is True or not requests:
                break
            if round_no == MAX_LIBRARY_ROUNDS:
                self._log(
                    goal_id, None, "warn",
                    f"librarian reached the {MAX_LIBRARY_ROUNDS}-round cap with "
                    f"{len(requests)} request(s) still pending — using what it has",
                )
                # The pack is real but partial, and the difference matters: this
                # is the difference between "the workspace had nothing more" and
                # "the engine stopped asking", which the metrics read as two
                # different outcomes.
                capped = True
                break
            text, opened_now, matched_now, refused = await self._serve_library_requests(goal_id, lib, requests)
            opened |= opened_now
            matched |= matched_now
            if refused:
                self._log(
                    goal_id, None, "warn",
                    f"librarian asked for {refused} thing(s) it may not have — refused, not run",
                )
            # The prior is repeated every round on purpose: a later round is a
            # fresh model call with a fresh prompt, and a librarian that
            # forgets the architecture note halfway through is worse than one
            # that never read it.
            prompt = (
                f"Goal: {goal.title}\nDescription:\n{goal.description}\n\n"
                f"Material you asked for:\n{text}\n\n"
                f"{prior}\n\n"
                "Now answer with the evidence pack. Set enough=true if you have what "
                "the goal needs, or keep asking by filling reads/searches/git/run."
            )

        evidence = self._evidence_pack(
            goal_id, last, opened, matched, listed, rounds_used,
            capped=capped, knowledge=knowledge,
        )
        self.goals.publish(self._event(goal_id, None, "library_evidence", evidence))
        self._log(
            goal_id, None, "info",
            f"librarian: {len(evidence['files'])} file(s) cited from {len(listed)} considered, "
            f"{rounds_used} round(s)"
            + (f", test command {evidence['test_command']}" if evidence.get("test_command") else ""),
        )
        return evidence

    def _library_requests(self, out: dict[str, Any]) -> list[tuple[str, Any]]:
        """What the librarian wants to see next, trimmed to the per-round caps."""
        caps = (
            ("reads", MAX_LIBRARY_READS_PER_ROUND),
            ("searches", MAX_LIBRARY_SEARCHES_PER_ROUND),
            ("git", MAX_LIBRARY_GIT_PER_ROUND),
            ("run", MAX_LIBRARY_RUNS_PER_ROUND),
        )
        requests: list[tuple[str, Any]] = []
        for kind, cap in caps:
            raw = out.get(kind) or []
            if isinstance(raw, (str, dict)):
                raw = [raw]
            if not isinstance(raw, list):
                continue
            for item in raw[:cap]:
                # A read may be a plain path string or {path, offset, limit} —
                # the line-range form that reaches the bottom half of a big file.
                if kind == "reads" and isinstance(item, dict) and item.get("path"):
                    requests.append((kind, {
                        "path": str(item["path"]),
                        "offset": _as_read_int(item.get("offset"), 1),
                        "limit": _as_read_int(item.get("limit"), None),
                    }))
                elif kind == "searches" and isinstance(item, dict) and item.get("query"):
                    # A search may be a plain string or {query, regex, glob} —
                    # the pattern form for structural questions. The regex
                    # itself is validated (and bounded) by the library, so a
                    # bad pattern arrives here as a refusal, not a crash.
                    requests.append((kind, {
                        "query": str(item["query"]),
                        "regex": bool(item.get("regex")),
                        "glob": str(item["glob"]) if item.get("glob") else None,
                    }))
                else:
                    requests.append((kind, item))
        return requests

    async def _serve_library_requests(
        self, goal_id: str, lib: LibraryService, requests: list[tuple[str, Any]],
    ) -> tuple[str, set[str], set[str], int]:
        """Fetch what the librarian asked for, within one round's budget.

        A refusal is information, never a failure: an escape attempt, a forbidden
        command, or a file that is not there is reported back so the librarian can
        ask for something else instead of the goal dying over it.

        Async because three of the four kinds are slow off the CPU: `git` and
        `run` start a **process** through the sandbox, and a search walks the
        tree. Run in the event loop, one twenty-second command held every
        WebSocket tick, every `/health` probe and every cancel request behind it
        — the app spent the command's whole duration reporting itself offline.
        """
        chunks: list[str] = []
        opened: set[str] = set()
        matched: set[str] = set()
        refused = 0
        budget = MAX_ROUND_CHARS

        for kind, item in requests:
            if budget <= 0:
                chunks.append("… (this round's material budget is spent — ask again next round)")
                break
            label = " ".join(str(a) for a in item) if isinstance(item, list) else str(item)
            try:
                if kind == "reads" and isinstance(item, dict):
                    # Line-range read: the path plus the 1-based window.
                    res = lib.read(item["path"], offset=item.get("offset"), limit=item.get("limit"))
                    label = (
                        f"{item['path']} lines {item.get('offset') or 1}-"
                        f"{(item.get('offset') or 1) + (item.get('limit') or 400) - 1}"
                    )
                    opened.add(res["path"])
                    text = format_read(res)
                elif kind == "reads":
                    res = lib.read(label)
                    opened.add(res["path"])
                    text = format_read(res)
                elif kind == "searches" and isinstance(item, dict):
                    # Structured search: {query, regex, glob}.
                    res = await asyncio.to_thread(
                        lib.search, item["query"],
                        glob=item.get("glob"), regex=bool(item.get("regex")),
                    )
                    matched.update(m["path"] for m in res["matches"])
                    text = format_search(res)
                elif kind == "searches":
                    res = await asyncio.to_thread(lib.search, label)
                    matched.update(m["path"] for m in res["matches"])
                    text = format_search(res)
                else:
                    args = [str(a) for a in item] if isinstance(item, list) else [str(item)]
                    run = lib.git if kind == "git" else lib.run
                    res = await asyncio.to_thread(run, args)
                    text = format_command(res)
            except (PathEscapeError, CommandNotAllowed) as exc:
                refused += 1
                text = f"--- refused ({kind}): {label} — {exc}"
            except (OSError, ValueError) as exc:
                refused += 1
                text = f"--- could not read ({kind}): {label} — {exc}"
            chunks.append(text)
            budget -= len(text)

        return "\n\n".join(chunks), opened, matched, refused

    @staticmethod
    def _clean_cited_path(raw: Any) -> str:
        """Normalize a model-cited path without eating its leading dots.

        `lstrip("./")` strips *characters*, not the prefix: `.gitignore` came
        back as `gitignore` and `..env` as `env`, so a cited dotfile never
        matched what the librarian actually opened and was dropped as unseen —
        or planned under a name that does not exist. Only a true `./` prefix
        is removed.
        """
        text = str(raw or "").strip()
        return text[2:] if text.startswith("./") else text

    def _evidence_pack(
        self,
        goal_id: str,
        out: dict[str, Any],
        opened: set[str],
        matched: set[str],
        listed: set[str],
        rounds_used: int,
        capped: bool = False,
        knowledge: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Keep only the claims the engine can stand behind.

        A path is supported when the librarian actually opened it, when a search
        showed a matching line in it, or when it exists in the tree listing. A path
        in none of those was never seen — it is dropped and logged, because a
        confident list of files that do not exist is exactly how "planning from the
        repository" turns into planning from a hallucination.
        """
        def strength(path: str) -> str | None:
            if path in opened:
                return "opened"
            if path in matched:
                return "matched"
            if path in listed:
                return "listed"
            return None

        files: list[dict[str, Any]] = []
        unsupported: list[str] = []
        for entry in out.get("files") or []:
            if not isinstance(entry, dict):
                continue
            path = self._clean_cited_path(entry.get("path"))
            how = strength(path)
            if how:
                files.append(
                    {"path": path, "why": str(entry.get("why") or "")[:300], "evidence": how}
                )
            else:
                unsupported.append(path or "(empty)")
        if unsupported:
            self._log(
                goal_id, None, "warn",
                f"librarian cited {len(unsupported)} path(s) it never saw — dropped: "
                + ", ".join(unsupported[:5]),
            )

        symbols: list[dict[str, Any]] = []
        for s in out.get("symbols") or []:
            if not isinstance(s, dict):
                continue
            name = str(s.get("name") or "").strip()
            path = self._clean_cited_path(s.get("path"))
            if name and (not path or strength(path)):
                symbols.append({"name": name[:120], "path": path})

        raw_cmd = out.get("test_command")
        if isinstance(raw_cmd, str):
            raw_cmd = [raw_cmd]
        test_command = (
            [str(t) for t in raw_cmd][:12]
            if isinstance(raw_cmd, list) and raw_cmd
            else None
        )

        def strings(key: str, limit: int, width: int) -> list[str]:
            raw = out.get(key) or []
            if isinstance(raw, str):
                raw = [raw]
            if not isinstance(raw, list):
                return []
            return [str(v)[:width] for v in raw if str(v).strip()][:limit]

        return {
            "summary": (str(out["summary"])[:1200] if out.get("summary") else None),
            "files": files[:20],
            "symbols": symbols[:20],
            "conventions": strings("conventions", 10, 200),
            "test_command": test_command,
            "risks": strings("risks", 10, 300),
            "rounds": rounds_used,
            "counts": {
                "opened": len(opened),
                "matched": len(matched),
                "considered": len(listed),
            },
            "dropped_paths": unsupported[:5],
            # True when the round cap stopped the search with material still
            # outstanding (docs/04 §4.0). It is the difference between "this
            # workspace has no more to say" and "the engine stopped asking",
            # and it is worth knowing in the pack rather than only in a log line.
            "capped": capped,
            # The workspace's own prior, summarised and kept OUT of `files`,
            # `symbols` and `dropped_paths`. Those three are the engine standing
            # behind a claim; this is a claim, with the staleness check attached
            # so the roles reading it can see how old it is. docs/04 §4.9.
            "knowledge": (
                {
                    "path": knowledge["path"],
                    "chars": knowledge["chars"],
                    "truncated": knowledge["truncated"],
                    "stale_paths": knowledge["stale_paths"],
                }
                if knowledge
                else None
            ),
        }

    def _suggested_paths_context(
        self, fs: FileSystemService, paths: list[str], limit: int = 4000
    ) -> tuple[str, str]:
        """(readable contents, note about the ones that could not be read).

        A planner's `suggested_paths` are a guess, and a guess can name a path that
        escapes the workspace or a file that is not text. Failing the step on that
        before the fixer is ever called means the model never gets to correct
        itself, so the guess is reported as material that was not available rather
        than raised.
        """
        lines: list[str] = []
        skipped: list[str] = []
        for p in paths:
            try:
                target = fs.resolve(p)
            except PathEscapeError:
                skipped.append(f"{p} (outside the workspace)")
                continue
            text = fs.read_text_or_none(p)
            if text is None:
                kind = "a directory" if target.is_dir() else "not readable as text"
                skipped.append(f"{p} ({kind})")
                continue
            shown = f"- {p}: {text[:limit]}"
            if len(text) > limit:
                # Said, not implied: a model shown the head of a long file with nothing marking the cut edits
                # the top of it believing it has read all of it.
                shown += f"\n…[{len(text) - limit} more characters of {p} not shown]"
            lines.append(shown)
        ctx = "\n".join(lines) if lines else "(no readable suggested path)"
        note = ""
        if skipped:
            note = (
                "\nSuggested paths that could not be read (do not assume their contents):\n"
                + "\n".join(f"- {s}" for s in skipped)
            )
        return ctx, note

    def _evidence_text(self, evidence: dict[str, Any]) -> str:
        """Render an evidence pack for a prompt. Never invents a section."""
        if not evidence:
            return (
                "(none — no reconnaissance was available, so keep steps small, prefer "
                "paths the goal names, and say what you could not verify)"
            )
        lines: list[str] = []
        if evidence.get("summary"):
            lines.append(f"Summary: {evidence['summary']}")
        files = evidence.get("files") or []
        if files:
            lines.append(f"Files ({len(files)}, each marked with how it was verified):")
            lines += [f"- {f['path']} [{f['evidence']}] — {f['why']}" for f in files]
        symbols = evidence.get("symbols") or []
        if symbols:
            # The parenthesised path is built from a string literal rather than a
            # nested f-string quoting itself with the outer one's character: reusing
            # the outer quote is PEP 701, which only Python 3.12+ accepts, and this
            # module has to import on the 3.10 minimum pyproject.toml declares.
            # `tests/test_min_python_syntax.py` guards the rule.
            lines.append("Symbols: " + ", ".join(
                f"{s['name']}" + (f" ({s['path']})" if s.get("path") else "")
                for s in symbols
            ))
        if evidence.get("conventions"):
            lines.append("Conventions: " + "; ".join(evidence["conventions"]))
        if evidence.get("test_command"):
            lines.append("Test command this repository runs: " + " ".join(evidence["test_command"]))
        known = evidence.get("knowledge")
        if known:
            # Last, and labelled: a prior that arrives first reads as a fact.
            lines += [
                "",
                f"PRIOR — not evidence: this workspace's {known['path']} was read this "
                f"run ({known['chars']} chars"
                + (", truncated" if known["truncated"] else "")
                + "). It was written by an earlier run and none of it is verified; "
                "use it to aim your steps' paths, and do not restate it as a finding.",
            ]
            if known.get("stale_paths"):
                lines.append(
                    "It names path(s) absent from this workspace: "
                    + ", ".join(known["stale_paths"][:5])
                    + ". Do not plan a step against those."
                )
        if evidence.get("risks"):
            lines.append("Risks: " + "; ".join(evidence["risks"]))
        if evidence.get("dropped_paths"):
            lines.append(
                "Paths the librarian mentioned but never opened (unverified, do not rely on "
                f"them): {', '.join(evidence['dropped_paths'])}"
            )
        return "\n".join(lines)
