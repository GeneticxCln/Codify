"""Seat a real page in the embedded browser and wait for its first paint.

**Why this exists.** Everything short of a paint is checkable without a
display, and the Rust tests check it: the capability set, the navigation
guard, the wiring of the embed. What none of them can claim is that a page
*actually renders* on a real Linux session, because the ways an embedded page
fails to first-paint all live outside the crate:

* the **single-instance guard** claims a D-Bus name inside `Builder::build()`,
  so a second launch (an instance left running, a stale one) exits before any
  webview exists — a "blocked first paint" that is really an exit;
* **WebKit's bubblewrap sandbox** can refuse to start the web process at all
  (`/usr/bin/bwrap`, `kernel.unprivileged_userns_clone`) — the page loads
  nothing and paints nothing;
* the **DMABUF renderer** can hang first paint on some driver/compositor
  combinations (the known workaround is `WEBKIT_DISABLE_DMABUF_RENDERER=1`,
  which this app deliberately never sets itself — the user decides).

`make smoke-embed` runs this script. It builds the shell if needed, launches
it with `CODEIFY_EMBED_SMOKE=<url>` — the mode that seats the page, listens
for the paint announcement, and never starts an engine — and relays the
output. The child prints four `[Codify] browser env:` diagnostics lines at
boot saying which of the facts above holds on this machine, announces
`embed-smoke: painted` when the page has composited two animation frames,
prints `embed-smoke: report …` each time the page describes what it is
showing, and fails itself after 20s. Both detectors are injected as
initialization scripts (`SMOKE_PAINT_SCRIPT` and `SMOKE_PROBE_SCRIPT` in
`src-tauri/src/browser.rs`); "two rAFs" proves the compositor consumed a
frame, which "document loaded" does not.

**Two deliverables, and the paint is only the first.** A paint says the embed
path works; it says nothing about whether the *site* works, and every page
composites a first frame in about a second — including one that is showing the
user an error. Measured on this checkout before the probe existed:
`www.youtube.com`, `accounts.google.com` and a GitHub issue each reported
`painted` in 1.2–3.6s, which said nothing at all about whether any of them
worked. So the run also asks the page what it is showing — and, when it shows
nothing, **why**: which subresources failed, what the page threw, what it
logged, how many subresources completed, its `readyState`, and its user agent.
The mechanism is what separates "this site does not work here" from "this
engine cannot render modern sites", and only the first of those turned out to be
true.

The answer is read into a named verdict: `rendered`,
`content-behind-an-iframe`, `refused-embedded-browser`, `no-media-codecs`,
`page-errored`, `document-arrived-dead`, `rendered-nothing`, `failed-to-load`,
`no-report`. The names are chosen so that a limit of the *measurement* never
reads as a fault in the *site* — `content-behind-an-iframe` exists because
`innerText` reaches the top document only, so a sign-in flow that puts its form
in a cross-origin frame renders perfectly and reads here as 135 characters,
which is what `accounts.google.com` measured.

**A second verdict, because "what did the page end up showing" is not the
question a blank page asks.** Every field the first report carries counts
something that *happened*: resources completed, scripts present, errors caught.
None of them can see a request still in flight, and that is the whole question
when a page is stuck — measured on `youtube.com` at 10s, 92 completed
subresources, `readyState` still `loading`, and a report that could say nothing
about what it was waiting for. Resource timing cannot close that gap twice
over: an entry exists only once a response has *finished*, and a cross-origin
response with no `Timing-Allow-Origin` produces no entry at all. So the page
also reports `q`, the requests that had not come back when it was asked, with
an age where it can give one, and the run reads that into its own verdict:
`loaded`, `waiting-on-request`, `waiting-on-media`, `load-never-fired`,
`no-stall-data`, `no-report`. `no-stall-data` is the load-bearing name — a
report from a build that predates the field, or a page that defeated the
patches, carries no `q` at all, and reading that as "nothing is outstanding"
would be the same confident mistake as reading a missing bridge line as a page
that said nothing.

**The channel is about 980 characters wide, and that is measured rather than
assumed.** The payload is percent-encoded JSON in a document title: a 907
character payload arrived whole and a 981 character one came back **cut off in
the middle of a field**, twice, at the same character. A cut payload decodes to
nothing, so without a check it is indistinguishable from a page that never
answered — and that is precisely what happened the first time a report grew:
a youtube.com run reported `no-report` about a page that had described itself
twice. So both ends spend the width deliberately — no query strings, a
64-character address, three outstanding rows, empty lists left out entirely, and
the user agent sent **once** because the shell set it before the page existed —
and the run **names** what is left over as `truncated-report` rather than
letting it read as silence.

The child's **stderr** is tapped as well as relayed: WebKit's own complaints
(`internallyFailedLoadTimerFired` and friends) arrive there, and a page that
stalls mid-load says so on that stream in terms no amount of staring at the
rendered page would produce.

The exit code is still deliberately narrow: 0 means *the embed path painted a
real page on this display and that page could describe itself*, nothing more.
A failure is diagnosed from what the child did and said, and the boot
diagnostics are replayed with every failure, because they are the part that
names which fact held.

Two of the ways a run measures nothing look identical from outside, and
telling them apart is the harness's job rather than the reader's:

* a **stale binary** prints neither the diagnostics nor the mode line — it
  printed neither, so it predates both, and it launched the whole app;
* the **single-instance guard** refused a second launch — the diagnostics are
  there, then silence. On Linux the plugin has the *newcomer* call the
  incumbent's callback and then `std::process::exit(0)`, all inside
  `Builder::build()` and therefore before this app's `setup` runs, so the
  refusal is silent and exits 0. The "Second launch…" line a person waits for
  is printed by the *other* instance; measured by running two smokes at once,
  one painted in 0.9s and the other died at 0.1s having said nothing.

One more thing this harness learned the hard way, and the reason the page
marks its paint in its own document title rather than invoking Tauri: a denied
invoke is silent. The page holding the empty `browser-*` capability set cannot
emit an event, so the first version of this smoke reported a 20s first-paint
timeout on a machine where the page was painting in 0.9s and every diagnostic
read healthy. A measurement that cannot report through the boundary it claims
to test will report that boundary as broken.
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import FrameType
from urllib.parse import unquote

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SHELL_BINARY = PROJECT_ROOT / "src-tauri" / "target" / "debug" / "codify-desktop"
SRC_TAURI = PROJECT_ROOT / "src-tauri"

DEFAULT_URL = "https://example.com/"
# The child fails itself at 20s; this bounds the whole child run with slack
# for a child that cannot even reach its own timeout (a wedged `build()`,
# a compositor stall).
DEFAULT_TIMEOUT_S = 45.0
# A binary that never reaches `setup` announces nothing — a stale one, or a
# launch the single-instance guard refused inside `build()`. Bound that wait
# separately and short: the mode line is printed in `setup`, so a current build
# says it within a second or two and neither of those cases ever will. Without
# this they cost the full 45s and report "no verdict".
ENGAGE_TIMEOUT_S = 10.0

PAINTED_LINE = "embed-smoke: painted"
MODE_LINE = "embed-smoke: mode engaged"
FAILED_PREFIX = "embed-smoke: FAILED"
SECOND_LAUNCH_MARKER = "[Codify] Second launch"
ENV_DIAGNOSTIC_PREFIX = "[Codify] browser env:"


def is_paint(line: str) -> bool:
    """The one line that means a frame was composited."""
    return line.strip() == PAINTED_LINE


# The page's own report of what it is showing, one line per announcement. The
# page re-announces as it renders (a site that paints first and fills in later
# is empty at load and real a second later), so the **last** one is the most
# complete and the only one worth reading.
#
# This is the shell's line, not the page's title: `browser.rs`'s
# `SMOKE_REPORT_LINE` is the other half of the pair, and its test reads this
# file, so the two spellings cannot drift — which matters because a drifted one
# prints every report and reads none.
REPORT_LINE = "embed-smoke: report "


def read_report(line: str) -> dict[str, object] | None:
    """The decoded page report on this line, or None for anything else.

    Percent-decoded JSON, because the payload rides in a document title and a
    title cannot hold a raw ``{"u": …}`` without quoting trouble on three
    layers. A line that does not decode is None rather than an exception: the
    report is a fact about a page, and a page that cannot describe itself is
    itself a finding, not a crash in the thing doing the measuring.
    """
    stripped = line.strip()
    if not stripped.startswith(REPORT_LINE):
        return None
    payload = stripped[len(REPORT_LINE) :].strip()
    try:
        decoded = json.loads(unquote(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def is_truncated_report(line: str) -> bool:
    """A report arrived and did not survive the channel it rides in.

    The payload is percent-encoded JSON in a document title, and the title has
    a width: measured on this checkout, a report past roughly a kilobyte of
    encoded text comes back **cut off mid-string**, so it decodes to nothing at
    all. Without this check that is indistinguishable from a page that never
    answered — the same mistake a missing bridge line used to cause, in a
    different place, and the one that made a run report a rich page as
    `no-report`. The fix is a smaller report from the page; the name is here so
    the run can say which of the two it was looking at.
    """
    stripped = line.strip()
    return stripped.startswith(REPORT_LINE) and read_report(stripped) is None


# What a report *means*, as a name a test or a person can hold. Kept pure and
# kept here rather than in the run, so every claim below is checkable without a
# display — the display is the slow, flaky half of this harness, and the
# judgement is the half worth freezing.
#
# The ordering is the argument. A refusal outranks everything: a site that has
# decided this browser is unacceptable says so in text, and reading that as
# "no media support" or "no content" would explain a *symptom* and send
# someone to fix the wrong thing. Only once the page is known not to be
# refusing do its emptiness and its codecs count as evidence.
def _count(report: dict[str, object], key: str) -> int:
    """A count from a report, tolerating whatever the page put there.

    The report is a page's own account of itself, which is exactly the kind of
    value that arrives as the wrong type. One coercion, typed once, beats
    `int(x or 0)` sprinkled with ignores — and a count that reads 0 is the
    honest reading of a field that is not a number, not a crash in the harness.
    """
    value = report.get(key)
    return value if isinstance(value, int) else 0


def _markers(report: dict[str, object]) -> list[str]:
    """The refusal sentences the page said it was showing."""
    found = report.get("e")
    if not isinstance(found, list):
        return []
    return [str(m) for m in found]


def _entries(report: dict[str, object], key: str) -> list[str]:
    """A list field from a report, tolerating whatever the page put there."""
    found = report.get(key)
    if not isinstance(found, list):
        return []
    return [str(item) for item in found]


def classify_report(
    report: dict[str, object] | None, *, truncated: bool = False
) -> str:
    """A verdict name for one page report, or for its absence.

    `truncated` is the distinction that keeps a *harness* limit out of the
    site's column: a report that was cut by the title channel is not a page
    that said nothing, it is a page that said something the harness could not
    hold, and the fix is a smaller report rather than a different site.
    """
    if report is None:
        return "truncated-report" if truncated else "no-report"
    if _markers(report):
        return "refused-embedded-browser"
    if str(report.get("u") or "").startswith("chrome-error://"):
        return "failed-to-load"
    media = _count(report, "v") + _count(report, "d")
    if media and not (report.get("h") or report.get("c")):
        return "no-media-codecs"
    thin = _count(report, "n") < 200
    if thin and _count(report, "f"):
        # A framed page that reads as empty is the **probe's** limit, not the
        # site's. `innerText` reaches the top document only, so a sign-in flow
        # that puts its form in a cross-origin frame renders perfectly and is
        # reported here as 135 characters. Measured: accounts.google.com, a
        # working page, one frame. Calling that "rendered nothing" would blame
        # the site for the harness's blindness, and the fix for a broken page
        # is not the fix for a blind probe.
        return "content-behind-an-iframe"
    if thin and (_entries(report, "x") or _entries(report, "j")):
        # A blank page **with a reason**: a subresource that failed to load, or
        # an error the page threw. The mechanism is what makes this actionable,
        # and it is a different bug from a page that loaded and drew nothing.
        return "page-errored"
    if thin and _count(report, "sc") and _count(report, "rp") == 0:
        # Scripts present, nothing fetched, nothing drawn: the document arrived
        # and the page never got going. Named separately because the fix is not
        # in the renderer.
        return "document-arrived-dead"
    if thin:
        return "rendered-nothing"
    return "rendered"


def report_sentence(report: dict[str, object], verdict: str) -> str:
    """One line a person reads, for a report and the name it earned.

    The second line is the mechanism, and it is printed whenever there is one
    to print: a verdict that says "nothing rendered" without saying "these
    four subresources failed" makes the reader guess, and guessing is what
    turns a network fault into a renderer bug in somebody's head.
    """
    title = str(report.get("t") or "(no title)")
    url = str(report.get("u") or "(no url)")
    media = _count(report, "v") + _count(report, "d")
    facts = [
        f"title {title!r}",
        f"{_count(report, 'n')} chars of text, {media} media element(s), "
        f"{_count(report, 'f')} frame(s)",
        f"h264={report.get('h') or 'no'} aac={report.get('c') or 'no'}",
    ]
    refusals = _markers(report)
    if refusals:
        facts.append(f"page says {', '.join(repr(r) for r in refusals)}")
    line = f"  {verdict}: {url}\n    " + "; ".join(facts)
    mechanism = report.get("ua") is not None or _entries(report, "x")
    if not mechanism:
        return line
    # The load's own shape: what was asked for, what arrived, what broke.
    # `rp` is **completed** subresources — a resource timing entry only exists
    # once the response finished — so "loading, with 20 completed" says the
    # stall is after the fetching, in the page's own script.
    line += (
        f"\n    load: readyState={report.get('rs') or '?'} after "
        f"{_count(report, 'ms')}ms, {_count(report, 'sc')} script(s), "
        f"{_count(report, 'lk')} stylesheet(s), "
        f"{_count(report, 'rp')} subresource(s) completed"
    )
    if report.get("ua"):
        line += f"\n    ua: {report['ua']}"
    for label, key in (("failed", "x"), ("page errors", "j"), ("console", "c9")):
        items = _entries(report, key)
        if items:
            listed = "; ".join(item for item in items)
            line += f"\n    {label}: {listed}"
    return line


# ── which request is outstanding ──
#
# `classify_report` answers "what did the page end up showing"; this answers
# "what is it still waiting for", which is the question a blank or half-drawn
# page actually asks. The page carries the field (`q`, and `le` for the
# document's own load timestamp), and every name below is a claim about the
# *measurement* as much as about the site.
STALL_LOADED = "loaded"
STALL_ON_REQUEST = "waiting-on-request"
STALL_ON_MEDIA = "waiting-on-media"
STALL_LOAD_NEVER_FIRED = "load-never-fired"
STALL_NO_DATA = "no-stall-data"
STALL_NO_REPORT = "no-report"

#: How many outstanding requests a sentence names. The page caps the list at
#: four (it rides in a document title), so this matches rather than exceeds
#: it, and the count above the cap is stated instead of silently dropped.
STALL_LISTED = 4


def _pending(report: dict[str, object]) -> list[dict[str, object]] | None:
    """The outstanding requests, or None when the page never measured them.

    None and not ``[]``, and the difference is the point: an empty list is the
    page saying it has nothing outstanding, and a missing field is the *harness*
    saying it never asked. Reading either as the other produces a confident
    sentence about a question nobody put.
    """
    rows = report.get("q")
    if not isinstance(rows, list):
        return None
    return [row for row in rows if isinstance(row, dict)]


def _age_ms(row: dict[str, object]) -> int | None:
    """How long a request has been outstanding, or None if it cannot say.

    The page sends -1 for a request it *found* in flight rather than watched
    leave, and a zero there would read as "just started" for something that has
    been waiting since before the probe looked. So -1, and anything else that
    is not a non-negative number, is no age at all.
    """
    value = row.get("ms")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def classify_stall(
    report: dict[str, object] | None, *, truncated: bool = False
) -> str:
    """A verdict name for what a page is still waiting on, or for its absence.

    The ordering is the argument, and it is the same one `classify_report`
    makes. A document that has finished loading is named `loaded` first and
    unconditionally: an XHR a page deliberately left open is a poll, not a
    stall, and calling it one sends the reader after a request that was never
    going to be the problem. Only once the document is still loading does what
    is outstanding become evidence.

    **What the name does not claim.** An outstanding request is a request the
    page has not got back, which is a fact. That it is *what is holding the
    document* is a different claim, and the page cannot make it: the load
    event does not wait for a `fetch`, so an open XHR and an undelayed subresource
    can both be true at once, with only one of them being the story. The
    sentence therefore names what has not come back and prints the last thing
    that did arrive, and leaves the reader to look at the gap.
    """
    if report is None:
        # A report the title channel cut is not a page with nothing pending —
        # the half of the payload that would have said so is exactly what did
        # not arrive.
        return STALL_NO_DATA if truncated else STALL_NO_REPORT
    pending = _pending(report)
    if pending is None:
        return STALL_NO_DATA
    state = str(report.get("rs") or "")
    if state in ("interactive", "complete"):
        return STALL_LOADED
    kinds = {str(row.get("k") or "") for row in pending}
    if kinds - {"media"}:
        # A fetch or an XHR is unambiguously a request the page is waiting on,
        # and it is worth naming even when a media element is outstanding too.
        return STALL_ON_REQUEST
    if "media" in kinds:
        return STALL_ON_MEDIA
    if state == "loading":
        # Nothing outstanding the probe can see, and the document has not
        # finished. Something *is* outstanding and it is something the probe
        # cannot see: a cross-origin subresource with no `Timing-Allow-Origin`
        # produces no resource timing entry at all, and a subresource the page
        # requested directly — an `<img>`, a `<script>`, a `<link>` — is never
        # observed from here either. The name says the load never fired; the
        # sentence below says why "nothing is pending" is not the finding.
        return STALL_LOAD_NEVER_FIRED
    return STALL_NO_DATA


def _stall_entry(row: dict[str, object]) -> str:
    """One outstanding request as a person reads it: method, address, age."""
    kind = str(row.get("k") or "request")
    method = str(row.get("m") or "GET").upper()
    address = str(row.get("u") or "(address unreadable)")
    age = _age_ms(row)
    waited = f", waiting {age}ms" if age is not None else ""
    return f"{method} {address} ({kind}{waited})"


def _oldest_first(row: dict[str, object]) -> int:
    """Sort key: the age, or -1 for an entry the page could not date.

    The reader re-sorts rather than trusting the page's order, for the same
    reason it re-checks every type: the array arrived from a web page, and the
    one entry a reader most wants is the one that has waited longest. A `-1`
    here keeps an undated request *after* every dated one, so "no age" never
    borrows the authority of the longest wait on the page.
    """
    age = _age_ms(row)
    return -1 if age is None else age


def stall_sentence(report: dict[str, object] | None, verdict: str) -> str:
    """One block a person reads, for what a page is waiting on and its name.

    Where there is no age to give, none is printed: the sentence that says a
    request has been outstanding for 0ms is worse than the sentence that says
    nothing about how long, because the first one looks like a measurement.
    """
    if report is None and verdict == STALL_NO_REPORT:
        return (
            f"  {verdict}\n    the page never described itself, so nothing can "
            "be said about what it is waiting for"
        )
    # A `None` report with any other verdict is a report that **was** cut off
    # by the channel, and it falls through to the `no-stall-data` sentence
    # below: saying a page that did answer "never described itself" is the
    # same mistake one level down.
    pending = (_pending(report) or []) if report is not None else []
    lines = [f"  {verdict}"]
    # The page spends a channel of about 980 characters, and when a report
    # would not fit it gives up its least useful fields and *says which* in
    # `df`. Naming that is the difference between "this build does not measure
    # stalls" and "this page was too big to carry the measurement", which are
    # different problems with different fixes.
    dropped = report.get("df") if report is not None else None
    if isinstance(dropped, str) and dropped.strip():
        lines.append(
            f"    the page dropped {dropped} from this report to fit the title "
            "channel, so what it is waiting for is unknown rather than nothing"
        )
    listed = sorted(pending, key=_oldest_first, reverse=True)
    for row in listed[:STALL_LISTED]:
        lines.append(f"    outstanding: {_stall_entry(row)}")
    if len(pending) > STALL_LISTED:
        lines.append(f"    … and {len(pending) - STALL_LISTED} more outstanding")
    if any(str(row.get("m") or "").upper() == "MEDIASOURCE" for row in listed):
        # A `blob:` URL is a handle on a Media Source stream, not an address on
        # the network, and the requests behind it are invisible to the page's own
        # instrumentation. Printed as one, it would name a "request" to a URL
        # that is not one, and the reader would go looking for a fetch that
        # never happened.
        lines.append(
            "    a blob: URL is a Media Source handle, not an address on the "
            "network: the bytes come from requests the page cannot see, so the "
            "one behind it is not named here"
        )
    load_end = report.get("le") if report is not None else None
    state = str(report.get("rs") or "") if report is not None else ""
    if isinstance(load_end, int) and not isinstance(load_end, bool):
        if load_end == 0 and state in ("interactive", "complete"):
            # Measured: `readyState` `complete` and `loadEventEnd` `0` in the
            # same report, four runs in seventeen. The two are different
            # questions to the engine and it answers them at different moments,
            # so the page prints the disagreement rather than the more alarming
            # half of it: "the load event never fired" beside a verdict that
            # says the document finished is a sentence nobody can act on, and
            # read quickly it is a false alarm about the site.
            lines.append(
                "    the document reports itself finished while its own "
                "navigation timing still shows no load event — the two answers "
                "disagree, which is a fact about what this page can see, not "
                "about the site"
            )
        elif load_end == 0:
            lines.append("    the document's own load event has never fired")
        elif load_end > 0:
            lines.append(f"    the document's load event fired at {load_end}ms")
    arrival = report.get("lz") if report is not None else None
    if isinstance(arrival, str) and arrival.strip():
        # The other half of the same question, from resource timing: `q` says
        # what has not come back and this says what did, last. On a stuck page
        # the gap between the two is the finding.
        lines.append(f"    last thing that arrived: {arrival}")
    if verdict == STALL_LOAD_NEVER_FIRED:
        lines.append(
            "    the probe sees nothing outstanding and the document has not "
            "finished, so what it waits for is something this probe cannot see: "
            "a cross-origin subresource with no Timing-Allow-Origin produces no "
            "resource timing entry at all, and a subresource the page asked for "
            "itself (an <img>, a <script>, a <link>) is never observed from here"
        )
    elif verdict == STALL_NO_DATA:
        lines.append(
            "    this report carries no outstanding-request field — a report "
            "the title channel cut off mid-string, a page that dropped it to "
            "fit, a build older than the probe that adds one, or a page that "
            "defeated the patches. 'Nothing is outstanding' must not be read "
            "from any of those."
        )
    return "\n".join(lines)


# The bridge leg: the page read back through the same channel the AI uses.
#
# This is a *different kind* of fact from the two above. The paint and the
# report are the page talking about itself, over the document title. This one
# is the shell asking a question and a string coming back — eval into a
# `browser-*` webview, out through the `codify-bridge` custom scheme, through
# chunk reassembly, into JSON. Everything the model's read depends on is in
# that path, and none of it is reachable from a unit test.
#
# It is also the leg that most easily fails *quietly*: a page that renders
# perfectly can have every way of talking to the shell blocked by its own
# Content-Security-Policy, and the symptom a reader would otherwise see is
# "the AI says the page is blank". So this leg is required by default and the
# harness fails when the line never arrives.
#
# `webview_bridge::SMOKE_BRIDGE_LINE` is the other half of the pair, and a
# Rust test reads this file to keep the two spellings identical — a drifted
# prefix prints every answer and reads none, which looks exactly like a page
# that refused to answer.
BRIDGE_LINE = "embed-smoke: bridge "
BRIDGE_OK = "ok "
BRIDGE_FAIL = "fail "


def read_bridge(line: str) -> tuple[str, dict[str, object]] | None:
    """The bridge verdict and payload on this line, or None for anything else.

    Returns the verdict and the payload separately because they answer
    different questions: `ok` means the round trip completed, and the payload
    is what it completed *with*. A page that answered with an empty body has
    completed the trip and failed the site, which is a different report from a
    page that never answered at all.
    """
    stripped = line.strip()
    if not stripped.startswith(BRIDGE_LINE):
        return None
    payload = stripped[len(BRIDGE_LINE) :].strip()
    # The bare word, not just the word plus a space: a failure line with no
    # reason after it is still a failure, and matching only `"fail "` let the
    # shortest one through as None — which reads as "no answer arrived" and so
    # reports a refused navigation as a page that never spoke.
    if payload == "fail" or payload.startswith(BRIDGE_FAIL):
        reason = payload[len("fail") :].strip()
        return "fail", {"error": reason or "no reason given"}
    if not payload.startswith(BRIDGE_OK):
        return None
    body = payload[len(BRIDGE_OK) :].strip()
    try:
        decoded = json.loads(body) if body else {}
    except (ValueError, json.JSONDecodeError):
        return "fail", {"error": f"the answer was not JSON: {body[:120]}"}
    return ("ok", decoded) if isinstance(decoded, dict) else (
        "fail",
        {"error": "the answer was not a JSON object"},
    )


def _with_carried_fields(reports: list[dict[str, object]]) -> dict[str, object]:
    """The last report, plus the one field a later report may legitimately omit.

    The page sends its user agent **once** — it cannot change, and it is the
    largest field that does not vary between announcements, on a channel
    measured at about 980 characters. The reader therefore keeps the first one
    it saw, so the run's verdict still quotes the string the page read.

    Only `ua`. Every other field a report can change is read from the *last*
    report and nothing is carried forward, because a page that failed a script
    at 800ms and stopped failing at 2s must not go on being reported as
    failing: a stale mechanism is worse than a missing one, because it reads
    as measured.
    """
    final = dict(reports[-1])
    if "ua" not in final:
        for earlier in reversed(reports[:-1]):
            agent = earlier.get("ua")
            if isinstance(agent, str) and agent:
                final["ua"] = agent
                break
    return final


def classify_bridge(verdict: str, payload: dict[str, object]) -> str:
    """A verdict name for one page read.

    `never-answered` is the one that matters most and the one a plain
    "did anything come back" check would miss: it is what a Content-Security-
    Policy blocking `connect-src`, `img-src` and `sendBeacon` looks like from
    here, and it is a broken feature rather than a broken site.
    """
    if verdict == "fail":
        return "never-answered"
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        # The trip completed and the page had nothing to say. That is a fact
        # about the page — a canvas, a video, or a body that has not rendered
        # yet — and it is not the same failure as no answer at all.
        return "answered-nothing"
    return "answered"


def bridge_clause(verdict: str) -> str:
    """What the final PASS line may claim about the bridge leg.

    One function because the claim is the dangerous part. The line used to be
    `", described itself, and read back through the bridge"` whenever *any*
    `embed-smoke: bridge` line had been parsed — including one that said the
    page never answered. That is a run that printed `bridge fail … did not
    answer within 15s` and then reported that the page had been read back,
    which is the one sentence a person skimming the output would believe.

    So the clause is derived from the verdict and nothing else, and a leg that
    never reached the page gets no clause at all: it cannot be summarised
    without claiming something false.
    """
    return {
        "answered": ", described itself, and read back through the bridge",
        "answered-nothing": (
            ", described itself, and was reached through the bridge — the page "
            "had no text to give"
        ),
    }.get(verdict, ", described itself")


def bridge_sentence(verdict: str, payload: dict[str, object], name: str) -> str:
    """One line a person reads, for a page read and the name it earned.

    Prints the page's own title and address back. That is the evidence that a
    round trip happened rather than merely that a line appeared: a report of
    "answered 4211 characters from `Example Domain`" cannot be produced by a
    shell that never reached the page.
    """
    if verdict == "fail":
        return f"  {name}: {payload.get('error') or 'the page never answered'}"
    title = str(payload.get("title") or "(no title)")
    url = str(payload.get("url") or "(no url)")
    links = payload.get("links")
    link_count = len(links) if isinstance(links, list) else 0
    facts = [
        f"{_count(payload, 'chars')} chars read back",
        f"readyState={payload.get('ready_state') or '?'}",
    ]
    if payload.get("truncated"):
        facts.append("truncated to the read's budget")
    if link_count:
        facts.append(f"{link_count} link(s)")
    return f"  {name}: {url}\n    " + "; ".join(
        [f"title {title!r}"] + facts
    )


def is_mode_engaged(line: str) -> bool:
    """The child's own confirmation that it is in smoke mode, not the app.

    Without it a stale binary is indistinguishable from a hung one: both
    simply never paint. This is the line that tells them apart.
    """
    return line.strip().startswith(MODE_LINE)


def paint_failure(line: str) -> str | None:
    """The child's own timeout sentence, or None for anything else."""
    stripped = line.strip()
    if stripped.startswith(FAILED_PREFIX):
        return stripped
    return None


def is_second_launch(line: str) -> bool:
    """The single-instance guard's exit line — the run ended in `build()`."""
    return SECOND_LAUNCH_MARKER in line


def is_env_diagnostic(line: str) -> bool:
    """One of the four `[Codify] browser env:` lines."""
    return line.startswith(ENV_DIAGNOSTIC_PREFIX)


# WebKit's own complaints, which arrive on the child's **stderr** and are the
# most direct evidence there is that a page never arrived. Measured on this
# checkout: a YouTube run painted and then printed
#
#     ERROR: WebKit encountered an internal error. This is a WebKit bug.
#     .../WebLoaderStrategy.cpp(640) : void WebKit::WebLoaderStrategy::
#         internallyFailedLoadTimerFired()
#
# which is the *network process* failing a load outright, and it scrolled past
# unread because stderr was inherited. Tapping the stream keeps the
# "reaches the terminal live" property and makes the lines usable at all.
WEBKIT_ERROR_MARKERS = (
    "ERROR: WebKit",
    "internallyFailedLoad",
    "WebProcess: Page load",
    "Failed to load",
)


def is_webkit_complaint(line: str) -> bool:
    """A WebKit-side error, from the web or network process."""
    return any(marker in line for marker in WEBKIT_ERROR_MARKERS)


def crate_version() -> str:
    """The shell crate's version, read from its `Cargo.toml`.

    Regex rather than `tomllib`, because `tomllib` is 3.11+ and 3.10 is a real
    deployment target here (`CLAUDE.md`). The shell builds its own string from
    `CARGO_PKG_VERSION`, so a harness that spelled the version out would be
    comparing a string the app never sends.
    """
    cargo = (
        pathlib.Path(__file__).resolve().parent.parent
        / "src-tauri"
        / "Cargo.toml"
    )
    try:
        match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', cargo.read_text())
    except OSError:
        return "unknown"
    return match.group(1) if match else "unknown"


#: The three strings `--user-agent` can send, and why these three.
#:
#: `honest` is what the shell sends with nothing set: the engine token, no
#: Safari release. `safari-default` is what wry sent before, verbatim, and is
#: the control — without it there is nothing to say a difference is a
#: difference. `chrome` is the string a large share of the web is served to,
#: and the one that answers the question people actually mean by "would this
#: site work in a normal browser".
#:
#: Spelled out here rather than left to the shell so the harness and the
#: comparison table cannot drift: a run labelled `chrome` must send the string
#: this file says it sends.
USER_AGENTS: dict[str, str] = {
    "honest": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Codify/" + crate_version()
    ),
    "safari-default": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/60.5 Safari/605.1.15"
    ),
    "chrome": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
}

#: `browser::UA_ENV` — named rather than imported, for the reason the bridge
#: opt-out is named: this script runs as a file and a Rust constant is not a
#: Python import.
UA_ENV = "CODIFY_PAGE_USER_AGENT"


def child_env(url: str, bridge: bool = True, user_agent: str | None = None) -> dict[str, str]:
    """The environment the smoke child runs in.

    Everything passes through untouched — that is the point: the smoke must
    see the developer's real session (bus, display, any DMABUF override),
    because a sanitised environment would diagnose a machine nobody is on.
    Two additions: the gate itself, and the bridge opt-out when a run is
    deliberately paint-only.
    """
    env = dict(os.environ)
    env["CODEIFY_EMBED_SMOKE"] = url
    if user_agent:
        env[UA_ENV] = user_agent
    if not bridge:
        # `webview_bridge::SMOKE_BRIDGE_SKIP_ENV`, named here rather than
        # imported: this script is run directly as a file, and a sibling Rust
        # module is not an importable module.
        env["CODEIFY_EMBED_SMOKE_NO_BRIDGE"] = "1"
    return env


def cargo_binary() -> str | None:
    """Cargo as an absolute path, or None.

    `$CARGO` first (the same override the Makefile's MYPY honours — a
    developer's toolchain may not be on PATH), then PATH. The absolute path
    is not ceremony: the argv runs with `cwd=src-tauri`, and a bare `cargo`
    would be a partial path resolved against a directory this script does
    not control.
    """
    override = os.environ.get("CARGO", "").strip()
    if override:
        return override
    return shutil.which("cargo")


def build_shell() -> int:
    """`cargo build` in src-tauri, streamed. Returns its exit status."""
    cargo = cargo_binary()
    if cargo is None:
        print(
            "embed-smoke: cargo is not on PATH and $CARGO is unset — cannot build "
            "the shell. Install Rust, or point $CARGO at it.",
            file=sys.stderr,
            flush=True,
        )
        return 1
    print("embed-smoke: building the shell (cargo build)…", flush=True)
    return subprocess.run(  # noqa: S603 — a fixed argv, no shell; see the spawn-guard entry
        [cargo, "build"],
        cwd=SRC_TAURI,
    ).returncode


def run_smoke(
    argv: list[str], env: dict[str, str], timeout_s: float, bridge: bool = True
) -> int:
    """Launch the shell in smoke mode, relay output, judge the verdict.

    Returns 0 only when the paint line was seen, the page reported itself,
    the page read back through the bridge, and the child exited 0 — the child
    calls `exit(0)` itself once all three legs are in, so anything else is a
    story worth telling in the verdict below.

    `bridge=False` drops the third leg from both sides: the child is told to
    skip it and this function stops requiring it. The two have to agree or a
    paint-only run would wait 20s for a leg that was never going to run.
    """
    state: dict[str, str | bool | None] = {
        "engaged": False,
        "painted": False,
        "second_launch": False,
        "failed_line": None,
    }
    diagnostics: list[str] = []
    reports: list[dict[str, object]] = []
    # A report that was cut by the title channel, counted separately from one
    # that never arrived: the two need opposite fixes and the same "no-report"
    # verdict would send the reader to the wrong one.
    truncated_reports = 0
    # One entry per bridge line, last one winning for the same reason the
    # report does: the probe asks once here, but a shape that changes under a
    # fix should show the later reading.
    bridges: list[tuple[str, dict[str, object]]] = []
    started = time.monotonic()

    child = subprocess.Popen(  # noqa: S603 — a fixed argv, no shell; see the spawn-guard entry
        argv,
        env=env,
        # stdout is piped because the verdict is read from it. stderr is piped
        # too — and relayed live by a reader thread below — so a WebKit crash
        # dump or a network-process error still reaches the developer's
        # terminal as it happens, *and* can be quoted back in the diagnosis
        # instead of scrolling past unread.
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # stderr, relayed and kept. A thread rather than a second loop because the
    # two streams are both live: draining stdout in the main loop while stderr
    # filled a pipe would deadlock the child, and draining stderr second would
    # stall a child that fills stderr first.
    webkit_complaints: list[str] = []

    def relay_stderr() -> None:
        stream = child.stderr
        if stream is None:  # pragma: no cover — PIPE above guarantees a pipe
            return
        for raw in stream:
            sys.stderr.write(raw)
            sys.stderr.flush()
            if is_webkit_complaint(raw):
                webkit_complaints.append(raw.rstrip("\n"))

    stderr_thread = threading.Thread(target=relay_stderr, daemon=True)
    stderr_thread.start()

    def deadline() -> None:
        # No paint within the bound and no exit: kill the child rather than
        # leave a window with a smoke page sitting on the developer's
        # desktop. The replay below still says which facts held.
        if child.poll() is None:
            print(
                f"\nembed-smoke: no verdict within {timeout_s:g}s — stopping the shell",
                file=sys.stderr,
                flush=True,
            )
            child.terminate()

    def engagement_deadline() -> None:
        # Still nothing after the short bound: this binary never entered smoke
        # mode, so it is older than this harness and the run is measuring the
        # whole app instead of the embed path. Stop it — it is a full window
        # and an engine on the developer's desktop, and its engine takes a
        # port.
        if state["engaged"] is not True and child.poll() is None:
            print(
                f"\nembed-smoke: the shell never announced smoke mode within "
                f"{ENGAGE_TIMEOUT_S:g}s — stopping it",
                file=sys.stderr,
                flush=True,
            )
            child.terminate()

    def stop(signum: int, _frame: FrameType | None) -> None:
        # Ctrl-C reaches the child too (same foreground session); terminating
        # it here as well keeps one Ctrl-C one clean stop.
        if child.poll() is None:
            child.terminate()
        raise KeyboardInterrupt(f"signal {signum}")

    def terminate_if_running() -> None:
        # The last backstop, for an exit path that never reached the finally
        # below (an unhandled exception, a killed parent). A real function
        # rather than a lambda: `and` on `terminate()`'s None return reads as
        # a value mypy (rightly) refuses to type.
        if child.poll() is None:
            child.terminate()

    timer = threading.Timer(timeout_s, deadline)
    timer.daemon = True
    timer.start()
    engage_timer = threading.Timer(ENGAGE_TIMEOUT_S, engagement_deadline)
    engage_timer.daemon = True
    engage_timer.start()
    for name in ("SIGINT", "SIGTERM"):
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, stop)
    atexit.register(terminate_if_running)

    # stdout=PIPE above guarantees a pipe, but the type is Optional and an
    # assert here would need an S101 exemption in pyproject.toml; an
    # explicit check keeps the lint table smaller for the same guarantee.
    pipe = child.stdout
    if pipe is None:
        raise RuntimeError("the smoke child was launched without a stdout pipe")
    try:
        for line in pipe:
            sys.stdout.write(line)
            sys.stdout.flush()
            if is_paint(line):
                state["painted"] = True
            elif is_mode_engaged(line):
                state["engaged"] = True
                # Engaged, so the short bound has served its purpose; cancel
                # it rather than let a thread sit armed for the rest of the run.
                engage_timer.cancel()
            elif paint_failure(line) is not None:
                state["failed_line"] = paint_failure(line)
            elif is_second_launch(line):
                state["second_launch"] = True
            elif is_env_diagnostic(line):
                diagnostics.append(line.rstrip("\n"))
            else:
                report = read_report(line)
                if report is not None:
                    # Last one wins, deliberately: the page re-announces as it
                    # renders, and the final report is the complete one.
                    reports.append(report)
                elif is_truncated_report(line):
                    truncated_reports += 1
                bridge_line = read_bridge(line)
                if bridge_line is not None:
                    bridges.append(bridge_line)
    except KeyboardInterrupt:
        pass
    finally:
        timer.cancel()
        engage_timer.cancel()
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        else:
            child.wait()

    painted = state["painted"] is True
    engaged = state["engaged"] is True
    second_launch = state["second_launch"] is True
    failed_line = state["failed_line"]
    saw_diagnostics = bool(diagnostics)
    elapsed = time.monotonic() - started

    # What the page *said about itself*, once the paint verdict is settled.
    # Printed on every path that reached a page, pass or fail, because the two
    # questions are different and only answering one of them is how a broken
    # site gets reported as a working one.
    page_report = _with_carried_fields(reports) if reports else None
    page_verdict = classify_report(page_report, truncated=bool(truncated_reports))
    # The second verdict, over the same report: what the page ended up showing
    # and what it is still waiting for are different questions, and a page that
    # renders perfectly while one request hangs is the case only the second one
    # names. It is computed on every path because the interesting case is
    # usually a *passing* run — a page that paints and reports itself and never
    # finishes loading.
    stall_verdict = classify_stall(page_report, truncated=bool(truncated_reports))
    bridge_read = bridges[-1] if bridges else None
    bridge_verdict = classify_bridge(*bridge_read) if bridge_read else "skipped"
    # The third leg is required by default, and *silence* is the failure. A
    # run where the page painted and reported itself beautifully and the
    # bridge line never appeared is a run where the AI cannot read the page —
    # so it is a FAIL that names the bridge, not a PASS with a missing line.
    bridge_missing = bridge and bridge_read is None
    # …and so is a leg that *ran* and came back with nothing. A `never-answered`
    # read is the AI being unable to reach the page, which is a broken feature
    # and not a property of this site; letting it pass was how a run whose
    # output said "the page did not answer within 15s" still ended in PASS.
    bridge_failed = bridge and bridge_read is not None and bridge_verdict == "never-answered"
    if painted and child.returncode == 0 and not bridge_missing and not bridge_failed:
        print(f"\nembed-smoke: page reports — {page_verdict}", flush=True)
        if page_report is not None:
            print(report_sentence(page_report, page_verdict), flush=True)
        elif truncated_reports:
            # The channel, not the page: this is a report the title could not
            # carry, and a run that says "no-report" here is telling a person
            # their page said nothing when it said something and the harness
            # dropped it.
            print(
                f"  a report arrived and was cut off mid-string — the payload "
                f"is percent-encoded JSON in a document title and this one was "
                f"too big for it ({truncated_reports} truncated line(s)). The "
                "page described itself and the harness could not hold it; the "
                "fix is a smaller report from the page, not a different site.",
                flush=True,
            )
        # Printed on a pass too, and that is the point: the common case is a
        # page that painted, described itself and is still waiting on a request
        # that never came back, which is a *pass* by every other measure and a
        # mystery to anyone reading the transcript.
        print(
            f"\nembed-smoke: what the page is waiting for — {stall_verdict}",
            flush=True,
        )
        print(stall_sentence(page_report, stall_verdict), flush=True)
        if bridge_read is not None:
            print(f"\nembed-smoke: page read back — {bridge_verdict}", flush=True)
            print(bridge_sentence(*bridge_read, "read"), flush=True)
        # WebKit's own account, quoted back. These are the lines that were
        # relayed live and scrolled past unread; a run that renders nothing and
        # says "painted in 1.2s" is otherwise a mystery, and a mystery is what
        # gets blamed on the wrong layer.
        for complaint in webkit_complaints:
            print(f"    webkit: {complaint.strip()}", flush=True)
        print(
            f"\nembed-smoke: PASS in {elapsed:.1f}s — the embedded page composited "
            "a real first paint"
            + bridge_clause(bridge_verdict),
            flush=True,
        )
        # A pass is a pass, but this note is not nothing: the marker is
        # printed by the *incumbent* instance, so seeing it here means this
        # child held the bus name while another launch knocked — two
        # instances were alive, and a second window may be on the desktop.
        if second_launch:
            print(
                "  (note: another Codify launch knocked while this run held the "
                "single-instance name — a second instance is probably running; "
                "the paint fact above is still this run's own)",
                file=sys.stderr,
                flush=True,
            )
        return 0

    # Always the two facts a reader needs before the story: how long the
    # child lived, and how it ended. The child exits 0 on a graceful
    # shutdown *and* the guard exits a refused launch with 0, so the status
    # alone never tells the story — and a verdict that omits it is a verdict
    # nobody can act on.
    print(
        f"\nembed-smoke: FAIL after {elapsed:.1f}s "
        f"(child exit status {child.returncode})",
        file=sys.stderr,
        flush=True,
    )
    if not engaged and not saw_diagnostics:
        # No diagnostics either, and this build prints them before the
        # builder — so this process is not a build that knows about the
        # smoke at all. That is the stale-binary shape, and it is the one
        # case here the harness can name with certainty.
        print(
            f"  {SHELL_BINARY} printed neither the environment diagnostics nor "
            "the smoke-mode line — the binary predates both. It launched the "
            "whole app instead. Rebuild: make smoke-embed SMOKE_EMBED_ARGS=--rebuild",
            file=sys.stderr,
            flush=True,
        )
    elif not engaged:
        # The diagnostics are there, so this is a current build that never
        # reached `setup` — which on Linux is the single-instance guard
        # refusing a second launch: tauri-plugin-single-instance claims the
        # bus name inside `Builder::build()` and, on NameTaken, has the
        # *newcomer* call the incumbent's callback and then
        # `std::process::exit(0)`. That happens before this app's `setup`
        # runs, so the refusal is silent: no mode line, status 0, nothing on
        # stdout. Worth stating precisely, because the callback line a
        # person expects to see is printed by the *other* instance.
        print(
            "  the launch ended inside build(), before the app's setup ran — on "
            "Linux that is the single-instance guard refusing a second launch "
            "(it exits the newcomer with status 0, silently). Stop any other "
            "Codify instance and re-run; this run measured nothing.",
            file=sys.stderr,
            flush=True,
        )
    elif second_launch:
        print(
            "  another instance is already running — the single-instance guard "
            "exited this launch inside build(), before any webview existed. "
            "Stop the running instance and re-run; until then this run says "
            "nothing about first paint.",
            file=sys.stderr,
            flush=True,
        )
    elif isinstance(failed_line, str):
        print(
            f"  the shell gave up: {failed_line}",
            file=sys.stderr,
            flush=True,
        )
    elif child.returncode not in (0, None):
        print(
            f"  the shell exited with status {child.returncode} without painting",
            file=sys.stderr,
            flush=True,
        )
    else:
        print(
            "  the shell said nothing more — no paint line, no failure line, and "
            "a status that claims success anyway (see the child status above)",
            file=sys.stderr,
            flush=True,
        )
    if bridge_missing:
        # Named before the generic tail, because it is the only one of these
        # that is about a *feature* rather than about this run's environment,
        # and it is the one that a reader is most likely to misread as a flaky
        # site.
        print(
            "  the page read never came back: the shell painted and the page "
            "described itself, but no `embed-smoke: bridge` line ever arrived. "
            "The AI cannot read the page — most often a Content-Security-Policy "
            "on the site forbidding connect-src, img-src and sendBeacon, which "
            "are the three ways a page can reach the shell. Re-run with "
            "--no-bridge for a paint-only run.",
            file=sys.stderr,
            flush=True,
        )
    if bridge_failed:
        # The same sentence as the missing-line case, because it is the same
        # fact: the AI could not read the page. What differs is only *how* it is
        # known — the shell said so, rather than the line never arriving.
        reason = (bridge_read[1].get("error") if bridge_read else None) or "no reason given"
        print(
            "  the page read failed: the shell painted and the page described "
            "itself, but the read the AI would make never came back — "
            f"{reason}. This run is a FAIL because the AI cannot read the "
            "page, not because of this site. Re-run with --no-bridge for a "
            "paint-only run.",
            file=sys.stderr,
            flush=True,
        )
    if page_report is not None:
        # A page that described itself *and* the run failed is the case where
        # the second verdict is worth the most: the run is red for a reason
        # the transcript otherwise has to be read for, and the reason is often
        # that the page is still waiting on a request.
        print(
            f"\nembed-smoke: what the page was waiting for — {stall_verdict}",
            file=sys.stderr,
            flush=True,
        )
        print(stall_sentence(page_report, stall_verdict), file=sys.stderr, flush=True)
    elif truncated_reports:
        print(
            "\nembed-smoke: a report was cut off by the title channel, so what "
            "the page was waiting for is unknown rather than nothing "
            f"({stall_verdict})",
            file=sys.stderr,
            flush=True,
        )
    if diagnostics:
        print(
            "\n  what the environment said at boot:",
            file=sys.stderr,
            flush=True,
        )
        for line in diagnostics:
            print(f"    {line}", file=sys.stderr, flush=True)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Smoke-test the embedded browser's first paint (make smoke-embed).",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"the page to seat (default {DEFAULT_URL})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=(
            "whole-run bound in seconds; the shell fails itself at 20s, so this "
            f"needs slack (default {DEFAULT_TIMEOUT_S:g})"
        ),
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="run `cargo build` even if the binary already exists",
    )
    parser.add_argument(
        "--no-bridge",
        dest="bridge",
        action="store_false",
        help=(
            "skip the page-read leg: measure first paint and the page's own "
            "report only. For when the site itself is what you are debugging "
            "and the bridge is known to work; a run with the bridge enabled "
            "is the honest default"
        ),
    )
    parser.add_argument(
        "--user-agent",
        choices=sorted(USER_AGENTS),
        default="honest",
        help=(
            "which user agent to send: 'honest' (the app's own), "
            "'safari-default' (what wry sent before this was changed) or "
            "'chrome'. Running the same URL under each is how 'does this site "
            "treat us differently?' gets measured instead of argued"
        ),
    )
    args = parser.parse_args(argv)
    if args.timeout <= 20:
        parser.error("--timeout must exceed the shell's own 20s paint timeout")
    if not (args.url.startswith("http://") or args.url.startswith("https://")):
        # The same rule the navigation guard enforces, applied before the
        # child ever starts: a non-web URL is refused there, so failing here
        # says so without a launch in between.
        parser.error(f"--url must be http(s), not {args.url!r}")

    # A paint needs a compositor. Without a display the child would fail at
    # its own timeout after 20 uninformative seconds; this says so up front.
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        print(
            "embed-smoke: FAIL — no display: neither WAYLAND_DISPLAY nor "
            "DISPLAY is set, and a page cannot paint without a compositor",
            file=sys.stderr,
            flush=True,
        )
        return 1

    if args.rebuild or not SHELL_BINARY.exists():
        if build_shell() != 0:
            print(
                "embed-smoke: FAIL — cargo build failed; nothing to launch",
                file=sys.stderr,
                flush=True,
            )
            return 1
    if not SHELL_BINARY.exists():
        print(
            f"embed-smoke: FAIL — {SHELL_BINARY} does not exist even after the "
            "build; check src-tauri/target/",
            file=sys.stderr,
            flush=True,
        )
        return 1

    print(
        f"embed-smoke: launching {SHELL_BINARY.name} with CODEIFY_EMBED_SMOKE={args.url}",
    )
    print(
        f"embed-smoke: user agent [{args.user_agent}] {USER_AGENTS[args.user_agent]}",
        flush=True,
    )
    return run_smoke(
        [str(SHELL_BINARY)],
        child_env(
            args.url, bridge=args.bridge, user_agent=USER_AGENTS[args.user_agent]
        ),
        args.timeout,
        bridge=args.bridge,
    )


if __name__ == "__main__":
    raise SystemExit(main())
