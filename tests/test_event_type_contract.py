"""The engine's event union and the UI's renderers of it cannot drift apart.

`engine/models.py`'s `EventType`, `ui/src/types.ts`'s and the literal `docs/04`
§1.4 quotes are three hand-written lists of the same twenty names. Adding a
member to the engine's is the easy half and nothing has ever noticed the other
halves: the event is published into every goal's log, counted in the doc's
tables, and rendered by nobody. That is not a type error, because the UI's union
is only ever used as the type of `Event.type` — widening it is legal and silent
— so the drift shows up as a feature that emits into the void.

It is not hypothetical. `stage_result` has been in the engine union since the
per-stage timing work, `engine/metrics.py` reads it to build `StatsOverview
.by_stage`, and it was in *neither* of the other two lists: `GET /goals/{id}/events`
could return an event the UI's own `Event` interface could not name, and the one
place documenting each event's payload had no row for it. Writing this test is
what found that. `usage` is the other member with no renderer, and it is
deliberate — it is aggregated, not rendered (see `SERVED_ELSEWHERE`).

Four things are checked, in increasing order of how much they would have caught:

* **Parity.** All three lists are the same set. Either side drifting is a
  failure, and the message names the members that differ rather than dumping
  both lists. The doc's §1.4 payload table is checked against the union too: it
  is the only place a reader learns what an event carries, and a row that
  outlives its type is as wrong as a type with no row.
* **Consumption.** Every member is either branched on somewhere in `ui/src` or
  exempt. "Branched on" is a scan, and a scan is only as good as its spellings,
  so it accepts the two the UI could plausibly use — a `.type` comparison (the
  subject is the left operand in every site today; equality is symmetric, but a
  mirrored spelling is a spelling this scan has never had to read) and a `case`
  label inside a `switch` whose discriminant is a `.type`. A string literal that merely
  *looks* like an event type does not count, which is why this is a scan over
  comparison sites and not a `grep` for the name: `"usage"` in
  `statsHistory.ts` is a *field* of `StatsHistoryDay`, and counting it would
  have made the one honest exemption look like a lie.
* **The exemptions are still true.** Each entry in `SERVED_ELSEWHERE` is a
  claim — this event reaches the user through an aggregate rather than through
  the stream — and the claim is checked against both sides: the engine anchor
  must still exist, the UI anchor must still exist, and the member must *still*
  not be rendered. An entry that stops being needed is reported as stale, so the
  table cannot quietly outlive the exemptions it justifies.
* **The scan is not vacuous.** A regex that matches nothing, or a `ui/src` path
  that no longer exists, would make the three tests above pass forever. This one
  asserts the scan found the bulk of the union, and that a name nothing in the
  UI mentions is *not* reported as consumed.

`ui/tests/` is deliberately outside the scan. A test asserting on an event type
is not the UI consuming it, and letting `ui/tests` satisfy the contract would
make the check about the tests rather than about the product.

A static scan cannot prove that a renderer is correct, or even that the branch
it found is reachable. It freezes decision sites, which is the point: an event
type that reaches `ui/src` without a decision site, or without an exemption
that says where it is served instead, cannot arrive unannounced.
"""

from __future__ import annotations

import re
import typing
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

from engine.models import EventType

ROOT = Path(__file__).resolve().parent.parent
UI_SRC = ROOT / "ui" / "src"
UI_TYPES = UI_SRC / "types.ts"
# `docs/04` §1.4 quotes the union and tabulates a payload per member. It is a
# third copy, and it had already fallen behind: `stage_result` was in the union,
# in the payload table and in neither the quoted literal nor the UI.
DOC_04 = ROOT / "docs" / "04-engine-data-and-runtime.md"

# The engine's union, read from the `Literal` itself rather than from a copy, so
# a member added to `engine/models.py` is visible to this test with no edit here.
ENGINE_EVENT_TYPES: tuple[str, ...] = typing.get_args(EventType)


class Exemption(NamedTuple):
    """An event type the UI does not render, and where it is served instead."""

    #: The engine-side fact the claim rests on, as a regex over that file.
    engine_anchor: str
    engine_file: str
    #: The UI-side fact: the read that stands in for the missing renderer.
    ui_anchor: str
    ui_file: str
    #: Why the aggregate is the right shape for this event.
    why: str


# Event types that reach the user through an aggregate rather than through the
# event stream. Each is a claim about *where* the information surfaces, and each
# is checked below against both the engine that serves it and the UI that reads
# it — so renaming the aggregate, dropping the route, or reverting to rendering
# the event directly each fail here rather than leaving a stale justification.
SERVED_ELSEWHERE: dict[str, Exemption] = {
    "usage": Exemption(
        engine_anchor=r"def _usage_from_events\(",
        engine_file="engine/app.py",
        ui_anchor=r"export async function getGoalUsage\(",
        ui_file="ui/src/api.ts",
        why=(
            "one `usage` event per model call, folded into a per-goal total by "
            "`_usage_from_events` and served as `GET /goals/{goal_id}/usage`. "
            "Rendering the raw events would show a running column of numbers, "
            "which is what the total is for; ChatTimeline reads the aggregate."
        ),
    ),
    "stage_result": Exemption(
        engine_anchor=r'"stage_result"',
        engine_file="engine/metrics.py",
        ui_anchor=r"stats\.by_stage",
        ui_file="ui/src/components/StatsPanel.tsx",
        why=(
            "per-stage cost and outcome, folded across goals by `engine/metrics.py` "
            "into `StatsOverview.by_stage` and read by the stage table. The "
            "per-goal numbers that reach the UI already come through the "
            "aggregate, so a second read of the same events in the timeline "
            "would be a second answer to one question."
        ),
    ),
}

# `ui/src/types.ts` declares the union; it is not a consumer of it. Excluding
# the file rather than the members is deliberate — a narrowing helper added
# beside the union would be a real consumer, and the union itself is the one
# thing that must not count as a reason for its own members to exist.
def _ui_sources() -> list[Path]:
    found = sorted(UI_SRC.rglob("*.ts")) + sorted(UI_SRC.rglob("*.tsx"))
    found = [p for p in found if p != UI_TYPES]
    if not found:
        raise AssertionError(f"{UI_SRC} yielded no sources — the scan is pointed at nothing")
    return found


# Comments carry the reasons, not the decisions: `ChatTimeline` documents an
# event type in prose ("rendered here because…") and a scan that read comments
# would call that a renderer. `(?<!:)` keeps a `//` inside a URL intact.
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"(?<!:)//[^\n]*")


def _code_only(text: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _ui_event_types() -> tuple[str, ...]:
    """The members of the UI's `EventType`, in declaration order."""
    text = _code_only(UI_TYPES.read_text(encoding="utf-8"))
    match = re.search(r"export\s+type\s+EventType\s*=(.*?);", text, re.S)
    if match is None:
        raise AssertionError(
            f"no `export type EventType = …;` in {UI_TYPES} — the union moved or was "
            "renamed, so this test is comparing nothing against nothing"
        )
    members = tuple(re.findall(r"""["']([a-z_]+)["']""", match.group(1)))
    if len(members) < 15:
        raise AssertionError(
            f"the UI's EventType parsed to {len(members)} members ({members}) — the "
            "extractor is stale, not the union"
        )
    return members


# `ev.type === "diff"`, `e.type !== "step_status"`. The subject is the left
# operand in every site in the UI today, which is the only reason the scan does
# not also look for the mirrored spelling.
_TYPE_COMPARISON = re.compile(r"""\.type\s*(?:===|!==|==|!=)\s*(['"])([a-z_]+)\1""")
_SWITCH_HEAD = re.compile(r"switch\s*\(([^)]*)\)\s*\{")
_CASE_LABEL = re.compile(r"""case\s+(['"])([a-z_]+)\1\s*:""")


def _switched_case_types(text: str) -> Iterator[tuple[int, str]]:
    """`(offset, name)` for each `case` label of a switch over a `.type`.

    Restricted to switches whose discriminant ends in `.type`: a `switch` over a
    Tauri command name in `api.ts` compares string literals to string literals
    and means something else entirely.
    """
    for head in _SWITCH_HEAD.finditer(text):
        if not head.group(1).rstrip().endswith(".type"):
            continue
        depth = 0
        end = len(text)
        for index in range(head.end() - 1, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        for label in _CASE_LABEL.finditer(text, head.end(), end):
            yield label.start(), label.group(2)


def _doc_event_types() -> tuple[str, ...]:
    """The members of the `EventType` literal `docs/04` §1.4 quotes."""
    text = DOC_04.read_text(encoding="utf-8")
    match = re.search(r"EventType = Literal\[(.*?)\]", text, re.S)
    if match is None:
        raise AssertionError(f"{DOC_04} no longer quotes `EventType = Literal[…]`")
    return tuple(re.findall(r'"([a-z_]+)"', match.group(1)))


def _doc_payload_rows() -> tuple[str, ...]:
    """The types the §1.4 payload table gives a row, in table order."""
    rows = re.findall(r"^\| `([a-z_]+)` \|", DOC_04.read_text(encoding="utf-8"), re.M)
    if len(rows) < 15:
        raise AssertionError(
            f"the §1.4 payload table parsed to {len(rows)} rows ({rows}) — the table "
            "moved or was reformatted, so this test is checking nothing"
        )
    return tuple(rows)


def _consumed() -> dict[str, list[str]]:
    """`{event type: ["ui/src/goalStream.ts:49", …]}` — where the UI branches on it."""
    found: dict[str, list[str]] = {}
    for path in _ui_sources():
        rel = path.relative_to(ROOT)
        text = _code_only(path.read_text(encoding="utf-8"))
        for match in _TYPE_COMPARISON.finditer(text):
            found.setdefault(match.group(2), []).append(f"{rel}:{_line_of(text, match.start())}")
        for offset, name in _switched_case_types(text):
            found.setdefault(name, []).append(f"{rel}:{_line_of(text, offset)}")
    return found


class UnionParity(unittest.TestCase):
    """Every copy of the twenty names is the same list."""

    def test_the_ui_union_is_the_engine_union(self) -> None:
        engine, ui = set(ENGINE_EVENT_TYPES), set(_ui_event_types())
        self.assertEqual(
            [], sorted(engine - ui) + sorted(ui - engine),
            "\n`engine/models.py`'s EventType and `ui/src/types.ts`'s are one list"
            "\nwritten twice, and they have come apart."
            + (f"\n  the engine publishes, the UI cannot name: {sorted(engine - ui)}" if engine - ui else "")
            + (f"\n  the UI can name, the engine never publishes: {sorted(ui - engine)}" if ui - engine else ""),
        )

    def test_both_unions_are_non_empty(self) -> None:
        self.assertTrue(ENGINE_EVENT_TYPES, "engine/models.py's EventType has no members")
        self.assertTrue(_ui_event_types(), "ui/src/types.ts's EventType has no members")

    def test_the_quoted_literal_in_docs_04_is_the_engine_union(self) -> None:
        self.assertEqual(
            [], sorted(set(ENGINE_EVENT_TYPES) - set(_doc_event_types())),
            "\ndocs/04 §1.4 quotes an `EventType` that no longer matches "
            "`engine/models.py`'s: the engine publishes "
            f"{sorted(set(ENGINE_EVENT_TYPES) - set(_doc_event_types()))} and the doc "
            "does not list it. The §1.4 payload table says a new event type means a "
            "new row here in the same change; the literal above it is the same list.",
        )

    def test_every_published_type_has_a_payload_row_in_docs_04(self) -> None:
        missing = sorted(set(ENGINE_EVENT_TYPES) - set(_doc_payload_rows()))
        self.assertEqual(
            [], missing,
            f"\ndocs/04 §1.4 documents no payload for {missing}. The table is the only"
            "\nplace a reader learns what an event carries.",
        )


class EveryEventIsRendered(unittest.TestCase):
    """The freeze: no member of the union reaches the UI with nothing reading it."""

    def test_every_event_type_is_rendered_or_exemptly_served(self) -> None:
        consumed = _consumed()
        ignored = [
            f'  "{member}" — published into every goal\'s log, typed for in the UI, '
            "rendered by nothing"
            for member in sorted(set(ENGINE_EVENT_TYPES) - set(consumed) - set(SERVED_ELSEWHERE))
        ]
        self.assertEqual(
            [], ignored,
            "\nAn event type the UI has no reader for. Either it is noise and the"
            "\nengine should stop publishing it, or somebody has to render it —"
            "\nChatTimeline.tsx is the single place every event type is drawn. If"
            "\nit is deliberately an aggregate instead, add it to SERVED_ELSEWHERE"
            "\nnaming where the user sees it, and that claim gets checked too.",
        )

    def test_a_ui_reader_is_not_mistaken_for_a_renderer(self) -> None:
        """`StatsHistoryDay["usage"]` is a field, and must not read as an event."""
        consumed = _consumed()
        self.assertNotIn(
            "usage", consumed,
            "the scan matched a string literal rather than a comparison site, so an"
            " exemption can be satisfied by a name that merely looks like an event type",
        )


class ExemptionsAreTrue(unittest.TestCase):
    """Each exemption is a claim about two files, and both are re-read."""

    def test_every_exemption_still_describes_a_live_aggregate(self) -> None:
        broken: list[str] = []
        for member, exemption in sorted(SERVED_ELSEWHERE.items()):
            engine_text = (ROOT / exemption.engine_file).read_text(encoding="utf-8")
            if not re.search(exemption.engine_anchor, engine_text):
                broken.append(
                    f'  "{member}" — {exemption.engine_file} no longer matches '
                    f"{exemption.engine_anchor!r}, so the event is not served the way "
                    "this entry says it is"
                )
            ui_text = (ROOT / exemption.ui_file).read_text(encoding="utf-8")
            if not re.search(exemption.ui_anchor, ui_text):
                broken.append(
                    f'  "{member}" — {exemption.ui_file} no longer matches '
                    f"{exemption.ui_anchor!r}, so nothing reads the aggregate the "
                    "user is meant to see instead"
                )
        self.assertEqual(
            [], broken,
            "\nSERVED_ELSEWHERE justifies an event type the UI does not render. A"
            "\nbroken anchor means the justification is no longer true, which is"
            "\nindistinguishable from the event reaching nobody at all.",
        )

    def test_the_exemptions_never_outlive_what_they_exempt(self) -> None:
        consumed = _consumed()
        stale: list[str] = []
        for member in sorted(SERVED_ELSEWHERE):
            if member in consumed:
                sites = ", ".join(consumed[member][:3])
                stale.append(f'  "{member}" — rendered after all ({sites}); drop the exemption')
            if member not in set(ENGINE_EVENT_TYPES):
                stale.append(f'  "{member}" — no longer an EventType; drop the exemption')
        self.assertEqual(
            [], stale,
            "\nThe table must name only live exemptions, or it becomes a place where"
            "\na new event type can hide.",
        )


class TheScanWorks(unittest.TestCase):
    """Guard the guard. A regex that matches nothing passes the three above forever."""

    def test_the_scan_finds_the_events_it_is_meant_to_find(self) -> None:
        consumed = _consumed()
        found = set(consumed) & set(ENGINE_EVENT_TYPES)
        self.assertGreaterEqual(
            len(found), 15,
            f"the scan found only {len(found)} of {len(ENGINE_EVENT_TYPES)} members"
            f" ({sorted(found)}) — it is matching less than the UI does, so a new"
            " event type could arrive unrendered and this suite would stay green",
        )
        self.assertGreaterEqual(
            sum(len(sites) for sites in consumed.values()), 20,
            "the scan found almost no comparison sites at all — check the patterns",
        )

    def test_a_name_nothing_mentions_is_not_reported_as_consumed(self) -> None:
        consumed = _consumed()
        for phantom in ("an_event_type_nobody_uses", "goal_status_v2"):
            self.assertNotIn(phantom, consumed, "the scan matches arbitrary identifiers")

    def test_the_comparison_sites_are_the_ones_the_ui_really_has(self) -> None:
        """Spot-check the scan against a site that must be found, by hand."""
        consumed = _consumed()
        self.assertTrue(
            any(site.startswith("ui/src/components/ChatTimeline.tsx:") for site in consumed.get("diff", [])),
            "ChatTimeline renders `diff`; if the scan cannot see that, it is broken",
        )


if __name__ == "__main__":
    unittest.main()
