"""Reading what a model replied: the JSON in it, and the prompt that asks again.

Pure functions and constants, moved out of `engine/executor.py` (audit of 2026-09-29, 5.2) because nothing
here needs the orchestrator, the database or a provider — it is text in, a document (or a reason) out — and
because a parser that has had to grow to read what small local models really write (H4) is easier to test,
and to trust, on its own. `engine.executor` re-exports every public name, so `from engine.executor import
extract_json` still resolves.

`extract_json` is the one entry point. Its rules, and why each exists, are in `docs/04` §4 ("Reading a
reply"); the tests are `tests/test_extract_json.py`, with real captures in `tests/fixtures/model_replies/`.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Sequence
from typing import Any

# The keys each role's JSON contract carries (`engine/default_prompts.py`, docs/04 §4), so that when
# a reply holds more than one object the one the role asked for can be told from an example, an
# aside or a scrap of reasoning. A *preference*, not a requirement: a reply with none of them is still
# returned, and the role reports what it is missing in its own words.
REPLY_KEYS: dict[str, tuple[str, ...]] = {
    "librarian": ("summary", "files", "symbols", "reads", "searches", "git", "run", "enough", "test_command"),
    "design": ("applies",),
    "planner": ("steps", "consult", "reads", "searches"),
    "fixer": ("files",),
    "verifier": ("verdict", "argv"),
    "critic": ("decision", "reasons", "run_command"),
    "scribe": ("commit_message", "summary"),
}

# The one array a role's contract wraps in an object, and the key every entry of it must carry. A reply that is
# that array with nothing around it is unambiguous, so it is read as the contract's object (`coerce_object`).
BARE_ARRAY_KEYS: dict[str, tuple[str, str]] = {
    "fixer": ("files", "path"),
    "planner": ("steps", "title"),
}

# Reasoning models put their thinking in a tag before the answer, and the thinking mentions braces.
_THINK_BLOCK = re.compile(r"<(think|thinking|reasoning)\b[^>]*>.*?</\1\s*>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN = re.compile(r"<(think|thinking|reasoning)\b[^>]*>", re.IGNORECASE)
_MAX_REPAIR_CHARS = 1_000_000


# What follows the closing delimiter of a value: the next element, or the end of the container.
_VALUE_END = re.compile(r"\s*[,}\]]")


def _triple_end(text: str, i: int) -> int:
    """`text[i:i+3]` is `\"\"\"`: the index just past the delimiter that closes it (or the end).

    Python's triple-quoted string is what a small model reaches for when it writes a file's content as a
    JSON value, and the content is often Python with triple-quoted docstrings of its own. So the closing
    delimiter is the first one that *ends a value* — `,`, `}` or `]` follows it — not merely the first one.
    Valid JSON never holds `\"\"\"` outside a string, so nothing that already parsed is affected.
    """
    j = i + 3
    while True:
        j = text.find('"""', j)
        if j == -1:
            return len(text)
        if _VALUE_END.match(text, j + 3):
            return j + 3
        j += 1


def _skip_string(text: str, i: int) -> int:
    """`text[i]` opens a string with `"` or `'`: the index just past its closing quote (or the end)."""
    quote = text[i]
    if text.startswith('"""', i):
        return _triple_end(text, i)
    i += 1
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == quote:
            return i + 1
        i += 1
    return len(text)


def _balanced_end(text: str, start: int) -> int | None:
    """The index just past the bracket that closes the one at `start`, or None if it never closes."""
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch in "\"'":
            i = _skip_string(text, i)
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _strip_comments_and_trailing_commas(text: str) -> str:
    """Remove `//` and `/* */` comments and commas before a closer, leaving strings untouched."""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "\"'":
            end = _skip_string(text, i)
            out.append(text[i:end])
            i = end
        elif text.startswith("//", i):
            while i < len(text) and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            close = text.find("*/", i + 2)
            i = len(text) if close == -1 else close + 2
        elif ch == ",":
            j = i + 1
            while j < len(text) and text[j] in " \t\r\n":
                j += 1
            if j < len(text) and text[j] in "}]":
                i += 1  # a trailing comma: dropped
            else:
                out.append(ch)
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _triple_quoted_to_json(text: str) -> str:
    """Each `\"\"\"...\"\"\"` value as the JSON string with the same characters, outside ordinary strings.

    The text between the delimiters is taken as written — no escape is interpreted — because the model wrote
    the content as it should appear in the file. A triple-quoted string that never ends a value is left as
    it is, and the parse that follows refuses it.
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "\"'":
            end = _skip_string(text, i)
            if text.startswith('"""', i) and end - i >= 6 and text[end - 3:end] == '"""':
                out.append(json.dumps(text[i + 3:end - 3]))
            else:
                out.append(text[i:end])
            i = end
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _pythonish(text: str) -> str:
    """JSON's `true`/`false`/`null` as Python's, outside strings, so `ast.literal_eval` can read them."""
    out: list[str] = []
    i = 0
    swaps = {"true": "True", "false": "False", "null": "None"}
    while i < len(text):
        ch = text[i]
        if ch in "\"'":
            end = _skip_string(text, i)
            out.append(text[i:end])
            i = end
        elif ch.isalpha():
            j = i
            while j < len(text) and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word = text[i:j]
            out.append(swaps.get(word, word))
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _load_repaired(span: str) -> Any:
    """`span` as JSON after the repairs a model's near-misses need, or raise ValueError.

    In order of how little they change: comments and trailing commas, then Python's spelling of the
    same document (single quotes, `None`, `True`) via `ast.literal_eval`, which reads literals and
    executes nothing. Also read: raw control characters in strings, and Python triple-quoted values
    (both found in what a 1.5B local model really wrote, docs/08).
    """
    if len(span) > _MAX_REPAIR_CHARS:
        raise ValueError("the reply is too large to repair")
    cleaned = _triple_quoted_to_json(_strip_comments_and_trailing_commas(span))
    try:
        # `strict=False`: a raw newline or tab inside a string is how a model writes a file's content, and
        # the only reading of it is the character it is.
        return json.loads(cleaned, strict=False)
    except ValueError:
        pass
    try:
        value = ast.literal_eval(_pythonish(cleaned))
    except (ValueError, SyntaxError, MemoryError, RecursionError) as exc:
        raise ValueError(f"invalid JSON that could not be repaired ({exc})") from exc
    if not isinstance(value, (dict, list)):
        raise ValueError("invalid JSON that could not be repaired")
    return value


def _close_truncated(text: str) -> Any:
    """A reply cut off by `max_tokens`: keep what was finished, and drop the element that was not.

    Nothing is invented and no string is ever closed. A value the model was in the middle of writing is
    dropped *with the element it belongs to* — the last array item, or the last key of an object — and
    everything before it is kept. Closing an open string instead would turn a half-written value into a
    complete-looking one, which for a file's `content` is a truncated file written as if finished; so
    `extract_json` only calls this when the role has said that dropping a tail is safe for it
    (`REPLY_TOLERATES_TRUNCATION`, which excludes the fixer). The role's own contract check then decides
    whether what is left is enough, and the re-ask is what asks again if it is not.
    """
    stack: list[tuple[str, int]] = []  # (closer, index just after the opener)
    last_comma: dict[int, int] = {}    # depth -> index of the latest comma directly inside that depth
    complete_at = -1                   # the last index at which every open container held only whole values
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "\"'":
            end = _skip_string(text, i)
            if end > len(text) or text[end - 1] != ch or end - 1 == i:
                break  # the string never closed: the reply ends inside it
            i = end
            continue
        if ch in "{[":
            stack.append(("}" if ch == "{" else "]", i + 1))
        elif ch in "}]":
            if not stack:
                break
            stack.pop()
            for depth in [d for d in last_comma if d > len(stack)]:
                del last_comma[depth]
            if not stack:
                complete_at = i + 1
        elif ch == ",":
            last_comma[len(stack)] = i
        i += 1
    if not stack:
        return _load_repaired(text[:complete_at] if complete_at > 0 else text)
    # A reply that ends on a *closed* container and merely lacks the outer closers is complete: nothing
    # was half-written. Anything else at the tail — a string, a number, `tru` — may be unfinished.
    closers_all = "".join(closer for closer, _ in reversed(stack))
    if text.rstrip()[-1:] in ("}", "]"):
        try:
            return _load_repaired(text + closers_all)
        except ValueError:
            pass
    # Drop the partial element: cut at the last comma directly inside the innermost open array (or, with
    # no array open, the innermost object); with no comma there, the partial element was the first.
    arrays = [depth for depth, (closer, _) in enumerate(stack, 1) if closer == "]"]
    depth = arrays[-1] if arrays else len(stack)
    cut = last_comma.get(depth, stack[depth - 1][1])
    keep = text[:cut].rstrip().rstrip(",")
    closers = "".join(closer for closer, _ in reversed(stack[:depth]))
    try:
        return _load_repaired(keep + closers)
    except ValueError as exc:
        raise ValueError("invalid JSON: the reply ends before the document does") from exc


# The roles for which dropping the unfinished tail of a cut-off reply is safe. Not the fixer: its reply is
# file contents, and a document that stops half-way is a half-written file, not a shorter list.
REPLY_TOLERATES_TRUNCATION: frozenset[str] = frozenset(
    {"librarian", "design", "planner", "verifier", "critic", "scribe"}
)


def extract_json(raw: str, expect: Sequence[str] = (), *, repair_truncation: bool = False) -> Any:
    """The JSON document a model's reply contains, or `ValueError` saying why there is none.

    Models wrap the answer in a code fence, precede it with prose, or — the reasoning ones — with a
    `<think>` block that mentions braces; they put an example object before the real one, leave a
    trailing comma, write single quotes and `None`, or run out of tokens mid-document. This was "first
    `{` or `[` to last `}` or `]`, then `json.loads`", which handled the first two and none of the rest
    (audit of 2026-09-29, H4).

    So every top-level object or array in the reply is read (each one after the repairs a near-miss
    needs), and the one the role asked for is chosen: the last dict carrying any of `expect` (the role's
    contract keys, `REPLY_KEYS`), else the last dict, else the last list. Last, because a model states its
    answer after its examples; a dict over a list, because a bare `[1]` in the prose is a footnote.

    A reply that stops before its document does is refused unless `repair_truncation` is set (see
    `REPLY_TOLERATES_TRUNCATION` for who may set it).
    """
    text = (raw or "").lstrip("\ufeff").strip()
    if not text:
        raise ValueError("the reply was empty")
    stripped = _THINK_BLOCK.sub(" ", text)
    if stripped.strip() == "" or not any(ch in stripped for ch in "{["):
        # Either nothing is left after removing the reasoning, or the reasoning never closed.
        opened = _THINK_OPEN.search(stripped)
        if opened and "{" not in stripped[: opened.start()] and "[" not in stripped[: opened.start()]:
            # An unclosed reasoning tag: whatever JSON follows it is the answer that was meant.
            stripped = stripped[opened.end():]
    if not any(ch in stripped for ch in "{["):
        if _THINK_OPEN.search(text):
            raise ValueError("the reply is only unfinished reasoning: there is no JSON in it")
        raise ValueError("there is no JSON in the reply")

    decoder = json.JSONDecoder()
    found: list[Any] = []
    first_problem: str | None = None
    i = 0
    while i < len(stripped):
        ch = stripped[i]
        if ch not in "{[":
            i += 1
            continue
        try:
            value, end = decoder.raw_decode(stripped, i)
            found.append(value)
            i = end
            continue
        except ValueError as exc:
            problem = str(exc)
            if first_problem is None:
                first_problem = f"invalid JSON: {problem}"
        end_at = _balanced_end(stripped, i)
        if end_at is None:
            # It never closes: the reply was cut off, and this is the last thing in it.
            if repair_truncation:
                try:
                    found.append(_close_truncated(stripped[i:]))
                except ValueError:
                    pass
            else:
                # More specific than the parser's own complaint, and what the re-ask should quote.
                first_problem = f"invalid JSON: the reply ends before the document does ({problem})"
            break
        try:
            found.append(_load_repaired(stripped[i:end_at]))
        except ValueError:
            pass  # `first_problem` already holds why the plain parse refused it
        # Skip the whole span either way: a span that could not be read must not have its
        # *inner* objects returned in its place.
        i = end_at

    containers = [v for v in found if isinstance(v, (dict, list))]
    if not containers:
        raise ValueError(first_problem or "there is no JSON in the reply")
    wanted = set(expect)
    dicts = [v for v in containers if isinstance(v, dict)]
    with_keys = [d for d in dicts if wanted and wanted & d.keys()]
    if with_keys:
        return with_keys[-1]
    if dicts:
        return dicts[-1]
    return containers[-1]


_REPAIR_QUOTE_CHARS = 3000


def _repair_prompt(user_prompt: str, problem: str, previous: str) -> str:
    """The original task, unchanged, followed by what was wrong with the reply and what it said.

    The model is shown its own reply because a small model that is told only "invalid JSON" tends to
    produce a different wrong answer; shown the reply and the parser's reason, it usually fixes the
    slip. Reasoning blocks are dropped from the quote (they are the bulk of a reasoning model's reply
    and none of what was wrong), and a long reply is quoted by its head and tail.
    """
    quoted = _THINK_BLOCK.sub(" ", previous).strip()
    if len(quoted) > _REPAIR_QUOTE_CHARS:
        head = quoted[: _REPAIR_QUOTE_CHARS * 3 // 5]
        tail = quoted[-_REPAIR_QUOTE_CHARS * 2 // 5:]
        quoted = f"{head}\n… [{len(quoted) - len(head) - len(tail)} characters left out] …\n{tail}"
    return (
        f"{user_prompt}\n\n---\n"
        f"Your previous reply could not be used: {problem}.\n"
        "Your previous reply was:\n<<<\n"
        f"{quoted}\n>>>\n"
        "Reply again with the corrected JSON document only: no prose, no code fence, no thinking out loud."
    )


def coerce_object(role: str, parsed: Any) -> dict[str, Any]:
    """`parsed` as the JSON object a role's contract asks for, or `ValueError` saying what it was instead.

    Every role's contract is one object, and `extract_json` returns a list when a reply holds no object
    (it cannot know the contract) — which used to escape as `AttributeError` from the parsers downstream,
    reaching a user as `internal_error`: Codify blamed for a model's slip.

    One list is read rather than refused: the contract's own array with nothing around it. Qwen2.5-1.5B
    answered the fixer with `[{"path": ...}]` instead of `{"files": [...]}`, twice in a row when asked again.
    The contract names the key and every entry carries the field only that array's entries carry, so wrapping
    it invents nothing. Anything less plain — an empty list, a mix, entries of another shape — is refused.
    """
    if isinstance(parsed, dict):
        return parsed
    shape = BARE_ARRAY_KEYS.get(role)
    if (
        shape is not None and isinstance(parsed, list) and parsed
        and all(isinstance(entry, dict) and shape[1] in entry for entry in parsed)
    ):
        return {shape[0]: parsed}
    kind = "a list" if isinstance(parsed, list) else type(parsed).__name__
    raise ValueError(f"the reply must be a JSON object, not {kind}")
