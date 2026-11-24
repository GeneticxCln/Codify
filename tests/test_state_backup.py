"""Regression and integration tests for `engine/backup.py` and `/state/backup` + `/state/restore`."""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — isolated CODIFY_HOME for tests

import base64
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from engine import backup
from engine.app import BOOT_TOKEN, app, lifespan
from engine.db import SCHEMA_VERSION, connect
from engine.models import ConversationCreate, WorkspaceCreate
from engine.providers import Keychain
from engine.services import ConversationService, SettingsService, WorkspaceService


class StateBackupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.db_path = self.state_dir / "codify.db"
        self.secrets_path = self.state_dir / "secrets.json"
        self.ws_dir = self.root / "repo"
        self.ws_dir.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {BOOT_TOKEN}"}

    def test_online_backup_is_consistent_while_running_and_excludes_keys(self) -> None:
        conn = connect(self.db_path)
        try:
            ws = WorkspaceService(conn).create(WorkspaceCreate(name="Alpha", root_path=str(self.ws_dir)))
            ConversationService(conn).create(ConversationCreate(workspace_id=ws.id, title="Before backup"))
            SettingsService(conn).set_int("parallel_width", 7)
            keychain = Keychain(secrets_path=self.secrets_path)
            keychain.set_secret("provider/openai", "sk-live-secret-not-in-db")

            # Take online backup while `conn` remains open in WAL mode.
            created = backup.create_backup(self.db_path)
            snap_path = Path(created["path"])
            self.assertTrue(snap_path.is_file())
            self.assertEqual(snap_path.stat().st_mode & 0o077, 0)
            self.assertFalse(Path(f"{snap_path}-wal").exists())
            self.assertNotIn(b"sk-live-secret-not-in-db", snap_path.read_bytes())

            # Mutate the live database AFTER the backup (leaving WAL frames on disk).
            ConversationService(conn).create(ConversationCreate(workspace_id=ws.id, title="After backup"))
            SettingsService(conn).set_int("parallel_width", 2)

            # Stage the backup for restore; live connection still sees post-backup state until restart.
            staged = backup.stage_restore_from_backup(
                self.db_path, created["filename"], max_schema_version=SCHEMA_VERSION
            )
            self.assertEqual(staged["source"], created["filename"])
            self.assertEqual(SettingsService(conn).get_int("parallel_width"), 2)
        finally:
            conn.close()

        # Next start (`connect`) swaps `<db_path>.restore` in and clears stale WAL frames!
        reopened = connect(self.db_path)
        try:
            self.assertIsNone(backup.pending_restore_info(self.db_path))
            self.assertEqual(SettingsService(reopened).get_int("parallel_width"), 7)
            threads = ConversationService(reopened).list_conversations(ws.id)
            self.assertEqual([t.title for t in threads], ["Before backup"])
            # Keychain secret is untouched by the restore.
            self.assertEqual(Keychain(secrets_path=self.secrets_path).get("provider/openai"), "sk-live-secret-not-in-db")
        finally:
            reopened.close()

    def test_backup_and_restore_write_only_the_state_directory(self) -> None:
        sentinel = self.ws_dir / "person-file.txt"
        sentinel.write_text("untouched", encoding="utf-8")
        conn = connect(self.db_path)
        try:
            WorkspaceService(conn).create(WorkspaceCreate(name="Gamma", root_path=str(self.ws_dir)))
            created = backup.create_backup(self.db_path)
            backup.stage_restore_from_backup(
                self.db_path, created["filename"], max_schema_version=SCHEMA_VERSION
            )
        finally:
            conn.close()

        reopened = connect(self.db_path)
        try:
            self.assertEqual("untouched", sentinel.read_text(encoding="utf-8"))
            self.assertEqual([sentinel], list(self.ws_dir.iterdir()))
        finally:
            reopened.close()

    def test_http_routes_backup_download_stage_upload_cancel_and_swap_on_lifespan_restart(self) -> None:
        env = {
            "CODIFY_HOME": str(self.state_dir),
            "CODIFY_DB": str(self.db_path),
            "CODIFY_SECRETS": str(self.secrets_path),
        }
        with patch.dict(os.environ, env):
            with TestClient(app) as client:
                ws_res = client.post(
                    "/workspaces",
                    json={"name": "Beta", "root_path": str(self.ws_dir)},
                    headers=self._headers(),
                )
                self.assertEqual(ws_res.status_code, 200, ws_res.text)
                ws_id = ws_res.json()["id"]

                created = client.post("/state/backup", headers=self._headers())
                self.assertEqual(created.status_code, 200, created.text)
                fname = created.json()["filename"]

                dl = client.get(f"/state/backup/{fname}", headers=self._headers())
                self.assertEqual(dl.status_code, 200)
                self.assertTrue(dl.content.startswith(backup.SQLITE_MAGIC))

                # Mutate state after backup by adding a conversation.
                c_res = client.post(
                    "/conversations",
                    json={"workspace_id": ws_id, "title": "Post-backup thread"},
                    headers=self._headers(),
                )
                self.assertEqual(c_res.status_code, 200, c_res.text)

                # Stage via uploaded base64 bytes, cancel, then stage again from filename.
                staged_up = client.post(
                    "/state/restore",
                    json={
                        "data_base64": base64.b64encode(dl.content).decode("ascii"),
                        "source_name": "external-copy.db",
                    },
                    headers=self._headers(),
                )
                self.assertEqual(staged_up.status_code, 200, staged_up.text)
                self.assertEqual(staged_up.json()["source"], "external-copy.db")

                cancelled = client.delete("/state/restore", headers=self._headers())
                self.assertEqual(cancelled.status_code, 200, cancelled.text)
                self.assertTrue(cancelled.json()["cancelled"])

                status_after_cancel = client.get("/state/backup", headers=self._headers()).json()
                self.assertIsNone(status_after_cancel["pending_restore"])

                staged_saved = client.post(
                    "/state/restore",
                    json={"filename": fname},
                    headers=self._headers(),
                )
                self.assertEqual(staged_saved.status_code, 200, staged_saved.text)

            # Restarting the app runs `lifespan` -> `connect()` -> swaps backup in!
            with TestClient(app) as restarted:
                threads = restarted.get(
                    f"/conversations?workspace_id={ws_id}", headers=self._headers()
                )
                self.assertEqual(threads.status_code, 200, threads.text)
                self.assertEqual(threads.json(), [], "post-backup thread must be gone after restore")
                st = restarted.get("/state/backup", headers=self._headers()).json()
                self.assertIsNone(st["pending_restore"])

    def test_invalid_traversal_missing_and_newer_schema_backups_are_refused(self) -> None:
        env = {
            "CODIFY_HOME": str(self.state_dir),
            "CODIFY_DB": str(self.db_path),
            "CODIFY_SECRETS": str(self.secrets_path),
        }
        with patch.dict(os.environ, env), TestClient(app) as client:
            # Traversal / bad filename
            bad_name = client.get("/state/backup/..%2Fcodify.db", headers=self._headers())
            self.assertIn(bad_name.status_code, (404, 422))

            missing = client.post(
                "/state/restore",
                json={"filename": "codify-backup-20990101-000000.db"},
                headers=self._headers(),
            )
            self.assertEqual(missing.status_code, 404, missing.text)
            self.assertEqual(missing.json()["code"], "unknown_backup")

            # Non-SQLite bytes
            not_sqlite = client.post(
                "/state/restore",
                json={"data_base64": base64.b64encode(b"not a sqlite file at all").decode("ascii")},
                headers=self._headers(),
            )
            self.assertEqual(not_sqlite.status_code, 422, not_sqlite.text)
            self.assertEqual(not_sqlite.json()["code"], "invalid_backup")

            # Unrelated SQLite database missing Codify tables
            unrelated = self.root / "unrelated.db"
            uconn = sqlite3.connect(unrelated)
            uconn.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
            uconn.commit()
            uconn.close()
            missing_tables = client.post(
                "/state/restore",
                json={"data_base64": base64.b64encode(unrelated.read_bytes()).decode("ascii")},
                headers=self._headers(),
            )
            self.assertEqual(missing_tables.status_code, 422, missing_tables.text)
            self.assertEqual(missing_tables.json()["code"], "invalid_backup")

            # Newer schema version database
            snap = client.post("/state/backup", headers=self._headers()).json()
            future_db = Path(snap["path"])
            fconn = sqlite3.connect(future_db)
            fconn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 10}")
            fconn.commit()
            fconn.close()
            newer = client.post(
                "/state/restore",
                json={"filename": snap["filename"]},
                headers=self._headers(),
            )
            self.assertEqual(newer.status_code, 409, newer.text)
            self.assertEqual(newer.json()["code"], "newer_schema")

    def test_corrupt_staged_file_at_boot_is_discarded_without_breaking_boot(self) -> None:
        import asyncio

        conn = connect(self.db_path)
        ws = WorkspaceService(conn).create(WorkspaceCreate(name="Safe", root_path=str(self.ws_dir)))
        conn.close()

        # Write garbage directly to `<db_path>.restore` simulating disk corruption.
        backup.staged_restore_path(self.db_path).write_bytes(b"corrupted bytes")
        env = {
            "CODIFY_HOME": str(self.state_dir),
            "CODIFY_DB": str(self.db_path),
            "CODIFY_SECRETS": str(self.secrets_path),
        }

        async def _boot() -> list[str]:
            async with lifespan(app):
                workspaces: WorkspaceService = app.state.workspaces
                return [w.id for w in workspaces.list_workspaces()]

        with patch.dict(os.environ, env):
            ids = asyncio.run(_boot())
        self.assertEqual(ids, [ws.id])
        self.assertFalse(backup.staged_restore_path(self.db_path).exists())


if __name__ == "__main__":
    unittest.main()
