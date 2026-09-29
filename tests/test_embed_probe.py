"""What a page's report *means*, frozen without a display.

`scripts/embed_smoke.py` seats a real page in a real webview and asks it what
it is showing. The display is the slow, flaky, machine-specific half of that
harness; the judgement is not, and the judgement is the half that decides
whether a person is told "this site works in the embed" or "this site is
refusing you". So the reader's reading of a report is tested here, where it
runs in milliseconds and cannot be skipped for want of a compositor.

The reports below are the ones actually observed against real sites on this
checkout (see `docs/09` §7.3), which is what makes them worth pinning: each is
a site that a first-paint smoke happily passed.
"""
from __future__ import annotations

import json
import pathlib
import unittest
from urllib.parse import quote

# `scripts` is a package (`scripts/__init__.py`), imported the way
# `tests/test_benchmark_runner.py` imports `benchmarks` — a plain package
# import, no sys.path surgery and no `# type: ignore[import-not-found]`, which
# this repo treats as a config problem (pyproject.toml says why).
from scripts.embed_smoke import (
    UA_ENV,
    USER_AGENTS,
    _with_carried_fields,
    bridge_clause,
    bridge_sentence,
    child_env,
    classify_bridge,
    classify_report,
    classify_stall,
    is_truncated_report,
    read_bridge,
    crate_version,
    read_report,
    report_sentence,
    stall_sentence,
)

# What the probe saw on a GitHub issue: the ordinary case, and the one every
# other verdict is measured against.
GITHUB_ISSUE = {
    "u": "https://github.com/tauri-apps/tauri/issues/1",
    "t": "Some issue title · Issue #1 · tauri-apps/tauri",
    "n": 8421,
    "v": 0,
    "d": 0,
    "f": 0,
    "h": "",
    "c": "",
    "e": [],
}

# accounts.google.com, refused. Google's embedded-OAuth policy is a *server*
# decision, not a rendering fault, and the difference decides what a person
# does about it: nothing in this app can fix it, and nothing in this app may
# route around it either.
GOOGLE_REFUSAL = {
    "u": "https://accounts.google.com/signin/oauth",
    "t": "Sign in - Google Accounts",
    "n": 1204,
    "v": 0,
    "d": 0,
    "h": "",
    "c": "",
    "e": ["this browser or app", "may not be secure"],
}

# A video page on a WebKitGTK build with no H.264: the page is fine, the
# codec is not, and the two are different sentences.
NO_CODECS = {
    "u": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "t": "YouTube",
    "n": 3107,
    "v": 1,
    "d": 0,
    "f": 0,
    "h": "",
    "c": "",
    "e": [],
}

# accounts.google.com as measured: a **working** sign-in page that this probe
# cannot read, because the form lives in a cross-origin frame and `innerText`
# reaches the top document only. 135 characters on a page that renders fine.
GOOGLE_FRAMED = {
    "u": "https://accounts.google.com/v3/signin/identifier?continue=https://accounts.google.com/",
    "t": "Sign in - Google Accounts",
    "n": 135,
    "v": 0,
    "d": 0,
    "f": 1,
    "h": "probably",
    "c": "probably",
    "e": [],
}


class ReadReport(unittest.TestCase):
    def test_a_report_line_decodes(self) -> None:
        line = "embed-smoke: report " + quote(json.dumps(GITHUB_ISSUE))
        self.assertEqual(read_report(line), GITHUB_ISSUE)

    def test_every_other_line_is_not_a_report(self) -> None:
        for line in (
            "embed-smoke: painted",
            "[Codify] browser env: session bus: present",
            "embed-smoke: report not-json",
            "embed-smoke: report " + quote("[1, 2, 3]"),
            "",
        ):
            self.assertIsNone(read_report(line), line)

    def test_a_page_that_cannot_describe_itself_is_a_finding_not_a_crash(self) -> None:
        # The probe is a page's account of itself. A page that cannot give one
        # has told us something, and the harness must survive being told it.
        self.assertEqual(classify_report(read_report("embed-smoke: report %7Bbroken")), "no-report")


class ClassifyReport(unittest.TestCase):
    def test_a_working_site_renders(self) -> None:
        self.assertEqual(classify_report(GITHUB_ISSUE), "rendered")

    def test_a_refusal_outranks_every_other_reading(self) -> None:
        # The ordering is the argument. A refusal says the site has decided this
        # browser is unacceptable; reading that as "no codecs" or "no content"
        # would explain a symptom and send someone to fix the wrong thing. This
        # report has plenty of text, so it could be mistaken for a working page
        # if the text length were checked first.
        self.assertEqual(classify_report(GOOGLE_REFUSAL), "refused-embedded-browser")

    def test_a_refusal_wins_even_with_media_and_no_codecs(self) -> None:
        both = dict(NO_CODECS)
        both["e"] = ["unsupported browser"]
        self.assertEqual(classify_report(both), "refused-embedded-browser")

    def test_media_with_no_codecs_is_the_codec_fact(self) -> None:
        self.assertEqual(classify_report(NO_CODECS), "no-media-codecs")

    def test_media_with_codecs_is_not_the_codec_problem(self) -> None:
        playable = dict(NO_CODECS)
        playable["h"] = "probably"
        self.assertEqual(classify_report(playable), "rendered")

    def test_a_page_that_rendered_nothing_says_so(self) -> None:
        blank = {"u": "https://www.youtube.com/", "t": "YouTube", "n": 0, "v": 0, "d": 0, "f": 0, "e": []}
        self.assertEqual(classify_report(blank), "rendered-nothing")

    def test_a_framed_page_is_the_probe_blind_not_the_site_broken(self) -> None:
        # The measured Google sign-in: 135 characters, one frame, working. The
        # honest verdict names the limit of the measurement, because the fix
        # for a broken page and the fix for a blind probe are different work.
        self.assertEqual(classify_report(GOOGLE_FRAMED), "content-behind-an-iframe")

    def test_frames_do_not_excuse_a_page_that_rendered_plenty(self) -> None:
        # A page with 4,000 characters *and* a frame is a page that rendered;
        # the frame is a fact, not a disqualification.
        framed_but_real = dict(GITHUB_ISSUE)
        framed_but_real["f"] = 2
        self.assertEqual(classify_report(framed_but_real), "rendered")

    def test_a_page_that_never_loaded_is_not_a_page_that_rendered_nothing(self) -> None:
        # A load failure paints a WebKit error page. Reading that as "the site
        # rendered nothing" would blame the site for the network.
        broken = {"u": "chrome-error://chromewebdata/", "t": "", "n": 40, "e": []}
        self.assertEqual(classify_report(broken), "failed-to-load")

    def test_a_field_of_the_wrong_type_reads_as_zero_rather_than_crashing(self) -> None:
        # Annotated rather than inferred: a report is a page's own account of
        # itself, so its values are `object`, and a fixture that pretends
        # otherwise hides the case it exists to describe.
        odd: dict[str, object] = {
            "u": "https://a.example",
            "n": "lots",
            "v": None,
            "e": "not a list",
        }
        self.assertEqual(classify_report(odd), "rendered-nothing")

    def test_no_report_at_all_is_named_rather_than_assumed_good(self) -> None:
        # The dangerous default: a smoke that says "fine" when it heard nothing
        # is how a broken site gets reported as working.
        self.assertEqual(classify_report(None), "no-report")


class DiagnoseABlankPage(unittest.TestCase):
    """A blank page with a reason is not the same bug as a blank page."""

    def test_failed_subresources_name_the_mechanism(self) -> None:
        # What a YouTube watch page measured: the document arrived, its script
        # tags were served, and some of them never loaded. "rendered-nothing"
        # would be true and useless — the fix is not in the renderer.
        blank_with_failures: dict[str, object] = {
            "u": "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
            "t": "",
            "n": 0,
            "v": 0,
            "d": 0,
            "f": 0,
            "sc": 14,
            "rp": 3,
            "x": ["script https://www.youtube.com/s/player/abc/player.js"],
            "j": [],
        }
        self.assertEqual(classify_report(blank_with_failures), "page-errored")

    def test_a_page_that_threw_is_named_too(self) -> None:
        threw: dict[str, object] = {
            "u": "https://a.example",
            "n": 12,
            "f": 0,
            "x": [],
            "j": ["unhandled rejection: Failed to fetch"],
        }
        self.assertEqual(classify_report(threw), "page-errored")

    def test_scripts_and_nothing_fetched_is_a_document_that_arrived_dead(self) -> None:
        # Distinct from the two above and distinct again from a blank page that
        # simply drew nothing: the page never got going, and no subresource
        # failed to explain it.
        dead: dict[str, object] = {
            "u": "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
            "n": 0,
            "f": 0,
            "sc": 14,
            "rp": 0,
            "x": [],
            "j": [],
        }
        self.assertEqual(classify_report(dead), "document-arrived-dead")

    def test_a_working_page_is_never_downgraded_by_a_stray_error(self) -> None:
        # A page that rendered 4,000 characters and also logged a warning is a
        # page that worked. The mechanism is reported, never the verdict.
        worked: dict[str, object] = dict(GITHUB_ISSUE)
        worked["c9"] = ["warn: deprecated API"]
        self.assertEqual(classify_report(worked), "rendered")

    def test_the_mechanism_is_printed_when_there_is_one(self) -> None:
        with_failures: dict[str, object] = {
            "u": "https://a.example",
            "n": 0,
            "sc": 14,
            "rp": 3,
            "ua": "Mozilla/5.0 (X11; Linux x86_64)",
            "x": ["script https://a.example/app.js"],
        }
        sentence = report_sentence(with_failures, "page-errored")
        self.assertIn("app.js", sentence)
        self.assertIn("14 script(s)", sentence)
        self.assertIn("3 subresource(s) completed", sentence)
        self.assertIn("Mozilla/5.0", sentence)


class ReportSentence(unittest.TestCase):
    def test_the_sentence_carries_the_facts_a_person_acts_on(self) -> None:
        sentence = report_sentence(GOOGLE_REFUSAL, "refused-embedded-browser")
        self.assertIn("may not be secure", sentence)
        self.assertIn("accounts.google.com", sentence)

    def test_the_sentence_survives_an_empty_report(self) -> None:
        self.assertTrue(report_sentence({}, "rendered-nothing").strip())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

# ── the bridge leg ──────────────────────────────────────────────────────────
#
# The third leg is the one that measures a *feature* rather than a site: the
# shell asks a page a question and a string comes back. Every part of that
# path lives in a display process — eval, a custom URI scheme, chunk
# reassembly, JSON — so the only half that can be frozen without a compositor
# is the reading of the result, and the reading is where a silent failure
# would land: a line that decodes to nothing looks exactly like a page that
# refused to answer.
READ_BACK = {
    "url": "https://example.com/",
    "title": "Example Domain",
    "text": "This domain is for use in illustrative examples.",
    "chars": 53,
    "truncated": False,
    "links": [
        {"text": "More information...", "href": "https://www.iana.org/domains/example"}
    ],
    "ready_state": "complete",
}


def read_bridge_or_die(line: str) -> tuple[str, dict[str, object]]:
    """`read_bridge`, with the None case already asserted away.

    Every test below is about a line that *is* a bridge line, so unpacking
    the Optional directly is noise — and noise that mypy rightly refuses.
    """
    parsed = read_bridge(line)
    assert parsed is not None, f"not a bridge line: {line!r}"
    return parsed


class ReadAPageBack(unittest.TestCase):
    def test_a_completed_round_trip_is_read_as_one(self) -> None:
        self.assertEqual(
            read_bridge(f"embed-smoke: bridge ok {json.dumps(READ_BACK)}"),
            ("ok", READ_BACK),
        )
        self.assertEqual(classify_bridge("ok", READ_BACK), "answered")

    def test_a_line_that_is_not_the_bridge_line_is_not_a_bridge_line(self) -> None:
        # The other two legs share this stream. A reader that matched loosely
        # would attribute one of them to the bridge.
        for other in (
            "embed-smoke: painted",
            "embed-smoke: report %7B%22u%22%3A%22https%3A%2F%2Fx.test%22%7D",
            "embed-smoke: mode engaged (https://example.com/)",
            "[Codify] browser env: display: wayland",
        ):
            self.assertIsNone(read_bridge(other), other)

    def test_an_answer_that_is_not_json_is_a_failure_not_a_crash(self) -> None:
        verdict, payload = read_bridge_or_die("embed-smoke: bridge ok not json at all")
        self.assertEqual(verdict, "fail")
        self.assertIn("not JSON", str(payload.get("error")))

    def test_an_answer_that_is_json_but_not_an_object_is_refused(self) -> None:
        verdict, _ = read_bridge_or_die("embed-smoke: bridge ok [1, 2, 3]")
        self.assertEqual(verdict, "fail")

    def test_a_failure_line_names_the_reason_it_gave_up(self) -> None:
        verdict, payload = read_bridge_or_die(
            "embed-smoke: bridge fail the page did not answer within 15s"
        )
        self.assertEqual(verdict, "fail")
        self.assertEqual(classify_bridge("fail", payload), "never-answered")
        self.assertIn("15s", str(payload.get("error")))

    def test_a_bare_failure_is_still_a_failure(self) -> None:
        # The shortest possible refusal line, with no reason after it. A
        # parser that required the trailing space would return None here, which
        # reads as "no answer ever arrived" — so a refused navigation would be
        # reported as a page that never spoke.
        verdict, payload = read_bridge_or_die("embed-smoke: bridge fail")
        self.assertEqual(verdict, "fail")
        self.assertTrue(str(payload.get("error")).strip())
        self.assertEqual(classify_bridge("fail", payload), "never-answered")


class NameAPageRead(unittest.TestCase):
    def test_no_answer_is_never_answered_and_not_a_blank_page(self) -> None:
        """The distinction the whole leg exists to preserve.

        "The AI cannot reach the page" and "the page had nothing to say" both
        arrive as no text. One is a broken feature and one is a fact about a
        canvas, and conflating them sends someone to debug the wrong layer.
        """
        self.assertEqual(classify_bridge("fail", {}), "never-answered")
        self.assertEqual(
            classify_bridge("ok", {"text": "", "chars": 0}), "answered-nothing"
        )
        self.assertEqual(classify_bridge("ok", {"text": "   "}), "answered-nothing")
        self.assertNotEqual(
            classify_bridge("fail", {}), classify_bridge("ok", {"text": ""})
        )

    def test_a_completed_read_is_answered(self) -> None:
        self.assertEqual(classify_bridge("ok", READ_BACK), "answered")

    def test_whitespace_is_the_only_test_for_text(self) -> None:
        self.assertEqual(classify_bridge("ok", {"text": "\n\t "}), "answered-nothing")
        self.assertEqual(classify_bridge("ok", {"text": "x"}), "answered")


class TheSentenceAPersonReads(unittest.TestCase):
    def test_it_quotes_the_page_back_so_the_round_trip_is_evident(self) -> None:
        line = bridge_sentence("ok", READ_BACK, "read")
        self.assertIn("https://example.com/", line)
        self.assertIn("Example Domain", line)
        # A shell that never reached the page cannot produce a title and a
        # character count, so these two are the evidence that something did.
        self.assertIn("53 chars", line)
        self.assertIn("readyState=complete", line)

    def test_it_reports_the_links_because_that_is_what_makes_them_followable(self) -> None:
        self.assertIn("1 link(s)", bridge_sentence("ok", READ_BACK, "read"))
        self.assertNotIn(
            "link(s)", bridge_sentence("ok", {**READ_BACK, "links": []}, "read")
        )

    def test_a_truncated_read_says_it_was_truncated(self) -> None:
        self.assertIn(
            "truncated", bridge_sentence("ok", {**READ_BACK, "truncated": True}, "read")
        )

    def test_a_failure_prints_the_reason_and_nothing_pretend(self) -> None:
        line = bridge_sentence("fail", {"error": "no browser tab open"}, "read")
        self.assertIn("no browser tab open", line)
        self.assertNotIn("chars", line)

    def test_an_empty_payload_does_not_crash_the_sentence(self) -> None:
        self.assertIn("(no title)", bridge_sentence("ok", {}, "read"))


class TestWhatTheVerdictMayClaim(unittest.TestCase):
    """The PASS line is the sentence a person skims, so it has to be true.

    It was not. The clause was attached whenever *any* `embed-smoke: bridge`
    line had been parsed, including one that said the page never answered — so
    a run could print `bridge fail … did not answer within 15s` and then report
    that the page had been read back through the bridge. Worse, that run ended
    in PASS, because silence was treated as the only failure and a *refusal*
    was treated as a line.

    These are the two halves of that: the clause follows the verdict, and a
    leg that reached no page is not a pass at all.
    """

    def test_only_a_read_that_came_back_is_called_a_read_back(self) -> None:
        self.assertIn(
            "read back through the bridge", bridge_clause("answered")
        )
        # The near-miss: the trip happened and the page had nothing to say.
        # That is a fact about the page, so the run may pass — but it must not
        # claim the page was read.
        clause = bridge_clause("answered-nothing")
        self.assertNotIn("read back through the bridge", clause)
        self.assertIn("no text to give", clause)

    def test_a_leg_that_reached_nothing_gets_no_claim_at_all(self) -> None:
        for verdict in ("never-answered", "skipped", "", "something-new"):
            self.assertEqual(
                bridge_clause(verdict),
                ", described itself",
                f"{verdict!r} must not let the verdict claim a read-back",
            )

    def test_the_run_treats_a_failed_read_as_a_failure(self) -> None:
        """The condition that decided PASS, read as a predicate.

        `bridge_failed` is what the smoke computes now, and it is exactly the
        case the old branch let through: the line arrived, and what it said was
        that nothing came back.
        """
        source = " ".join(pathlib.Path("scripts/embed_smoke.py").read_text().split())
        self.assertIn(
            'bridge_failed = bridge and bridge_read is not None '
            'and bridge_verdict == "never-answered"',
            source,
            "the predicate that keeps a refused read out of the PASS branch",
        )
        self.assertIn(
            "not bridge_missing and not bridge_failed",
            " ".join(source.split()),
            "a failed bridge leg must keep the run out of the PASS branch",
        )
        # And the clause is derived, never appended on the strength of a line
        # having appeared.
        self.assertNotIn(
            'if bridge_read is not None else ""',
            " ".join(source.split()),
            "the PASS clause must come from the verdict, not from the presence "
            "of a bridge line",
        )


class TestTheThreeUserAgents(unittest.TestCase):
    """The comparison is only worth anything if the strings are the real ones.

    A matrix that quietly sent something else would produce the most expensive
    kind of wrong answer — a confident conclusion about a string no page ever
    saw. So each preset is checked against what it claims to be, the honest one
    against what the shell actually builds, and the env var against the Rust
    constant that reads it.
    """

    def test_the_three_presets_are_the_three_strings_they_claim_to_be(self) -> None:
        self.assertEqual(
            sorted(USER_AGENTS), ["chrome", "honest", "safari-default"]
        )
        honest = USER_AGENTS["honest"]
        self.assertIn("Codify/" + crate_version(), honest)
        self.assertNotIn("Safari", honest, "the honest one must not claim Safari")
        self.assertIn("KHTML, like Gecko", honest)
        # The control, verbatim: this is the string wry sent before, and a
        # "Safari default" that is not that string compares against nothing.
        self.assertEqual(
            USER_AGENTS["safari-default"],
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/60.5 Safari/605.1.15",
        )
        self.assertIn("Chrome/", USER_AGENTS["chrome"])
        self.assertIn("537.36", USER_AGENTS["chrome"])

    def test_the_honest_preset_is_the_string_the_shell_builds(self) -> None:
        """Cross-language, and worth the read: a version that drifts here would
        mean the matrix labelled a run with a string the app never sends."""
        source = pathlib.Path("src-tauri/src/browser.rs").read_text()
        for fragment in (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 (KHTML, like Gecko)",
            "Codify/{}",
            'env!("CARGO_PKG_VERSION")',
        ):
            self.assertIn(fragment, source, f"the shell no longer builds {fragment!r}")
        # …and exactly what the shell's format string produces, once the
        # compile-time version is filled in. Anything else would mean a run
        # labelled `honest` was labelled by a different rule than the app's.
        self.assertEqual(
            USER_AGENTS["honest"],
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Codify/" + crate_version(),
        )

    def test_the_env_var_is_the_one_the_shell_reads(self) -> None:
        source = pathlib.Path("src-tauri/src/browser.rs").read_text()
        self.assertIn(f'pub const UA_ENV: &str = "{UA_ENV}"', source)
        self.assertIn("std::env::var(UA_ENV)", source)

    def test_only_the_asked_for_override_reaches_the_child(self) -> None:
        """Everything else passes through, because the smoke diagnoses the
        developer's real session — a sanitised environment would describe a
        machine nobody is on."""
        plain = child_env("https://example.org/")
        self.assertNotIn(UA_ENV, plain, "the default run must send the app's own string")
        self.assertIn("CODEIFY_EMBED_SMOKE", plain)
        chosen = child_env("https://example.org/", user_agent=USER_AGENTS["chrome"])
        self.assertEqual(chosen[UA_ENV], USER_AGENTS["chrome"])
        # And an explicit "honest" is the same string as sending nothing.
        self.assertEqual(
            child_env("https://example.org/", user_agent=USER_AGENTS["honest"])[UA_ENV],
            USER_AGENTS["honest"],
        )


# ── which request is outstanding ─────────────────────────────────────────────
#
# The second verdict, and the one a blank page actually needs. Every field the
# first report carries counts something that *happened* — resources completed,
# scripts present, errors caught — and none of them can see a request still in
# flight. These are the readings of the page's new `q` field, frozen with the
# same discipline: the wrong name here sends a reader to the wrong layer, and
# the honest-sounding wrong name does it most easily.


class TestWhichRequestIsStalled(unittest.TestCase):
    """A youtube.com-shaped report, mid-load, with the request still out."""

    #: The `q` rows are their own constants so a report can be built out of
    #: them in more than one shape — a page with both a fetch and a media
    #: element outstanding is a different reading from either alone.
    FETCH_ROW: dict[str, object] = {
        "k": "fetch",
        "u": "www.youtube.com/youtubei/v1/player?prettyPrint=false",
        "m": "POST",
        "ms": 9123,
    }
    MEDIA_ROW: dict[str, object] = {
        "k": "media",
        "u": "rr3---sn-x.googlevideo.com/videoplayback?expire=…",
        "m": "MEDIA",
        # The page found this one already in flight, so it cannot say how long
        # it has been waiting. -1, and never 0.
        "ms": -1,
    }

    STUCK_ON_A_FETCH: dict[str, object] = {
        "u": "https://www.youtube.com/",
        "t": "YouTube",
        "n": 2093,
        "rs": "loading",
        "sc": 14,
        "rp": 92,
        "ms": 10012,
        "le": 0,
        "ua": "Mozilla/5.0 (X11; Linux x86_64)",
        "q": [FETCH_ROW],
    }

    WAITING_ON_MEDIA: dict[str, object] = {
        "u": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "t": "YouTube",
        "n": 20,
        "rs": "loading",
        "sc": 14,
        "rp": 40,
        "ms": 3000,
        "le": 0,
        "q": [MEDIA_ROW],
    }

    def test_the_stalled_request_is_named(self) -> None:
        self.assertEqual(classify_stall(self.STUCK_ON_A_FETCH), "waiting-on-request")
        sentence = stall_sentence(self.STUCK_ON_A_FETCH, "waiting-on-request")
        self.assertIn("youtubei/v1/player", sentence)
        self.assertIn("POST", sentence)
        self.assertIn("9123ms", sentence)

    def test_a_video_waiting_on_its_streams_is_a_different_name(self) -> None:
        self.assertEqual(classify_stall(self.WAITING_ON_MEDIA), "waiting-on-media")
        sentence = stall_sentence(self.WAITING_ON_MEDIA, "waiting-on-media")
        self.assertIn("googlevideo.com/videoplayback", sentence)
        # The page found that request in flight rather than watching it leave,
        # so it has no age to give. Printing 0ms would be a measurement that
        # looks like one.
        self.assertNotIn("0ms", sentence)
        self.assertIn("(media)", sentence)

    def test_media_does_not_hide_a_fetch_behind_it(self) -> None:
        both = dict(self.WAITING_ON_MEDIA)
        both["q"] = [self.MEDIA_ROW, self.FETCH_ROW]
        self.assertEqual(classify_stall(both), "waiting-on-request")

    def test_a_page_that_finished_loading_is_not_stalled_on_its_polls(self) -> None:
        """An open XHR is a poll. Calling it a stall sends someone after a
        request that was never going to be the problem."""
        done = dict(self.STUCK_ON_A_FETCH)
        done["rs"] = "complete"
        self.assertEqual(classify_stall(done), "loaded")

    def test_nothing_outstanding_and_still_loading_names_the_limits_of_the_eye(self) -> None:
        unseen = dict(self.STUCK_ON_A_FETCH)
        unseen["q"] = []
        self.assertEqual(classify_stall(unseen), "load-never-fired")
        sentence = stall_sentence(unseen, "load-never-fired")
        # The finding is the document's load event, and the sentence has to say
        # that "nothing is pending" is the probe's blindness rather than the
        # page's state: cross-origin subresources with no Timing-Allow-Origin
        # produce no resource timing entry at all.
        self.assertIn("load event has never fired", sentence)
        self.assertIn("Timing-Allow-Origin", sentence)

    def test_a_report_with_no_stall_field_is_never_read_as_nothing_pending(self) -> None:
        """The load-bearing case. A report from a build predating the field, or
        a page that defeated the patches, carries no `q` at all — and reading
        that as "nothing is outstanding" is the same confident mistake as
        reading a missing bridge line as a page that said nothing."""
        older = {k: v for k, v in self.STUCK_ON_A_FETCH.items() if k != "q"}
        self.assertEqual(classify_stall(older), "no-stall-data")
        self.assertIn("must not be read from", stall_sentence(older, "no-stall-data"))

    def test_a_field_of_the_wrong_type_is_no_data_rather_than_a_crash(self) -> None:
        for wrong in ("not a list", 3, {"k": "fetch"}, None):
            report = dict(self.STUCK_ON_A_FETCH)
            report["q"] = wrong
            self.assertEqual(classify_stall(report), "no-stall-data", wrong)

    def test_a_row_that_is_not_an_object_is_skipped_not_fatal(self) -> None:
        report = dict(self.STUCK_ON_A_FETCH)
        report["q"] = ["a string", 7, {"k": "xhr", "u": "a.example/x", "m": "get", "ms": 5}]
        self.assertEqual(classify_stall(report), "waiting-on-request")
        self.assertIn("a.example/x", stall_sentence(report, "waiting-on-request"))

    def test_no_report_at_all_says_so_rather_than_guessing(self) -> None:
        self.assertEqual(classify_stall(None), "no-report")
        self.assertIn("never described itself", stall_sentence(None, "no-report"))

    def test_a_cut_report_is_not_a_page_with_nothing_pending(self) -> None:
        """The two verdicts have to agree about *why* there is no data. A
        report the channel cut off is missing exactly the half that would have
        named the outstanding request, so the stall verdict is `no-stall-data`
        and says so — never `loaded`, and never a sentence about a page that
        said nothing."""
        self.assertEqual(
            classify_stall(None, truncated=True),
            "no-stall-data",
            "a cut report must not read as a page with nothing outstanding",
        )
        self.assertIn(
            "cut off mid-string",
            stall_sentence(None, "no-stall-data"),
        )

    def test_an_odd_ready_state_does_not_become_a_claim(self) -> None:
        report = dict(self.STUCK_ON_A_FETCH)
        report["q"] = []
        report["rs"] = "something-new"
        self.assertEqual(classify_stall(report), "no-stall-data")

    def test_the_ages_are_the_page_s_own_and_the_oldest_is_named_first(self) -> None:
        report = dict(self.STUCK_ON_A_FETCH)
        report["q"] = [
            {"k": "fetch", "u": "young.example/a", "m": "GET", "ms": 40},
            {"k": "fetch", "u": "old.example/b", "m": "GET", "ms": 9800},
        ]
        sentence = stall_sentence(report, "waiting-on-request")
        self.assertLess(sentence.index("old.example/b"), sentence.index("young.example/a"))

    def test_more_outstanding_than_the_page_lists_is_counted_not_hidden(self) -> None:
        report = dict(self.STUCK_ON_A_FETCH)
        report["q"] = [
            {"k": "fetch", "u": f"a.example/{i}", "m": "GET", "ms": 100 + i}
            for i in range(6)
        ]
        sentence = stall_sentence(report, "waiting-on-request")
        self.assertIn("and 2 more outstanding", sentence)

    def test_where_progress_stopped_is_printed_beside_what_is_outstanding(self) -> None:
        """The other half, and it is the half that comes from resource timing:
        `q` says what has not come back, `lz` says what did, last. On a stuck
        page the gap between them is the finding."""
        report = dict(self.STUCK_ON_A_FETCH)
        report["lz"] = "www.youtube.com/img/yt_1200.png at 1180ms"
        sentence = stall_sentence(report, "waiting-on-request")
        self.assertIn("last thing that arrived: www.youtube.com/img/yt_1200.png at 1180ms", sentence)

    def test_a_media_source_url_is_not_presented_as_a_fetch(self) -> None:
        """`blob:` is a handle on a stream, not an address on the network. Named
        as one, the reader goes looking for a request that never happened."""
        report = dict(self.WAITING_ON_MEDIA)
        report["q"] = [
            {
                "k": "media",
                "u": "blob:https://www.youtube.com/d37c4cb0-443d",
                "m": "MEDIASOURCE",
                "ms": 900,
            }
        ]
        sentence = stall_sentence(report, "waiting-on-media")
        self.assertIn("blob:https://www.youtube.com/d37c4cb0-443d", sentence)
        self.assertIn("Media Source handle", sentence)
        self.assertIn("waiting 900ms", sentence)

    def test_the_user_agent_rides_once_and_the_reader_carries_it_forward(self) -> None:
        """The one field a later report may omit, and the reason it is the only
        one: the shell sets the string before the page exists, so it cannot
        change, and at 133 encoded characters of a ~980-wide channel it is the
        largest fact that does not vary between announcements."""
        early = dict(self.STUCK_ON_A_FETCH)
        late = dict(self.STUCK_ON_A_FETCH)
        late.pop("ua")
        late["rs"] = "complete"
        carried = _with_carried_fields([early, late])
        self.assertEqual(carried.get("ua"), early["ua"])
        # Everything that *is* a fact about now comes from the last report, and
        # a stale mechanism is worse than a missing one — it reads as measured.
        self.assertEqual(carried.get("rs"), "complete")
        self.assertEqual(carried["q"], self.STUCK_ON_A_FETCH["q"])

    def test_a_cleared_failure_is_not_carried_forward(self) -> None:
        """A page that failed a script at 800ms and stopped failing at 2s has
        not failed for the rest of the run, and the merged report must not say
        it has. The empty list rides as an absent key, which is why only `ua`
        is carried."""
        early = dict(self.STUCK_ON_A_FETCH)
        early["x"] = ["script https://a.example/app.js"]
        late = dict(early)
        late.pop("x")
        carried = _with_carried_fields([early, late])
        self.assertNotIn("x", carried)

    def test_a_finished_document_does_not_also_say_its_load_event_never_fired(self) -> None:
        """Measured: `readyState` `complete` and `loadEventEnd` `0` in the same
        report. The engine answers the two at different moments, so the page
        prints the disagreement rather than the more alarming half — "the load
        event never fired" beside a verdict saying the document finished is a
        sentence nobody can act on, and read quickly it is a false alarm."""
        report = dict(self.WAITING_ON_MEDIA)
        report["rs"] = "complete"
        report["q"] = []
        sentence = stall_sentence(report, "loaded")
        self.assertNotIn("never fired", sentence)
        self.assertIn("the two answers disagree", sentence)
        # …and a document that really has not fired still says so.
        stuck = dict(self.WAITING_ON_MEDIA)
        self.assertIn("never fired", stall_sentence(stuck, "waiting-on-media"))

    def test_a_page_that_dropped_the_list_to_fit_says_which_field(self) -> None:
        """The page measures its own payload against a channel of about 980
        characters and gives up its least useful fields when it will not fit.
        Which field it gave up is the difference between "this build does not
        measure stalls" and "this page was too big to carry the measurement" —
        two different problems, two different fixes."""
        report = {k: v for k, v in self.STUCK_ON_A_FETCH.items() if k != "q"}
        report["df"] = "c9,x,q"
        self.assertEqual(classify_stall(report), "no-stall-data")
        sentence = stall_sentence(report, "no-stall-data")
        self.assertIn("dropped c9,x,q", sentence)
        self.assertIn("unknown rather than nothing", sentence)

    def test_the_reader_has_a_name_for_the_field_the_page_sends(self) -> None:
        """Cross-language, and the reason is the same one as the bridge prefix:
        the page can name the stalled request and the run can have no word for
        it, and then the fact is in the transcript and absent from the verdict.
        """
        source = pathlib.Path("src-tauri/src/browser.rs").read_text()
        self.assertIn("q: outstanding()", source)
        self.assertIn("le: loadEventEnd()", source)
        self.assertIn("lz: lastArrival()", source)
        self.assertIn("payload.df = dropped.join", source)

    def test_the_whole_thing_has_to_fit_in_a_document_title(self) -> None:
        """The channel is finite and it was measured, not guessed.

        A report past roughly a kilobyte of encoded text comes back cut off
        mid-string, which the reader sees as no report at all — a run that
        reported a rich youtube.com page as `no-report` because of a video URL's
        signed query string. So the page drops the query, caps the rows, and
        the reader names the truncation rather than blaming the page.
        """
        source = pathlib.Path("src-tauri/src/browser.rs").read_text()
        self.assertIn("PENDING_CAP = 3", source)
        self.assertIn(
            '(parsed.search ? "?" : "")',
            source,
            "the page is carrying query strings again — a googlevideo URL's "
            "signed parameters are the largest thing in the payload and name "
            "nothing; they cost the whole report",
        )
        whole_payload = quote(json.dumps(GITHUB_ISSUE))
        cut = "embed-smoke: report " + whole_payload[: len(whole_payload) // 2]
        self.assertTrue(is_truncated_report(cut), "a cut payload must be named")
        self.assertEqual(
            classify_report(None, truncated=True),
            "truncated-report",
            "a report the channel cut is not a page that said nothing",
        )
        self.assertEqual(classify_report(None), "no-report")
        whole = "embed-smoke: report " + quote(json.dumps(GITHUB_ISSUE))
        self.assertFalse(is_truncated_report(whole), "a whole report is not a cut one")
        self.assertFalse(
            is_truncated_report("embed-smoke: painted"),
            "an unrelated line is not a cut report",
        )
