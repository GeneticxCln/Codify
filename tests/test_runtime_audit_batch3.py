"""Regression tests for AI agent runtime audit — Batch 3 (H4, H8, H9, M8, M10)."""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import asyncio
import ipaddress
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
from httpx import ASGITransport

from engine import mcp as mcp_module
from engine.app import BOOT_TOKEN, app
from engine.db import connect
from engine.mcp import McpService
from engine.models import GoalCreate, McpServerCreate, WorkspaceCreate
from engine.providers import Keychain
from engine.services import ApiError, GoalService, WorkspaceService
from engine.surfaces import SurfaceBridge, SurfaceRefused
from engine.webview_bridge import BridgeRefused, WebviewBridge


class TestBatch3RuntimeAudit(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.ws_dir = self.root / "ws"
        self.ws_dir.mkdir()
        self.conn = connect(self.root / "codify.db")
        self.addCleanup(self.conn.close)
        self.keychain = Keychain(secrets_path=self.root / "secrets.json")
        self.workspaces = WorkspaceService(self.conn)
        self.goals = GoalService(self.conn)

    # ── H4: Disconnected long-poll does not steal requests or refresh liveness

    async def test_h4_disconnected_surface_poll_releases_and_live_poll_gets_request(self) -> None:
        bridge = SurfaceBridge()
        bridge.note_ui()
        dead = False

        async def dead_check() -> bool:
            return dead

        dead_poll = asyncio.create_task(bridge.next_request(wait_s=5.0, is_disconnected=dead_check))
        await asyncio.sleep(0.02)
        # First poll disconnects before the question arrives.
        dead = True
        live_poll = asyncio.create_task(bridge.next_request(wait_s=5.0))
        await asyncio.sleep(0.02)

        ask_task = asyncio.create_task(
            bridge.ask("editor", "read", {"path": None}, workspace_id="ws-1")
        )
        dead_res = await asyncio.wait_for(dead_poll, timeout=2.0)
        live_res = await asyncio.wait_for(live_poll, timeout=2.0)
        self.assertIsNone(dead_res)
        self.assertIsNotNone(live_res)
        assert live_res is not None
        self.assertEqual(live_res["surface"], "editor")

        bridge.answer(live_res["id"], True, {"open": [], "active": None})
        await asyncio.wait_for(ask_task, timeout=2.0)

    async def test_h4_webview_bridge_release_and_disconnect_handover(self) -> None:
        bridge = WebviewBridge()
        bridge.note_shell()
        dead = False

        async def dead_check() -> bool:
            return dead

        dead_poll = asyncio.create_task(bridge.next_request(wait_s=5.0, is_disconnected=dead_check))
        await asyncio.sleep(0.02)
        dead = True
        live_poll = asyncio.create_task(bridge.next_request(wait_s=5.0))
        await asyncio.sleep(0.02)

        read_task = asyncio.create_task(bridge.read_page())
        dead_res = await asyncio.wait_for(dead_poll, timeout=2.0)
        live_res = await asyncio.wait_for(live_poll, timeout=2.0)
        self.assertIsNone(dead_res)
        self.assertIsNotNone(live_res)
        assert live_res is not None

        # Explicit release puts the request back on the queue for another poll.
        self.assertTrue(bridge.release(live_res["id"]))
        redelivered = await asyncio.wait_for(bridge.next_request(wait_s=1.0), timeout=1.0)
        self.assertIsNotNone(redelivered)
        assert redelivered is not None
        self.assertEqual(redelivered["id"], live_res["id"])
        bridge.answer(redelivered["id"], True, {"url": "https://example.com", "title": "t", "text": "ok"})
        await asyncio.wait_for(read_task, timeout=2.0)

    # ── H8: Namespaced MCP tool containing '/' is routable via PUT ───────────

    async def test_h8_namespaced_mcp_tool_with_slash_matches_route(self) -> None:
        now = time.time()
        self.conn.execute(
            "INSERT INTO mcp_servers (id, name, url, auth_kind, auth_header, api_key_ref, "
            "protocol_version, server_name, enabled, created_at, updated_at) "
            "VALUES ('srv-1', 'gh', 'https://mcp.example.com', 'none', '', '', '2025-03-26', 'gh', 1, ?, ?)",
            (now, now),
        )
        self.conn.execute(
            "INSERT INTO mcp_tools (id, server_id, name, description, input_schema_json, schema_hash, "
            "authority, enabled, state, last_seen_at) "
            "VALUES ('tool-1', 'srv-1', 'github/get_issue', 'fetch issue', '{\"type\":\"object\"}', 'h1', NULL, 0, 'new', ?)",
            (now,),
        )
        self.conn.commit()

        app.state.mcp = McpService(self.conn, self.keychain)
        app.state.token = BOOT_TOKEN
        headers = {"Authorization": f"Bearer {BOOT_TOKEN}"}
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.put(
                "/settings/mcp/srv-1/tools/github/get_issue",
                headers=headers,
                json={"authority": "read", "enabled": True},
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["name"], "github/get_issue")
        self.assertEqual(body["authority"], "read")
        self.assertTrue(body["enabled"])

    # ── H9: SQLite error/conflict paths rollback and probe handles concurrent delete

    def test_h9_failed_writes_do_not_leave_open_sqlite_transaction(self) -> None:
        ws = self.workspaces.create(WorkspaceCreate(name="ws", root_path=str(self.ws_dir)))
        with self.assertRaises(ApiError):
            self.workspaces.create(WorkspaceCreate(name="dup", root_path=str(self.ws_dir)))
        self.assertFalse(self.conn.in_transaction)

        goal = self.goals.create(GoalCreate(workspace_id=ws.id, title="g", description=""))
        self.conn.execute(
            "INSERT INTO plan_steps (id, goal_id, ordinal, title, description, suggested_paths, status) "
            "VALUES ('s1', ?, 0, 't', 'd', '[]', 'PENDING')",
            (goal.id,),
        )
        self.conn.commit()

        with self.assertRaises(ApiError):
            self.goals.update_step(goal.id, "s1", goal.version + 99, {"title": "x"})
        self.assertFalse(self.conn.in_transaction)

        with self.assertRaises(ApiError):
            self.goals.set_dry_run("missing-goal", True)
        self.assertFalse(self.conn.in_transaction)

        with self.assertRaises(ApiError):
            self.goals.set_trace("missing-goal", True)
        self.assertFalse(self.conn.in_transaction)

        with self.assertRaises(ApiError):
            self.goals.set_plan_only("missing-goal", True)
        self.assertFalse(self.conn.in_transaction)

        with self.assertRaises(ApiError):
            self.goals.update_status(goal.id, goal.version + 99, "CANCELLED")
        self.assertFalse(self.conn.in_transaction)

        with self.assertRaises(ApiError):
            self.goals.next_sequence("missing-goal")
        self.assertFalse(self.conn.in_transaction)

    async def test_h9_mcp_probe_concurrent_delete_returns_404_and_rolls_back(self) -> None:
        now = time.time()
        self.conn.execute(
            "INSERT INTO mcp_servers (id, name, url, auth_kind, auth_header, api_key_ref, "
            "protocol_version, server_name, enabled, created_at, updated_at) "
            "VALUES ('srv-del', 'ephemeral', 'https://mcp.example.com', 'none', '', '', '', '', 1, ?, ?)",
            (now, now),
        )
        self.conn.commit()

        async def fake_resolve(_host: str, _port: int) -> list[Any]:
            return [ipaddress.ip_address("93.184.216.34")]

        mcp = McpService(self.conn, self.keychain, resolve=fake_resolve)

        async def deleting_probe(*_args: Any, **_kwargs: Any) -> Any:
            # Simulate another request deleting the server while probe is in flight.
            mcp.delete("srv-del")
            return types.SimpleNamespace(
                protocol_version="2025-03-26",
                server_name="ephemeral",
                tools=[{"name": "ping", "description": "p", "input_schema": {}, "schema_hash": "abc"}],
            )

        with mock.patch.object(mcp_module, "probe_server", side_effect=deleting_probe):
            with self.assertRaises(ApiError) as ctx:
                await mcp.probe("srv-del")

        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.code, "unknown_mcp_server")
        self.assertFalse(self.conn.in_transaction)

    # ── M8: Bridge close() fails pending asks immediately and rejects new ones

    async def test_m8_surface_and_webview_close_fails_pending_and_new_asks(self) -> None:
        sb = SurfaceBridge()
        sb.note_ui()
        ask_task = asyncio.create_task(sb.ask("editor", "read", {"path": None}, workspace_id="ws-1"))
        await asyncio.sleep(0.01)
        sb.close()
        with self.assertRaises(SurfaceRefused):
            await asyncio.wait_for(ask_task, timeout=1.0)
        with self.assertRaises(SurfaceRefused):
            await sb.ask("editor", "read", {"path": None}, workspace_id="ws-1")

        wb = WebviewBridge()
        wb.note_shell()
        read_task = asyncio.create_task(wb.read_page())
        await asyncio.sleep(0.01)
        wb.close()
        with self.assertRaises(BridgeRefused):
            await asyncio.wait_for(read_task, timeout=1.0)
        with self.assertRaises(BridgeRefused):
            await wb.read_page()

    # ── M10: DNS resolve_target timeout and resilient _PerThreadConnection.close

    async def test_m10_mcp_create_bounds_hanging_dns_resolve(self) -> None:
        async def hanging_resolve(_host: str, _port: int) -> list[Any]:
            await asyncio.sleep(10.0)
            return []

        mcp = McpService(self.conn, self.keychain, resolve=hanging_resolve)
        with mock.patch.object(mcp_module, "PROBE_TOTAL_TIMEOUT_S", 0.05):
            with self.assertRaises(ApiError) as ctx:
                await asyncio.wait_for(
                    mcp.create(McpServerCreate(name="slow", url="https://slow.example.com", auth_kind="none")),
                    timeout=1.0,
                )
        self.assertEqual(ctx.exception.status, 400)

    def test_m10_per_thread_connection_close_survives_failing_worker_handle(self) -> None:
        conn: Any = connect(self.root / "close_test.db")
        closed_second = False

        # Open a worker-thread handle first.
        def worker() -> None:
            conn.execute("SELECT 1").fetchone()

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        class _FailingConn:
            def close(self) -> None:
                raise RuntimeError("busy")

        class _GoodConn:
            def close(self) -> None:
                nonlocal closed_second
                closed_second = True

        with conn._opened_lock:
            conn._opened[999991] = (threading.current_thread(), _FailingConn())
            conn._opened[999992] = (threading.current_thread(), _GoodConn())

        # Must not raise, and must still close the remaining handles.
        conn.close()
        self.assertTrue(closed_second)


if __name__ == "__main__":
    unittest.main()
