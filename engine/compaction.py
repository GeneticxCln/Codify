"""Rolling summaries for a thread, so a long conversation survives its own length.

A thread's earlier turns are sent to the model on every turn, capped by
`TURN_HISTORY_TURNS`. That cap is what keeps a long thread from growing the
prompt without bound, and it is also what makes an old thread slowly forget its
beginning: the oldest turns fall out first.

Compaction is the one thing that fixes that without sending the whole history.
An older span of turns is replaced by one summary, stored on the conversation
and put in front of the model *instead of* those turns. The raw turns stay
where they are — the transcript is the person's record, not a cache — so a
summary is a second reading of the same rows, never a rewrite of them.

What this module owns is the plan and the store. The model call itself belongs
to the executor (`ExecutorService.compact_thread`), because it is the one thing
that knows which configuration a turn's own calls use. Everything here is
bounded: a summary is capped by `MAX_SUMMARY_CHARS`, and one compaction reads
at most `MAX_COMPACTION_TURNS` turns, so a pathological thread cannot turn a
click in the Session drawer into an unbounded request.
"""

from __future__ import annotations

import re
import sqlite3
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any

from engine.models import CompactionStatus, ConversationCompaction
from engine.services import GoalService, SettingsService

# One compaction reads at most this many turns. A thread larger than this is
# compacted in rounds: the summary carries what fell out of the window.
MAX_COMPACTION_TURNS = 500

# The summary a model may write back. Long enough for the decisions and the
# open threads, short enough that it is a summary rather than a second copy.
MAX_SUMMARY_CHARS = 4_000

# The prompt the summary is made from, before the model's own output cap. A
# bound here is what keeps a 500-turn compaction from being a 500-turn request.
MAX_COMPACT_INPUT_CHARS = 12_000

# Auto-compaction waits for this many turns beyond the kept ones. Compacting one
# turn at a time would spend a model call on every message and buy nothing.
AUTO_MIN_OLD_TURNS = 4


def summary_is_usable(summary: str) -> bool:
    """Whether a model's summary is prose this thread can actually use.

    A model asked for prose can answer with an empty JSON container — measured
    against a real local model, which returned `{}` — or with a JSON-shaped
    fragment that then repeats `#}` until the character cap. Both contain
    letters, so a word count is not enough. The checks below are deliberately
    about shape, not style: a summary must begin like prose, be mostly letters,
    and not be dominated by JSON punctuation or a two-character loop.

    The rule is conservative on purpose. A refused summary costs one more call
    (the executor re-asks once); a stored malformed one is put in front of every
    later turn as the record of what the thread decided.
    """
    text = summary.strip()
    if not text:
        return False
    if text.startswith(("{", "[", "```")):
        return False
    # Words in any script, not ASCII ones: this used to count `[A-Za-z0-9]{2,}`, which finds nothing in a Russian,
    # Greek, Arabic, Chinese or Japanese summary, so a thread in those languages could never be compacted (every
    # attempt cost two model calls and stored nothing). Chinese and Japanese have no spaces, so a run of thirty
    # letters counts for as much as six words.
    words = re.findall(r"[^\W_]{2,}", text)
    if len(words) < 6 and sum(len(word) for word in words) < 30:
        return False
    letters = sum(char.isalpha() for char in text)
    if letters / len(text) < 0.45:
        return False
    structural = sum(char in "{}[]<>|" for char in text)
    if structural / len(text) > 0.05:
        return False
    pairs = Counter(
        text[i:i + 2]
        for i in range(len(text) - 1)
        if not text[i].isspace() and not text[i + 1].isspace()
    )
    if pairs and pairs.most_common(1)[0][1] > max(12, len(text) // 20):
        return False
    return True


@dataclass(frozen=True)
class CompactionPlan:
    """The turns one compaction would replace, and the summary it builds on."""

    older: list[dict[str, Any]]
    recent: list[dict[str, Any]]
    previous: ConversationCompaction | None
    keep_turns: int

    @property
    def compactable(self) -> int:
        return len(self.older)

    @property
    def covers_through(self) -> float:
        """The last replaced turn's timestamp; zero when nothing is replaced."""
        if not self.older:
            return 0.0
        return float(self.older[-1].get("created_at") or 0.0)


class CompactionService:
    """The stored summaries for each conversation, and the plan to add one.

    Deliberately knows nothing about models: `plan` says what to summarise,
    `create` stores what a model wrote, and the executor is the only caller that
    can do the middle part. That split is what lets the plan be tested without a
    provider and keeps the model call out of a read path.
    """

    def __init__(self, conn: sqlite3.Connection, goals: GoalService) -> None:
        self._db = conn
        self.goals = goals

    @staticmethod
    def _row_to_compaction(row: sqlite3.Row) -> ConversationCompaction:
        return ConversationCompaction(
            id=row["id"],
            conversation_id=row["conversation_id"],
            summary=row["summary"],
            turns=int(row["turns"]),
            covers_through=row["covers_through"],
            created_at=row["created_at"],
        )

    def latest(self, conversation_id: str) -> ConversationCompaction | None:
        row = self._db.execute(
            "SELECT id, conversation_id, summary, turns, covers_through, created_at "
            "FROM conversation_compactions WHERE conversation_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        return None if row is None else self._row_to_compaction(row)

    def plan(
        self,
        conversation_id: str,
        keep_turns: int,
        *,
        exclude_goal_id: str | None = None,
    ) -> CompactionPlan:
        """What a compaction of this thread would cover right now.

        Only turns newer than the latest summary are candidates: anything older
        is already represented by that summary. The newest `keep_turns` of those
        are left verbatim, because a follow-up is about the recent exchange, and
        a summary of what was just said is strictly worse than what was said.

        Empty `older` means there is nothing to do: either the thread is short,
        or the latest summary already covers everything but the kept turns.
        """
        keep = max(1, int(keep_turns))
        latest = self.latest(conversation_id)
        history = self.goals.turn_history(
            conversation_id,
            MAX_COMPACTION_TURNS,
            exclude_goal_id=exclude_goal_id,
        )
        if latest is not None:
            history = [
                turn for turn in history
                if float(turn.get("created_at") or 0) > latest.covers_through
            ]
        if len(history) <= keep:
            return CompactionPlan([], history, latest, keep)
        return CompactionPlan(history[:-keep], history[-keep:], latest, keep)

    def should_auto(self, plan: CompactionPlan) -> bool:
        """Whether auto-compaction should spend a model call on this plan.

        A batch floor, not `compactable > 0`: with a floor of one, the first turn
        past the keep line would compact on every message forever. Four turns is
        the smallest batch that is worth a call and still small enough that a
        forgotten summary never falls far behind.
        """
        return plan.compactable >= AUTO_MIN_OLD_TURNS

    def create(
        self,
        conversation_id: str,
        summary: str,
        turns: int,
        covers_through: float,
    ) -> ConversationCompaction:
        """Store one summary, replacing nothing: the newest row is the live one.

        Keeping the earlier rows is what makes undo a delete of the newest row
        rather than a reconstruction of the old state. It also leaves an audit
        trail of how a thread was compacted, without a second table for it.
        """
        clean = summary.strip()
        if not summary_is_usable(clean):
            raise ValueError("a compaction summary must be usable prose")
        record = ConversationCompaction(
            id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            summary=clean[:MAX_SUMMARY_CHARS],
            turns=max(0, int(turns)),
            covers_through=max(0.0, float(covers_through)),
            created_at=time.time(),
        )
        self._db.execute(
            "INSERT INTO conversation_compactions "
            "(id, conversation_id, summary, turns, covers_through, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (record.id, record.conversation_id, record.summary, record.turns,
             record.covers_through, record.created_at),
        )
        self._db.commit()
        return record

    def undo(self, conversation_id: str) -> bool:
        """Forget the newest summary; True when there was one to forget.

        Not a cascade and not a rewrite of any turn: the raw rows were never
        touched, so undo is exactly the statement that the summary stopped
        being the live reading.
        """
        latest = self.latest(conversation_id)
        if latest is None:
            return False
        self._db.execute(
            "DELETE FROM conversation_compactions WHERE id = ?", (latest.id,)
        )
        self._db.commit()
        return True

    def is_busy(self, conversation_id: str) -> bool:
        """Whether a turn in this thread is still being answered.

        A manual compaction is refused while one is: the summary would be built
        from rows that are still changing, and the answer being written would be
        the one turn the summary could never include.
        """
        row = self._db.execute(
            "SELECT 1 FROM goals WHERE conversation_id = ? "
            "AND status IN ('PLANNING', 'RUNNING') LIMIT 1",
            (conversation_id,),
        ).fetchone()
        return row is not None

    def anchor_goal_id(self, conversation_id: str) -> str | None:
        """The newest turn in the thread, for a manual compaction's model call.

        A compaction has no goal of its own, and `run_agent` books a call against
        one. The newest turn is the honest host: it is the row whose thread this
        is, and the call is about that thread. None means there is no turn to
        compact, which the caller reports rather than inventing a goal.
        """
        row = self._db.execute(
            "SELECT id FROM goals WHERE conversation_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        return None if row is None else str(row["id"])

    def status(
        self,
        conversation_id: str,
        settings: SettingsService,
        *,
        exclude_goal_id: str | None = None,
    ) -> CompactionStatus:
        """What the Session drawer shows, from the same plan a compaction uses."""
        keep = settings.get_int("session_compact_keep_turns")
        latest = self.latest(conversation_id)
        plan = self.plan(conversation_id, keep, exclude_goal_id=exclude_goal_id)
        total = self._count_turns(conversation_id)
        return CompactionStatus(
            conversation_id=conversation_id,
            latest=latest,
            turns=total,
            compactable=plan.compactable,
            keep_turns=keep,
            auto=settings.get_int("session_compact_auto") == 1,
        )

    def _count_turns(self, conversation_id: str) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) AS n FROM goals WHERE conversation_id = ? AND mode = 'chat'",
            (conversation_id,),
        ).fetchone()
        return 0 if row is None else int(row["n"])

    def build_prompt(self, plan: CompactionPlan) -> str:
        """The bounded transcript a model is asked to summarise.

        Built newest-first while fitting the budget, then reversed: when a plan
        is larger than the budget, the recent turns are the ones worth keeping
        verbatim, and the prior summary already carries the older decisions.
        """
        lines = [
            "Summarise the earlier part of this conversation for a model that "
            "will continue it. Keep the user's requests, decisions, constraints, "
            "paths and commands that still matter, and anything left unfinished. "
            "Do not add advice, and do not invent facts that are not in the "
            "transcript. Reply with 2-5 plain sentences only: no JSON, braces, "
            "code fences, headings, bullets or markdown. Begin with a sentence.",
        ]
        if plan.previous is not None and plan.previous.summary:
            lines.append("Summary so far:\n" + plan.previous.summary)
        chunks: list[str] = []
        size = 0
        for turn in reversed(plan.older):
            prompt = str(turn.get("prompt") or "").strip()
            reply = str(turn.get("reply") or "").strip() or "(no answer)"
            chunk = f"User: {prompt[:600]}\nAssistant: {reply[:900]}"
            if size + len(chunk) > MAX_COMPACT_INPUT_CHARS:
                break
            chunks.append(chunk)
            size += len(chunk)
        lines.extend(reversed(chunks))
        return "\n\n".join(lines)


COMPACTION_SYSTEM_PROMPT = (
    "You maintain a rolling summary of a long chat thread. You are not answering "
    "the user and you have no tools. Preserve what the thread needs to continue "
    "coherently: what was asked, what was decided, what was changed, and what is "
    "still open. Omit pleasantries and repetition. Reply in 2-5 plain sentences, "
    "beginning with a sentence. Never reply with JSON, braces, a code fence, a "
    "heading, a bullet or markdown."
)
