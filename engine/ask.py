"""A question the conductor puts to the person: what it may say, and how it reads.

`ask_user` ends the run, so what it carries has to be something a window can draw as choices and a history can
replay as words. Pure and free of the pipeline, so the rules have tests that need no goal.

The question is the model's text and is shown to the person the way any answer is (as Markdown, drawn as
elements and never as HTML); the options are short single lines the window offers as buttons, and picking one
sends its text as an ordinary next message. There is nothing here a model can use to *do* anything: a question
cannot approve a plan, answer itself or reach a tool. The limits are bounds, not policy:

* at most `MAX_OPTIONS` options, each at most `MAX_OPTION_CHARS` characters, because a button is not a paragraph
  and a model that wants to offer six things wants a different question;
* never exactly one option, because one option is not a choice;
* a question of at most `MAX_QUESTION_CHARS`, so one reply cannot be the whole turn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_QUESTION_CHARS = 500
MAX_OPTIONS = 4
MAX_OPTION_CHARS = 80

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")
_BLANK_LINES = re.compile(r"\n{3,}")


class AskRefused(Exception):
    """A question the engine will not put. The message is what the model is told, and is a sentence."""


@dataclass
class Question:
    text: str
    options: list[str] = field(default_factory=list)

    def prose(self) -> str:
        """The question as words: what the turn's answer says, what the next turn's history replays, what is read aloud."""
        if not self.options:
            return self.text
        numbered = "\n".join(f"{n}. {option}" for n, option in enumerate(self.options, 1))
        return f"{self.text}\n\n{numbered}"

    def to_payload(self) -> dict[str, Any]:
        return {"text": self.text, "options": list(self.options)}


def _line(value: str) -> str:
    return _WHITESPACE.sub(" ", _CONTROL.sub("", value)).strip()


def parse_question(args: dict[str, Any]) -> Question:
    """The question in a tool call's arguments, or `AskRefused` saying what to change."""
    raw = args.get("question")
    text = ""
    if isinstance(raw, str):
        # Paragraphs are kept (a question can have a sentence of context before it); runs of blank lines are not.
        text = _BLANK_LINES.sub("\n\n", _CONTROL.sub("", raw).replace("\r\n", "\n").replace("\r", "\n")).strip()
    if not text:
        raise AskRefused("`ask_user` needs `question`: one clear question for the person.")
    if len(text) > MAX_QUESTION_CHARS:
        raise AskRefused(
            f"That question is {len(text)} characters and it may be at most {MAX_QUESTION_CHARS}. "
            "Ask the one thing you need."
        )

    offered = args.get("options")
    options: list[str] = []
    if offered is not None:
        if not isinstance(offered, list) or not all(isinstance(o, str) for o in offered):
            raise AskRefused("`options` must be a list of short strings, or left out.")
        seen: set[str] = set()
        for number, entry in enumerate(offered, 1):
            option = _line(entry)
            if not option:
                continue
            if len(option) > MAX_OPTION_CHARS:
                raise AskRefused(
                    f"Option {number} is {len(option)} characters and an option may be at most "
                    f"{MAX_OPTION_CHARS}. Say it in a few words."
                )
            key = option.casefold()
            if key not in seen:
                seen.add(key)
                options.append(option)
        if len(options) > MAX_OPTIONS:
            raise AskRefused(
                f"You gave {len(options)} options and at most {MAX_OPTIONS} are allowed. Keep the ones that "
                "matter, or ask without options."
            )
        if len(options) == 1:
            raise AskRefused("Give at least two options, or none: one option is not a choice.")
    return Question(text=text, options=options)
