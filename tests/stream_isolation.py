"""Shared isolation assertions for the two stream-isolation test layers.

`test_concurrent_streams.py` collects events straight from the service layer
(pydantic `Event` models) and `test_concurrent_streams_ws.py` reads JSON frames
off real WebSockets. They must assert the *same* guarantees, or one layer can
quietly drift from the other — a weaker wire check hiding a service regression,
or vice versa. So the properties live here exactly once, in a form both layers
can feed:

    assert_stream_pure(tc, frames, goal_id, "alpha")
    assert_dense_from_one(tc, frames, "alpha")
    assert_content_separated(tc, {"alpha": frames_a, "beta": frames_b})
    assert_replay_equals_live(tc, replay_seqs, live_seqs, "alpha")
    assert_midrun_resume_is_seamless(tc, live, midrun, floor, goal_id, "gamma")
    assert_pause_spell_is_midstream(tc, frames, "beta")
    assert_cancelled_stream_ends_cleanly(tc, frames, goal_id, "delta")

Each function takes the TestCase as `tc` so failures are reported against the
running test. Event objects and raw JSON dicts are both accepted: the
normalizers accept anything with attribute access or a mapping.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

# The two stream layers hand in different event shapes: the service layer works
# with Event models, the wire layer with parsed JSON dicts. Both answer the same
# attribute questions, so the helpers read both through one alias — `Any` is
# the honest union here, not a shrug.
Frame = Any


def _get(item: Any, key: str) -> Any:
    """Read `key` from an Event object or a JSON dict, the same way."""
    if isinstance(item, Mapping):
        return item.get(key)
    return getattr(item, key)


def _as_dicts(frames: Iterable[Any]) -> list[dict[str, Any]]:
    """Normalize frames to plain dicts so json.dumps sees identical bytes."""
    out: list[dict[str, Any]] = []
    for f in frames:
        out.append(dict(f) if isinstance(f, Mapping) else f.model_dump())
    return out


def _seqs(frames: Iterable[Frame]) -> list[int]:
    return [_get(e, "sequence") for e in frames]


def _frame_text(frames: Iterable[Frame]) -> str:
    # `type` is engine vocabulary (e.g. the `model_delta` event type), not
    # goal content — one of the goals is literally named "delta" — so it is
    # masked before the substring scan.
    return "\n".join(
        json.dumps({k: v for k, v in e.items() if k != "type"}, sort_keys=True)
        for e in _as_dicts(frames)
    )


def assert_stream_pure(tc: Any, frames: Iterable[Frame], goal_id: str, name: str) -> None:
    """1. Purity: every event a reader saw belongs to its goal."""
    for event in frames:
        tc.assertEqual(
            _get(event, "goal_id"), goal_id,
            f"a foreign event reached {name}'s stream",
        )


def assert_dense_from_one(tc: Any, frames: Iterable[Frame], name: str) -> None:
    """2. Integrity: sequences dense and ascending from 1.

    The counter is per-goal (UPDATE … RETURNING on the goal row) — a shared
    counter would interleave and break density; a racy one would skip; a lost
    or duplicated frame on a live connection would leave a hole.
    """
    seqs = _seqs(frames)
    tc.assertEqual(
        seqs, list(range(1, len(seqs) + 1)),
        f"{name}'s sequence broke",
    )


def assert_content_separated(
    tc: Any, streams: dict[str, list[Frame]], body_markers: dict[str, str] | None = None
) -> None:
    """3. Separation by content, pairwise: no byte of one goal's marker
    appears in another's stream, and each stream carries its own body text.

    The markers are chosen so UUID hex (which every frame carries) cannot
    contain them — a hit is real content, not an id collision.
    """
    texts = {name: _frame_text(frames) for name, frames in streams.items()}
    for name, text in texts.items():
        for other in texts:
            if other != name:
                tc.assertNotIn(
                    other, text,
                    f"{name}'s stream contains {other}'s content",
                )
        if body_markers:
            tc.assertIn(body_markers[name], text, f"{name}'s own content is missing")


def assert_replay_equals_live(
    tc: Any, replay_seqs: list[int], live_seqs: list[int], name: str
) -> None:
    """4. Replay equality: a subscriber that attaches late (or re-attaches)
    sees exactly what the live subscriber collected, in order."""
    tc.assertEqual(
        replay_seqs, live_seqs,
        f"{name}'s replay diverged from what the live subscriber saw",
    )


def assert_midrun_resume_is_seamless(
    tc: Any, live_frames: Iterable[Frame], midrun_frames: Iterable[Frame],
    floor: int, goal_id: str, name: str,
) -> None:
    """A subscriber attaching mid-run must see a stream with no seam.

    The engine replays from 0 on every connect and the client dedups by
    sequence (goalStream.ts), so the mid-run frames must be dense from 1,
    a prefix-equal of the live stream, strictly beyond the observed floor,
    and pure.
    """
    seqs_live = _seqs(live_frames)
    seqs_mid = _seqs(midrun_frames)
    tc.assertGreater(
        len(seqs_mid), floor,
        f"the mid-run subscriber attached to {name} saw nothing new",
    )
    tc.assertEqual(
        seqs_mid, list(range(1, len(seqs_mid) + 1)),
        f"{name}'s mid-run replay is not dense — a seam at the attach point",
    )
    tc.assertEqual(
        seqs_mid, seqs_live[: len(seqs_mid)],
        f"{name}'s mid-run subscriber diverged from the live stream",
    )
    assert_stream_pure(tc, midrun_frames, goal_id, f"{name} (mid-run)")


def assert_pause_spell_is_midstream(tc: Any, frames: Iterable[Frame], name: str) -> None:
    """A mid-run PAUSED spell leaves the stream complete and coherent.

    Density survives the pause; PAUSED and the later RUNNING both reach the
    stream; PAUSED lands before the goal's last completed step (the spell was
    genuinely mid-run, not after the work); the goal ends COMPLETED.
    """
    assert_dense_from_one(tc, frames, name)
    statuses = [
        _get(e, "payload")["status"]
        for e in frames
        if _get(e, "type") == "goal_status"
    ]
    tc.assertIn("PAUSED", statuses, f"{name}'s pause never reached the stream")
    tc.assertIn(
        "RUNNING", statuses[statuses.index("PAUSED"):],
        f"{name}'s resume never reached the stream",
    )
    tc.assertEqual(statuses[-1], "COMPLETED", f"{name} did not end COMPLETED")

    dicts = _as_dicts(frames)
    paused_idx = next(
        i for i, e in enumerate(dicts)
        if e.get("type") == "goal_status" and e.get("payload", {}).get("status") == "PAUSED"
    )
    completed_step_idxs = [
        i for i, e in enumerate(dicts)
        if e.get("type") == "step_status" and e.get("payload", {}).get("status") == "COMPLETED"
    ]
    tc.assertTrue(completed_step_idxs, f"{name} shows no completed step at all")
    tc.assertLess(
        paused_idx, max(completed_step_idxs),
        f"{name}'s PAUSED frame arrived after the goal's work was done",
    )


def assert_cancelled_stream_ends_cleanly(
    tc: Any, frames: Iterable[Frame], goal_id: str, name: str
) -> None:
    """A goal cancelled mid-run ends its stream at the cancel, cleanly.

    After the CANCELLED frame the stream must stay silent — the step runner
    checks the status between steps and stops, so no scribe verdict or commit
    may dribble out afterwards. Before it: density intact (the cancel tears no
    hole), the CANCELLED frame lands mid-stream (the scenario was real, not a
    post-completion rename), and at most one step may show COMPLETED (the one
    in flight when the cancel landed — with a single-step plan, zero or one
    are both honest outcomes).
    """
    assert_stream_pure(tc, frames, goal_id, name)
    assert_dense_from_one(tc, frames, name)

    dicts = _as_dicts(frames)
    terminal = [
        e for e in dicts
        if e.get("type") == "goal_status"
        and e.get("payload", {}).get("status") in ("COMPLETED", "FAILED", "CANCELLED")
    ]
    tc.assertTrue(terminal, f"{name}'s stream never reached a terminal status")
    tc.assertEqual(
        terminal[-1]["payload"]["status"], "CANCELLED",
        f"{name} ended {terminal[-1]['payload']['status']}, not CANCELLED",
    )
    cancelled_idx = next(
        i for i, e in enumerate(dicts)
        if e.get("type") == "goal_status" and e.get("payload", {}).get("status") == "CANCELLED"
    )
    tc.assertLess(
        cancelled_idx, len(dicts) - 1,
        f"{name}'s CANCELLED frame is the last frame — the scenario was not mid-run",
        # The drain loop keeps reading a quiet spell after the terminal frame,
        # so at least one poll follows it; a cancel that arrived post-run would
        # sit at the very end and make every other assertion vacuous.
    )
    completed_steps = [
        e for e in dicts
        if e.get("type") == "step_status" and e.get("payload", {}).get("status") == "COMPLETED"
    ]
    tc.assertLessEqual(
        len(completed_steps), 1,
        f"{name} completed {len(completed_steps)} steps despite being cancelled",
    )
    after = dicts[cancelled_idx + 1:]
    for e in after:
        tc.assertNotEqual(
            _get(e, "type"), "goal_status",
            f"{name} changed status again after CANCELLED",
        )
