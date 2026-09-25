"""Fail when a module uses syntax the declared minimum Python cannot parse.

`pyproject.toml` declares `requires-python = ">=3.10"`, the README promises 3.10+,
and the Tauri shell boots the engine with whatever `python3` is on PATH. So a
3.12-only construct is not a style question: on 3.10/3.11 `import engine.executor`
raises `SyntaxError` before a single test can run, and the desktop window opens with
no engine behind it.

`ast.parse(..., feature_version=(3, 10))` looks like the right guard and is not one.
The feature-version switch does not gate PEP 701 f-strings, so
`f"{f'({d['k']})'}"` — which 3.10 and 3.11 reject outright — parses cleanly under a
3.10 feature version, and the mistake ships. This module tokenises instead, and
enforces the two PEP 701 rules that were hard syntax errors before 3.12:

1. the pre-3.12 scanner ends a string literal at the first occurrence of *that
   literal's own delimiter sequence*, so a literal inside an f-string's
   replacement field may not start with the enclosing delimiter:
   `f"{d["k"]}"` (a `"` literal inside a `"` f-string) is a SyntaxError, and so
   is the triple-quoted form `f'''{d['''k''']}'''` — but `f'''{d["k"]}'''` is
   *legal*, because a lone `"` never closes a three-quote literal. The rule is
   about the delimiter sequence, not the quote character;
2. a backslash may not appear in an f-string's expression part
   (`f"{'\\n'.join(x)}"`).

Both became legal in 3.12, which is where PEP 701 landed. The two rules are
pinned to the floor by measurement rather than recall: every case in
`test_the_checker_rejects_the_python_312_only_forms` and its "allows" twin was
compiled on 3.10 before it was written down.

The guard needs one strategy per interpreter era, because the two eras fail
differently:

- On 3.12+ the FSTRING_* tokens exist and the tokenizer pass above reads inside
  f-strings directly.
- On 3.10/3.11 those tokens do not exist — a tokenizer pass would silently see
  nothing and pass everything (its own self-tests catch exactly that). There the
  interpreter's own parser is the oracle: PEP 701 forms are grammar-level
  SyntaxErrors below 3.12 no matter what, and `feature_version=(3, 10)` further
  rejects 3.11-only syntax. Parsing *is* the check, so the guard parses.

The audience is worth stating: on 3.10/3.11 a bad construct already fails loudly at
import time. What nothing catches is the same mistake made *on the 3.12+ interpreter
a developer actually runs*, where it parses fine and only breaks the users this
project claims to support. That is what this test is for.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import ast
import io
import token
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Every Python file the project ships — not only engine/. A test or helper module
# that needs 3.12 breaks `make test` on the minimum interpreter just as hard.
SCAN_DIRS = ("engine", "tests", "scripts")

# The FSTRING_* token types exist only on 3.12+, which is also the only interpreter
# that can tokenise PEP 701 f-strings at all.
FSTRING_START = getattr(token, "FSTRING_START", None)
FSTRING_END = getattr(token, "FSTRING_END", None)
FSTRING_MIDDLE = getattr(token, "FSTRING_MIDDLE", None)


def _delimiter(text: str) -> str:
    """The quote sequence a string token is delimited by, or "" for anything else.

    One quote or three — the length matters, because a lone `"` cannot end a
    three-quote literal. Handles prefixes (`f`, `r`, `rb`, …). An empty answer means
    the token is not a quoted literal — a middle chunk of f-string literal text, or
    an operator.
    """
    stripped = text.lstrip("rRbBuUfF")
    for quote in ("'", '"'):
        for run in (quote * 3, quote):
            if stripped.startswith(run):
                return run
    return ""


def _collides(inner: str, outer: str) -> bool:
    """Would a literal delimited by `inner` end an `outer`-delimited f-string early?

    Python 3.11 and older scan an f-string as one string token, so the literal ends
    at the first occurrence of *its own* delimiter sequence — anywhere, including
    inside a replacement field. A single quote collides with a single quote of the
    same kind, a three-quote delimiter with the same three-quote delimiter, and a
    three-quote delimiter with a single one (its first quote ends the f-string) —
    but a lone quote never collides with a three-quote delimiter. Measured on 3.10,
    not inferred: `f'''{d["k"]}'''` parses, `f'''{d['''k''']}'''` does not.
    """
    return bool(inner) and bool(outer) and inner.startswith(outer)


def _collision(filename: str, line: int, outer: str, inner: str) -> str:
    return (
        f"{filename}:{line}: text delimited by {inner} sits inside an f-string "
        f"delimited by {outer} — Python 3.11 and older end the f-string at that "
        f"quote sequence (PEP 701, accepted in 3.12)"
    )


def _backslash(filename: str, line: int) -> str:
    return (
        f"{filename}:{line}: a backslash appears inside an f-string's expression part — "
        f"Python 3.11 and older reject this (PEP 701, accepted in 3.12)"
    )


def min_version_problems(source: str, filename: str = "<source>") -> list[str]:
    """Every place in `source` that only Python 3.12+ can parse. Empty when clean."""
    if FSTRING_START is None:
        # 3.11 or older. This interpreter's own grammar is the floor's grammar:
        # PEP 701 forms are SyntaxErrors here unconditionally, and
        # feature_version=(3, 10) also rejects any 3.11-only syntax. The old
        # tokenizer strategy would see nothing (no FSTRING_* tokens) and pass
        # everything — parsing is the only honest check on this era.
        try:
            ast.parse(source, feature_version=(3, 10))
        except SyntaxError as exc:
            line = exc.lineno if exc.lineno is not None else 0
            return [
                f"{filename}:{line}: {exc.msg} — this interpreter (< 3.12) rejects "
                f"syntax the project ships; the declared minimum is 3.10"
            ]
        return []

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
        return [f"{filename}: cannot tokenise ({exc})"]

    problems: list[str] = []
    # The delimiter of each open f-string, outermost first: a token is only
    # restricted by the f-string it is directly inside.
    enclosing: list[str] = []

    for tok in tokens:
        if tok.type == FSTRING_START:
            delimiter = _delimiter(tok.string)
            # A nested f-string may not collide with its parent's delimiter either
            # (`f"{f"{x}"}"` is a SyntaxError below 3.12) — unless the parent is
            # triple-quoted, where a lone quote is harmless.
            if enclosing and _collides(delimiter, enclosing[-1]):
                problems.append(_collision(filename, tok.start[0], enclosing[-1], delimiter))
            enclosing.append(delimiter)
        elif tok.type == FSTRING_END:
            if enclosing:
                enclosing.pop()
        elif tok.type == FSTRING_MIDDLE:
            # Literal text: escapes and both quote characters are legal here on every
            # supported version. Only the expression parts are restricted.
            continue
        elif enclosing:
            # Between an f-string's braces, and nowhere else.
            if "\\" in tok.string:
                problems.append(_backslash(filename, tok.start[0]))
            elif tok.type == token.STRING:
                delimiter = _delimiter(tok.string)
                if _collides(delimiter, enclosing[-1]):
                    problems.append(
                        _collision(filename, tok.start[0], enclosing[-1], delimiter)
                    )

    return problems


def _read(path: Path) -> str:
    """Read a module the way Python does — honouring a PEP 263 coding cookie."""
    with tokenize.open(path) as handle:
        return handle.read()


def _python_files() -> list[Path]:
    """Every shipped module, in a stable order."""
    files: list[Path] = []
    for name in SCAN_DIRS:
        files.extend(sorted((ROOT / name).glob("*.py")))
    return files


def scan_tree() -> dict[str, list[str]]:
    """Repo-relative path -> problems, for every module that has any."""
    offenders: dict[str, list[str]] = {}
    for path in _python_files():
        relative = str(path.relative_to(ROOT))
        problems = min_version_problems(_read(path), relative)
        if problems:
            offenders[relative] = problems
    return offenders


class MinPythonSyntaxTest(unittest.TestCase):
    def test_every_shipped_module_parses_on_the_declared_minimum(self) -> None:
        offenders = scan_tree()
        self.assertEqual(
            [],
            [f"{path} — {problem}" for path, problems in sorted(offenders.items()) for problem in problems],
            "this syntax needs Python 3.12+, but pyproject.toml declares '>=3.10' "
            "and the README promises 3.10+ — on the minimum interpreter the engine "
            "fails to import and `make test` cannot run at all",
        )

    def test_the_scan_reaches_the_modules_that_matter(self) -> None:
        """A glob that quietly matches nothing must fail rather than pass."""
        scanned = {str(path.relative_to(ROOT)) for path in _python_files()}
        self.assertIn("engine/executor.py", scanned)
        self.assertIn("engine/app.py", scanned)
        self.assertGreaterEqual(len(scanned), 20, "the scan covered almost nothing")

    def test_the_checker_rejects_the_python_312_only_forms(self) -> None:
        # Every case below was compiled on 3.10 first: these are the forms the
        # floor rejects, not the forms it looks like it should reject.
        cases = [
            # a nested f-string reusing the outer quote character
            'value = f"{f\'({d[\'k\']})\'}"',
            # the same reuse in a directly nested f-string
            'value = f"{f"{x}"}"',
            # a plain dict subscript in the expression using the outer quote
            'value = f"{d["k"]}"',
            # the triple-quoted form of that same collision
            'value = f"""{d["""k"""]}"""',
            # a backslash inside the expression part
            "value = f\"{'\\n'.join(x)}\"",
        ]
        for source in cases:
            with self.subTest(source=source):
                self.assertTrue(
                    min_version_problems(source, "x.py"),
                    f"the guard missed 3.12-only syntax: {source}",
                )

    def test_the_checker_accepts_what_the_minimum_python_allows(self) -> None:
        # Each of these is legal on 3.10 and must stay legal here.
        allowed = [
            'value = f"{d[\'k\']}"',  # a different quote character inside the field
            "value = f'{d[\"k\"]}'",
            'value = f"a\\nb"',  # an escape in the literal text, not the expression
            'value = f"{x} {y}"',
            "value = f'{f\"{x}\"}'",  # a nested f-string with a different delimiter
            # A lone quote cannot close a triple-quoted f-string early. Both of
            # these parse on 3.10, so flagging them would be a false alarm — and
            # the first one is a natural way to write a dict lookup in a long
            # string, exactly what a developer on 3.14 would reach for.
            'value = f"""{d["k"]}"""',
            'value = f"""{f"{x}"}"""',
            'value = "plain"',
            "letters = r'\\d+'",
        ]
        for source in allowed:
            with self.subTest(source=source):
                self.assertEqual([], min_version_problems(source, "x.py"), source)

    def test_a_problem_names_the_file_and_the_line(self) -> None:
        source = "x = 1\ny = f\"{f'({d['k']})'}\"\n"
        problems = min_version_problems(source, "engine/thing.py")
        self.assertEqual(1, len(problems), problems)
        self.assertIn("engine/thing.py:2", problems[0])


if __name__ == "__main__":
    unittest.main()
