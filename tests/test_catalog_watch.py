"""The catalogue watch: a release reaches open screens without anybody asking.

Everything here exists because the alternative is a model list that is only
correct if you happened to look at the right moment. `GET /models` is a snapshot;
a provider that ships a model changes the world, and this engine is the only
thing that can notice.

Four properties, and each of them is a way the feature can be useless:

* **Silent when nothing moved.** A channel that says "nothing changed" every
  minute is a channel people learn to ignore, and then they miss the one that
  mattered. So an unchanged sweep sends nothing at all.
* **A frame when something did**, naming the provider and the ids. A bare
  "something changed" leaves the reader to diff two lists by hand.
* **No traffic without a subscriber.** An idle engine that polls eight providers
  forever spends a provider's rate limit on a list nobody is reading, and the
  rate limit is what makes a user's key stop working.
* **A silent baseline on connect.** The alternative is a first sweep that
  announces every model on every provider at once, which is a notification that
  teaches its reader to ignore it.

And one that is about the wire rather than the loop: a socket with a bad boot
token is refused with 4401, because invariant 3 (docs/00 §6.3) holds on a
WebSocket exactly as it does on every route. A push channel that leaks a
provider's model list to anything that can open a socket is not a feature.
"""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py
import asyncio
import json
import unittest
from collections.abc import Callable
from typing import Any

import httpx

from engine.catalog_watch import (
    CATALOG_WATCH_INTERVAL_S,
    CatalogWatch,
    catalog_changes,
    ids_by_provider,
)
from engine.model_catalog import ModelCatalogService

from tests.test_model_catalog import _StubKeychain, _StubRegistry


def _catalog(
    models: list[dict[str, Any]], routes: dict[str, tuple[int, Any]] | None = None,
    calls: list[httpx.Request] | None = None,
) -> ModelCatalogService:
    """A real `ModelCatalogService` over a mock transport, so the watch is tested
    against the discovery it actually drives rather than a hand-written stub of it.

    Ollama is the provider in play: it needs no key, so it is discovered whenever
    anything is, and its `/api/tags` response is the easiest to make change
    between two sweeps.
    """
    body = {
        "models": [
            {"name": m["id"], "model": m["id"], "size": 1, "details": {}}
            for m in models
        ]
    }
    table = dict(routes or {})
    table["/api/tags"] = (200, body)
    return ModelCatalogService(
        _StubRegistry(), _StubKeychain(), transport=httpx.MockTransport(_mock(table, calls))
    )


def _mock(
    routes: dict[str, tuple[int, Any]], calls: list[httpx.Request] | None
) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        for prefix, (status, payload) in routes.items():
            if request.url.path.endswith(prefix):
                return httpx.Response(
                    status, text=json.dumps(payload), headers={"content-type": "application/json"}
                )
        return httpx.Response(404, json={"error": "not found"})

    return handle


def _watch(catalog: ModelCatalogService) -> tuple[CatalogWatch, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []
    return CatalogWatch(catalog, publish=sent.append), sent


def _changes(sent: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only the frames that claim something moved.

    Every sweep reports itself (docs/04 §6.0), so an assertion about "was a
    change announced" has to say so itself — otherwise it is really an assertion
    about whether the heartbeat exists, and it fails for the wrong reason the day
    the heartbeat changes shape.
    """
    return [f for f in sent if f["type"] == "model_catalog_changed"]


# ── the diff ───────────────────────────────────────────────────────────────


class TestCatalogChanges(unittest.TestCase):
    def test_a_new_id_is_named_and_nothing_else_is(self) -> None:
        changes = catalog_changes(
            {"openai": ["a", "b"]}, {"openai": ["a", "b", "c"]}
        )
        self.assertEqual(changes, {"openai": {"added": ["c"], "removed": []}})

    def test_an_unchanged_catalogue_reports_nothing(self) -> None:
        # The common case, and the one that decides whether the channel is worth
        # having: a frame every minute saying "nothing changed" is how a reader
        # learns to stop reading it.
        self.assertEqual(catalog_changes({"openai": ["a", "b"]}, {"openai": ["b", "a"]}), {})

    def test_a_retired_model_is_removed_rather_than_going_quiet(self) -> None:
        # A provider that stops serving something is a fact a reader of a model
        # list needs: a role pointing at the retired id is now broken, and the
        # diff is the only place that says so.
        changes = catalog_changes({"openai": ["a", "gone"]}, {"openai": ["a"]})
        self.assertEqual(changes, {"openai": {"added": [], "removed": ["gone"]}})

    def test_a_provider_that_stopped_answering_is_not_silence(self) -> None:
        # Its disappearance is reported as everything it had, removed — which is
        # what a reader needs to know, rather than a provider quietly absent from
        # a list that is supposed to be complete.
        changes = catalog_changes({"groq": ["a", "b"]}, {})
        self.assertEqual(changes, {"groq": {"added": [], "removed": ["a", "b"]}})

    def test_a_provider_that_answered_for_the_first_time_is_all_added(self) -> None:
        changes = catalog_changes({}, {"nvidia": ["meta-llama/x"]})
        self.assertEqual(changes, {"nvidia": {"added": ["meta-llama/x"], "removed": []}})

    def test_no_previous_snapshot_reads_as_all_added(self) -> None:
        # The caller decides whether to announce a baseline; this function only
        # reports the difference, so it has one rule for both cases.
        changes = catalog_changes(None, {"openai": ["a", "b"]})
        self.assertEqual(changes, {"openai": {"added": ["a", "b"], "removed": []}})

    def test_ids_are_grouped_by_provider_and_keep_their_order(self) -> None:
        grouped = ids_by_provider(
            [
                {"provider": "ollama", "id": "newest"},
                {"provider": "ollama", "id": "older"},
                {"provider": "openai", "id": "gpt-x"},
            ]
        )
        self.assertEqual(
            grouped, {"ollama": ["newest", "older"], "openai": ["gpt-x"]}
        )

    def test_a_model_without_a_provider_is_not_attributed_to_one(self) -> None:
        # Grouping an unnamed model under "" would report a provider that does
        # not exist, and a change to a provider nobody can configure.
        self.assertEqual(ids_by_provider([{"id": "x"}]), {})


# ── the loop ───────────────────────────────────────────────────────────────


class TestCatalogWatch(unittest.IsolatedAsyncioTestCase):
    async def test_nobody_connected_means_nobody_asked(self) -> None:
        # The whole reason the watch is subscriber-gated. A desktop app left open
        # on the settings screen would otherwise poll eight providers every minute
        # all evening, spending a rate limit on a list nobody is reading.
        calls: list[httpx.Request] = []
        watch, sent = _watch(_catalog([{"id": "a"}], calls=calls))
        self.assertEqual(await watch.sweep(), {})
        self.assertEqual(calls, [], "the engine asked a provider with nobody listening")
        self.assertEqual(sent, [])

    async def test_the_first_sweep_after_a_subscriber_arrives_is_a_baseline(self) -> None:
        # Otherwise opening Codify after an hour away announces every model on
        # every provider at once, which is a notification that teaches its reader
        # to ignore it.
        watch, sent = _watch(_catalog([{"id": "a"}, {"id": "b"}]))
        watch.subscribe()
        self.assertEqual(await watch.sweep(), {})
        self.assertEqual([f["type"] for f in sent], ["model_catalog_checked"])
        self.assertNotIn("added", sent[0]["payload"], "a baseline was announced as news")
        self.assertEqual(watch.subscribers, 1)

    async def test_a_release_is_announced_to_everyone_listening(self) -> None:
        watch, sent = _watch(_catalog([{"id": "a"}]))
        watch.subscribe()
        await watch.sweep()  # baseline
        # The provider now serves one more model, which is the event this whole
        # module exists for.
        watch._catalog = _catalog([{"id": "a"}, {"id": "b"}])
        changes = await watch.sweep()

        self.assertEqual(changes, {"ollama": {"added": ["b"], "removed": []}})
        self.assertEqual(len(_changes(sent)), 1, "the change was not announced")
        frame = _changes(sent)[0]
        self.assertEqual(frame["payload"]["added"], {"ollama": ["b"]})
        self.assertEqual(frame["payload"]["removed"], {})
        self.assertIn("fetched_at", frame["payload"])

    async def test_an_unchanged_sweep_sends_nothing_but_says_when_it_asked(self) -> None:
        # Silence about *changes* and a report of the *check* are different jobs.
        # A screen that no longer polls cannot report the age of its own list
        # without this, and the alternative is either a number nobody can check or
        # a timer back on the client spending a rate limit to keep the number
        # fresh.
        watch, sent = _watch(_catalog([{"id": "a"}]))
        watch.subscribe()
        for _ in range(3):
            self.assertEqual(await watch.sweep(), {})
        self.assertEqual(len(sent), 3, "a sweep went unreported")
        for frame in sent:
            self.assertEqual(frame["type"], "model_catalog_checked")
            self.assertIn("fetched_at", frame["payload"])
            self.assertNotIn("added", frame["payload"], "a heartbeat is not a change")

    async def test_reconnecting_does_not_announce_everything_as_new(self) -> None:
        # The baseline is dropped when the last subscriber leaves, so the client
        # that comes back is not told about an hour of accumulated "news" it never
        # had the chance to see.
        watch, sent = _watch(_catalog([{"id": "a"}]))
        watch.subscribe()
        await watch.sweep()
        watch.unsubscribe()
        watch._catalog = _catalog([{"id": "a"}, {"id": "b"}, {"id": "c"}])
        watch.subscribe()
        self.assertEqual(await watch.sweep(), {})
        self.assertEqual(_changes(sent), [], "an hour of accumulated news was announced")

    async def test_the_frame_carries_a_diff_rather_than_the_catalogue(self) -> None:
        # Eight providers at five hundred models each is a payload no screen asked
        # for, sent on every sweep. The reader re-reads /models, which is a cache
        # hit because this loop is what warmed it.
        watch, sent = _watch(_catalog([{"id": f"m{i}"} for i in range(200)]))
        watch.subscribe()
        await watch.sweep()
        watch._catalog = _catalog([{"id": f"m{i}"} for i in range(201)])
        await watch.sweep()
        payload = json.dumps(_changes(sent)[0])
        self.assertLess(len(payload), 400, f"the frame carries the catalogue: {len(payload)} bytes")

    async def test_the_interval_matches_the_catalogue_cache(self) -> None:
        # A sweep can never be more than one cache period stale, and is never an
        # echo of the previous answer — the same reasoning as the settings panel's
        # own refresh, kept in one place.
        from engine.model_catalog import DEFAULT_TTL_S

        self.assertEqual(CATALOG_WATCH_INTERVAL_S, DEFAULT_TTL_S)

    async def test_a_provider_that_fails_announces_a_removal_rather_than_silence(self) -> None:
        watch, sent = _watch(_catalog([{"id": "a"}]))
        watch.subscribe()
        await watch.sweep()
        watch._catalog = ModelCatalogService(
            _StubRegistry(), _StubKeychain(), transport=httpx.MockTransport(_mock({}, None))
        )
        changes = await watch.sweep()
        self.assertEqual(changes, {"ollama": {"added": [], "removed": ["a"]}})
        self.assertEqual(_changes(sent)[0]["payload"]["removed"], {"ollama": ["a"]})

    async def test_every_sweep_is_reported_to_every_subscriber(self) -> None:
        # One sweep, one answer, and the answer reaches the client that asked for
        # it. The point of the channel is that N screens cost one discovery.
        watch, sent = _watch(_catalog([{"id": "a"}]))
        watch.subscribe()
        watch.subscribe()
        await watch.sweep()
        self.assertEqual(len(sent), 1, "a second subscriber got its own discovery")
        self.assertEqual(watch.subscribers, 2)


# ── the socket ─────────────────────────────────────────────────────────────


class TestEngineSocket(unittest.TestCase):
    def _app(self) -> Any:
        try:
            from starlette.testclient import TestClient  # noqa: F401
        except ImportError:
            self.skipTest("starlette testclient's websocket support unavailable")
        from engine.app import app

        return app

    def test_a_bad_token_is_refused_before_anything_is_sent(self) -> None:
        # Invariant 3 (docs/00 §6.3) on a socket exactly as on every route: a push
        # channel that leaks a provider's model list to anything that can open a
        # socket is not a feature.
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        app = self._app()
        with TestClient(app) as client:
            with self.assertRaises(WebSocketDisconnect) as ctx:
                with client.websocket_connect(
                    "/ws/engine", headers={"Authorization": "Bearer not-the-token"}
                ) as ws:
                    ws.receive_text()
            self.assertEqual(ctx.exception.code, 4401)

            # And the engine is not left believing somebody is listening, which
            # would keep it polling providers for a client that never arrived.
            self.assertEqual(app.state.catalog_watch.subscribers, 0)

    def test_a_connection_subscribes_and_unsubscribes(self) -> None:
        # The subscription count is what the watch loop gates provider traffic on,
        # so a connection that does not register is a feature that costs
        # everything and announces nothing.
        from starlette.testclient import TestClient

        app = self._app()
        with TestClient(app) as client:
            with client.websocket_connect(
                "/ws/engine", headers={"Authorization": f"Bearer {app.state.token}"}
            ):
                self.assertEqual(app.state.catalog_watch.subscribers, 1)
            # The socket is closed by the `with`; the endpoint's `finally` runs as
            # the connection tears down, so give the loop one turn to notice.
            for _ in range(20):
                if app.state.catalog_watch.subscribers == 0:
                    break
                _turn()
            self.assertEqual(app.state.catalog_watch.subscribers, 0)

    def test_a_change_reaches_a_connected_client(self) -> None:
        # The socket half of the chain, over a real connection: whatever the
        # watcher publishes lands in the client's hands.
        #
        # Publishing is a `put_nowait` on the server's own loop, and the endpoint
        # drains it there — so this needs no second event loop. Driving a sweep
        # from the test's loop instead is the shape that hangs: the TestClient
        # runs the app on a loop of its own, an httpx client bound to another one
        # never completes, and `receive_text` waits forever on a frame that was
        # never queued. The watch-to-publish half is covered above, on the loop it
        # actually runs on.
        from starlette.testclient import TestClient

        app = self._app()
        frame = {
            "type": "model_catalog_changed",
            "payload": {"added": {"ollama": ["b"]}, "removed": {}, "fetched_at": 1.0},
        }
        with TestClient(app) as client:
            with client.websocket_connect(
                "/ws/engine", headers={"Authorization": f"Bearer {app.state.token}"}
            ) as ws:
                app.state.publish_engine_event(frame)
                received = json.loads(ws.receive_text())
        self.assertEqual(received, frame)


def _turn() -> None:
    """Let the server's teardown coroutine run.

    The TestClient drives the app on its own loop, so the wait is a real
    `asyncio.sleep` on a throwaway loop rather than a `time.sleep` that would only
    make the test slower without making it correct.
    """
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(asyncio.sleep(0))
    finally:
        loop.close()


if __name__ == "__main__":
    unittest.main()
