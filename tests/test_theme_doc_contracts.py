"""The documented theme layer must say what the theme layer does.

`tests/test_claude_md_contracts.py` freezes the *security* invariants, because
those are the ones a reader is entitled to rely on and a copy is the easiest way
to make them quietly wrong. The theme layer is the same shape of claim with
three properties that make it worse.

* It moves. A theme is data — adding one is five lines of tokens — so the layer
  is the fastest-moving thing in `ui/src/`, and the prose describing it is the
  slowest.
* It is advertised. The README's Appearance section makes four claims "a reader
  is entitled to check against `ui/src/appearance.ts`", which is a promise that
  a stale bullet breaks in the most visible way available.
* Its rules get *reversed*, not just extended. A reversal leaves the old claim
  grammatically intact and merely false: "the status hues are not themed" was
  true, was well argued, and became untrue when `THEME_TONES` landed, while
  reading exactly as written. That is the failure this module was written for:
  it happened once already, in a worktree, hours after the code that caused it.

So this parses the TypeScript and checks the prose against it. It never imports
or executes the UI, for the reason `test_claude_md_contracts.py` gives — a doc
that has drifted must fail a test, not import a doc that happens to run — and
because there is no TS parser here anyway. **The scan is lexical, and that is
its weakness, stated rather than hidden:** it recognises `id: "…"` because
that is how a theme spells its identifier today. A rename to `key:` makes the
parser find nothing, so every check below asserts that it found *something*
first — a parse that matches zero themes is itself a failure, not a vacuous
pass. That is the difference between a weak check and a broken one.

Two numbers are pinned rather than the sentences that contain them, because a
count and a name go stale silently while a sentence tends to be rewritten when
somebody edits around it: how many themes ship, and what the effects' frame
rate is. A theme added without a doc row is the most likely drift in this
repository and the least visible.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

README = PROJECT_ROOT / "README.md"
DESIGN = PROJECT_ROOT / "DESIGN.md"
CLAUDE = PROJECT_ROOT / "CLAUDE.md"
APPEARANCE_TS = PROJECT_ROOT / "ui" / "src" / "appearance.ts"
ATMOSPHERE_TS = PROJECT_ROOT / "ui" / "src" / "hooks" / "useAtmosphereCanvas.ts"
TERMINAL_TS = PROJECT_ROOT / "ui" / "src" / "terminalTheme.ts"
TERMINAL_PANE = PROJECT_ROOT / "ui" / "src" / "components" / "TerminalPane.tsx"

# The three documents that describe the theme layer, in the order a reader meets
# them. All three are checked by the claims below: a rule stated in DESIGN.md
# and contradicted in the README is still a lie a reader believes.
THEME_DOCS = (README, DESIGN, CLAUDE)

# `id:` immediately followed by `label:` is how a theme spells itself today.
# Pairing them in one pattern rather than collecting all `id:`s and all
# `label:`s is what keeps the swatch labels out of the set — the theme table
# carries `{ v: "--cmatrix-rain", label: "rain" }` rows, so a loose
# `label: "…"` scan collects six extra names that are not themes.
THEME_PAIR = re.compile(
    r'id:\s*"(?P<id>[a-z0-9-]+)",\s*\n\s*label:\s*"(?P<label>[^"]+)"',
)

# A row of the README's theme table: `| **Codify Dark** | … |`. Scoped to the
# Appearance section below, because the README has other tables.
TABLE_ROW = re.compile(r"^\|\s*\*\*(?P<label>[^*]+?)\*\*\s*\|", re.M)

# The count claim, in the exact form the README states it. Matching the whole
# phrase rather than the words "eight themes" anywhere matters: DESIGN.md says
# "things broke together at eight themes" about a past incident, and a bare
# grep for the number would demand that historical sentence be rewritten every
# time a theme is added.
COUNT_CLAIM = re.compile(r"\b(\w+)\s+themes ship in the box\b")

# A documented frame rate, and — the part that took a false positive to learn —
# only a *budget* claim. DESIGN.md also says "1 FPS snowfall is still a snowfall",
# which is prose about a qualitative floor and has nothing to do with the code's
# rate; a bare `\d+ fps` sweep flagged it and would have failed the suite for a
# sentence nobody had changed. A number counts as the budget only when the same
# sentence also says what the budget *is*.
FPS_CLAIM = re.compile(r"\b(\d+)\s*fps\b", re.I)
BUDGET_WORDS = re.compile(r"accumulator|budget|capped|paced|pace|per second", re.I)

# How far either side of a match to look for the budget word. Two sentences' worth
# of slop, so a claim and the phrase describing it may sit either side of a
# period, and not so much that an unrelated paragraph bleeds in.
FPS_CONTEXT = 140

# The rule that was reversed. Each alternative is a way of saying the same false
# thing, because the reversal happened once and could plausibly be attempted
# again by someone who preferred the old behaviour.
RETRACTED_TONE_CLAIM = re.compile(
    r"(status hues? (?:are|is) not themed)"
    r"|(?:a|every) theme (?:may not|cannot|must not|does not|doesn't) repaint"
    r"|themes? (?:may not|cannot|must not) (?:change|touch) (?:the )?status"
    r"|only surfaces and text are themed",
    re.I,
)

# What replaced it. Two rules, and both have to be in a document that talks
# about the tones: a theme that restates "failed" without the warm-arc rule
# stated somewhere is exactly the gap this module exists to catch.
RULE_ALL_OR_NONE = re.compile(r"all six or none|all six,? or none", re.I)
RULE_WARM_ARC = re.compile(r"warm arc|stay warm|remains? warm", re.I)

# Spelled-out counts the README is allowed to use. Bounded on purpose rather than
# parsed: a general English-number parser would have to agree with the document
# about "a dozen" and "a score" as well, and a table that stops working at "ten"
# is a table that fails on the eleventh theme for a reason that has nothing to do
# with what is being checked. It runs to twenty so a reader adding themes is not
# stopped by this module before the count check can tell them anything.
NUMBER_WORDS = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}

# ── which themes have no motion ───────────────────────────────────────────────
#
# Two of the sixteen ship none: `codify-dark`, which never had an effect, and
# `still`, for which having none is the point. `MOTIONLESS_THEME_IDS` in
# `ui/src/appearance.ts` is where that decision is declared, and DESIGN.md §6
# already says so.
#
# The thing that drifts is not the list — it is the sentences *about* the theme
# set. A caption reading "all sixteen, each running its own animated backdrop"
# is false for two of them, and false in the one place a reader is most likely
# to believe it. It was false twice in the same file at once: the header caption
# still counted eight themes, and the gallery's `alt` said "each" while the
# caption below it named only one of the two flat themes — and `alt` is the one
# line of that image a screen reader receives with nothing to soften it.
#
# So the check is on markdown captions and image alt text, and only those.
# DESIGN.md's prose wraps, so an exception on the following line is not a missing
# exception; a line-based scan cannot tell the two apart and would report every
# paragraph. Captions do not wrap, which is what makes a line the honest unit.

# `MOTIONLESS_THEME_IDS` holds `NAME.id` references rather than literals, so the
# names have to be resolved through the constants they point at.
MOTIONLESS_ASSIGN = re.compile(r"MOTIONLESS_THEME_IDS[^=\[]*=\s*\[([^\]]*)\]")
MOTIONLESS_REF = re.compile(r"(\w+)\.id")
TS_CONST_ID = r"\b{0}\s*:\s*AppearanceTheme\s*=\s*\{{\s*id:\s*\"([^\"]+)\""

# A markdown caption or an image `alt` — the units that do not wrap.
MARKDOWN_CAPTION = re.compile(r"^\s*(?:<sub>|<img\b|!\()", re.I)

# A quantifier over the theme set and a word for motion, in one line: "each with
# its own animated backdrop", "each running its own animated backdrop", "every
# theme animates". Deliberately generous about the words and tight about the
# quantifier, because a sentence is only a claim about *all* the themes if it
# says so.
UNIVERSAL_ANIMATION_CLAIM = re.compile(
    r"\b(?:every|each|all)\b[^.\n]{0,90}?"
    r"\b(?:animat\w*|backdrop\w*|canvas\w*|moving|motion)\b",
    re.I,
)

# What discharges it. The test is that the caption tells the reader that some do
# not, not that it uses any particular word to do it, so "except", "no canvas"
# and "flat one" all count.
ANIMATION_EXCEPTION = re.compile(
    r"\bexcept\b|\bbut\b|other than|none of|not animated|no canvas|without one|flat one",
    re.I,
)

# A caption that states how many themes animate, rather than claiming all of
# them do: "fourteen of them with an animated backdrop of their own". This is the
# other half of the caption check — replacing a false universal with a true count
# is the fix, and the count is a number, so it goes stale on its own.
#
# "of them" and nothing looser, because the looser reading is not hypothetical:
# the gallery caption's "a **screenshot** of the running app in that theme: the
# backdrops read their colours" is the same three words in the same order, and a
# first draft of this matched it and reported a theme count of NaN.
ANIMATED_COUNT = re.compile(
    r"\b(\w+)\s+of\s+(?:them|these|those)\b[^.\n]{0,40}?\b(?:animat\w*|backdrop\w*)\b",
    re.I,
)

# The gallery image and whatever it tells a reader with no eyes for it.
IMG_SRC = re.compile(r'<img\b[^>]*\bsrc="([^"]*themes\.gif)"[^>]*>', re.I)
IMG_ALT = re.compile(r'alt="([^"]*)"', re.I)

# ── the terminal layer ───────────────────────────────────────────────────────
#
# xterm.js paints its own canvas and takes an `ITheme` of concrete colour
# strings, so the CSS layer in `ui/src/index.css` cannot reach it: the terminal
# was `#0d1117` on `#c9d1d9` — the *default* theme's hexes — in every theme,
# which made it the one grey-blue box in an app that was otherwise black and
# phosphor. `ui/src/terminalTheme.ts` closes that by reading the theme out of
# the document and handing it over.
#
# So the terminal is a surface a document can make a claim about: it either
# follows the theme or it does not, and before this module nothing checked which.
# The claim is now in the README, and these are the checks behind it.

# `MAPPING` and `FALLBACK` are flat `key: "value",` records; the leading indent
# is what separates a record entry from a `label:` inside a `swatch:` array.
TS_RECORD_ENTRY = re.compile(r'^\s{2}(\w+):\s*"([^"]+)",\s*$', re.M)

# The effect namespaces: the variables a *backdrop* publishes rather than a
# theme's own surfaces. A terminal mapped to one of these would follow the
# weather instead of the palette, which is the wrong thing for text to sit on.
EFFECT_NAMESPACES = (
    "--cmatrix-", "--cyber-", "--flare-", "--neural-", "--hud-", "--abyss-", "--ascii-",
)

# A document claiming the terminal follows the theme. Deliberately about the
# *claim*, not the word "terminal": DESIGN.md discusses the terminal for other
# reasons (the pane, the grid, spawn safety) and must not trip this.
TERMINAL_THEMED_CLAIM = re.compile(
    r"terminal[^.\n]{0,120}(?:follows|reads|paints|repaints|takes)[^.\n]{0,80}theme"
    r"|(?:theme|themes)[^.\n]{0,120}terminal[^.\n]{0,80}(?:follows|reads|paints|repaints)",
    re.I,
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _ts_record(text: str, const: str) -> dict[str, str]:
    """A flat `const NAME = { key: "value", … }` record from a TypeScript source.

    The closing brace is found by *counting*, not by looking for the first `};`.
    Counting matters because the first-brace version is silently wrong in the one
    case that matters: a record whose closing brace is missing runs on and reads
    the entries of the record after it. That is not a crash — it is a plausible
    wrong answer, which is the failure mode this module is about. A nested `{}`
    inside a record value is handled for the same reason; a regex cannot count.

    Returns empty rather than raising, because every caller has a guard that says
    so: a helper that threw would report "the parse stopped matching" on an
    unrelated line.
    """
    marker = f"const {const}"
    if marker not in text:
        return {}
    after = text.split(marker, 1)[1]
    start = after.find("{")
    if start < 0:
        return {}
    depth = 0
    end = -1
    for offset, char in enumerate(after[start:], start=start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = offset
                break
    if end < 0:
        return {}
    return {m.group(1): m.group(2) for m in TS_RECORD_ENTRY.finditer(after[start + 1 : end])}


def _managed_vars() -> set[str]:
    """Every variable `applyTheme` clears before applying the next theme."""
    text = _read(APPEARANCE_TS)
    if "MANAGED_VARS" not in text:
        return set()
    block = text.split("MANAGED_VARS", 1)[1].split("]", 1)[0]
    return set(re.findall(r'"(--[a-z0-9-]+)"', block))


def _published() -> dict[str, dict[str, str]]:
    """`{theme id: {variable: value}}` for what each theme actually publishes.

    Two tables, because a theme publishes in two: `tokens` carries the surfaces
    and the text, and `THEME_TONES` carries the five status tones and the accent.
    Reading only `tokens` makes every tone look unset — which is how the
    fallback check below first reported twelve mismatches that were not there.
    """
    text = _read(APPEARANCE_TS)
    consts = dict(
        re.findall(
            r'export const (\w+):\s*AppearanceTheme = \{\s*\n\s*id:\s*"([a-z0-9-]+)"', text
        )
    )
    out: dict[str, dict[str, str]] = {}
    for const, theme_id in consts.items():
        values: dict[str, str] = {}
        block = re.search(
            rf'export const {const}:\s*AppearanceTheme = \{{.*?tokens:\s*\{{(.*?)\n  \}}',
            text,
            re.S,
        )
        if block:
            values.update(
                dict(re.findall(r'"(--[a-z0-9-]+)":\s*"([^"]+)"', block.group(1)))
            )
        out[theme_id] = values

    if "THEME_TONES" in text:
        rows = re.findall(r"\[(\w+)\.id\]:\s*\{(.*?)\n  \},", text, re.S)
        for const, body in rows:
            # A const the `export const X: AppearanceTheme` scan did not find is
            # still a theme; keeping the const name as the key means the row is
            # visible in a failure message rather than dropped on the floor.
            theme_id = consts.get(const) or const
            out.setdefault(theme_id, {}).update(
                dict(re.findall(r'"(--[a-z0-9-]+)":\s*"([^"]+)"', body))
            )
    return out


def _terminal_parts() -> tuple[dict[str, str], dict[str, str]]:
    """(`MAPPING`, `FALLBACK`) from `ui/src/terminalTheme.ts`."""
    text = _read(TERMINAL_TS)
    return _ts_record(text, "MAPPING"), _ts_record(text, "FALLBACK")


def _themes() -> dict[str, str]:
    """`{id: label}` for every theme in `ui/src/appearance.ts`.

    Raises if the pattern stops matching, so a renamed field fails loudly
    instead of returning an empty set and passing every comparison below.
    """
    themes = {m.group("id"): m.group("label") for m in THEME_PAIR.finditer(_read(APPEARANCE_TS))}
    if not themes:
        raise AssertionError(
            f"{APPEARANCE_TS.name} yielded no `id: … label: …` pairs. Either every "
            "theme was removed — in which case the Appearance table in the README "
            "is asserting nine things that do not exist — or the shape of a theme "
            f"changed and THEME_PAIR no longer matches it: {THEME_PAIR.pattern}"
        )
    return themes


def _appearance_section(text: str) -> str:
    """The README's `## 🎨 Appearance` section, or the whole file if renamed.

    Falling back to the whole file is deliberate: a renamed heading must not
    make every table check quietly vacuous, and a false positive on an unrelated
    bolded table row is recoverable in a way a silently skipped check is not.
    """
    start = text.find("## \U0001f3a8 Appearance")
    if start < 0:
        return text
    rest = text[start:]
    end = rest.find("\n## ", 1)
    return rest if end < 0 else rest[:end]


def _table_labels() -> list[str]:
    return [m.group("label") for m in TABLE_ROW.finditer(_appearance_section(_read(README)))]


def _frame_rate_claims(text: str) -> list[int]:
    """Frame rates the document states as *the budget*, not as an example.

    A rate in a sentence that also calls it a budget, a cap or an accumulator is
    a claim about `useAtmosphereCanvas` and is compared against the code. A rate
    in any other sentence is prose — "1 FPS snowfall is still a snowfall" is a true
    sentence about a low bar, and failing the suite over it teaches people to
    ignore the check.
    """
    claims: list[int] = []
    for match in FPS_CLAIM.finditer(text):
        window = text[max(0, match.start() - FPS_CONTEXT) : match.end() + FPS_CONTEXT]
        if BUDGET_WORDS.search(window):
            claims.append(int(match.group(1)))
    return claims


def _effect_fps() -> dict[str, int]:
    """`{component: default fps}` for every atmosphere effect that declares one.

    The frame rate is a *property of the code*, and the documents assert it, so
    it is read from the components rather than from a doc. A component that
    raises its own rate to 60 is a legitimate change — and one that invalidates
    "a 30 FPS accumulator" in two documents, which is why the docs are checked
    against this value afterwards.
    """
    defaults: dict[str, int] = {}
    for path in sorted((PROJECT_ROOT / "ui" / "src" / "components" / "ui").glob("*.tsx")):
        text = _read(path)
        if "useAtmosphereCanvas" not in text:
            continue
        for match in re.finditer(r"\bfps\s*[:=]\s*(\d+)", text):
            defaults[path.name] = int(match.group(1))
            break
    return defaults


def _motionless_theme_ids() -> set[str]:
    """The ids in `MOTIONLESS_THEME_IDS`, resolved through their constants.

    Raises rather than returning an empty set. An empty answer would make every
    check below vacuously true, which is the failure mode this module was
    written to avoid: a parse that matches nothing is a broken parse.
    """
    text = _read(APPEARANCE_TS)
    assign = MOTIONLESS_ASSIGN.search(text)
    if assign is None:
        raise AssertionError(
            f"{APPEARANCE_TS.name} no longer declares MOTIONLESS_THEME_IDS as a "
            "bracketed list, so MOTIONLESS_ASSIGN no longer matches it: "
            f"{MOTIONLESS_ASSIGN.pattern}"
        )
    ids: set[str] = set()
    for name in MOTIONLESS_REF.findall(assign.group(1)):
        const = re.search(TS_CONST_ID.format(re.escape(name)), text)
        if const is None:
            raise AssertionError(
                f"MOTIONLESS_THEME_IDS names {name}.id, but {APPEARANCE_TS.name} "
                f"declares no `const {name}: AppearanceTheme` with an id — either "
                "the constant was renamed or it stopped being a theme"
            )
        ids.add(const.group(1))
    if not ids:
        raise AssertionError(
            "MOTIONLESS_THEME_IDS resolved to no ids. A theme with no effect and "
            "no place on the list is a theme whose canvas never mounts, and it is "
            "the decision this module exists to keep written down."
        )
    return ids


def _caption_animation_claims(text: str) -> list[tuple[int, str]]:
    """`[(line number, line)]` for each caption claiming the theme set animates."""
    return [
        (number, line.strip())
        for number, line in enumerate(text.splitlines(), 1)
        if MARKDOWN_CAPTION.match(line) and UNIVERSAL_ANIMATION_CLAIM.search(line)
    ]


def _gallery_alt(text: str) -> str:
    """The `alt` of the themes gallery, or a loud failure if the image moved."""
    img = IMG_SRC.search(text)
    if img is None:
        raise AssertionError(
            'No <img src="…themes.gif"> in the README. The gallery is what a '
            "reader meets first and the claim under test is the one welded to "
            "it; an image that moved is recoverable, a silently skipped check "
            "is not."
        )
    alt = IMG_ALT.search(img.group(0))
    if alt is None:
        raise AssertionError("The themes gallery carries no alt attribute at all.")
    return alt.group(1)


class MotionlessThemeContractsTest(unittest.TestCase):
    """Two of the sixteen themes ship no animation, and the prose must know it."""

    def test_a_caption_cannot_claim_every_theme_animates_unless_it_says_otherwise(self) -> None:
        claims = [
            (doc.name, number, line)
            for doc in THEME_DOCS
            for number, line in _caption_animation_claims(_read(doc))
        ]
        self.assertTrue(
            claims,
            "No caption in any theme document claims the theme set animates any "
            "more, so this check has nothing left to verify. Either the prose was "
            "rewritten into a shape it does not recognise — in which case it may "
            "be wrong in a way this module can no longer see — or the captions "
            "moved somewhere this module does not read.",
        )
        unfounded = [
            f"{doc}:{number}: {line}"
            for doc, number, line in claims
            if not ANIMATION_EXCEPTION.search(line)
        ]
        flat = ", ".join(sorted(_motionless_theme_ids()))
        self.assertEqual(
            [],
            unfounded,
            "A caption claims the theme set animates without saying that some of "
            f"them do not. {flat} ship no canvas at all, so the sentence is false "
            "as written — and a caption is read by people who never reach the "
            "paragraph underneath it.",
        )

    def test_the_gallery_alt_names_every_theme_that_ships_no_backdrop(self) -> None:
        """`alt` is the one line of a caption pair that arrives with nothing else."""
        themes = _themes()
        alt = _gallery_alt(_read(README))
        for theme_id in sorted(_motionless_theme_ids()):
            label = themes[theme_id]
            self.assertIn(
                label,
                alt,
                f"the gallery's alt text does not name {label}, which ships no "
                "backdrop. alt is read alone — no caption, no table, no "
                "paragraph below it — so an exception written anywhere else in "
                "the section never reaches the reader it was written for.",
            )

    def test_a_caption_counting_the_themes_that_animate_counts_them_right(self) -> None:
        """Swapping a false universal for a true count is the fix; the count rots.

        "All sixteen, each animating" and "fourteen of them animate" fail in
        opposite directions — the first is false now, the second is false the
        moment somebody adds a theme — and only the second one is pinned to the
        code, which is the same bargain the theme count and the frame rate get.
        """
        expected = len(_themes()) - len(_motionless_theme_ids())
        claims = [
            (doc.name, match.group(1), match.group(0))
            for doc in THEME_DOCS
            for match in ANIMATED_COUNT.finditer(_read(doc))
        ]
        self.assertTrue(
            claims,
            "No caption states how many themes animate. The caption check above "
            "only catches a caption that claims *all* of them do; a document that "
            "had quietly stopped making the claim at all would read as correct, "
            "and this is the one that says whether the number is right.",
        )
        wrong: list[str] = []
        for name, word, text in claims:
            stated = NUMBER_WORDS.get(word.lower())
            if stated is None:
                # The same policy as the theme count: an unreadable number is a
                # failure, not a skip. "several of them" is a claim that cannot
                # be checked today and will not become checkable by being left
                # alone, and a silently skipped claim is the one failure mode
                # this module was written to stop.
                if not re.fullmatch(r"\d+", word):
                    wrong.append(
                        f"{name}: {text!r} states '{word}', which this check cannot "
                        f"read. Add it to NUMBER_WORDS in {Path(__file__).name} or "
                        "write a digit, rather than letting the count go unchecked"
                    )
                    continue
                stated = int(word)
            if stated != expected:
                wrong.append(f"{name}: {text!r} states {stated}, expected {expected}")
        self.assertEqual(
            [],
            wrong,
            "A caption miscounts the themes that animate, or states the count in a "
            f"form this check cannot verify. {len(_themes())} themes ship and "
            f"{len(_motionless_theme_ids())} are declared motionless, so {expected} "
            "of them animate.",
        )

    def test_a_document_naming_the_motionless_list_names_all_of_it(self) -> None:
        """The list is the decision; the prose is the part that goes stale.

        Naming `codify-dark` and forgetting `still` is not a shorter sentence,
        it is a different claim — that exactly one theme has no effect — and it
        is the shape a third motionless theme would arrive in.
        """
        ids = _motionless_theme_ids()
        unknown = ids - set(_themes())
        self.assertEqual(
            set(),
            unknown,
            "MOTIONLESS_THEME_IDS names ids that no theme declares: "
            f"{sorted(unknown)}",
        )
        naming: list[str] = []
        for doc in THEME_DOCS:
            text = _read(doc)
            if "MOTIONLESS_THEME_IDS" not in text:
                continue
            naming.append(doc.name)
            missing = sorted(theme_id for theme_id in ids if theme_id not in text)
            self.assertEqual(
                [],
                missing,
                f"{doc.name} names MOTIONLESS_THEME_IDS but not {missing}. It "
                "describes the list without completing it, which reads as a "
                "shorter sentence and is in fact a different claim about which "
                "themes are still.",
            )
        self.assertTrue(
            naming,
            "No theme document mentions MOTIONLESS_THEME_IDS at all, so the "
            "declaration in `appearance.ts` has no prose pointing at it. A list "
            "nobody is told about is a list nobody maintains.",
        )


class ThemeDocContractsTest(unittest.TestCase):
    def test_every_theme_has_a_row_in_the_readme_table(self) -> None:
        """The table is the enumeration, so a theme with no row is invisible.

        This is bidirectional on purpose. A missing row hides a shipped theme
        from the one document whose job is to list them, and a row for a theme
        that no longer exists is worse: it advertises a choice that is not there,
        and a reader who picked it would have been told the truth by the code.
        """
        themes = _themes()
        rows = _table_labels()
        self.assertTrue(rows, "the README's Appearance table has no rows to compare")

        missing = sorted(label for label in themes.values() if label not in rows)
        self.assertEqual(
            [],
            missing,
            f"{len(themes)} themes ship in ui/src/appearance.ts and the README's table "
            f"omits {missing}. Either give {missing} a row that says what it is, or "
            "remove the theme from the code — a table that is a subset of the code is "
            "the drift this module exists to catch",
        )

        unknown = sorted(label for label in rows if label not in set(themes.values()))
        self.assertEqual(
            [],
            unknown,
            f"the README's table lists {unknown}, which ui/src/appearance.ts does not "
            "define. A theme row for a theme that is not there advertises a choice "
            "the app cannot offer",
        )

    def test_the_stated_theme_count_matches_the_code(self) -> None:
        """The "N themes ship in the box" sentence is a number, and numbers go stale alone.

        The row check above catches a theme with no row; this catches the sentence
        that counts them, which no other check reads. Both are needed: adding a
        theme and forgetting the row fails the first, adding one and forgetting the
        sentence fails only this. It is quoted here without a number on purpose —
        a docstring that named the current count would be the next thing to go
        stale, in the one file whose job is noticing that.
        """
        themes = _themes()
        match = COUNT_CLAIM.search(_read(README))
        self.assertIsNotNone(
            match,
            "the README no longer claims how many themes ship in the box; the "
            "Appearance section opens with that count and this check reads it",
        )
        assert match is not None
        word = match.group(1)
        stated = NUMBER_WORDS.get(word.lower())
        if stated is None:
            digits = re.fullmatch(r"\d+", word)
            self.assertIsNotNone(
                digits,
                f"the README states '{word} themes ship in the box', which this check "
                f"cannot read. Add it to NUMBER_WORDS in {Path(__file__).name} rather "
                "than letting the count go unchecked",
            )
            stated = int(word)
        self.assertEqual(
            len(themes),
            stated,
            f"the README says {stated} theme(s) ship in the box and "
            f"ui/src/appearance.ts defines {len(themes)}: {sorted(themes)}",
        )

    def test_no_document_still_asserts_the_reversed_tone_rule(self) -> None:
        """The claim that was true, was argued, and became false while reading true.

        `THEME_TONES` lets a theme restate the five status tones. Before it
        landed, the badges and status dots ignored the theme entirely, so a
        success pill in the OLED app was Tailwind green — the one saturated thing
        on a black-and-phosphor screen, belonging to no palette the user had
        chosen. Reversing that was right. Leaving the old sentence in place is
        not: "the status hues are not themed" is exactly as readable as before and
        now false, and it is the sentence a reader would check first.
        """
        self.assertIn(
            "THEME_TONES",
            _read(APPEARANCE_TS),
            "ui/src/appearance.ts no longer exports THEME_TONES. If the tones are "
            "themeable again, every document that still forbids it needs rereading; "
            "if they are not, the rule this module retired was premature",
        )
        for path in THEME_DOCS:
            match = RETRACTED_TONE_CLAIM.search(_read(path))
            with self.subTest(document=path.name):
                self.assertIsNone(
                    match,
                    f"{path.name} still asserts a rule the code no longer implements: "
                    f"{match.group(0) if match else ''!r}. A theme may restate the "
                    "status tones, under two rules — all six or none, and danger and "
                    "warning stay on the warm arc. Say those instead",
                )

    def test_a_document_about_the_tones_states_both_rules_that_replace_them(self) -> None:
        """Permission without a limit is not a rule, and a reader cannot check it.

        A document that says a theme may restate "failed" without saying that
        `danger` and `warning` must stay warm has documented a permission where
        the code implements a constraint — and the constraint is the part that
        matters, because it is the one protecting a meaning a user has learned.
        """
        for path in THEME_DOCS:
            text = _read(path)
            if "THEME_TONES" not in text and "status tone" not in text.lower():
                continue
            with self.subTest(document=path.name):
                self.assertRegex(
                    text,
                    RULE_ALL_OR_NONE,
                    f"{path.name} discusses the status tones without stating the "
                    "all-six-or-none rule. applyTheme clears every managed variable "
                    "before applying the next theme, so a theme that stated three "
                    "would show the previous theme's remaining three",
                )
                self.assertRegex(
                    text,
                    RULE_WARM_ARC,
                    f"{path.name} discusses the status tones without stating that "
                    "danger and warning stay on the warm arc. A theme may restate "
                    "failure in its own palette; it may not make it cyan",
                )

    def test_every_effect_runs_at_the_frame_rate_the_documents_state(self) -> None:
        """One clock, one budget — so the budget is the same everywhere, and
        the number the README prints is that number.

        Two halves. First the effects agree with each other, because a component
        quietly defaulting to 60 makes the 30 FPS claim false in the one place it
        is most load-bearing: the accumulator that is supposed to stop a 144 Hz
        display from asking for more. Then the documents match the code.
        """
        defaults = _effect_fps()
        self.assertTrue(
            defaults,
            f"no component under ui/src/components/ui/ declares an fps default that "
            f"{Path(__file__).name} could read. The parse below is "
            f"`{_effect_fps.__doc__ and 'fps = <n>'}` over every file that calls "
            "useAtmosphereCanvas",
        )
        distinct = set(defaults.values())
        self.assertEqual(
            1,
            len(distinct),
            f"the atmosphere effects do not agree on a frame rate: "
            f"{ {k: v for k, v in defaults.items()} }. One clock, one budget is the "
            "claim; two defaults is two budgets, whichever is slower",
        )
        rate = distinct.pop()
        claimed_anywhere = False
        for path in THEME_DOCS:
            for claimed in _frame_rate_claims(_read(path)):
                claimed_anywhere = True
                with self.subTest(document=path.name, claimed=claimed):
                    self.assertEqual(
                        rate,
                        claimed,
                        f"{path.name} states {claimed} FPS as the budget and the "
                        f"effects default to {rate} (`{defaults}`). Change the "
                        "default in ui/src/components/ui/ or the claim in the "
                        "document, in the same change",
                    )
        self.assertTrue(
            claimed_anywhere,
            "no document states a frame rate as the budget any more, so this check "
            "compared nothing. DESIGN.md and the README both name it; if the "
            "wording changed, widen BUDGET_WORDS rather than leaving the check "
            "silently green",
        )

    def test_the_parser_still_reads_every_theme(self) -> None:
        """The guard on every other check here.

        Each comparison above is a difference between what a regex found and what
        the code holds. A regex that finds *nothing* makes all of them vacuously
        true — a scan that has stopped working reports success, which is the one
        outcome worse than reporting nothing.

        Asserting "more than one theme" was not strong enough, and the way it
        proved that is the reason this is worded as a count. Two seasonal themes
        were added while this module was being written; `THEME_PAIR` requires
        `id:` immediately followed by `label:`, both new themes spell it that
        way, and the suite stayed green through the addition — because the guard
        only asked whether *something* had been parsed. The stronger version
        counts the declarations the TypeScript itself declares, which is the one
        number no parse can be quietly short of.
        """
        themes = _themes()
        declared = re.findall(
            r"export const \w+:\s*AppearanceTheme\s*=", _read(APPEARANCE_TS)
        )
        self.assertTrue(
            declared,
            f"{APPEARANCE_TS.name} declares no `AppearanceTheme`; the theme layer has "
            "been restructured and this module is comparing against nothing",
        )
        self.assertEqual(
            len(declared),
            len(themes),
            f"{APPEARANCE_TS.name} declares {len(declared)} themes and THEME_PAIR "
            f"found {len(themes)}: {sorted(themes)}. A theme whose fields are spelled "
            "in a different order or separated by a comment is invisible to every "
            "check in this module — they will pass on a subset. Loosen THEME_PAIR "
            "to match the new shape rather than leaving the docs quietly unchecked",
        )
        for theme_id, label in themes.items():
            with self.subTest(theme=theme_id):
                self.assertRegex(theme_id, r"^[a-z0-9-]+$", f"{theme_id!r} is not a slug")
                self.assertTrue(label.strip(), f"theme {theme_id!r} has an empty label")


class TerminalSurfaceContractsTest(unittest.TestCase):
    """The terminal is a surface the theme layer has to reach, so a document's
    claim about it is a claim about code.

    Before `ui/src/terminalTheme.ts` the claim could not honestly have been made:
    xterm paints its own canvas and takes concrete hexes, so the CSS layer never
    reached it and the terminal was the default theme's colours in every theme.
    It can be made now, and these are the checks that make it true rather than
    merely stated.
    """

    def test_the_parser_still_reads_the_terminal_mapping(self) -> None:
        """The guard on the three below, for the reason the module docstring gives.

        All twenty colours and all twenty fallbacks are found, and they cover the
        same keys: a mapping or a fallback silently reduced to a handful of
        entries would leave the rest unchecked while every comparison still passed.
        """
        mapping, fallback = _terminal_parts()
        self.assertEqual(
            20,
            len(mapping),
            f"{TERMINAL_TS.name}: MAPPING yielded {len(mapping)} entries, expected the "
            f"twenty of xterm's ITheme: {sorted(mapping)}. Either the record moved or "
            f"TS_RECORD_ENTRY no longer matches it ({TS_RECORD_ENTRY.pattern})",
        )
        self.assertEqual(
            sorted(mapping),
            sorted(fallback),
            "MAPPING and FALLBACK cover different colours, so some colour has no "
            f"answer for a document that says nothing: "
            f"{sorted(set(mapping) ^ set(fallback))}",
        )

    def test_every_colour_the_terminal_paints_is_a_variable_a_theme_publishes(self) -> None:
        """The mapping has to name variables that exist, or the theme does not
        reach the terminal and nothing says so.

        A colour pointed at a variable no theme sets falls back to Codify Dark's
        hex. That is correct in one theme and wrong in the other ten, which is the
        quietest possible failure: the terminal looks themed, the test suite is
        green, and the OLED app has a grey-blue terminal again.
        """
        mapping, _ = _terminal_parts()
        managed = _managed_vars()
        self.assertTrue(managed, "MANAGED_VARS did not parse; nothing can be compared")
        published = _published()

        unmanaged = {k: v for k, v in mapping.items() if v not in managed}
        self.assertEqual(
            {},
            unmanaged,
            f"the terminal paints {unmanaged} with variables `applyTheme` does not "
            f"clear or write, so no theme can ever set them and every theme falls "
            f"back. MANAGED_VARS has {len(managed)} names; add the variable, or map "
            f"the colour to one that exists",
        )

        for theme_id, values in sorted(published.items()):
            unset = sorted({v for v in mapping.values() if v not in values})
            with self.subTest(theme=theme_id):
                self.assertEqual(
                    [],
                    unset,
                    f"{theme_id} publishes none of {unset}, which the terminal's "
                    f"MAPPING reads. Those colours fall back to Codify Dark's hexes "
                    f"in this theme only",
                )

    def test_the_terminal_follows_surfaces_and_tones_not_the_weather(self) -> None:
        """Every mapped variable is one of the theme's own.

        A theme publishes two different kinds of variable: its surfaces, and the
        effect variables its backdrop draws with. Text should sit on the first.
        Mapping a terminal colour to an effect variable would make the rain the
        terminal's background, which is coherent in one theme and wrong in the
        eight with no rain at all.
        """
        mapping, _ = _terminal_parts()
        effects = {
            key: var
            for key, var in mapping.items()
            if var.startswith(EFFECT_NAMESPACES)
        }
        self.assertEqual(
            {},
            effects,
            f"the terminal maps {effects} onto backdrop variables. Those belong to "
            "the weather, not the palette — map it to a --codify- surface or tone, "
            "or a theme with no backdrop of that kind leaves the terminal unpainted",
        )
        for key, var in mapping.items():
            with self.subTest(colour=key):
                self.assertRegex(
                    var,
                    r"^--codify-[a-z0-9-]+$",
                    f"{key} is painted by {var}, which is not a theme surface or tone",
                )

    def test_the_documented_fallback_is_the_default_theme(self) -> None:
        """`FALLBACK` is documented as the default theme's own values.

        That is what makes a terminal that opens before a theme is applied
        indistinguishable from one that opens after: same answer, so no flash and
        no grey-blue box. It is a claim about two files agreeing, which is exactly
        the kind of claim that goes stale when someone edits one of them — and the
        two tables it spans (`tokens` and `THEME_TONES`) are why reading only one
        of them is not enough.
        """
        mapping, fallback = _terminal_parts()
        published = _published()
        default = published.get("codify-dark")
        self.assertIsNotNone(
            default, "codify-dark publishes nothing; the fallback has nothing to mirror"
        )
        assert default is not None

        drifted = {
            key: (fallback[key], default.get(mapping[key]))
            for key in fallback
            if default.get(mapping[key]) != fallback[key]
        }
        self.assertEqual(
            {},
            drifted,
            "FALLBACK has drifted from codify-dark's own tokens: "
            f"{ {k: f'fallback {f}, codify-dark {d}' for k, (f, d) in drifted.items()} }. "
            "Either codify-dark's token changed and the fallback should follow it, or "
            "the fallback is deliberately different and the docstring in "
            f"{TERMINAL_TS.name} should stop calling it the default theme's values",
        )

    def test_a_document_claiming_the_terminal_is_themed_is_backed_by_the_wiring(self) -> None:
        """The claim on the documentation side, and the wiring it rests on.

        Bidirectional on purpose. A document that says the terminal follows the
        theme must be able to point at code that does it, or the sentence is a
        wish. And a terminal layer that exists but is not wired into the pane is
        a module nobody uses — the defect `ui/tests/terminalTheme.test.ts` guards
        against, restated here against the documents that make the claim.
        """
        claimed = [
            path.name
            for path in THEME_DOCS
            if TERMINAL_THEMED_CLAIM.search(_read(path))
        ]
        self.assertTrue(
            claimed,
            "no document states that the terminal follows the theme, so the claim "
            "this module checks does not exist. `ui/src/terminalTheme.ts` exists and "
            "the terminal was the one surface CSS could not reach — say so in the "
            "README's Appearance section, and this check will hold it to the wiring",
        )

        source = _read(TERMINAL_PANE)
        self.assertRegex(
            source,
            r"xtermThemeFromDocument\(\)",
            "a document says the terminal follows the theme, but TerminalPane.tsx "
            "never calls xtermThemeFromDocument(). The module exists and the pane "
            "does not use it",
        )
        stripped = re.sub(r"/\*[\s\S]*?\*/", "", source)
        stripped = re.sub(r"//.*$", "", stripped, flags=re.M)
        literals = re.findall(r"#[0-9a-fA-F]{6}\b", stripped)
        self.assertEqual(
            [],
            literals,
            f"TerminalPane.tsx hard-codes {literals}; a terminal that carries its own "
            "colours is a terminal a theme cannot reach, whatever the module says",
        )

    def test_a_theme_change_repaints_a_live_terminal(self) -> None:
        """A terminal themed on open and stale after a switch is the same defect
        one moment later, so the pane has to subscribe *and* unsubscribe.

        Both halves are asserted as the call that makes them, not as the event's
        name appearing somewhere. Asserting the bare name passed for the wrong
        reason when this was first written: deleting the `addEventListener` line
        left the import and the `removeEventListener` cleanup both still naming
        `THEME_CHANGE_EVENT`, so the check stayed green on a pane that no longer
        repaints. Mutation testing is the only reason that was found — the code
        it was checking was correct, and the check was not.
        """
        source = _read(TERMINAL_PANE)
        self.assertRegex(
            source,
            r"addEventListener\(\s*THEME_CHANGE_EVENT",
            "TerminalPane.tsx never subscribes to THEME_CHANGE_EVENT, so a terminal "
            "opened before a theme switch keeps the old theme's colours until the "
            "pane is closed and reopened. The name may appear in the import and in "
            "the cleanup; this needs the call that actually listens",
        )
        self.assertRegex(
            source,
            r"removeEventListener\(\s*THEME_CHANGE_EVENT",
            "TerminalPane.tsx subscribes to THEME_CHANGE_EVENT with nothing that "
            "unsubscribes, so every theme switch leaves another listener on the "
            "window, each one re-reading a computed style for a terminal that is "
            "still the same terminal",
        )


if __name__ == "__main__":
    unittest.main()
