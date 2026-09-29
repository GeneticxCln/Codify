"""The open tabs, held by the engine, so a second window opens onto the same strip.

Before this the whole tab layout lived in one window's `localStorage`. That is a
fine answer to "where should a view state live" and a wrong answer to "how does
a second window know what is open", because the strip a second window wants is
the strip the first window has, not a copy of it from whenever the profile was
last written.

These tests hold the half of that the engine owns. The split is the design, and
it is worth stating before the assertions:

- **The engine holds which tabs exist and in what order.** Two windows must
  agree about that, so it is the engine's, and it is a table of rows rather than
  a blob because a shared strip has two writers and a blob cannot say *which*
  tab a write was about.
- **The client holds what each tab is showing** — a thread, an address, a
  back/forward stack — as an opaque JSON payload. The engine checks that the
  payload is JSON and how large it is, and deliberately does not learn the
  shape (see `ShellTabService`), because a store that learns a view's shape is
  how the two drift apart.

What is pinned below:

* **A tab outlives the window that opened it**, read back through the API and
  then through a *new connection to the same file*, which is what a restart is.
* **Order is the strip's order**, and a second tab lands after the first rather
  than beside it at an arbitrary position.
* **A write is one tab, not the layout.** Sending one tab must not disturb
  another tab's row — the failure a whole-layout write would have, and the one
  that would close a second window's tabs every time it saved.
* **A close is shared and idempotent.** With two windows on one strip, closing
  the same tab in both is the shared strip working, not a client bug, so the
  second close is not a 404. It is also why the response is the strip: what is
  left is what the caller needed.
* **The refusals are the engine's.** A payload that is not JSON is refused here,
  with a sentence, rather than stored and failing in every window that reads it.
  `extra: "forbid"` keeps a client from inventing `updated_at`, the one fact the
  engine owns.
* **The token is still required** (docs/00 §6.3). A new resource is not an
  exemption, and this is the test that says so.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import httpx
from httpx import ASGITransport

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.app import BOOT_TOKEN, app
from engine.db import connect
from engine.executor import ExecutorService
from engine.providers import Keychain, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import (
    AgentRegistryService,
    ConversationService,
    GoalService,
    SettingsService,
    ShellTabService,
    WorkspaceService,
)


def payload(conversation_id: str | None = None, url: str | None = None) -> str:
    """A tab's payload, in the shape the client sends (opaque to the engine)."""
    body: dict[str, Any] = {"kind": "chat"}
    if conversation_id is not None:
        body["conversationId"] = conversation_id
    if url is not None:
        body["url"] = url
        body["history"] = {"entries": [url], "index": 0}
    return json.dumps(body)


class ShellTabTestCase(unittest.IsolatedAsyncioTestCase):
    """An isolated engine, and a client that talks to it over real HTTP."""

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.db_path = self.root / "test.db"

        conn = connect(self.db_path)
        keychain = Keychain(secrets_path=self.root / "secrets.json")
        factory = ProviderFactory(keychain)
        app.state.conn = conn
        app.state.keychain = keychain
        app.state.factory = factory
        app.state.registry = AgentRegistryService(conn, factory, keychain)
        app.state.workspaces = WorkspaceService(conn)
        app.state.conversations = ConversationService(conn)
        app.state.shell_tabs = ShellTabService(conn)
        app.state.goals = GoalService(conn)
        app.state.sandbox = SandboxService()
        app.state.settings = SettingsService(conn)
        app.state.executor = ExecutorService(
            app.state.goals, app.state.workspaces, app.state.registry, app.state.sandbox
        )
        app.state.executor.settings = app.state.settings
        app.state.token = BOOT_TOKEN

        self.client = httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://engine",
            headers={"Authorization": f"Bearer {BOOT_TOKEN}"},
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        app.state.conn.close()
        self.temp_dir.cleanup()

    async def call(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """One request, with the boot token (docs/00 §6.3)."""
        return await self.client.request(method, url, **kwargs)

    # -- helpers ---------------------------------------------------------

    async def put_tab(
        self, key: str, position: int, kind: str = "chat", body: str | None = None
    ) -> list[dict[str, Any]]:
        response = await self.client.put(
            "/shell/tabs",
            json={
                "key": key,
                "position": position,
                "kind": kind,
                "payload": body if body is not None else payload("c1"),
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        rows: list[dict[str, Any]] = response.json()
        return rows

    async def list_tabs(self) -> list[dict[str, Any]]:
        response = await self.client.get("/shell/tabs")
        self.assertEqual(response.status_code, 200, response.text)
        rows: list[dict[str, Any]] = response.json()
        return rows

    # -- the tests -------------------------------------------------------

    async def test_a_tab_outlives_the_window_that_opened_it(self) -> None:
        await self.put_tab("k1", 0, kind="browser", body=payload(url="https://example.com/"))

        # Over a *new connection to the same file*, which is what a restart is.
        # Reading it back through the API is the honest assertion: a row in the
        # same process would prove only that a dict still had a key in it.
        app.state.conn.close()
        conn = connect(self.db_path)
        try:
            app.state.shell_tabs = ShellTabService(conn)
            tabs = await self.list_tabs()
        finally:
            conn.close()

        self.assertEqual(len(tabs), 1)
        self.assertEqual(tabs[0]["key"], "k1")
        self.assertEqual(tabs[0]["kind"], "browser")
        # The payload comes back byte for byte: the engine stores what a tab is
        # showing without learning its shape, and a lossy round trip through a
        # store that "understood" it would be the first step towards owning it.
        self.assertEqual(
            json.loads(tabs[0]["payload"]),
            {"kind": "chat", "url": "https://example.com/",
             "history": {"entries": ["https://example.com/"], "index": 0}},
        )

    async def test_the_strip_reads_in_the_order_it_was_written(self) -> None:
        for key, position in (("third", 2), ("first", 0), ("second", 1)):
            await self.put_tab(key, position)
        self.assertEqual(
            [tab["key"] for tab in await self.list_tabs()],
            ["first", "second", "third"],
            "position is the strip's order, not the order rows happened to arrive in",
        )

    async def test_two_tabs_claiming_one_slot_have_a_defined_order(self) -> None:
        # Not a uniqueness claim — a client re-pushes positions on its next
        # change — but a *stable* read, so two clients that read the same strip
        # see it in the same order.
        await self.put_tab("b", 0)
        await self.put_tab("a", 0)
        self.assertEqual([tab["key"] for tab in await self.list_tabs()], ["a", "b"])

    async def test_a_write_is_one_tab_and_disturbs_nobody_else(self) -> None:
        await self.put_tab("k1", 0, body=payload("conv-1"))
        await self.put_tab("k2", 1, body=payload("conv-2"))
        # Navigating the first tab must not rewrite the second one's row, which
        # is exactly what a whole-layout write would do — and would do to a
        # *second window's* tab, closing work it had open.
        await self.put_tab("k1", 0, body=payload("conv-1", url="https://a.example/"))

        tabs = {tab["key"]: tab for tab in await self.list_tabs()}
        self.assertEqual(json.loads(tabs["k1"]["payload"])["url"], "https://a.example/")
        self.assertEqual(json.loads(tabs["k2"]["payload"]), {"kind": "chat", "conversationId": "conv-2"})

    async def test_a_write_returns_the_merged_strip(self) -> None:
        await self.put_tab("k1", 0)
        returned = await self.put_tab("k2", 1)
        self.assertEqual(
            [tab["key"] for tab in returned],
            ["k1", "k2"],
            "the caller's own write and everything else arrive in one response, \
             which is how two windows meet without waiting for each other",
        )

    async def test_rewriting_a_tab_keeps_its_key_and_moves_it(self) -> None:
        # Positions are slots, not a re-ordering primitive: two tabs may claim
        # one, and the read tiebreaks on `key` (`test_two_tabs_claiming_one_slot…`
        # pins that). So "moved" here means the row is updated in place and the
        # strip still holds two tabs — not that a client can shuffle the strip
        # by rewriting positions. Reordering is the client's arithmetic, and it
        # re-pushes every tab's slot when the user drags one.
        await self.put_tab("k1", 0)
        await self.put_tab("k2", 1)
        await self.put_tab("k1", 1)  # the same tab, claiming k2's slot
        tabs = await self.list_tabs()
        self.assertEqual(len(tabs), 2, "a re-send is an update, not a second row")
        self.assertEqual([tab["key"] for tab in tabs], ["k1", "k2"], "ties read by key")
        self.assertEqual([tab["position"] for tab in tabs], [1, 1])

    async def test_a_close_is_shared_and_idempotent(self) -> None:
        await self.put_tab("k1", 0)
        await self.put_tab("k2", 1)
        first = await self.client.delete("/shell/tabs/k1")
        self.assertEqual(first.status_code, 200)
        self.assertEqual([tab["key"] for tab in first.json()], ["k2"])
        # The second window closing the same tab is the shared strip working.
        second = await self.client.delete("/shell/tabs/k1")
        self.assertEqual(
            second.status_code,
            200,
            "closing a tab in two windows is not a client bug, and a 404 here \
             would make the ordinary case an error the user has to see",
        )
        self.assertEqual([tab["key"] for tab in second.json()], ["k2"])

    async def test_a_payload_that_is_not_json_is_refused_with_a_sentence(self) -> None:
        response = await self.client.put(
            "/shell/tabs",
            json={"key": "k1", "position": 0, "kind": "browser", "payload": "not json {"},
        )
        self.assertEqual(response.status_code, 422, response.text)
        body = response.json()
        self.assertEqual(body["code"], "invalid_tab_payload")
        self.assertTrue(body["message"].strip())
        # And it was not stored: a row holding text that is not JSON would fail
        # in every window that read it, and the failure would surface there
        # instead of here.
        self.assertEqual(await self.list_tabs(), [])

    async def test_a_client_cannot_invent_the_timings(self) -> None:
        response = await self.client.put(
            "/shell/tabs",
            json={
                "key": "k1",
                "position": 0,
                "kind": "chat",
                "payload": payload("c1"),
                "updated_at": 1.0,
            },
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["code"], "invalid_request")

    async def test_the_usual_shape_checks_still_apply(self) -> None:
        cases = [
            {"key": "", "position": 0, "kind": "chat", "payload": "{}"},
            {"key": "k1", "position": -1, "kind": "chat", "payload": "{}"},
            {"key": "k1", "position": 0, "kind": "terminal", "payload": "{}"},
            {"key": "k1", "position": 0, "kind": "chat"},
            {"key": "x" * 65, "position": 0, "kind": "chat", "payload": "{}"},
            {"key": "k1", "position": 0, "kind": "chat", "payload": "y" * 32_001},
        ]
        for body in cases:
            with self.subTest(body=body):
                response = await self.client.put("/shell/tabs", json=body)
                self.assertEqual(response.status_code, 422, response.text)

    async def test_a_terminal_tab_is_not_a_kind(self) -> None:
        # The client does not remember terminal tabs at all — a PTY is a live
        # process — so the engine has no row shape for one and refuses rather
        # than storing something no client can restore.
        response = await self.client.put(
            "/shell/tabs",
            json={"key": "k1", "position": 0, "kind": "terminal", "payload": "{}"},
        )
        self.assertEqual(response.status_code, 422)

    async def test_the_token_is_still_required(self) -> None:
        # docs/00 §6.3. A new resource is not an exemption.
        anonymous = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://engine")
        try:
            self.assertEqual((await anonymous.get("/shell/tabs")).status_code, 401)
            self.assertEqual(
                (
                    await anonymous.put(
                        "/shell/tabs",
                        json={"key": "k1", "position": 0, "kind": "chat", "payload": "{}"},
                    )
                ).status_code,
                401,
            )
        finally:
            await anonymous.aclose()

    async def test_the_table_appears_in_a_database_that_predates_it(self) -> None:
        # An install that has never heard of shell tabs must not need a
        # migration: the table is created by the schema script that runs on
        # every connect, exactly as every other table in this store is.
        legacy = connect(self.root / "legacy.db")
        try:
            legacy.execute("DROP TABLE shell_tabs")
            legacy.commit()
            legacy.close()
            reopened = connect(self.root / "legacy.db")
            try:
                rows = reopened.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'shell_tabs'"
                ).fetchall()
                self.assertEqual(len(rows), 1)
            finally:
                reopened.close()
        finally:
            pass
