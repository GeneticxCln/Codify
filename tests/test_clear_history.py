"""Regression and integration tests for `POST /history/clear` and `engine/history.py`."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import hermetic  # noqa: F401 — isolate CODIFY_HOME before engine imports

from fastapi.testclient import TestClient

from engine import app as app_module
from engine import history, home
from engine.db import connect
from engine.models import AgentConfigUpdate


class ClearHistoryRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home_dir = Path(self._tmp.name) / "codify-home"
        self.ws_dir = Path(self._tmp.name) / "project"
        self.ws_dir.mkdir(parents=True)
        self.project_file = self.ws_dir / "main.py"
        self.project_file.write_text("print('keep me')\n", encoding="utf-8")

        self._env = mock.patch.dict("os.environ", {"CODIFY_HOME": str(self.home_dir)})
        self._env.start()
        self.client_cm = TestClient(app_module.app)
        self.client = self.client_cm.__enter__()
        self.headers = {"Authorization": f"Bearer {app_module.BOOT_TOKEN}"}

    def tearDown(self) -> None:
        self.client_cm.__exit__(None, None, None)
        self._env.stop()
        self._tmp.cleanup()

    def _seed_everything(self) -> dict[str, str]:
        # 1. Workspace (kept) + disabled skill (kept)
        ws_res = self.client.post(
            "/workspaces",
            headers=self.headers,
            json={"name": "Demo", "root_path": str(self.ws_dir)},
        )
        self.assertEqual(ws_res.status_code, 200, ws_res.text)
        ws_id = ws_res.json()["id"]

        conn: sqlite3.Connection = app_module.app.state.conn
        now = time.time()

        conn.execute(
            "INSERT INTO workspace_disabled_skills (workspace_id, name, updated_at) VALUES (?, ?, ?)",
            (ws_id, "prove", now),
        )
        # 2. Agent config + provider key + engine setting + standing note + MCP server + stats import (all kept)
        app_module.app.state.registry.set_config(
            "scribe",
            AgentConfigUpdate(provider="ollama", model_name="qwen3:8b", temperature=0.2),
        )
        app_module.app.state.keychain.set_secret("provider/openai", "sk-keep-this-secret")
        app_module.app.state.settings.set_int("parallel_width", 6)
        note = app_module.app.state.notes.add("Always run mypy before committing.")
        conn.execute(
            "INSERT INTO mcp_servers (id, name, url, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("mcp-1", "Docs", "https://mcp.example.com/sse", now, now),
        )
        conn.execute(
            "INSERT INTO mcp_tools (id, server_id, name, last_seen_at) VALUES (?, ?, ?, ?)",
            ("tool-1", "mcp-1", "search_docs", now),
        )
        conn.execute(
            "INSERT INTO stats_imports (day, document, source, imported_at) VALUES (?, ?, ?, ?)",
            ("2026-10-01", '{"goals": 4}', "laptop.json", now),
        )
        conn.commit()

        # 3. Active + archived conversations, turns/goals, events, plan_steps, proposed_files,
        #    trace_calls, conversation_compactions, suggestions, observations, stats_snapshots
        c1 = self.client.post(
            "/conversations",
            headers=self.headers,
            json={"workspace_id": ws_id, "title": "Active thread"},
        ).json()["id"]
        c2 = self.client.post(
            "/conversations",
            headers=self.headers,
            json={"workspace_id": ws_id, "title": "Archived thread"},
        ).json()["id"]
        self.client.post(f"/conversations/{c2}/archive", headers=self.headers)

        g1 = "goal-canary-1"
        g2 = "goal-canary-2"
        conn.execute(
            "INSERT INTO goals (id, workspace_id, conversation_id, title, description, status, version, event_seq, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'COMPLETED', 1, 2, ?, ?)",
            (g1, ws_id, c1, "Prompt canary SECRET_PROMPT_99182", "Body SECRET_BODY_88172", now, now),
        )
        conn.execute(
            "INSERT INTO goals (id, workspace_id, conversation_id, title, description, status, version, event_seq, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'FAILED', 1, 1, ?, ?)",
            (g2, ws_id, c2, "Second turn", "Archived body", now, now),
        )
        conn.execute(
            "INSERT INTO events (id, goal_id, step_id, type, payload, timestamp, sequence) VALUES (?, ?, NULL, 'log', ?, ?, 1)",
            ("ev-1", g1, json.dumps({"message": "SECRET_REPLY_77412", "turn": True}), now),
        )
        conn.execute(
            "INSERT INTO events (id, goal_id, step_id, type, payload, timestamp, sequence) VALUES (?, ?, 'step-1', 'diff', ?, ?, 2)",
            ("ev-2", g1, json.dumps({"path": "main.py", "unified_diff": "+SECRET_DIFF_55301"}), now),
        )
        conn.execute(
            "INSERT INTO plan_steps (id, goal_id, ordinal, title, description, status) VALUES (?, ?, 1, 'Step 1', 'Edit main', 'COMPLETED')",
            ("step-1", g1),
        )
        conn.execute(
            "INSERT INTO proposed_files (id, goal_id, step_id, path, action, content, created_at) VALUES (?, ?, 'step-1', 'main.py', 'modify', 'SECRET_PROPOSAL_66123', ?)",
            ("prop-1", g1, now),
        )
        conn.execute(
            "INSERT INTO trace_calls (id, goal_id, step_id, seq, role, prompt_hash, user_prompt, response, created_at) "
            "VALUES ('tr-1', ?, 'step-1', 1, 'fixer', 'abcd1234', 'SECRET_TRACE_PROMPT_33219', 'SECRET_TRACE_REPLY_33220', ?)",
            (g1, now),
        )
        conn.execute(
            "INSERT INTO conversation_compactions (id, conversation_id, summary, turns, covers_through, created_at) "
            "VALUES ('cmp-1', ?, 'SECRET_SUMMARY_11084', 3, ?, ?)",
            (c1, now, now),
        )
        conn.execute(
            "INSERT INTO suggestions (id, workspace_id, kind, key, title, detail, prompt, status, goal_id, created_at, updated_at) "
            "VALUES ('sug-1', ?, 'failing_command', 'pytest', 'Tests failing', 'detail', 'fix pytest', 'open', ?, ?, ?)",
            (ws_id, g2, now, now),
        )
        conn.execute(
            "INSERT INTO observations (id, workspace_id, subject, lesson, example, proof, evidence, created_at, refined_at) "
            "VALUES ('obs-1', ?, 'testing', 'SECRET_LESSON_44510', 'pytest -q', 2, '[\"ev-1\"]', ?, ?)",
            (ws_id, now, now),
        )
        conn.execute(
            "INSERT INTO stats_snapshots (day, document, created_at) VALUES ('2026-11-20', '{\"goals\": 2}', ?)",
            (now,),
        )
        conn.commit()

        # 4. Shared shell_tabs: one chat tab pointing at c1, one browser tab
        self.client.put(
            "/shell/tabs",
            headers=self.headers,
            json={
                "key": "k_testchat_1",
                "position": 0,
                "kind": "chat",
                "payload": json.dumps({"kind": "chat", "conversationId": c1, "workspaceId": ws_id}),
            },
        )
        self.client.put(
            "/shell/tabs",
            headers=self.headers,
            json={
                "key": "k_testbrowser_2",
                "position": 1,
                "kind": "browser",
                "payload": json.dumps({"kind": "browser", "url": "https://example.com/", "workspaceId": ws_id}),
            },
        )
        conn.commit()
        return {"ws_id": ws_id, "c1": c1, "g1": g1, "note_id": note["id"]}

    def test_requires_auth_and_exact_confirmation_phrase(self) -> None:
        seeded = self._seed_everything()

        unauth = self.client.post("/history/clear", json={"confirm": "delete all history"})
        self.assertEqual(unauth.status_code, 401)

        for wrong in ("", "delete", "clear history", "yes"):
            res = self.client.post("/history/clear", headers=self.headers, json={"confirm": wrong})
            self.assertEqual(res.status_code, 422)
            self.assertEqual(res.json()["code"], "confirmation_required")

        extra = self.client.post(
            "/history/clear",
            headers=self.headers,
            json={"confirm": "delete all history", "unexpected": True},
        )
        self.assertEqual(extra.status_code, 422)

        # Nothing was deleted by any rejected call.
        convs = self.client.get(f"/conversations?workspace_id={seeded['ws_id']}", headers=self.headers).json()
        self.assertEqual(len(convs), 1)

    def test_refuses_while_goal_is_running_or_driven(self) -> None:
        seeded = self._seed_everything()
        conn: sqlite3.Connection = app_module.app.state.conn

        # Case 1: stored status is RUNNING
        conn.execute("UPDATE goals SET status = 'RUNNING' WHERE id = ?", (seeded["g1"],))
        conn.commit()
        res = self.client.post(
            "/history/clear", headers=self.headers, json={"confirm": "delete all history"}
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "thread_in_progress")
        self.assertIn(seeded["g1"], res.json()["goal_ids"])

        # Case 2: stored status is COMPLETED, but executor still holds the driver claim
        conn.execute("UPDATE goals SET status = 'COMPLETED' WHERE id = ?", (seeded["g1"],))
        conn.commit()
        executor = app_module.app.state.executor
        executor.claim_driver(seeded["g1"])
        try:
            res2 = self.client.post(
                "/history/clear", headers=self.headers, json={"confirm": "delete all history"}
            )
            self.assertEqual(res2.status_code, 409)
            self.assertEqual(res2.json()["code"], "thread_in_progress")
        finally:
            executor.release_driver(seeded["g1"])

        # History is still untouched.
        self.assertEqual(
            int(conn.execute("SELECT count(*) FROM goals").fetchone()[0]), 2
        )

    def test_clears_all_history_scrubs_sqlite_file_and_keeps_user_settings(self) -> None:
        seeded = self._seed_everything()
        db_file = home.db_path()
        wal_file = Path(f"{db_file}-wal")

        canaries = (
            b"SECRET_PROMPT_99182",
            b"SECRET_BODY_88172",
            b"SECRET_REPLY_77412",
            b"SECRET_DIFF_55301",
            b"SECRET_PROPOSAL_66123",
            b"SECRET_TRACE_PROMPT_33219",
            b"SECRET_TRACE_REPLY_33220",
            b"SECRET_SUMMARY_11084",
            b"SECRET_LESSON_44510",
            seeded["c1"].encode("utf-8"),
        )
        before_bytes = db_file.read_bytes() + (wal_file.read_bytes() if wal_file.exists() else b"")
        for canary in canaries:
            self.assertIn(canary, before_bytes)

        res = self.client.post(
            "/history/clear",
            headers=self.headers,
            json={"confirm": "  Delete All History  "},
        )
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(
            body,
            {
                "deleted": True,
                "files_touched": False,
                "scrubbed": True,
                "removed": {
                    "events": 2,
                    "plan_steps": 1,
                    "proposals": 1,
                    "recordings": 1,
                    "summaries": 1,
                    "suggestions": 1,
                    "observations": 1,
                    "stats_days": 1,
                    "turns": 2,
                    "threads": 2,
                },
            },
        )

        # Every history table is empty.
        conn: sqlite3.Connection = app_module.app.state.conn
        for _, table in history._CLEARED:
            count = int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # noqa: S608
            self.assertEqual(count, 0, f"{table} still has rows")

        # Deleted text + conversation UUID are physically gone from codify.db and codify.db-wal.
        after_bytes = db_file.read_bytes() + (wal_file.read_bytes() if wal_file.exists() else b"")
        for canary in canaries:
            self.assertNotIn(canary, after_bytes, f"canary {canary!r} lingered on disk after VACUUM")

        # Person-owned configuration and workspace files are completely untouched.
        self.assertEqual(self.project_file.read_text(encoding="utf-8"), "print('keep me')\n")
        self.assertEqual(app_module.app.state.keychain.get("provider/openai"), "sk-keep-this-secret")
        self.assertEqual(app_module.app.state.settings.get_int("parallel_width"), 6)
        self.assertEqual(app_module.app.state.registry.get_config("scribe").model_name, "qwen3:8b")
        notes = app_module.app.state.notes.list()
        self.assertEqual([n["id"] for n in notes], [seeded["note_id"]])
        self.assertEqual(
            int(conn.execute("SELECT count(*) FROM workspaces WHERE id = ?", (seeded["ws_id"],)).fetchone()[0]), 1
        )
        self.assertEqual(
            int(conn.execute("SELECT count(*) FROM workspace_disabled_skills").fetchone()[0]), 1
        )
        self.assertEqual(int(conn.execute("SELECT count(*) FROM mcp_servers").fetchone()[0]), 1)
        self.assertEqual(int(conn.execute("SELECT count(*) FROM mcp_tools").fetchone()[0]), 1)
        self.assertEqual(int(conn.execute("SELECT count(*) FROM stats_imports").fetchone()[0]), 1)

        # Shared tabs: browser tab untouched; chat tab kept its workspaceId and dropped conversationId.
        tabs = {t["key"]: json.loads(t["payload"]) for t in self.client.get("/shell/tabs", headers=self.headers).json()}
        self.assertEqual(tabs["k_testchat_1"], {"kind": "chat", "workspaceId": seeded["ws_id"]})
        self.assertEqual(
            tabs["k_testbrowser_2"],
            {"kind": "browser", "url": "https://example.com/", "workspaceId": seeded["ws_id"]},
        )

    def test_scrub_file_reports_false_when_another_connection_holds_a_lock(self) -> None:
        db_file = self.home_dir / "locked.db"
        conn = connect(db_file)
        blocker = sqlite3.connect(db_file, timeout=0.05)
        try:
            conn.execute("PRAGMA busy_timeout = 50")
            blocker.execute("BEGIN EXCLUSIVE")
            self.assertFalse(history.scrub_file(conn))
        finally:
            blocker.rollback()
            blocker.close()
            conn.close()


if __name__ == "__main__":
    unittest.main()
