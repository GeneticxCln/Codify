"""A conversation is a thread of turns, and it is the thing a tab points at.

Before this existed the transcript was one array of React state: every send was
one goal, flat and unlabelled, and reloading the window lost the thread. These
tests are what makes a thread a durable thing rather than a UI convenience.

The shape they hold, and why:

- **A thread outlives the window.** Created in the engine, listed by the engine.
  A conversation that lived in `localStorage` would vanish with the browser
  profile — the exact defect this replaces.
- **A goal joins a thread, and the link is checked.** Not a free label: the
  thread has to exist and be in the same workspace, or a client could show one
  workspace's work under another's name.
- **Deleting a thread keeps its runs.** `ON DELETE SET NULL`. A goal's history is
  the audit trail of what the engine did, and closing a tab is not a way to
  erase it.
- **A rename cannot move or archive the thread.** `extra: "forbid"`, for the same
  reason invariant 2 (docs/00 §6.2) forbids extras on `POST /goals`.
"""

from __future__ import annotations

import sqlite3
import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
from httpx import ASGITransport

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.app import BOOT_TOKEN, app
from engine.db import connect
from engine.executor import ExecutorService
from engine.models import ConversationCreate, GoalCreate, WorkspaceCreate
from engine.providers import Keychain, ProviderFactory
from engine.sandbox import SandboxService
from engine.services import (
    AgentRegistryService,
    ConversationService,
    GoalService,
    SettingsService,
    WorkspaceService,
)


class ConversationTestCase(unittest.IsolatedAsyncioTestCase):
    """An isolated engine with a workspace in it, and a client to talk to it."""

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
        app.state.goals = GoalService(conn)
        app.state.sandbox = SandboxService()
        app.state.settings = SettingsService(conn)
        app.state.executor = ExecutorService(
            app.state.goals, app.state.workspaces, app.state.registry, app.state.sandbox
        )
        app.state.executor.settings = app.state.settings

        # Planning is stubbed for the same reason it is in `test_api.py`: these
        # are tests about which thread a goal joins, and a live LLM provider has
        # nothing to say about that.
        async def mock_run_planning(goal_id: str) -> None:
            return None

        app.state.executor.run_planning = mock_run_planning
        app.state.token = BOOT_TOKEN

        self.conn = conn
        self.conversations = app.state.conversations
        self.goals = app.state.goals

        repo = self.root / "repo"
        repo.mkdir()
        (self.root / "other").mkdir()
        ws_service: WorkspaceService = app.state.workspaces
        self.workspace = ws_service.create(_workspace_create("codify", str(repo)))

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    async def call(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        # Invariant 3 (docs/00 §6.3): every request carries the boot token. A
        # test that skipped it would be asserting on an unauthenticated engine,
        # which is not the one anyone runs.
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {BOOT_TOKEN}"},
        ) as client:
            return await client.request(method, url, **kwargs)

    # ── a thread is a durable thing ────────────────────────────────────────

    async def test_a_conversation_outlives_the_window_that_opened_it(self) -> None:
        # The whole point. It lives in the engine's store, not in the client, so
        # a reload is a list read and not a loss.
        created = await self.call(
            "POST", "/conversations", json={"workspace_id": self.workspace.id}
        )
        self.assertEqual(created.status_code, 200, created.text)
        thread_id = created.json()["id"]

        listed = await self.call(
            "GET", "/conversations", params={"workspace_id": self.workspace.id}
        )
        self.assertEqual(listed.status_code, 200)
        self.assertIn(thread_id, [c["id"] for c in listed.json()])

        fetched = await self.call("GET", f"/conversations/{thread_id}")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["id"], thread_id)
        # No title is invented. A placeholder in the UI is more honest than a
        # string the user never chose.
        self.assertEqual(fetched.json()["title"], "")

    async def test_a_thread_is_named_from_its_first_prompt(self) -> None:
        created = await self.call(
            "POST",
            "/conversations",
            json={"workspace_id": self.workspace.id, "title": "  Fix the flaky test  "},
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["title"], "Fix the flaky test")

    async def test_an_unknown_conversation_is_a_404_rather_than_a_blank_tab(
        self,
    ) -> None:
        resp = await self.call("GET", "/conversations/nope")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["code"], "unknown_conversation")

    # ── a goal joins a thread, and the link is checked ─────────────────────

    async def test_a_goal_joins_a_thread_and_says_which_one(self) -> None:
        thread = await self.call(
            "POST", "/conversations", json={"workspace_id": self.workspace.id}
        )
        thread_id = thread.json()["id"]
        goal = await self.call(
            "POST",
            "/goals",
            json={
                "workspace_id": self.workspace.id,
                "conversation_id": thread_id,
                "title": "Add a docstring",
            },
        )
        self.assertEqual(goal.status_code, 200, goal.text)
        self.assertEqual(goal.json()["conversation_id"], thread_id)

        # And it reads back on the goal, not only on the create response.
        got = await self.call("GET", f"/goals/{goal.json()['id']}")
        self.assertEqual(got.json()["conversation_id"], thread_id)

    async def test_turns_come_back_oldest_first(self) -> None:
        # A transcript is a sequence, and the engine owns the order. Reversing it
        # would put the answer before the question.
        thread = await self.call(
            "POST", "/conversations", json={"workspace_id": self.workspace.id}
        )
        thread_id = thread.json()["id"]
        made = []
        for i in range(3):
            resp = await self.call(
                "POST",
                "/goals",
                json={
                    "workspace_id": self.workspace.id,
                    "conversation_id": thread_id,
                    "title": f"question {i}",
                    "description": f"question {i}",
                },
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            made.append(resp.json()["id"])

        turns = await self.call("GET", f"/conversations/{thread_id}/turns")
        self.assertEqual(turns.status_code, 200)
        self.assertEqual([t["goal_id"] for t in turns.json()], made)
        self.assertEqual([t["prompt"] for t in turns.json()], [
            "question 0",
            "question 1",
            "question 2",
        ])

    async def test_a_conversation_from_another_workspace_is_refused(self) -> None:
        # Without this a client could attach a run to another workspace's thread,
        # and a tab would show one project's work under another's name.
        other = app.state.workspaces.create(
            _workspace_create("other", str(self.root / "other"))
        )
        thread = await self.call(
            "POST", "/conversations", json={"workspace_id": other.id}
        )
        thread_id = thread.json()["id"]
        resp = await self.call(
            "POST",
            "/goals",
            json={
                "workspace_id": self.workspace.id,
                "conversation_id": thread_id,
                "title": "sneak",
            },
        )
        self.assertEqual(resp.status_code, 422, resp.text)
        self.assertEqual(resp.json()["code"], "conversation_workspace_mismatch")

    async def test_a_goal_pointing_at_a_thread_that_is_not_there_is_refused(
        self,
    ) -> None:
        # A silently-orphaned goal would render as a tab that never fills.
        resp = await self.call(
            "POST",
            "/goals",
            json={
                "workspace_id": self.workspace.id,
                "conversation_id": "missing",
                "title": "orphan",
            },
        )
        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(resp.json()["code"], "unknown_conversation")

    async def test_a_new_turn_moves_the_thread_to_the_top(self) -> None:
        # The side panel is ordered by last touch, not by creation. A thread
        # nobody has spoken in for a week must not stay pinned above one opened
        # today.
        first = (
            await self.call(
                "POST", "/conversations", json={"workspace_id": self.workspace.id}
            )
        ).json()["id"]
        second = (
            await self.call(
                "POST", "/conversations", json={"workspace_id": self.workspace.id}
            )
        ).json()["id"]

        listed = await self.call(
            "GET", "/conversations", params={"workspace_id": self.workspace.id}
        )
        self.assertEqual([c["id"] for c in listed.json()][0], second)

        # A turn in the older thread moves it back to the front.
        await self.call(
            "POST",
            "/goals",
            json={
                "workspace_id": self.workspace.id,
                "conversation_id": first,
                "title": "still here",
            },
        )
        listed = await self.call(
            "GET", "/conversations", params={"workspace_id": self.workspace.id}
        )
        self.assertEqual([c["id"] for c in listed.json()][0], first)

    # ── a rename is the only mutation, and it is a narrow one ──────────────

    async def test_a_rename_cannot_move_the_thread_or_archive_it(self) -> None:
        # `extra: "forbid"` for the same reason invariant 2 forbids extras on
        # `POST /goals`: a body that tried to set `archived` here would be a
        # silent rewrite through a route that was never meant to carry it.
        thread = (
            await self.call(
                "POST", "/conversations", json={"workspace_id": self.workspace.id}
            )
        ).json()["id"]
        resp = await self.call(
            "PATCH",
            f"/conversations/{thread}",
            json={"title": "renamed", "archived": True},
        )
        self.assertEqual(resp.status_code, 422, resp.text)
        # And the thread is untouched by the refused request.
        got = await self.call("GET", f"/conversations/{thread}")
        self.assertEqual(got.json()["title"], "")
        self.assertFalse(got.json()["archived"])

    async def test_a_child_keeps_its_parent_name_after_the_parent_is_archived(self) -> None:
        """The lineage is data, not something the panel has to go and find.

        The panel lists one workspace's *live* threads and hides archived ones,
        so a child whose parent was archived could not resolve that parent's
        name from the list it was given. The id was still on the row and the
        name still in the database, but nothing on screen could connect them —
        so the label silently degraded to a generic word and never recovered.
        The name now rides along on the child.
        """
        parent = (
            await self.call(
                "POST",
                "/conversations",
                json={"workspace_id": self.workspace.id, "title": "Refactor the parser"},
            )
        ).json()
        child = (
            await self.call(
                "POST",
                "/conversations",
                json={
                    "workspace_id": self.workspace.id,
                    "title": "What about the lexer?",
                    "parent_id": parent["id"],
                },
            )
        ).json()
        # Creation returns the joined read, so a thread born on another one
        # already carries its parent's name.
        self.assertEqual(child["parent_title"], "Refactor the parser")

        await self.call("POST", f"/conversations/{parent['id']}/archive", json={})
        listed = (await self.call("GET", "/conversations")).json()
        by_id = {c["id"]: c for c in listed}
        # The parent is gone from the panel, and the child still says what it is
        # a thread on.
        self.assertNotIn(parent["id"], by_id, "the archived parent is still listed")
        self.assertEqual(
            by_id[child["id"]]["parent_title"],
            "Refactor the parser",
            "archiving a parent erased its child's lineage label",
        )

    async def test_a_child_whose_parent_is_deleted_reports_no_parent_name(self) -> None:
        """The one case where the name really is gone.

        `ON DELETE SET NULL` orphans rather than cascades, so the child survives
        — and then `parent_title` is honestly `None` rather than a stale string
        that names a thread that no longer exists.
        """
        parent = (
            await self.call(
                "POST", "/conversations", json={"workspace_id": self.workspace.id}
            )
        ).json()
        child = (
            await self.call(
                "POST",
                "/conversations",
                json={"workspace_id": self.workspace.id, "parent_id": parent["id"]},
            )
        ).json()
        await self.call("DELETE", f"/conversations/{parent['id']}")
        after = (await self.call("GET", f"/conversations/{child['id']}")).json()
        self.assertIsNone(after["parent_id"])
        self.assertIsNone(after["parent_title"])

    async def test_a_top_level_thread_has_no_parent_name(self) -> None:
        plain = (
            await self.call(
                "POST", "/conversations", json={"workspace_id": self.workspace.id}
            )
        ).json()
        self.assertIsNone(plain["parent_id"])
        self.assertIsNone(plain["parent_title"])

    async def test_a_thread_started_on_another_records_where_it_came_from(self) -> None:
        """The column that makes a thread a thread rather than a new chat.

        A right-click menu whose "New thread" item creates a conversation with
        no parent is a new chat with a misleading label, and nothing about the
        resulting row says which it was. `parent_id` is the whole difference, so
        this pins the link in both directions: what was asked for, and what is
        read back.
        """
        parent = (
            await self.call(
                "POST",
                "/conversations",
                json={"workspace_id": self.workspace.id, "title": "Refactor the parser"},
            )
        ).json()
        self.assertIsNone(parent["parent_id"], "a thread made from nothing has a parent")

        child = (
            await self.call(
                "POST",
                "/conversations",
                json={
                    "workspace_id": self.workspace.id,
                    "title": "What about the lexer?",
                    "parent_id": parent["id"],
                },
            )
        ).json()
        self.assertEqual(child["parent_id"], parent["id"])

        # And it survives a re-read, which is the part that matters: a link that
        # is set in the response but not in the table is a link the panel cannot
        # use on the next render.
        listed = (await self.call("GET", "/conversations")).json()
        by_id = {c["id"]: c for c in listed}
        self.assertEqual(by_id[child["id"]]["parent_id"], parent["id"])
        self.assertIsNone(by_id[parent["id"]]["parent_id"])

    async def test_a_thread_from_another_workspace_is_refused(self) -> None:
        """`parent_id` is client input, so it is checked rather than trusted.

        A parent from another workspace is a thread hanging off a tree the
        caller cannot see — the panel would name a conversation the caller has
        no access to. Refused, not silently dropped: dropping it would produce
        exactly the unlinked new chat this column exists to prevent.
        """
        # A second workspace, in a folder of its own: `/tmp` itself is a shared directory and is
        # not accepted as a workspace root (docs/03, protected roots).
        other_root = self.root / "other-workspace"
        other_root.mkdir()
        other = (
            await self.call("POST", "/workspaces", json={"name": "other", "root_path": str(other_root)})
        ).json()
        parent = (
            await self.call(
                "POST", "/conversations", json={"workspace_id": self.workspace.id}
            )
        ).json()
        resp = await self.call(
            "POST",
            "/conversations",
            json={"workspace_id": other["id"], "parent_id": parent["id"]},
        )
        self.assertEqual(resp.status_code, 422, resp.text)

    async def test_a_parent_that_does_not_exist_is_refused(self) -> None:
        resp = await self.call(
            "POST",
            "/conversations",
            json={"workspace_id": self.workspace.id, "parent_id": "no-such-thread"},
        )
        self.assertEqual(resp.status_code, 404, resp.text)

    async def test_a_thread_cannot_be_re_parented_after_it_is_created(self) -> None:
        """`parent_id` is set at creation and nowhere else.

        A thread's lineage is history. If a rename could move a thread onto
        another one, the shape of what was asked and when would be rewritable
        from the client — the same reason `ConversationUpdate` forbids extras.
        """
        parent = (
            await self.call(
                "POST", "/conversations", json={"workspace_id": self.workspace.id}
            )
        ).json()
        child = (
            await self.call(
                "POST",
                "/conversations",
                json={"workspace_id": self.workspace.id, "parent_id": parent["id"]},
            )
        ).json()
        resp = await self.call(
            "PATCH",
            f"/conversations/{child['id']}",
            json={"title": "renamed", "parent_id": "somewhere-else"},
        )
        self.assertEqual(resp.status_code, 422, resp.text)
        after = (await self.call("GET", f"/conversations/{child['id']}")).json()
        self.assertEqual(after["parent_id"], parent["id"])

    async def test_a_rename_takes(self) -> None:
        thread = (
            await self.call(
                "POST", "/conversations", json={"workspace_id": self.workspace.id}
            )
        ).json()["id"]
        resp = await self.call(
            "PATCH", f"/conversations/{thread}", json={"title": "Refactor the parser"}
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["title"], "Refactor the parser")

    async def test_an_archived_thread_leaves_the_panel_but_is_not_lost(self) -> None:
        thread = (
            await self.call(
                "POST", "/conversations", json={"workspace_id": self.workspace.id}
            )
        ).json()["id"]
        archived = await self.call("POST", f"/conversations/{thread}/archive")
        self.assertEqual(archived.status_code, 200)
        self.assertTrue(archived.json()["archived"])

        # Hidden from the panel's default read...
        panel = await self.call(
            "GET", "/conversations", params={"workspace_id": self.workspace.id}
        )
        self.assertNotIn(thread, [c["id"] for c in panel.json()])
        # ...but reachable when asked for, and still fetchable by id.
        everything = await self.call(
            "GET",
            "/conversations",
            params={"workspace_id": self.workspace.id, "include_archived": True},
        )
        self.assertIn(thread, [c["id"] for c in everything.json()])

        restored = await self.call(
            "POST", f"/conversations/{thread}/archive", params={"archived": False}
        )
        self.assertFalse(restored.json()["archived"])

    # ── deleting a thread keeps the runs ───────────────────────────────────

    async def test_deleting_a_thread_keeps_its_runs_in_the_history(self) -> None:
        # Closing a tab is not a way to erase an audit trail. `ON DELETE SET
        # NULL` means the goals survive as single-turn threads.
        thread = (
            await self.call(
                "POST", "/conversations", json={"workspace_id": self.workspace.id}
            )
        ).json()["id"]
        goal = (
            await self.call(
                "POST",
                "/goals",
                json={
                    "workspace_id": self.workspace.id,
                    "conversation_id": thread,
                    "title": "the run that mattered",
                },
            )
        ).json()

        deleted = await self.call("DELETE", f"/conversations/{thread}")
        self.assertEqual(deleted.status_code, 200, deleted.text)

        gone = await self.call("GET", f"/conversations/{thread}")
        self.assertEqual(gone.status_code, 404)

        survivor = await self.call("GET", f"/goals/{goal['id']}")
        self.assertEqual(survivor.status_code, 200)
        self.assertIsNone(survivor.json()["conversation_id"])

        history = await self.call(
            "GET", "/goals", params={"workspace_id": self.workspace.id}
        )
        self.assertIn(goal["id"], [g["id"] for g in history.json()])

    # ── invariant 2 survives the new field ────────────────────────────────

    async def test_agent_config_is_still_refused_on_goal_creation(self) -> None:
        # Adding `conversation_id` to `GoalCreate` must not weaken the
        # `extra: "forbid"` that invariant 2 (docs/00 §6.2) rests on. The two
        # mutators remain `PUT /settings/agents/{role}` and nothing else.
        resp = await self.call(
            "POST",
            "/goals",
            json={
                "workspace_id": self.workspace.id,
                "title": "x",
                "agent_config": {"role": "fixer", "provider": "anthropic"},
            },
        )
        self.assertEqual(resp.status_code, 422, resp.text)

    # ── attaching an existing goal to a thread ─────────────────────────────

    async def test_an_existing_goal_attaches_to_a_thread_and_survives_a_restart(
        self,
    ) -> None:
        # The headline. A goal that predates conversations is created with no
        # thread; the restore path then files it under one — and that link has
        # to reach the store, or the next restart reads the run as its own
        # thread again (docs/09 §6).
        goal = (
            await self.call(
                "POST",
                "/goals",
                json={"workspace_id": self.workspace.id, "title": "old run"},
            )
        ).json()
        self.assertIsNone(goal["conversation_id"])

        thread = (
            await self.call(
                "POST", "/conversations", json={"workspace_id": self.workspace.id}
            )
        ).json()["id"]

        attached = await self.call(
            "PUT",
            f"/goals/{goal['id']}/conversation",
            json={"conversation_id": thread},
        )
        self.assertEqual(attached.status_code, 200, attached.text)
        self.assertEqual(attached.json()["conversation_id"], thread)
        # The link is metadata, not a concurrent state change: `version` stays
        # what it was, so a client holding an expected_version is not silently
        # invalidated by filing the run somewhere.
        self.assertEqual(attached.json()["version"], goal["version"])

        # Restart: close the connection and open a new one over the same file —
        # everything a fresh engine process would do — then read the link back
        # over HTTP. A link that only lived in the old connection's memory
        # would fail exactly here.
        self.conn.close()
        conn = connect(self.db_path)
        app.state.conn = conn
        app.state.goals = GoalService(conn)
        app.state.conversations = ConversationService(conn)
        self.conn = conn

        reread = await self.call("GET", f"/goals/{goal['id']}")
        self.assertEqual(reread.status_code, 200, reread.text)
        self.assertEqual(reread.json()["conversation_id"], thread)

        # And the thread's transcript contains it — this is what a tab loads
        # after a restart to rebuild itself in the right place.
        turns = await self.call("GET", f"/conversations/{thread}/turns")
        self.assertEqual([t["goal_id"] for t in turns.json()], [goal["id"]])

    async def test_attaching_checks_the_link_the_same_way_creation_does(self) -> None:
        # One check, shared by create and attach (GoalService._check_link): if
        # attachment were looser than creation, the route below would be the
        # reach-around docs/09 §3.1 forbids.
        goal = (
            await self.call(
                "POST",
                "/goals",
                json={"workspace_id": self.workspace.id, "title": "old run"},
            )
        ).json()

        unknown_goal = await self.call(
            "PUT", "/goals/missing/conversation", json={"conversation_id": "c"}
        )
        self.assertEqual(unknown_goal.status_code, 404, unknown_goal.text)
        self.assertEqual(unknown_goal.json()["code"], "unknown_goal")

        unknown_thread = await self.call(
            "PUT",
            f"/goals/{goal['id']}/conversation",
            json={"conversation_id": "missing"},
        )
        self.assertEqual(unknown_thread.status_code, 404, unknown_thread.text)
        self.assertEqual(unknown_thread.json()["code"], "unknown_conversation")

        other = app.state.workspaces.create(
            _workspace_create("other", str(self.root / "other"))
        )
        foreign = (
            await self.call(
                "POST", "/conversations", json={"workspace_id": other.id}
            )
        ).json()["id"]
        mismatch = await self.call(
            "PUT",
            f"/goals/{goal['id']}/conversation",
            json={"conversation_id": foreign},
        )
        self.assertEqual(mismatch.status_code, 422, mismatch.text)
        self.assertEqual(mismatch.json()["code"], "conversation_workspace_mismatch")

        # `extra: "forbid"`, same reach-around as invariant 2 on POST /goals:
        # this route moves one link and is not a general goal editor. The
        # conversation here is a *valid* one on purpose — with a cross-workspace
        # id the 422 could come from the link check and the forbid would go
        # untested, which is exactly how this test fooled itself once while
        # being mutation-tested.
        own = (
            await self.call(
                "POST", "/conversations", json={"workspace_id": self.workspace.id}
            )
        ).json()["id"]
        extra = await self.call(
            "PUT",
            f"/goals/{goal['id']}/conversation",
            json={"conversation_id": own, "status": "COMPLETED"},
        )
        self.assertEqual(extra.status_code, 422, extra.text)
        self.assertEqual(extra.json()["code"], "invalid_request", extra.text)
        self.assertIn("status", extra.json()["message"], extra.text)

        # Every refusal left the link alone.
        untouched = await self.call("GET", f"/goals/{goal['id']}")
        self.assertIsNone(untouched.json()["conversation_id"])

    async def test_attaching_moves_a_run_between_threads_and_touches_the_target(
        self,
    ) -> None:
        # Re-attaching is the same act as the first attach: history is filed
        # where the user just put it, not permanently where it first landed.
        first = (
            await self.call(
                "POST", "/conversations", json={"workspace_id": self.workspace.id}
            )
        ).json()["id"]
        second = (
            await self.call(
                "POST", "/conversations", json={"workspace_id": self.workspace.id}
            )
        ).json()["id"]
        goal = (
            await self.call(
                "POST",
                "/goals",
                json={
                    "workspace_id": self.workspace.id,
                    "conversation_id": second,
                    "title": "movable run",
                },
            )
        ).json()

        # Moved *backwards* on purpose: `first` is the older thread and sits
        # below `second` in the panel, so only the updated_at bump can put it
        # on top after the attach.
        moved = await self.call(
            "PUT",
            f"/goals/{goal['id']}/conversation",
            json={"conversation_id": first},
        )
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(moved.json()["conversation_id"], first)

        old_turns = await self.call("GET", f"/conversations/{second}/turns")
        self.assertEqual(old_turns.json(), [], "the old thread keeps no ghost")
        new_turns = await self.call("GET", f"/conversations/{first}/turns")
        self.assertEqual([t["goal_id"] for t in new_turns.json()], [goal["id"]])

        # Filing a run into a thread is a touch: the panel orders by last
        # touch, so the target moves to the top exactly as it does on create.
        listed = await self.call(
            "GET", "/conversations", params={"workspace_id": self.workspace.id}
        )
        self.assertEqual([c["id"] for c in listed.json()][0], first)


class ServiceLevelTestCase(unittest.IsolatedAsyncioTestCase):
    """The service, directly — for the reads an HTTP client cannot express."""

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.conn = connect(self.root / "test.db")
        self.workspaces = WorkspaceService(self.conn)
        self.conversations = ConversationService(self.conn)
        self.goals = GoalService(self.conn)
        repo = self.root / "repo"
        repo.mkdir()
        self.ws = self.workspaces.create(_workspace_create("codify", str(repo)))

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    async def test_turns_fall_back_to_the_title_for_a_goal_with_no_description(
        self,
    ) -> None:
        # A goal created before descriptions carried the full prompt still has to
        # name itself in a transcript. The description is preferred because it is
        # the user's whole sentence; the title is the 200-char truncation.
        thread = self.conversations.create(
            ConversationCreate(workspace_id=self.ws.id)
        )
        goal = self.goals.create(
            _goal_create(self.ws.id, thread.id, title="short title only")
        )
        turns = self.conversations.turns(thread.id)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].prompt, "short title only")
        self.assertEqual(turns[0].goal_id, goal.id)

    async def test_an_empty_title_is_kept_empty_rather_than_trimmed_to_nothing(
        self,
    ) -> None:
        thread = self.conversations.create(
            ConversationCreate(workspace_id=self.ws.id, title="   ")
        )
        self.assertEqual(thread.title, "")


# `nobody`, by number: it need not exist by name for the kernel to honour the id.
UNPRIVILEGED_UID = 65534


@contextmanager
def _without_root_privileges() -> Iterator[None]:
    """Run as a user that file modes actually bind.

    Root ignores `chmod 0444`, so a test that makes a database read-only to prove a
    migration cannot run passed for everyone except whoever ran the suite as root —
    which is what every container does, and what made `make test` fail there for a
    reason that had nothing to do with the code. Not a skip: the same real read-only
    failure is produced, by dropping to an unprivileged euid around the one call that
    must be refused. A no-op when the suite is already unprivileged.
    """
    if os.geteuid() != 0:
        yield
        return
    os.seteuid(UNPRIVILEGED_UID)
    try:
        yield
    finally:
        os.seteuid(0)


class MigrationTestCase(unittest.TestCase):
    """A database from before conversations must still open and keep its goals.

    The fixture below freezes the *past* shape on purpose. It is the schema as it
    was, hand-written, because a migration test that only ran against today's
    schema would pass no matter what the migration did.
    """

    # goals as it stood before `conversation_id` existed.
    OLD_SCHEMA = """
    PRAGMA foreign_keys = ON;
    CREATE TABLE workspaces (
      id TEXT PRIMARY KEY, name TEXT NOT NULL, root_path TEXT NOT NULL UNIQUE,
      design_contract_path TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
    );
    CREATE TABLE goals (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL REFERENCES workspaces(id),
      title TEXT NOT NULL,
      description TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL,
      dry_run INTEGER NOT NULL DEFAULT 0,
      plan_only INTEGER NOT NULL DEFAULT 0,
      parallel INTEGER NOT NULL DEFAULT 0,
      mode TEXT NOT NULL DEFAULT 'normal',
      version INTEGER NOT NULL DEFAULT 0,
      event_seq INTEGER NOT NULL DEFAULT 0,
      created_at REAL NOT NULL,
      updated_at REAL NOT NULL,
      provider TEXT,
      model TEXT
    );
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.db_path = self.root / "old.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_an_install_from_before_conversations_keeps_every_goal(self) -> None:
        old = sqlite3.connect(self.db_path)
        old.executescript(self.OLD_SCHEMA)
        old.execute(
            "INSERT INTO workspaces (id, name, root_path, created_at) VALUES (?, ?, ?, ?)",
            ("w1", "codify", str(self.root / "repo"), 1.0),
        )
        old.execute(
            """INSERT INTO goals
               (id, workspace_id, title, description, status, version, event_seq,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?)""",
            ("g1", "w1", "old run", "old run", "COMPLETED", 1.0, 1.0),
        )
        old.commit()
        old.close()

        # Opening it today brings the column in without disturbing the rows.
        conn = connect(self.db_path)
        try:
            goal = conn.execute("SELECT * FROM goals WHERE id = 'g1'").fetchone()
            self.assertIsNotNone(goal)
            self.assertIsNone(goal["conversation_id"])

            # And it reads as its own single-turn thread rather than as nothing.
            goals = GoalService(conn)
            self.assertIsNone(goals.get("g1").conversation_id)

            # A thread can be created alongside it, and the old goal is not
            # swept into it.
            conversations = ConversationService(conn)
            thread = conversations.create(ConversationCreate(workspace_id="w1"))
            self.assertEqual(conversations.turns(thread.id), [])
        finally:
            conn.close()

    def test_reopening_a_migrated_database_is_not_an_error(self) -> None:
        """The "duplicate column" case is the one the try/except exists for.

        Every install opens its database on every launch, so the migration has
        to be a no-op the second time. If the narrowed `except` were wrong about
        which errors are expected, this is where it would show: the engine would
        refuse to start against a database it had itself created a moment ago.
        """
        path = Path(tempfile.mkdtemp()) / "twice.db"
        first = connect(path)
        try:
            cols = [r[1] for r in first.execute("PRAGMA table_info(conversations)")]
            self.assertIn("parent_id", cols)
        finally:
            first.close()
        second = connect(path)
        try:
            cols = [r[1] for r in second.execute("PRAGMA table_info(conversations)")]
            self.assertIn("parent_id", cols, "the second open dropped the column")
        finally:
            second.close()

    def test_a_migration_that_cannot_run_says_so_instead_of_hiding(self) -> None:
        """A failed migration must not become a 500 on the first read.

        `_row_to_conversation` reads `parent_id` on every conversation read, so
        a migration that quietly failed and did not add the column would surface
        as an `IndexError` inside a request — a 500 naming nothing, with the real
        cause (a database nobody can write to) invisible. Making the file
        read-only is the closest a hermetic test gets to that.
        """
        path = Path(tempfile.mkdtemp()) / "readonly.db"
        seed = connect(path)
        seed.close()
        # Drop the column so the migration has work to do, which it cannot. The
        # index over it has to go first — SQLite refuses to drop a column an
        # index still names, which is itself a small argument for keeping the
        # migration and its index adjacent.
        rw = sqlite3.connect(path)
        rw.execute("DROP INDEX IF EXISTS idx_conversations_parent")
        rw.execute("ALTER TABLE conversations DROP COLUMN parent_id")
        rw.commit()
        rw.close()
        os.chmod(path, 0o444)
        # A private temp directory is 0700, which an unprivileged user cannot even
        # enter; the file stays unwritable, which is the whole point.
        os.chmod(path.parent, 0o755)
        try:
            with _without_root_privileges(), self.assertRaises(sqlite3.OperationalError) as caught:
                connect(path)
            self.assertNotIn(
                "duplicate column",
                str(caught.exception).lower(),
                "the failure was mistaken for the expected one",
            )
        finally:
            os.chmod(path, 0o644)


def _workspace_create(name: str, root: str) -> WorkspaceCreate:
    return WorkspaceCreate(name=name, root_path=root)


def _goal_create(
    workspace_id: str, conversation_id: str | None, **kwargs: str
) -> GoalCreate:
    return GoalCreate(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        title=kwargs.get("title", "t"),
        description=kwargs.get("description", ""),
    )


if __name__ == "__main__":
    unittest.main()
