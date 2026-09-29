"""`CLAUDE.md`, the README, `.claude/agents/` and `.claude/commands/` must stay true.

`CLAUDE.md` is the agent-facing distillation of `CONTRIBUTING.md` and `docs/00`–`07`.
That is exactly the shape of thing that goes stale: it is a *copy* of a contract that
is maintained somewhere else, so it can only ever be as current as the last time
somebody remembered to update both. `CONTRIBUTING.md` puts the failure mode plainly —
"the docs have lied before; don't add to it" — and this module is the mechanical half
of that promise for the new files.

It parses, and never imports or executes: a doc that has drifted must fail a test, not
import a doc that happens to be runnable. Same approach, and for the same reason, as
`test_no_unguarded_spawns.py`, which freezes decision sites rather than trying to prove
their semantics.

What it guarantees:

* the invariants quoted in `CLAUDE.md` match `docs/00` §6, one for one — none added,
  none dropped, none renumbered, and none reworded into a weaker claim. The count is
  deliberately not named here: it was written as "seven" and stayed there through the
  two invariants that came after, which is the same staleness this module exists to
  catch. The sets are compared, not counted;
* every repository path any of these files names actually exists, in backticks *and*
  in the fenced layout tree;
* the numbered `docs/` files are registered everywhere registration is owed: a row
  in `docs/00` §5's document-set table, a row in `CLAUDE.md`'s depth table, and the
  `00–NN` range lines in `CLAUDE.md`'s layout tree and `CONTRIBUTING.md`. Adding
  `docs/10` meant editing four places by hand, and hand-edited tables are the exact
  thing that silently drifts — so the directory itself is now the source of truth
  and  the tables are compared against it, in both directions (a file with no row,
  and a row with no file);
* the README points at the same set. It is the one place a reader *outside* this
  repository is sent, and it linked five of the eleven docs, so six
  specifications — the settings app, the security spec, the spawn guard, the
  benchmarks, the workspace shell, the architecture overview — were reachable
  only by a reader who already knew they existed. Every `docs/` link it makes
  must also resolve, which is the only check here covering the demo assets;

* `CLAUDE.md` still says that `CONTRIBUTING.md` and `docs/` outrank it, because a
  distillation that can silently outrank its source is the failure mode of the idea;
* the agent and command files carry the frontmatter Claude Code reads, and their
  `name` matches their filename (an agent whose name disagrees with its path is
  selected by the wrong key);
* `.claude/.cc-writes/` is ignored by this repository rather than by one machine's
  global ignore, which is the only reason it would not be committed by accident.

`KNOWN_DANGLING` is empty. It once held the benchmark doc `docs/08-benchmarks.md`
that `benchmarks/` cited before anybody wrote it, and the docstring above claimed
the exemption "expires the moment the doc is written" — so the test that checks
that claim is the reason the entry is gone rather than merely permitted to
linger. An empty allowlist is a better artefact than a satisfied one: the next
defect is named deliberately, and this test says so the day it stops being true.

The word-overlap bar is set high (90%) *because* `CLAUDE.md` quotes these near
verbatim — there is no reason for a divergence to be small. The stopword list below is
deliberately built to keep every word that carries contractual weight: negations
(`no`, `not`, `never`) and modals of obligation (`must`, `only`, `cannot`) are
counted, because softening "Responses NEVER include raw API keys" into "should not"
is the single most dangerous edit anyone can make to an invariant — and a stopword list
that drops those words makes exactly that edit invisible to a word-overlap check.
"""

from __future__ import annotations

import re
import unittest
from collections.abc import Iterator
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = PROJECT_ROOT / "CLAUDE.md"
ARCHITECTURE = PROJECT_ROOT / "docs" / "00-codify-architecture-overview.md"

# `CLAUDE.md` lines that quote an invariant carry its source as a trailing tag, so a
# reader can jump to the owner and the test can pair the two up. The tag is what makes
# the mapping explicit rather than positional: an invariant inserted in the middle of
# `docs/00` §6 renumbers everything after it, and a positional match would silently
# compare the wrong two lines.
INVARIANT_TAG = re.compile(r"\(docs/00 §6\.(\d+)\)\*?\s*$")

# How closely the two texts must agree, scored *symmetrically*. An earlier version
# scored only `|docs ∩ copy| / |docs|`, which quietly passed the most dangerous
# mutation there is: shorten an invariant in `docs/00` and every remaining word is
# still present in the longer, stale `CLAUDE.md` line, so the score stayed at 1.0.
# A copy that says *more* than its source is not consistent with it — if `docs/00`
# names six roles and `CLAUDE.md` still teaches eight, every agent reading the copy
# learns a role set that does not exist. Dividing by `max(len(docs), len(copy))`
# makes a word missing from either side count, which is the only way the shortening
# registers.
#
# Measured against the file as written, every invariant scores 1.0 — the quotes are
# verbatim — so the bar is a floor between "identical" and "badly wrong", not a
# tolerance. Calibrated against realistic single edits: `docs/00` dropping two of the
# eight role names lands at 0.87, dropping "There is no `agents.db`" at 0.83, and
# softening "NEVER include" to "should not include" at 0.67. Each of those is a
# contract quietly turned into a wrong one, so the bar sits above all of them.
MIN_WORD_OVERLAP = 0.90

# Function words only. Negations and modals of obligation are *absent on purpose* —
# see the module docstring for why that matters more than anything else here.
STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "been", "being", "before", "but",
        "by", "did", "do", "does", "for", "from", "had", "has", "have", "if", "in",
        "into", "is", "it", "its", "of", "on", "one", "or", "own", "so", "same",
        "than", "that", "the", "their", "there", "then", "they", "them", "this", "to",
        "use", "used", "was", "were", "when", "while", "with",
    }
)

# A backticked span is treated as a repository path when it names one. The exclusions
# keep prose that happens to sit in code spans from being checked as a file: spans with
# whitespace (`make check`, `python3 -m engine`), HTTP routes and bindings
# (`PUT /settings/agents/{role}`, `127.0.0.1`), home-relative paths that are not in the
# tree (`~/.codify/codify.db`), and dotted Python identifiers rather than files
# (`SandboxService.run_command`, `validate_local_base_url`).
PATH_LIKE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")
KNOWN_SUFFIXES = (".md", ".py", ".toml", ".json", ".yml", ".yaml", ".sh", ".txt", ".lock")

# One reference in the agent-facing files is known to be dangling, and it is a real
# defect in the repository rather than in these files: `benchmarks/manifest.json`,
# `benchmarks/__init__.py` and `benchmarks/runner.py` all cite `docs/08-benchmarks.md`
# for the benchmark harness, and that doc was never written. Fixing it means writing
# the benchmark doc, which is a separate piece of work, so it is recorded here instead
# of being papered over.
#
# The entry is checked in both directions on purpose — an entry that has been fixed is
# itself a failure, so writing `docs/08-benchmarks.md` cannot leave a stale exemption
# behind. This is the allowlist pattern `test_no_unguarded_spawns.py` already uses for
# its spawn sites, for the same reason: a table entry that outlives what it names is
# how a guarantee quietly stops being checked.
KNOWN_DANGLING: frozenset[tuple[str, str]] = frozenset()


def _content_words(text: str) -> set[str]:
    """Distinct lowercase words of `text`, minus pure function words and bare numbers.

    Markdown is stripped rather than parsed: backticks, emphasis markers and the `§`
    section sign carry no contractual content, and a reader rewriting the file should
    not be able to satisfy (or break) this check by changing formatting.
    """
    cleaned = text.replace("`", "").replace("*", "").replace("§", " ")
    tokens = re.findall(r"[A-Za-z0-9_]+", cleaned.lower())
    return {t for t in tokens if t not in STOPWORDS and not t.isdigit()}


def _doc_invariants() -> dict[int, str]:
    """`{number: text}` for the numbered invariants in `docs/00` §6.

    The section is a wrapped markdown list, so an item is not one line: a numbered
    first line plus every following line indented under it. Reading one physical line
    per invariant would truncate 1, 2 and 6 mid-clause, and a check that compares
    half a contract against half a copy proves nothing.
    """
    lines = ARCHITECTURE.read_text(encoding="utf-8").splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith("## 6."))
    except StopIteration:  # pragma: no cover - the section is a documented contract
        raise AssertionError(
            f"{ARCHITECTURE.name} has no '## 6.' section; CLAUDE.md's invariant "
            "tags can no longer be paired with their source"
        ) from None

    invariants: dict[int, str] = {}
    current: int | None = None
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        match = re.match(r"^(\d+)\.\s+(.*)$", line)
        if match:
            current = int(match.group(1))
            invariants[current] = match.group(2)
        elif current is not None and line.startswith("   ") and line.strip():
            invariants[current] += " " + line.strip()
    return invariants


def _claude_invariants() -> dict[int, str]:
    """`{number: quote}` for the `CLAUDE.md` lines tagged as quoting an invariant.

    The `(docs/00 §6.N)` tag is stripped from the stored value. It is provenance —
    where to look when the two disagree — not part of the quotation, and leaving it in
    made the word comparison score a `docs` that the source cannot possibly contain.
    On the short invariants that was a seventh of the score, which is enough to fail a
    verbatim quote for the crime of citing it.
    """
    tagged: dict[int, str] = {}
    for raw in CLAUDE_MD.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        match = INVARIANT_TAG.search(line)
        if match:
            number = int(match.group(1))
            if number in tagged:
                raise AssertionError(
                    f"CLAUDE.md tags invariant {number} twice; one of them is stale"
                )
            tagged[number] = INVARIANT_TAG.sub("", line).strip()
    return tagged


def _referenced_paths(text: str) -> Iterator[str]:
    """Every span in `text` that names a path inside this repository.

    Two sources, because a path can be written in backticks *or* inside a fenced
    block, and the layout section of `CLAUDE.md` is a tree drawn in a fence where the
    indentation carries the directory:

        engine/         Python: orchestration, providers, sandbox, git, db, trace
          models.py     ROLES + ROLE_JOB + ROLE_TIMING — the one place roles live

    Reading only backticked spans silently skipped the entire layout section — the
    block an agent is most likely to follow a path out of. A fenced token is accepted
    when it resolves to exactly one file anywhere in the tree, so a bare `models.py`
    under an `engine/` heading is still proved to exist rather than assumed.
    """
    for span in re.findall(r"`([^`\n]+)`", text):
        candidate = span.strip().rstrip(".,;:")
        if " " in candidate or not PATH_LIKE.match(candidate):
            continue
        if candidate.startswith(("/", "~")):
            continue
        if not ("/" in candidate or candidate.endswith(KNOWN_SUFFIXES)):
            continue
        # A dotted Python identifier, not a file: `LocalProvider.base_url`.
        head = candidate.rsplit("/", 1)[-1]
        if "." in head and not head.endswith(KNOWN_SUFFIXES):
            continue
        yield candidate

    for block in re.findall(r"```[a-z]*\n(.*?)```", text, re.DOTALL):
        for line in block.splitlines():
            # The first whitespace-delimited token of an indented line is the entry
            # name; the prose after it describes it and must not be scanned.
            token = line.strip().split(" ")[0].split("\t")[0]
            if not token or " " in token or not PATH_LIKE.match(token):
                continue
            if token.startswith(("/", "~", "#", "|", "-")):
                continue
            # "Names a path" is deliberately loose about the extension: a candidate is
            # anything with a directory separator or *any* dotted suffix. Requiring a
            # known extension made a misspelt one invisible — `models.pyy` was not
            # recognised as a path claim, so the typo it was went unchecked, which is
            # precisely the case this check exists for.
            if not ("/" in token or re.search(r"\.[A-Za-z0-9_]+$", token)):
                continue
            yield token


def _resolves_anywhere(referenced: str) -> bool:
    """Whether `referenced` names exactly one file, at any depth or by prefix.

    Two references cannot be checked as plain repo-relative paths. The layout block
    writes entries the way a person reads a tree — the directory on one line, the
    files indented under it without repeating it — so `models.py` means
    `engine/models.py`. And a doc is cited by its number throughout these files
    (`docs/04`, not `docs/04-engine-data-and-runtime.md`), because the number is the
    stable handle and the title changes.

    Requiring a *single* match applies only to a slash-bearing reference, which is a
    real path claim that a reader will follow literally. A bare basename is a weaker
    claim — `models.py` exists here, and the tree above it says which one — and common
    basenames are legitimately ambiguous: `models.py` and `trace.py` are not unique in
    this repository, so demanding uniqueness there reported a file that demonstrably
    exists as a defect. Existence is the claim worth checking for those.
    """
    if (PROJECT_ROOT / referenced).exists():
        return True
    if "/" in referenced:
        matches = [p for p in PROJECT_ROOT.glob(f"{referenced}*") if p.is_file()]
        return len(matches) == 1
    return any(p.is_file() for p in PROJECT_ROOT.glob(f"**/{referenced}"))


def _tracked_doc_files() -> list[Path]:
    """The hand-written agent/command files, in a stable order."""
    found: list[Path] = []
    for sub in ("agents", "commands"):
        directory = PROJECT_ROOT / ".claude" / sub
        if directory.is_dir():
            found.extend(sorted(directory.glob("*.md")))
    return found


NUMBERED_DOC = re.compile(r"^(\d{2})-[^/]*\.md$")
CONTRIBUTING_MD = PROJECT_ROOT / "CONTRIBUTING.md"
README_MD = PROJECT_ROOT / "README.md"

# A reference the README makes *into* `docs/`, in either syntax it uses: a Markdown
# link target, or the `src`/`href` of the `<img>`/`<a>` tags the hero images and the
# demo link are written in. Both halves are needed — reading only `](` would check
# the specifications and skip the three assets the README opens with, and reading
# only the tags would find none of the prose.
#
# Fenced blocks are deliberately not scanned. The project-structure tree draws
# `docs/` as a directory with a comment beside it; that is a picture of the layout,
# not a link anybody follows, and treating it as one would be checking a claim the
# file does not make.
README_LINK = re.compile(
    r"""\]\(\s*<?(docs/[^)\s>]+)>?\s*\)     # [`docs/04`](docs/04-....md)
    |  (?:src|href)\s*=\s*["'](docs/[^"']+)["']   # <img src="docs/demo.gif">
    """,
    re.VERBOSE,
)


def _readme_doc_links() -> dict[str, int]:
    """`{target: line}` for every reference the README makes into `docs/`.

    Line numbers are kept so a failure names the line to edit rather than only the
    file, which matters in a document where the same sentence gets rewritten long
    after the defect is found. Repeated targets collapse to their first mention:
    the README legitimately cites `docs/04` from four sections, and that is not a
    defect, so counting citations would only reward a link being added twice.

    Fragments and the `<...>` form are stripped, so `(docs/04-....md#sql)` and
    `(docs/demo.webm)` both report the file they point at rather than a string
    nothing on disk can match.
    """
    links: dict[str, int] = {}
    for number, line in enumerate(README_MD.read_text(encoding="utf-8").splitlines(), start=1):
        for match in README_LINK.finditer(line):
            target = (match.group(1) or match.group(2)).split("#", 1)[0]
            links.setdefault(target, number)
    return links


def _numbered_docs() -> dict[int, str]:
    """`{number: filename}` for every numbered file in `docs/`, read from the
    directory itself.

    The directory is the source of truth *by enumeration*: a new numbered doc
    appears here the moment the file does, which is what makes the registration
    checks below able to fail. A doc that is written but not registered is
    precisely the drift this catches — and it is invisible to every other check
    in this module, because an unregistered doc is usually still referenced by
    number in prose that resolves.
    """
    docs: dict[int, str] = {}
    for path in sorted((PROJECT_ROOT / "docs").glob("*.md")):
        match = NUMBERED_DOC.match(path.name)
        if match:
            docs[int(match.group(1))] = path.name
    return docs


def _section(text: str, heading_prefix: str) -> str:
    """The body of the first `##` heading starting with `heading_prefix`, up to
    the next `##` heading. Scoping keeps a stray mention elsewhere in the file
    from satisfying a registration the real table does not carry."""
    start = text.find(heading_prefix)
    if start == -1:
        return ""
    end = text.find("\n## ", start + len(heading_prefix))
    return text[start:end if end != -1 else len(text)]


def _doc_set_table_numbers() -> set[int]:
    """Numbers with a row in `docs/00` §5's document-set table."""
    section = _section(ARCHITECTURE.read_text(encoding="utf-8"), "## 5.")
    found: set[int] = set()
    for row in section.splitlines():
        match = re.match(r"^\|\s*`(\d{2})-[^`]*\.md`\s*\|", row)
        if match:
            found.add(int(match.group(1)))
    return found


def _depth_table_numbers() -> set[int]:
    """Numbers with a row in `CLAUDE.md`'s depth table."""
    section = _section(CLAUDE_MD.read_text(encoding="utf-8"), "## Depth, on demand")
    return {
        int(match.group(1))
        for match in re.finditer(r"^\|\s*`docs/(\d{2})`\s*\|", section, re.MULTILINE)
    }


def _claudes_layout_range() -> int | None:
    """The `NN` in the layout tree's `docs/ 00–NN, below` line, or None."""
    match = re.search(r"^docs/\s+00.?(\d{2})\b", CLAUDE_MD.read_text(encoding="utf-8"), re.MULTILINE)
    return int(match.group(1)) if match else None


def _contributing_ranges() -> list[int]:
    """Every `NN` in CONTRIBUTING.md's `` `docs/00`–`NN` `` ranges."""
    text = CONTRIBUTING_MD.read_text(encoding="utf-8")
    return [int(n) for n in re.findall(r"docs/00`–`(\d{2})", text)]


class ClaudeMdContractsTest(unittest.TestCase):
    def test_claude_md_exists(self) -> None:
        # The rest of this module is about keeping one file true. If the file is
        # renamed or moved, say so as a contract break rather than as a pile of
        # "document not found" errors from the parsers above.
        self.assertTrue(
            CLAUDE_MD.is_file(),
            "CLAUDE.md is missing; the agent-facing rules and their pointer to "
            "CONTRIBUTING.md/docs went with it",
        )

    def test_every_documented_invariant_is_quoted(self) -> None:
        """Nothing in `docs/00` §6 is missing from `CLAUDE.md`."""
        invariants = _doc_invariants()
        self.assertTrue(
            invariants,
            f"no invariants parsed from {ARCHITECTURE.name} §6 — the section is empty, "
            "or the parser no longer matches it",
        )
        tagged = _claude_invariants()
        missing = sorted(set(invariants) - set(tagged))
        self.assertEqual(
            [],
            missing,
            f"CLAUDE.md does not quote invariant(s) {missing} from docs/00 §6. An "
            "agent reading only CLAUDE.md would not learn them. Quote it and add the "
            "'(docs/00 §6.N)' tag, or delete the invariant from docs/00 if it was "
            "retracted",
        )

    def test_no_invariant_is_quoted_that_does_not_exist(self) -> None:
        """`CLAUDE.md` tags no number `docs/00` §6 does not define.

        The other direction, and the one a stale file produces: `docs/00` dropped or
        renumbered an invariant, and the tag in `CLAUDE.md` now points at nothing.
        """
        invariants = _doc_invariants()
        tagged = _claude_invariants()
        extra = sorted(set(tagged) - set(invariants))
        self.assertEqual(
            [],
            extra,
            f"CLAUDE.md tags invariant(s) {extra}, which docs/00 §6 does not define "
            f"(it has {sorted(invariants)}). Either the tag is stale or docs/00 §6 "
            "was renumbered",
        )

    def test_quoted_invariants_match_their_source(self) -> None:
        """Each quote reproduces the source invariant's own words.

        This is the check that catches the *reworded* invariant — the one that is still
        numbered correctly, still present, and no longer says what it said. A softened
        guarantee ("MUST reject" becomes "should reject", "NEVER" becomes "rarely")
        keeps every sentence and drops the promise, so counting sentences or tags
        cannot see it and only the words can.
        """
        invariants = _doc_invariants()
        tagged = _claude_invariants()
        for number in sorted(set(invariants) & set(tagged)):
            source = _content_words(invariants[number])
            copy = _content_words(tagged[number])
            with self.subTest(invariant=number):
                self.assertTrue(
                    source,
                    f"invariant {number} in docs/00 §6 has no content words to compare",
                )
                self.assertTrue(
                    copy,
                    f"CLAUDE.md's quote of invariant {number} has no content words, so "
                    "it says nothing that could be compared with its source",
                )
                # Symmetric: divided by the *larger* word set, so a word present in
                # only one of the two texts counts against the score in either
                # direction. `only_in_source` is the shortened-invariant case,
                # `only_in_copy` the stale copy.
                score = len(source & copy) / max(len(source), len(copy))
                self.assertGreaterEqual(
                    score,
                    MIN_WORD_OVERLAP,
                    f"CLAUDE.md's quote of invariant {number} agrees with docs/00 §6 "
                    f"on only {score:.0%} of its words, below the "
                    f"{MIN_WORD_OVERLAP:.0%} floor "
                    f"(only in docs/00: {sorted(source - copy)}; "
                    f"only in CLAUDE.md: {sorted(copy - source)}). docs/00 §6 is the "
                    "owner: if it changed, re-quote it here; if this line is right "
                    "and docs/00 is wrong, fix docs/00 instead",
                )

    def test_claude_md_defers_to_its_sources(self) -> None:
        """The file states that `CONTRIBUTING.md` and `docs/` outrank it.

        A distillation that could silently outrank its source is the whole failure
        mode of being a distillation. This clause is what makes `CLAUDE.md` a map
        rather than a competing source of truth, and it is short enough that deleting
        it would be an easy mistake.

        Matched as two regexes over the substance rather than as fixed strings: the
        guarantee is that the file names its sources, says they win, and names itself
        as the thing to fix — not that it uses one particular phrasing to say so.
        Pinning exact wording here would only mean the next person to reword the
        sentence deletes the exemption, and the check goes quiet.
        """
        text = CLAUDE_MD.read_text(encoding="utf-8")
        with self.subTest(clause="names its sources"):
            self.assertRegex(
                text,
                r"CONTRIBUTING\.md",
                "CLAUDE.md no longer names CONTRIBUTING.md, so nothing says where "
                "its rules come from",
            )
        with self.subTest(clause="sources win"):
            self.assertRegex(
                text,
                r"(?i)(they win|takes precedence|outranks?|is not a second source)",
                "CLAUDE.md no longer states that CONTRIBUTING.md and docs/ outrank "
                "it. Without that, this file reads as a peer of the docs it copies "
                "rather than a map of them",
            )
        with self.subTest(clause="names itself as the thing to fix"):
            self.assertRegex(
                text,
                r"(?i)(this file|CLAUDE\.md)[^.\n]{0,40}is the bug",
                "CLAUDE.md no longer says that a disagreement with its sources makes "
                "*this* file the defect. The precedence is only actionable if the "
                "reader is told which side is wrong",
            )

    def test_referenced_paths_exist(self) -> None:
        """Every repository path named in the agent-facing files resolves.

        A renamed or moved file turns each of these references into a small lie, and
        the cost is highest when an agent follows one: it is told to read
        `docs/07` or `engine/spawn_guard.py` and finds neither. The gate cannot catch
        that, because prose is not compiled.
        """
        targets = [(CLAUDE_MD, CLAUDE_MD.read_text(encoding="utf-8"))]
        for path in _tracked_doc_files():
            targets.append((path, path.read_text(encoding="utf-8")))

        for path, text in targets:
            for referenced in _referenced_paths(text):
                if (path.name, referenced) in KNOWN_DANGLING:
                    continue
                with self.subTest(file=path.name, path=referenced):
                    self.assertTrue(
                        _resolves_anywhere(referenced),
                        f"{path.name} references {referenced!r}, which does not exist. "
                        "Either the path moved or the file is stale",
                    )

    def test_known_dangling_references_are_still_dangling(self) -> None:
        """`KNOWN_DANGLING` holds no entry that has since been fixed.

        A one-directional exemption is a hole that widens by itself: once an entry
        exists, nothing ever re-examines it, and a reference that was repaired stays
        excused for the life of the test. Requiring the defect to still be present
        means the day someone writes the missing doc, this test tells them to delete
        the entry — the exemption cannot outlive its own reason.
        """
        for (filename, referenced) in sorted(KNOWN_DANGLING):
            with self.subTest(file=filename, path=referenced):
                self.assertFalse(
                    _resolves_anywhere(referenced),
                    f"KNOWN_DANGLING exempts {filename}'s reference to {referenced!r}, "
                    "but that path resolves now. The referenced file has been "
                    f"written — remove the {referenced!r} entry from KNOWN_DANGLING "
                    "so the exemption cannot outlive its own reason",
                )

    def test_every_numbered_doc_has_a_row_in_both_tables(self) -> None:
        """The directory is the source of truth; both tables must match it.

        `docs/00` §5's document-set table and `CLAUDE.md`'s depth table are the
        two registrations a new numbered doc owes, and both were maintained by
        hand until this test existed — which is to say, by remembering. A doc
        written without its rows is invisible to every other check here (its
        prose still resolves, its invariants are still quoted), so this is the
        one place the drift can be caught, per doc, by name.
        """
        docs = _numbered_docs()
        self.assertTrue(docs, "no numbered docs found — the check lost its subject")
        doc_set = _doc_set_table_numbers()
        depth = _depth_table_numbers()
        for number, filename in sorted(docs.items()):
            with self.subTest(doc=filename):
                self.assertIn(
                    number, doc_set,
                    f"{filename} exists but has no row in docs/00 §5's document-set "
                    "table. Add the row — a doc the document set does not name is a "
                    "doc nobody is told to read",
                )
                self.assertIn(
                    number, depth,
                    f"{filename} exists but has no row in CLAUDE.md's depth table. "
                    "Add the row — an agent-facing map that skips a doc points every "
                    "reader around it",
                )

    def test_no_table_row_names_a_doc_that_does_not_exist(self) -> None:
        """The comparison is two-directional, like `KNOWN_DANGLING`'s.

        A row naming a file nobody wrote is a promise of depth that does not
        exist — the exact failure `KNOWN_DANGLING`'s docstring records, where a
        table entry outlives what it names and nothing ever re-examines it.
        The history is real here: `docs/08-benchmarks.md` was cited before it
        was written, and the exemption was only removed when the file landed.
        """
        docs = _numbered_docs()
        for label, numbers in (
            ("docs/00 §5 document-set table", _doc_set_table_numbers()),
            ("CLAUDE.md depth table", _depth_table_numbers()),
        ):        
            for number in sorted(numbers):
                with self.subTest(table=label, doc=number):
                    self.assertIn(
                        number, docs,
                        f"{label} has a row for docs/{number:02d}, but no such file "
                        "exists in docs/. Write the doc or delete the row — a table "
                        "that names absent files is how the docs started lying before",
                    )

    def test_the_registered_range_matches_the_doc_set(self) -> None:
        """`docs/ 00–NN` and `docs/00`–`NN` name the highest registered doc.

        Four hand-maintained lines say how far the set runs — CLAUDE.md's layout
        tree, and the range lines in CONTRIBUTING.md (two there). Each drifted
        when `docs/10` was added and was fixed by hand, which is the moment to
        make the fix permanent: the range must name exactly the highest
        numbered file, and CONTRIBUTING's every range must agree with itself.
        """
        docs = _numbered_docs()
        highest = max(docs)
        layout_range = _claudes_layout_range()
        self.assertIsNotNone(
            layout_range,
            "CLAUDE.md's layout tree no longer has a `docs/  00–NN, below` line",
        )
        self.assertEqual(
            layout_range, highest,
            f"CLAUDE.md's layout tree says the docs run 00–{layout_range:02d} but "
            f"docs/{highest:02d} exists. Bump the range when you add a numbered doc",
        )
        contributing = _contributing_ranges()
        self.assertTrue(
            contributing,
            "CONTRIBUTING.md no longer carries a `docs/00`–`NN` range to check",
        )
        for value in contributing:
            with self.subTest(file="CONTRIBUTING.md", range=value):
                self.assertEqual(
                    value, highest,
                    f"CONTRIBUTING.md says `docs/00`–`{value:02d}` but docs/{highest:02d} "
                    "exists. Ranges are how a reader is told where the set ends",
                )

    def test_the_readme_links_every_numbered_doc(self) -> None:
        """Every numbered doc is cited from the section that describes it.

        The three registrations above are the ones an *agent* follows; this is the
        one a *person* meets first, and nothing tested it. The README cited five of
        eleven docs, so half the specifications were reachable only by knowing in
        advance that they existed — which is the situation the document-set table
        exists to prevent inside the repository and nothing was preventing outside
        it.
        """
        docs = _numbered_docs()
        self.assertTrue(docs, "no numbered docs found — the check lost its subject")
        links = _readme_doc_links()
        for filename in sorted(docs.values()):
            with self.subTest(doc=filename):
                self.assertIn(
                    f"docs/{filename}", links,
                    f"docs/{filename} exists but the README never links it. Cite it "
                    "from the feature section that describes it — a specification "
                    "nobody is sent to is maintained only for whoever wrote it",
                )

    def test_every_readme_doc_link_resolves(self) -> None:
        """No reference the README makes into `docs/` points at a missing file.

        The other direction, and the one a rename produces. A doc retitled
        `docs/04-engine-and-runtime.md` leaves the README citing a path that is
        gone, and a broken link in the repository's front page is a reader's first
        impression of it — the one failure mode in this module that a person, not
        an agent, is the one to notice.

        Existence rather than `NUMBERED_DOC`, so the three demo assets are covered
        too: they are the first thing the README renders, they are the only files
        in `docs/` a browser ever fetches, and nothing else here would notice one
        of them going missing.
        """
        for target, line in sorted(_readme_doc_links().items()):
            with self.subTest(link=target):
                self.assertTrue(
                    (PROJECT_ROOT / target).is_file(),
                    f"README.md:{line} links to {target}, which does not exist. "
                    "Every reference the README makes into docs/ must resolve",
                )

    def test_agent_and_command_files_are_well_formed(self) -> None:
        """Each carries the frontmatter Claude Code reads, with a matching name.

        Frontmatter is not decorative: a subagent with no `description` is never
        selected, and one whose `name` disagrees with its filename is selected by a
        key the file is not stored under — a silently dead agent, which looks
        identical to a correctly configured one.
        """
        tracked = _tracked_doc_files()
        self.assertTrue(
            tracked,
            "no .claude/agents or .claude/commands files found; the agent-facing "
            "configuration was deleted",
        )
        for path in tracked:
            text = path.read_text(encoding="utf-8")
            relative = path.relative_to(PROJECT_ROOT).as_posix()
            is_agent = path.parent.name == "agents"
            with self.subTest(file=relative):
                self.assertTrue(
                    text.startswith("---\n"),
                    f"{relative} has no YAML frontmatter block, so Claude Code will "
                    "not load it",
                )
                front = text.split("---", 2)[1]
                # `[ \t]*`, never `\s*`: inside a key with nothing after it, `\s*`
                # matches the newline and then `\S` picks up the first word of the
                # *next* line. That made a blanked-out `description:` pass as long as
                # the body below it began with a non-space character — which is every
                # file here, so the check never once fired. Horizontal whitespace only
                # is what "the value is on this line" actually means.
                self.assertRegex(
                    front,
                    r"(?m)^description:[ \t]*\S",
                    f"{relative} has no description; without one nothing ever "
                    "selects it",
                )
                if is_agent:
                    match = re.search(r"(?m)^name:[ \t]*(\S+)[ \t]*$", front)
                    self.assertIsNotNone(
                        match, f"{relative} is an agent with no 'name:' key"
                    )
                    assert match is not None
                    self.assertEqual(
                        path.stem,
                        match.group(1),
                        f"{relative} declares name {match.group(1)!r}; a subagent is "
                        f"selected by the key matching its filename ({path.stem!r})",
                    )

    def test_ignored_scratch_directory_stays_untracked(self) -> None:
        """`.claude/.cc-writes/` is ignored by this repository, not only by one machine.

        The rest of `.claude/` is project code and is tracked. The scratch directory
        is per-machine and mode 700, and it was covered only by a global ignore in
        `~/.config/git/ignore` — a property of one machine, not of the clone. On any
        other checkout it would be committed by accident.
        """
        gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        patterns = [
            line.strip()
            for line in gitignore.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertIn(
            ".claude/.cc-writes/",
            patterns,
            ".gitignore does not list .claude/.cc-writes/; the private per-machine "
            "scratch directory would be committed by any clone whose machine lacks "
            "the maintainer's global ignore",
        )


if __name__ == "__main__":
    unittest.main()
