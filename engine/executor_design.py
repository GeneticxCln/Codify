"""The design layer: the design stage, the brand contract, the design and knowledge deliverables, and the drift check.

The third layer of `ExecutorService` (see `engine/executor.py`); it calls `executor_core` and `executor_evidence`.

**Most of this module is a DRAFT RECONSTRUCTION, not the original code.** The design work (the design stage
called from `run_planning`, the `_design*` / `_brand_contract` / `_brand_drifts` helpers here, and the
deliverable paths in `_fixer` / `_verifier` / `_critic`) was written uncommitted and lost before it reached a
commit. Every block marked "DRAFT RECONSTRUCTION" was rebuilt from its *specification*:

  * `tests/test_design_role.py` (72 tests), which pins the behaviour;
  * `scripts/fake_ollama.py`, whose prompt parser pins the exact fixer framing
    (`--- DESIGN.md (write exactly this) ---` ... `--- end DESIGN.md ---`);
  * docs/01 §1.1a and docs/04 §4.0a, §4.0a.1, §4.0a.2, which specify the origin table, the stamping and
    body-dropping rules, the drift rules, and the four design-deliverable deltas.

What that buys is behaviour, not authorship. The original author's structure, helper decomposition, prompt
wording and reasoning are gone and are *not* reproduced here; three constants (`MAX_CONTRACT_FILE_CHARS`,
`MAX_DRIFT_DIFF_CHARS`, `MAX_BRAND_DRIFTS`) have no documented value and carry plausible guesses, since no
test pins them. Treat this as a proposal for their review rather than as their work restored: if the original
returns, prefer it, and expect to reconcile rather than to discard.
"""

from __future__ import annotations

import os
import re
from typing import Any

from engine.default_prompts import DESIGN_BRIEF_PROMPT, KNOWLEDGE_BRIEF_PROMPT
from engine.executor_support import AgentOutputInvalid
from engine.fs import FileSystemService
from engine.library import ROOT_KNOWLEDGE_MD, read_knowledge
from engine.models import Goal, PlanStep
from engine.executor_evidence import _Evidence


# ── design (DRAFT RECONSTRUCTION — see docs/04 §4.0a) ───────────────────────
# The direction one goal is built against. Every list is trimmed rather than
# dropped whole — a malformed row is prompt material, not a broken goal — and
# `artifact` is a closed vocabulary because the app renders it: this is not a
# place to pass a model's invention through, so anything else reads as "other".
DESIGN_ARTIFACTS = (
    "web_prototype", "page", "dashboard", "deck", "mobile",
    "document", "component", "style_system", "other",
)
# The one shape convention gets for free: a non-empty DESIGN.md at the
# workspace root is the brand contract, with nothing pinned. Zero-config is the
# point — a repository that already documents its brand should not have to be
# told twice — and one name in one place keeps it predictable. Everything else
# (another name, a nested path, a tokens JSON) is what the pin is for.
ROOT_DESIGN_MD = "DESIGN.md"
MAX_DESIGN_COLORS = 24
MAX_DESIGN_TYPOGRAPHY = 12
MAX_DESIGN_SPACING = 12
MAX_DESIGN_RADII = 8
MAX_DESIGN_COMPONENTS = 40
# conventions / constraints / acceptance: the three lists a human reads to judge
# the result, so they stay short enough to read.
MAX_DESIGN_LINES = 12
MAX_DESIGN_MD_CHARS = 8000
# A pinned brand contract is handed over whole, bounded. It is prose, not a
# corpus, and an unbounded read is an unbounded prompt — truncation is logged
# rather than silent, so a half-brand is visible in the transcript.
MAX_CONTRACT_FILE_CHARS = 20000
# The verifier's mechanical brand check (docs/04 §4.0a.1): how much of each
# changed file is read as evidence, and how many findings one step may publish.
# The check is advisory, and a transcript of forty findings is a finding nobody
# reads.
MAX_DRIFT_DIFF_CHARS = 20000
MAX_BRAND_DRIFTS = 8

# The one file each deliverable mode writes, and what that file *is* to the
# roles judging it. Both modes share a shape — an agent drafts the body, a step
# writes it verbatim, the verifier and critic read the bytes — and differ only
# in which file, so the two facts live in one table rather than in three `if
# mode == "design"` branches that could disagree about what is being written.
# A mode absent from this table has no deliverable, which is what stops a
# normal goal from being handed a document to write.
DELIVERABLE_FILES = {
    "design": ROOT_DESIGN_MD,
    "knowledge": ROOT_KNOWLEDGE_MD,
}
# What the file *is*, for the role that writes it, and what the critic is being
# asked to approve. Two tables rather than one because the two prompts genuinely
# say different things: the fixer is told to reproduce a draft, the critic to
# judge a document. Both design strings are byte-for-byte what they were before
# the second mode existed, because `tests/test_design_role.py` pins that wording
# and a contract test that breaks on a reword is a contract test that will be
# silenced rather than honoured.
DELIVERABLE_ROLE = {
    "design": "the workspace's brand contract, the file every later goal is planned against",
    "knowledge": (
        "the workspace's knowledge file, which every later run's librarian reads as a "
        "prior — wrong in it sends the next run to the wrong file"
    ),
}
DELIVERABLE_SUBJECT = {
    "design": (
        f"the workspace's {ROOT_DESIGN_MD} itself — the contract every later goal is "
        "planned against"
    ),
    "knowledge": (
        f"the workspace's {ROOT_KNOWLEDGE_MD} itself — the knowledge every later run's "
        "librarian reads as a prior, so a wrong claim in it misdirects the next run"
    ),
}

# How a deliverable is labelled to the two roles that judge it. The difference
# between these two strings is the difference between "this is the file" and
# "this is what the file will be" — and a role that guesses wrong is reviewing
# bytes that do not exist.
ARTIFACT_WRITTEN = "as written"
ARTIFACT_PROPOSED = "as proposed by this step (nothing was written to disk)"

# Function words carry no evidence. An acceptance line is prose ("every KPI
# renders in its own tile"), and matching its "in" and "its" against a diff
# would evidence a claim nothing in the change actually addressed.
_DRIFT_STOPWORDS = frozenset({
    "and", "are", "as", "at", "be", "been", "but", "by", "can", "for", "from",
    "has", "have", "into", "its", "may", "must", "not", "of", "on", "only",
    "or", "over", "should", "than", "that", "the", "their", "them", "then",
    "there", "these", "they", "this", "those", "use", "used", "using", "was",
    "were", "what", "when", "where", "which", "while", "with", "without",
    "you", "your",
})


def _text_words(text: str) -> list[str]:
    """Casefolded alphanumeric words, for evidence matching."""
    return re.findall(r"[a-z0-9]+", text.casefold())


class _Design(_Evidence):
    """The design stage and everything that keeps a goal's output on-brand.

    Reads and stamps the workspace's brand contract, drafts and validates the design and knowledge
    deliverables, and measures drift from the contract. Calls the core and evidence layers only."""

    # ── design (DRAFT RECONSTRUCTION — see docs/04 §4.0a) ───────────────────
    #
    # One bounded call, no tools: the design agent decides and the fixer stays
    # the only role whose changes reach the disk. The contract it locks is
    # published (`design_contract`) and read back per step (`_design_for`), so
    # the planner, the fixer and the critic work from the same direction rather
    # than from whichever prompt happened to produce it — including when a step
    # is driven in another process.

    def _design_for(self, goal_id: str) -> dict[str, Any]:
        """The contract this goal locked, read back from its event log.

        From events rather than memory, for the same reason `_evidence_for` is:
        a step driven in another process must be handed the direction its
        planner planned from. A goal planned before this stage existed has no
        contract, and an empty lookup is the answer, never a failure.
        """
        latest: dict[str, Any] = {}
        for ev in self.goals.events_after(goal_id, 0):
            if ev.type == "design_contract":
                latest = ev.payload or {}
        return latest

    async def _design(
        self, goal_id: str, goal: Goal, ws: Any, evidence: dict[str, Any],
        task: str = "",
    ) -> dict[str, Any]:
        """Lock the direction this goal is built against, and publish it.

        A brand contract the workspace already has wins over a proposal: it is
        handed over whole and framed as binding, and a reply cannot relabel
        where it came from. `applies: false` — or a reply naming no direction —
        is an answer, not a failure, and publishes nothing.

        `task` is the conductor's ask, on the same terms as `_librarian`'s: it
        narrows what the designer decides about, beside the goal, and it is
        placed *before* the contract block so the workspace's own binding brand
        is still the last thing in the prompt and still wins.
        """
        brand = self._brand_contract(goal_id, ws)
        prompt = self._design_prompt(goal, evidence, brand, deliverable=False, task=task)
        out = await self.orchestrator.run_agent("design", goal_id, None, prompt)
        contract = self._design_contract(out, brand)
        if not contract:
            self._log(
                goal_id, None, "info",
                "design: this goal locks no visual direction — the planner is told so "
                "rather than handed an invented one",
            )
            return {}
        published = {**contract, "mode": goal.mode}
        self.goals.publish(self._event(goal_id, None, "design_contract", published))
        # The published dict, not a narrower one: the local `design` and
        # `_design_for()` then agree on their shape, so a caller cannot read
        # `mode` from one and miss it in the other.
        return published

    async def _design_deliverable(
        self, goal_id: str, goal: Goal, ws: Any, evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """A design-deliverable goal: the design agent authors the file.

        Same slot, same single bounded call, same no-tools rule — only the
        relationship inverts. The contract the workspace already has is shown as
        *revision material* rather than as a law, and `_design_contract` is
        called WITHOUT the brand so the draft's body survives: dropping it is
        right for a normal goal (a second body would compete with a contract
        that exists as a file) and fatal here, since the body is the deliverable.

        A body is mandatory. A goal with nothing written has nothing to deliver,
        and that is the one thing this stage is loud about — the caller turns
        the raise into the usual non-fatal warning, so the goal still plans.
        """
        brand = self._brand_contract(goal_id, ws)
        prompt = self._design_prompt(goal, evidence, brand, deliverable=True)
        out = await self.orchestrator.run_agent("design", goal_id, None, prompt)
        contract = self._design_contract(out)
        if not (contract.get("design_md") or "").strip():
            raise AgentOutputInvalid(
                "a design-deliverable goal must author the complete DESIGN.md body in "
                "design_md — without one there is nothing to deliver",
                role="design",
            )
        published = {**contract, "mode": "design"}
        self.goals.publish(self._event(goal_id, None, "design_contract", published))
        return published

    async def _knowledge_deliverable(
        self, goal_id: str, goal: Goal, ws: Any, evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """A knowledge-deliverable goal: the design agent authors CODIFY.md.

        The same shape as a design deliverable with the relationship flipped
        again, and that is the whole point of the mode. A design goal revises a
        contract it is *bound* by; a knowledge goal rewrites a prior it is
        *superseding*, so the existing file is shown as revision material and
        the draft must stand on its own — a knowledge file that merely restates
        last month's file is worth less than none, because it looks current.

        The same body field carries it. `design_md` is the deliverable slot in
        the design reply, and the alternative — a second body field per file —
        would be a shape the model has to be told about twice.
        """
        # Read once and used twice: the prompt quotes these bytes, and the
        # published contract carries them, so a user comparing the two is
        # comparing a document against itself rather than against a second,
        # possibly newer, copy of the same file.
        known = read_knowledge(ws.root_path)
        prompt = self._knowledge_prompt(goal, evidence, ws, known)
        out = await self.orchestrator.run_agent("design", goal_id, None, prompt)
        contract = self._design_contract(out)
        if not (contract.get("design_md") or "").strip():
            raise AgentOutputInvalid(
                f"a knowledge-deliverable goal must author the complete {ROOT_KNOWLEDGE_MD} "
                f"body in design_md — without one there is nothing to deliver",
                role="design",
            )
        published = {**contract, "mode": "knowledge"}
        if known:
            # What the drafter was rewriting, published so the user can see it.
            # The body alone answers "what will this file say" and leaves "what
            # did it say" to a diff the user has to reconstruct — and for this
            # file that is the whole review, because a prior that only looks
            # plausible is exactly what this run exists to replace (docs/04
            # §4.9.2).
            #
            # The text is the fresh read the prompt quotes. The stale paths are
            # the pack's verdict on that same file, not the fresh read's: the
            # drafter's copy was taken with no tree listing, so its own
            # `stale_paths` is empty by construction, while the pack's is the
            # check the drafter was *also* given and has to live with. Publishing
            # the fresh read's empty list would read as "nothing here is stale",
            # which is the opposite of the truth.
            pack = evidence.get("knowledge") or {}
            published["revises"] = {
                "path": known["path"],
                "text": known["text"],
                "chars": known["chars"],
                "truncated": known["truncated"],
                "stale_paths": pack.get("stale_paths") or [],
            }
        self.goals.publish(self._event(goal_id, None, "design_contract", published))
        return published

    def _knowledge_prompt(
        self, goal: Goal, evidence: dict[str, Any], ws: Any,
        known: dict[str, Any] | None = None,
    ) -> str:
        """The knowledge call's prompt: the goal, what is known, what came before.

        The existing file is read here rather than from the pack because the
        pack deliberately does not carry it: a prior is not evidence, and the
        evidence block is the one part of these prompts the roles are told they
        may rely on. Mixing the two would spend the pack's credibility on a file
        nobody has checked.

        `known` is passed in rather than read here so the caller can publish the
        very bytes this prompt quotes. A second read would be a second file: one
        read for the prompt and another for the transcript, disagreeing whenever
        anything wrote in between, and a reviewer comparing them would be
        comparing a document against a slightly older copy of itself.
        """
        if known is None:
            known = read_knowledge(ws.root_path)
        parts = [
            f"Goal: {goal.title}\nDescription:\n{goal.description}",
            f"What the librarian found:\n{self._evidence_text(evidence)}",
            KNOWLEDGE_BRIEF_PROMPT,
        ]
        if known:
            parts.append(
                f"--- current {ROOT_KNOWLEDGE_MD} — revision material, NOT a prior you "
                "may trust ---\n"
                f"{known['text']}\n"
                f"--- end {ROOT_KNOWLEDGE_MD} ---\n"
                "The workspace has written knowledge down before. Check it against the "
                "evidence pack above and keep only what the evidence supports: a claim "
                f"this file makes that the pack cannot back is {ROOT_KNOWLEDGE_MD}'s "
                "whole failure mode, because the next run reads it as a prior. Write the "
                "file it should have."
            )
        else:
            parts.append(
                f"The workspace has no {ROOT_KNOWLEDGE_MD} today. That is the file this "
                "goal writes, so design_md must be a complete, standalone document."
            )
        return "\n\n".join(parts)

    def _design_prompt(
        self, goal: Goal, evidence: dict[str, Any], brand: dict[str, Any] | None,
        *, deliverable: bool, task: str = "",
    ) -> str:
        """The design call's prompt: the goal, what is known, and the contract.

        Two shapes, because the two goals want opposite things from the same
        file. A normal goal is *bound* by a contract the workspace already has;
        a design goal is writing it, so the same text is revision material and
        must not be mistaken for a law to obey.

        `task` is optional and last-of-the-instructions: everything the workspace
        has to say about the contract follows it, so a conductor's ask can shape
        what is decided without ever outranking the contract.
        """
        parts = [
            f"Goal: {goal.title}\nDescription:\n{goal.description}",
            f"What the librarian found:\n{self._evidence_text(evidence)}",
        ]
        if task.strip():
            parts.append(
                "The conductor asked for this specifically, in addition to the "
                f"goal above:\n{task.strip()}"
            )
        if deliverable:
            parts += [
                DESIGN_BRIEF_PROMPT,
                "Your draft is the deliverable, so it has to stand on its own. If the "
                "workspace already documents a brand it is shown below as revision "
                "material: write the one it should have, and put it in design_md.",
            ]
        if brand:
            if deliverable:
                parts.append(
                    f"--- current brand contract ({brand['origin']}) at {brand['path']} "
                    "— revision material, not a law to obey ---\n"
                    f"{brand['text']}\n"
                    "--- end brand contract ---\n"
                    "The workspace already documents a brand: revise or replace it. The "
                    "body you return is the draft a step will write, and the user pins it "
                    "from there — do not answer 'no change', because the deliverable is a "
                    "file."
                )
            else:
                parts.append(
                    f"--- Workspace brand contract ({brand['origin']}) at {brand['path']} "
                    "— BINDING ---\n"
                    f"{brand['text']}\n"
                    "--- end brand contract ---\n"
                    "That file is the workspace's own contract and is BINDING. Derive the "
                    "design system, tokens and components from it rather than inventing "
                    "new ones, and return the direction the rest of this goal obeys."
                )
        elif deliverable:
            parts.append(
                "The workspace has no brand contract today. That is the file this goal "
                "writes, so design_md must be a complete, standalone contract."
            )
        else:
            parts.append(
                "The workspace has no brand contract. Propose one, and put the DESIGN.md "
                "body a step should publish in design_md so the file exists to be pinned."
            )
        return "\n\n".join(parts)

    def _brand_contract(self, goal_id: str, ws: Any) -> dict[str, Any] | None:
        """The brand contract this workspace already has, or None.

        Resolution order (docs/04 §4.0a): the user's pin first, then a non-empty
        `DESIGN.md` at the workspace root. The pin wins outright — naming a file
        convention would not find is the whole reason to pin one.

        A pin whose file has gone missing is a warning and a fall back to
        proposing, never a silent downgrade to convention: the `origin` published
        with the contract says which of the two the result actually is, so nobody
        reads the answer as "the pinned brand was used".
        """
        fs = FileSystemService(ws.root_path)
        pinned = str(getattr(ws, "design_contract_path", "") or "").strip()
        if pinned:
            text = fs.read_text_or_none(pinned)
            if not text or not text.strip():
                self._log(
                    goal_id, None, "warn",
                    f"pinned brand contract {pinned!r} is not readable as text in this "
                    "workspace — proposing a brand instead, and the published contract "
                    "will not claim the pin",
                )
                return None
            return self._brand_file(goal_id, pinned, "pinned", text)
        text = fs.read_text_or_none(ROOT_DESIGN_MD)
        # An empty file is not a brand. Binding every goal to nothing would also
        # silently drop the body a fixer was supposed to write.
        if not text or not text.strip():
            return None
        return self._brand_file(goal_id, ROOT_DESIGN_MD, "discovered", text)

    def _brand_file(
        self, goal_id: str, path: str, origin: str, text: str,
    ) -> dict[str, Any]:
        """A resolved contract file, truncated to a prompt-safe bound and said so."""
        if len(text) > MAX_CONTRACT_FILE_CHARS:
            self._log(
                goal_id, None, "warn",
                f"brand contract {path!r} is {len(text)} chars — using the first "
                f"{MAX_CONTRACT_FILE_CHARS}",
            )
            text = text[:MAX_CONTRACT_FILE_CHARS]
        return {"path": path, "origin": origin, "text": text}

    def _design_contract(
        self, raw: object, brand: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Normalize one design reply into the contract the engine publishes.

        Trims rather than fails: this is prompt material, and a row the model
        got slightly wrong should not cost the goal its direction. Two things
        are the engine's fact rather than the model's claim — where the brand
        came from (`source`), and whether a body may compete with a contract
        file that already exists.
        """
        if not isinstance(raw, dict):
            raise AgentOutputInvalid(
                f"design contract must be a JSON object, got {type(raw).__name__}",
                role="design",
            )
        if not raw.get("applies"):
            # "This goal changes no rendered surface" is a real answer.
            return {}
        direction = str(raw.get("direction") or "").strip()
        if not direction:
            # `applies: true` with nothing to say is a decline, not a contract:
            # the planner plans against values, not against a heading.
            return {}

        artifact = str(raw.get("artifact") or "").strip()
        if artifact not in DESIGN_ARTIFACTS:
            artifact = "other"
        tokens_in = raw.get("tokens")
        if not isinstance(tokens_in, dict):
            tokens_in = {}
        system_in = raw.get("design_system")
        if not isinstance(system_in, dict):
            system_in = {}

        name = str(system_in.get("name") or "").strip()
        source: str | None = None
        origin: str | None = None
        if brand:
            source = str(brand.get("path") or "") or None
            origin = str(brand.get("origin") or "") or None
            if not name:
                # A pinned file must render as *something*: the filename is a
                # fact, where a made-up label would not be.
                name = os.path.splitext(os.path.basename(source or ""))[0]
        # The body is passed on as written: it is a file, and a file whose
        # trailing newline the engine decided to trim is a file that differs
        # from the one the agent authored.
        body_raw = raw.get("design_md")
        body = body_raw if isinstance(body_raw, str) else ""
        if brand:
            # A goal cannot answer a contract that exists as a file by writing a
            # second one over it — the exact drift the pin exists to stop.
            body = ""
        elif len(body) > MAX_DESIGN_MD_CHARS:
            body = body[:MAX_DESIGN_MD_CHARS]

        return {
            "applies": True,
            "artifact": artifact,
            "direction": direction,
            "design_system": {"name": name, "source": source, "origin": origin},
            "tokens": {
                "colors": self._design_rows(tokens_in.get("colors"), MAX_DESIGN_COLORS),
                "typography": self._design_rows(
                    tokens_in.get("typography"), MAX_DESIGN_TYPOGRAPHY,
                ),
                "spacing": self._design_lines(tokens_in.get("spacing"), MAX_DESIGN_SPACING),
                "radii": self._design_lines(tokens_in.get("radii"), MAX_DESIGN_RADII),
            },
            "components": [
                {"name": row["name"], "purpose": str(row.get("purpose") or "").strip()}
                for row in self._design_rows(raw.get("components"), MAX_DESIGN_COMPONENTS)
            ],
            "conventions": self._design_lines(raw.get("conventions"), MAX_DESIGN_LINES),
            "constraints": self._design_lines(raw.get("constraints"), MAX_DESIGN_LINES),
            "acceptance": self._design_lines(raw.get("acceptance"), MAX_DESIGN_LINES),
            "design_md": body or None,
        }

    @staticmethod
    def _design_rows(raw: Any, cap: int) -> list[dict[str, Any]]:
        """Named rows from a reply's list, trimmed to `cap`.

        A row with no `name` is dropped: an unnamed token is a value nothing can
        reference, so passing it on only gives the roles noise to be told to
        ignore.
        """
        rows: list[dict[str, Any]] = []
        if not isinstance(raw, list):
            return rows
        for item in raw[:cap]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            rows.append({**item, "name": name})
        return rows

    @staticmethod
    def _design_lines(raw: Any, cap: int) -> list[str]:
        """Non-empty strings from a reply's list, trimmed to `cap`."""
        lines: list[str] = []
        if not isinstance(raw, list):
            return lines
        for item in raw[:cap]:
            text = str(item or "").strip()
            if text:
                lines.append(text)
        return lines

    def _design_text(self, contract: dict[str, Any]) -> str:
        """Render a contract for the roles that must work from it.

        One renderer for the planner, the fixer and the critic, so three roles
        cannot end up with three different descriptions of one direction. An
        empty contract says so rather than saying nothing: "no direction" is
        information, and the alternative is each role inventing one.

        `pinned` and `discovered` are labelled differently on purpose. One is
        the user's instruction, the other a convention the engine noticed, and
        the roles' weight for it follows from which one it is.
        """
        if not contract:
            return (
                "Design contract: (none — this goal locked no visual direction). There "
                "is nothing to obey, so invent no visual direction: follow the "
                "repository's own conventions and keep every step's look consistent "
                "with the others."
            )
        system = contract.get("design_system") or {}
        source = str(system.get("source") or "")
        name = str(system.get("name") or "").strip() or "unnamed"
        if system.get("origin") == "pinned" and source:
            where = f"{name} (pinned at {source} — binding)"
        elif source:
            where = f"{name} (found at {source} — binding)"
        else:
            where = f"{name} (proposed by this run, not yet the workspace's)"

        lines = [
            "Design contract — locked before this plan was made, and binding for it.",
            f"Design system: {where}",
            f"Direction: {contract.get('direction', '')}",
        ]
        tokens = contract.get("tokens") or {}
        colors = [
            f"{c.get('name')} {c.get('value')}".strip() for c in tokens.get("colors") or []
        ]
        if colors:
            lines.append("Colors: " + ", ".join(colors))
        type_rows = [
            f"{t.get('name')} = {t.get('value')}".strip() for t in tokens.get("typography") or []
        ]
        if type_rows:
            lines.append("Typography: " + ", ".join(type_rows))
        scale = [*(tokens.get("spacing") or []), *(tokens.get("radii") or [])]
        if scale:
            lines.append("Spacing and radii: " + ", ".join(str(s) for s in scale))
        components = [
            f"{c.get('name')} — {c.get('purpose')}".strip(" —")
            for c in contract.get("components") or []
        ]
        if components:
            lines.append("Components: " + "; ".join(components))
        for label, key in (
            ("Conventions", "conventions"),
            ("Constraints", "constraints"),
            ("Acceptance", "acceptance"),
        ):
            rows = contract.get(key) or []
            if rows:
                lines.append(f"{label}: " + "; ".join(str(r) for r in rows))
        body = str(contract.get("design_md") or "")
        if body:
            # Named from the mode, not hardcoded: a planner told to write
            # "DESIGN.md body" during a knowledge goal plans a step that writes
            # the wrong file, and the fixer is then handed a document it has no
            # instruction to write.
            target = DELIVERABLE_FILES.get(str(contract.get("mode") or ""), ROOT_DESIGN_MD)
            lines += [
                "",
                f"--- {target} body (the file a step must produce) ---",
                body,
                f"--- end {target} body ---",
                "One planned step writes that file: write it verbatim in its own step, "
                "do not paste it into other files, and do not substitute a summary for it.",
            ]
        return "\n".join(lines)

    @staticmethod
    def _deliverable_write_path(step: PlanStep, contract: dict[str, Any]) -> str:
        """The deliverable a step means to write, or "" when it means none.

        `suggested_paths` is the only honest signal a plan gives about intent,
        and it is the same signal for all three roles that care — so one helper
        recognizes it for the fixer, the verifier and the critic rather than
        three that could disagree. Which filename counts is the mode's business,
        from `DELIVERABLE_FILES`: a step that happens to name CODIFY.md in a
        design goal is writing a document the pipeline did not ask for.
        """
        wanted = DELIVERABLE_FILES.get(str(contract.get("mode") or ""))
        if not wanted:
            return ""
        for raw in step.suggested_paths or []:
            path = str(raw or "").strip()
            if os.path.basename(path.replace("\\", "/")).casefold() == wanted.casefold():
                return path
        return ""

    def _deliverable_artifact(
        self, goal_id: str, step: PlanStep, ws_root: str, path: str,
    ) -> tuple[str | None, str]:
        """(content, "as written" | "as proposed") for a deliverable step.

        A dry run reaches the disk not at all, so there is no file for the
        verifier to read — but the step's proposal is stored, and it is exactly
        the bytes Apply replays. Reading it here, rather than re-rendering the
        diff, is what makes "reviewed" a fact about the deliverable instead of a
        claim about the pipeline. Both judges come through this one helper, so
        the role that decides the step and the role that reports on it cannot be
        reading different things.
        """
        if not path:
            return (None, "")
        written = FileSystemService(ws_root).read_text_or_none(path)
        if written is not None:
            return (written, ARTIFACT_WRITTEN)
        proposed = self.goals.proposed_content(goal_id, step.id, path)
        if proposed is not None:
            return (proposed, ARTIFACT_PROPOSED)
        return (None, "")

    def _brand_drifts(
        self, diffs: list[dict[str, Any]], design: dict[str, Any], root_path: str,
    ) -> list[str]:
        """What the written artifacts fail to evidence about a binding contract.

        Only what text comparison can prove is reported; everything else stays
        the critic's judgment. The evidence is the *changes*, never the whole
        tree — a workspace already full of the brand must not pass a step for
        that reason. A proposed brand draws nothing at all: enforcement is for
        what a workspace signed, not for advice.

        Advisory by construction: these findings ride on the verifier's own
        record and never flip the verdict. Only the tests (or the critic) fail a
        step, and a text-matching heuristic is not the evidence that should stop
        one.
        """
        system = design.get("design_system") or {}
        if system.get("origin") not in ("pinned", "discovered"):
            return []
        corpus = self._brand_corpus(diffs, root_path)
        if not corpus:
            # Nothing readable to check is not evidence of anything. A step that
            # wrote a binary blob, or wrote nothing, is reported as silent rather
            # than as a contract the whole workspace failed.
            return []

        tokens = design.get("tokens") or {}
        colors = [c for c in (tokens.get("colors") or []) if isinstance(c, dict)]
        faces = [t for t in (tokens.get("typography") or []) if isinstance(t, dict)]
        # The contract's own token names are not evidence of it: an acceptance
        # line naming `accent` must not pass because `accent` appears in the diff
        # as an identifier.
        token_names = {
            str(c.get("name") or "").casefold() for c in colors
        } | {str(t.get("name") or "").casefold() for t in faces}

        drifts: list[str] = []
        for color in colors:
            value = str(color.get("value") or "").strip()
            if value and value.casefold() not in corpus:
                drifts.append(
                    f"token color {color.get('name')} {value} appears nowhere in the changes"
                )
        for face in faces:
            stack = str(face.get("value") or "").strip()
            words = [
                w for w in _text_words(f"{stack} {face.get('name') or ''}") if len(w) > 2
            ]
            if words and not any(w in corpus for w in words):
                drifts.append(
                    f"typography {face.get('name')} ({stack}) appears nowhere in the changes"
                )
        scale = [
            str(s).strip() for s in (tokens.get("spacing") or []) + (tokens.get("radii") or [])
            if str(s).strip()
        ]
        if scale and not any(s.casefold() in corpus for s in scale):
            drifts.append("none of the contract's spacing/radius tokens appear in the changes")
        for label, key in (("acceptance", "acceptance"), ("constraint", "constraints")):
            for raw in design.get(key) or []:
                line = str(raw).strip()
                if line and not self._line_evidenced(line, corpus, token_names):
                    drifts.append(f"{label} not evidenced by any change: {line}")
        return drifts[:MAX_BRAND_DRIFTS]

    def _brand_corpus(self, diffs: list[dict[str, Any]], root_path: str) -> str:
        """The text this step wrote, casefolded, each file bounded.

        The applied content first (it is what a replay will write), then the
        diff, then the artifact on disk for a change whose diff carries no text —
        a binary write is still subject to the contract. A file that cannot be
        decoded contributes nothing rather than an empty string: absence of
        evidence is not evidence of absence.
        """
        fs = FileSystemService(root_path)
        chunks: list[str] = []
        for d in diffs:
            text = d.get("resolved_content")
            if not isinstance(text, str) or not text:
                text = d.get("unified_diff")
            if not isinstance(text, str) or not text:
                text = fs.read_text_or_none(str(d.get("path") or ""))
            if isinstance(text, str) and text:
                chunks.append(text[:MAX_DRIFT_DIFF_CHARS])
        return "\n".join(chunks).casefold()

    @staticmethod
    def _line_evidenced(line: str, corpus: str, token_names: set[str]) -> bool:
        """Does any substantive word of this line appear in what was written?"""
        words = [
            w for w in _text_words(line)
            if len(w) > 2 and w not in _DRIFT_STOPWORDS and w not in token_names
        ]
        if not words:
            # Nothing checkable in the line: not reportable as unaddressed
            # either, because a finding the checker cannot support is worse than
            # no finding at all.
            return True
        return any(w in corpus for w in words)
