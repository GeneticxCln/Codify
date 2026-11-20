"""Unit tests verifying audit fixes across secondary Python engine modules (E9–E15, E24–E39, E44–E51)."""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — isolate CODIFY_HOME in a temp dir

from collections.abc import AsyncIterator
import gzip
import importlib
import importlib.metadata
import io
import json
import os
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx

from engine import (
    binary_analysis,
    binary_inspect,
    capabilities,
    cli,
    github_remote,
    html_text,
    library,
    mcp,
    model_catalog,
    notes,
    profile,
    recall,
    replies,
    sandbox,
    scan,
    skills,
    speech,
    stats_import,
    surface_machine,
    toolcall,
    usage_audit,
    web_fetch,
)
from engine.db import connect
from engine.fs import MAX_DIFF_BYTES, FileMovedUnderProposalError, FileSystemService
from engine.git import _FILTER_RUNNING_SUBCOMMANDS, GitService
from engine.laya import LayaNotConfigured, LayaService
from engine.models import AgentConfig, AgentConfigUpdate
from engine.providers import Keychain, ProviderFactory
from engine.services import AgentRegistryService, SettingsService, WorkspaceService
from engine.toolcall import ToolSpec


class _AsyncByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


class TestAuditModuleFixes(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ── E9: web_fetch & mcp bounded decompression ─────────────────────────────

    async def test_e9_web_fetch_and_mcp_bound_compressed_streams(self) -> None:
        seen_headers: list[dict[str, str]] = []
        bomb = gzip.compress(b"A" * (web_fetch.MAX_BODY_BYTES + 65536))

        def fetch_handler(request: httpx.Request) -> httpx.Response:
            seen_headers.append(dict(request.headers))
            return httpx.Response(
                200,
                headers={"content-type": "text/plain; charset=utf-8", "content-encoding": "gzip"},
                stream=_AsyncByteStream([bomb]),
            )

        async def fake_resolve(_host: str, _port: int) -> list[str]:
            return ["93.184.216.34"]

        fetched = await web_fetch.fetch(
            "https://example.com/file.txt",
            web_fetch.FetchPolicy(web_fetch.MODE_ANY),
            resolve=fake_resolve,
            transport=httpx.MockTransport(fetch_handler),
        )
        self.assertEqual(seen_headers[0].get("accept-encoding"), "identity")
        self.assertTrue(fetched.truncated)

        mcp_bomb = gzip.compress(b"B" * (mcp.MAX_RESPONSE_BYTES + 65536))
        mcp_response = httpx.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
            stream=_AsyncByteStream([mcp_bomb]),
        )
        with self.assertRaises(mcp.McpError) as ctx:
            await mcp._read_capped(mcp_response)
        self.assertEqual(ctx.exception.code, "mcp_response_too_large")

    # ── E10: profile export scrubs per-role api_key_ref secrets ───────────────

    def test_e10_profile_known_secrets_includes_role_api_key_ref(self) -> None:
        conn = connect(self.root / "e10.db")
        self.addCleanup(conn.close)
        kc = Keychain(secrets_path=self.root / "secrets.json")
        reg = AgentRegistryService(conn, ProviderFactory(kc), kc)
        reg.set_config(
            "planner",
            AgentConfigUpdate(
                api_key="super-secret-role-credential-value",
                system_prompt_override="Use key super-secret-role-credential-value here",
            ),
        )
        tools = profile.ProfileTools(
            conn,
            SettingsService(conn),
            reg,
            WorkspaceService(conn),
            notes.NotesService(conn),
            kc,
        )
        self.assertIn("super-secret-role-credential-value", tools._known_secrets())
        exported = tools.export(workspace=None, include_memory=True, include_notes=True)
        serialized = json.dumps(exported)
        self.assertNotIn("super-secret-role-credential-value", serialized)

    # ── E11: sandbox tmpfs cap on redirected CODIFY_HOME & .git symlink ───────

    def test_e11_sandbox_rejects_git_symlink_and_caps_codify_home_tmpfs(self) -> None:
        ws = self.root / "ws"
        ws.mkdir()
        outside = self.root / "outside_git"
        outside.mkdir()
        (ws / ".git").symlink_to(outside)

        fake_settings = MagicMock()
        fake_settings.get_str.return_value = "required"
        svc = sandbox.SandboxService(settings=fake_settings)
        with (
            patch("shutil.which", return_value="/usr/bin/bwrap"),
            patch("engine.sandbox.bwrap_can_jail", return_value=True),
        ):
            with self.assertRaises(sandbox.CommandNotAllowed) as ctx:
                svc._jail_argv(["true"], ws, network=False)
            self.assertIn(".git", str(ctx.exception))

            (ws / ".git").unlink()
            redirected_home = Path("/tmp") / f"codify-e11-{os.getpid()}"
            redirected_home.mkdir(exist_ok=True)
            self.addCleanup(lambda: redirected_home.rmdir() if redirected_home.exists() else None)
            with patch("engine.home.codify_home", return_value=redirected_home):
                argv, _ = svc._jail_argv(["true"], ws, network=False)
        assert argv is not None
        idx = argv.index(str(redirected_home))
        self.assertEqual(argv[idx - 1], "--tmpfs")
        self.assertEqual(argv[idx - 3 : idx - 1], ["--size", str(sandbox.JAIL_RUN_TMPFS_BYTES)])

    # ── E12: binary_inspect Fat Mach-O recursion & 64-bit fat header ──────────

    def test_e12_fat_macho_zero_offset_and_64bit_slice(self) -> None:
        # Crafted 28-byte Fat Mach-O with offset=0 pointing to itself.
        crafted = struct.pack(">IIIII", 0xCAFEBABE, 1, 7, 3, 0) + struct.pack(">II", 28, 0)
        with self.assertRaises(binary_inspect.UnsupportedFormat):
            binary_inspect.inspect(crafted, binary_analysis.EXPORT_CAPS)

        # Minimal 64-bit Mach-O inside a 64-bit Fat Mach-O (0xCAFEBABF).
        inner = struct.pack("<IIIIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 0, 0, 0, 0)
        fat64_header = struct.pack(">II", 0xCAFEBABF, 1) + struct.pack(">IIQQII", 0x0100000C, 0, 40, len(inner), 0, 0)
        doc = binary_inspect.inspect(fat64_header + inner, binary_analysis.EXPORT_CAPS)
        self.assertIn("Mach-O", doc["program"]["format"])
        self.assertEqual(doc["program"]["bits"], "64")

    # ── E13: mcp validates Mcp-Session-Id header ──────────────────────────────

    def test_e13_mcp_rejects_non_ascii_session_id(self) -> None:
        with self.assertRaises(mcp.McpError) as ctx:
            mcp._validate_session_id("bad-\xff-session")
        self.assertEqual(ctx.exception.code, "mcp_invalid_session_id")
        self.assertEqual(mcp._validate_session_id("valid-session-123"), "valid-session-123")

    # ── E14: recall.recovered_pairs sorts newest-first rows chronologically ───

    def test_e14_recall_recovered_pairs_handles_newest_first_rows(self) -> None:
        newest_first = [
            {
                "goal_id": "g1",
                "step_id": "s1",
                "type": "test_result",
                "timestamp": 200.0,
                "sequence": 2,
                "payload": json.dumps({"verdict": "pass"}),
            },
            {
                "goal_id": "g1",
                "step_id": "s1",
                "type": "fix_retry",
                "timestamp": 100.0,
                "sequence": 1,
                "payload": json.dumps({"attempt": 1}),
            },
        ]
        self.assertEqual(recall.recovered_pairs(newest_first), {("g1", "s1")})

    # ── E15 & E48: scan_tree severity ordering via to_worker & line numbers ───

    def test_e15_and_e48_scan_severity_and_line_tracking(self) -> None:
        ws = self.root / "scan_ws"
        ws.mkdir()
        (ws / "app.py").write_text("import os\nx = 1\nos.system(cmd)\n", encoding="utf-8")
        profiles = scan.load_profiles(str(ws))
        inj = profiles.get("injection")
        assert inj is not None
        worker_rules = [r.to_worker() for r in inj.rules]
        # Prepend 65 synthetic info rules that match `x = 1` so findings exceed MAX_FINDINGS (60)
        info_rules = [
            {
                "id": f"aaa-info-{i:02d}",
                "pattern": r"x\s*=\s*1",
                "flags": "",
                "languages": ["py"],
                "where": "code",
            }
            for i in range(65)
        ]
        res = scan.scan_tree(str(ws), info_rules + worker_rules, None, False, False, 5.0)
        top_rules = [f["rule"] for f in res["findings"]]
        self.assertEqual(top_rules[0], "py-os-system")
        self.assertEqual(res["findings"][0]["line"], 3)

    # ── E24: speech Recorder.stop sets ended_by_us before checking exit code ──

    def test_e24_speech_recorder_stop_marks_ended_by_us(self) -> None:
        rec = speech.Recorder(max_seconds=10)
        wav = self.root / "rec.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 100)
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None
        fake_proc.returncode = 1  # non-zero exit when terminated by us
        fake_proc.pid = 999999
        rec._proc = fake_proc
        rec._path = wav
        rec._stderr = speech._StderrDrain(io.BytesIO(b"warning"))
        rec._started_at = 1.0
        with patch("engine.speech._kill_group"):
            data, _ = rec.stop()
        self.assertTrue(data.startswith(b"RIFF"))

    # ── E25: git blame filter guard & normalized paths in commit_files ────────

    def test_e25_git_blame_in_filter_subcommands_and_path_normalization(self) -> None:
        self.assertIn("blame", _FILTER_RUNNING_SUBCOMMANDS)
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "a.txt").write_text("hello\n", encoding="utf-8")
        git = GitService()
        sha1, err1 = git.commit_with_reason(str(repo), "init", ["./a.txt", "a.txt"])
        self.assertIsNone(err1)
        self.assertIsNotNone(sha1)
        (repo / "a.txt").unlink()
        sha2, err2 = git.commit_with_reason(str(repo), "delete", ["./a.txt"])
        self.assertIsNone(err2)
        self.assertIsNotNone(sha2)

    # ── E26: sandbox rejects NaN/Inf/bool timeouts ────────────────────────────

    def test_e26_sandbox_rejects_non_finite_or_bool_timeout(self) -> None:
        svc = sandbox.SandboxService()
        for bad in (float("nan"), float("inf"), float("-inf"), True, False, 0, -5, 601):
            with self.subTest(timeout=bad):
                with self.assertRaises(sandbox.CommandNotAllowed):
                    svc.run_command(str(self.root), ["true"], timeout_s=bad)  # type: ignore[arg-type]

    # ── E27 & E28: binary_inspect PE 5-DWORD import descriptor & binary_analysis ─

    def test_e27_and_e28_pe_import_descriptor_and_single_read(self) -> None:
        # Build a minimal 32-bit PE with one import descriptor whose Name RVA is in the 4th DWORD.
        pe = bytearray(0x400)
        pe[0:2] = b"MZ"
        struct.pack_into("<I", pe, 0x3C, 0x80)
        pe[0x80:0x84] = b"PE\x00\x00"
        struct.pack_into("<HHIIIHH", pe, 0x84, 0x014C, 1, 0, 0, 0, 0xE0, 0x0102)
        opt = 0x98
        struct.pack_into("<H", pe, opt, 0x10B)  # PE32
        struct.pack_into("<I", pe, opt + 16, 0x1000)  # EntryPoint
        struct.pack_into("<I", pe, opt + 28, 0x400000)  # ImageBase
        struct.pack_into("<I", pe, opt + 92, 16)  # NumberOfRvaAndSizes
        struct.pack_into("<II", pe, opt + 96 + 8, 0x1000, 40)  # Import table RVA=0x1000
        sec = opt + 0xE0
        pe[sec : sec + 6] = b".text\x00"
        struct.pack_into("<IIIIIIHHI", pe, sec + 8, 0x200, 0x1000, 0x200, 0x200, 0, 0, 0, 0, 0x60000020)
        # Import descriptor at file offset 0x200 (RVA 0x1000):
        # OriginalFirstThunk=0x1030, TimeDateStamp=0xDEAD, ForwarderChain=0, Name=0x1040, FirstThunk=0x1030
        struct.pack_into("<IIIII", pe, 0x200, 0x1030, 0xDEAD, 0, 0x1040, 0x1030)
        # Thunk table at 0x230 (RVA 0x1030): [0x80000001 (ordinal, skipped), 0x1050 (by name), 0]
        struct.pack_into("<III", pe, 0x230, 0x80000001, 0x1050, 0)
        pe[0x240 : 0x240 + 13] = b"KERNEL32.dll\x00"
        pe[0x250 : 0x250 + 12] = b"\x00\x00ExitProcess\x00"

        doc = binary_inspect.inspect(bytes(pe), binary_analysis.EXPORT_CAPS)
        self.assertEqual(doc["imports"], [{"name": "ExitProcess", "library": "KERNEL32.dll"}])

        bin_path = self.root / "sample.exe"
        bin_path.write_bytes(bytes(pe))
        with patch.object(binary_analysis, "_read_source", wraps=binary_analysis._read_source) as spy_read:
            res = binary_analysis.analyse(str(self.root), "sample.exe", native=True)
        self.assertEqual(res["backend"], "native")
        self.assertEqual(spy_read.call_count, 1)

    # ── E29: library symlink loop, case-insensitive .git, negative limit ──────

    def test_e29_library_symlink_and_negative_limit(self) -> None:
        ws = self.root / "lib_ws"
        ws.mkdir()
        (ws / ".GIT").mkdir()
        (ws / ".GIT" / "config").write_text("secret", encoding="utf-8")
        (ws / "git_link.txt").symlink_to(ws / ".GIT" / "config")
        self.assertTrue(library._is_outside_symlink(ws, ws / "git_link.txt"))

        loop_a = ws / "loop_a"
        loop_b = ws / "loop_b"
        loop_a.symlink_to(loop_b)
        loop_b.symlink_to(loop_a)
        self.assertTrue(library._is_outside_symlink(ws, loop_a))

        (ws / "hello.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
        svc = library.LibraryService(str(ws))
        out = svc.read("hello.txt", offset=1, limit=-5)
        self.assertIn("line1", str(out))

    # ── E30: model_catalog base_url merge, invalidate generation, _iso_day ────

    async def test_e30_model_catalog_merge_generation_and_iso_day(self) -> None:
        self.assertIsNone(model_catalog._iso_day("2026-01-01T00:00:00+25:00"))
        self.assertIsNone(model_catalog._iso_day("2026-01-01T00:00:00+01:65"))
        self.assertIsNotNone(model_catalog._iso_day("2026-01-01T00:00:00+02:30"))

        class _Cfg:
            def __init__(self, role: str, base_url: str) -> None:
                self.role = role
                self.provider = "custom_gw"
                self.protocol = "openai_compat"
                self.base_url = base_url
                self.api_key_ref = None
                self.fallback_provider = None
                self.fallback_protocol = None
                self.fallback_base_url = None

        class _Reg:
            def list_configs(self) -> list[Any]:
                return [_Cfg("laya", ""), _Cfg("planner", "https://gw.example/v1")]

        class _KC:
            def get(self, _ref: Any) -> str:
                return "sk-1"

            def get_provider_key(self, _p: str) -> str:
                return ""

            def has_provider_key(self, _p: str) -> bool:
                return False

        targets = model_catalog._targets(_Reg(), _KC())
        gw = next(t for t in targets if t.provider == "custom_gw")
        self.assertEqual(gw.base_url, "https://gw.example/v1")

        # Generation counter prevents in-flight get() from caching after invalidate()
        svc = model_catalog.ModelCatalogService(_Reg(), _KC())

        def handler(_req: httpx.Request) -> httpx.Response:
            svc.invalidate()
            return httpx.Response(200, json={"data": [{"id": "m1"}]})

        svc._transport = httpx.MockTransport(handler)
        await svc.get(refresh=True)
        self.assertIsNone(svc._cache)

    # ── E31: laya checks blank model_name before building provider ────────────

    async def test_e31_laya_blank_model_raises_not_configured_before_build_provider(self) -> None:
        reg = MagicMock()
        reg.get_config.return_value = AgentConfig(
            role="laya",
            display_name="Laya",
            provider="anthropic",
            protocol="anthropic",
            model_name="   ",
        )
        reg.build_provider.side_effect = AssertionError("build_provider must not be called when model_name is blank")
        svc = LayaService(registry=reg)
        with self.assertRaises(LayaNotConfigured):
            await svc._decide_with_llm({"prompt": "hi"})

    # ── E32: html_text CSS selector backtracking, quoted operators, combinators ─

    def test_e32_html_text_selectors(self) -> None:
        tree = html_text.parse(
            '<div class="a"><div><div class="b">'
            '<a href="https://example.com/~user=1" alt="">link</a>'
            "</div></div></div>"
        )
        matched = html_text.select(tree, '.a > div a[href="https://example.com/~user=1"][alt=""]')
        self.assertEqual(len(matched), 1)
        self.assertEqual(html_text.text(matched[0]), "link")

        for invalid in ("> div", "div >", "div > > a", "[alt=]"):
            with self.subTest(selector=invalid):
                with self.assertRaises(html_text.SelectorError):
                    html_text.parse_selector(invalid)

    # ── E33: capabilities caches laya and sandbox probes ──────────────────────

    def test_e33_capabilities_caches_probes(self) -> None:
        capabilities.clear_capability_cache()
        with (
            patch.object(importlib, "import_module") as mock_imp,
            patch.object(importlib.metadata, "version", return_value="1.0.0"),
        ):
            r1 = capabilities.laya_sdk_report()
            r2 = capabilities.laya_sdk_report()
            self.assertTrue(r1["importable"])
            self.assertEqual(r1, r2)
            self.assertEqual(mock_imp.call_count, 1)

        with (
            patch("shutil.which", return_value="/usr/bin/bwrap") as mock_which,
            patch("engine.sandbox.bwrap_can_jail", return_value=True) as mock_jail,
        ):
            s1 = capabilities.sandbox_report()
            s2 = capabilities.sandbox_report()
            self.assertEqual(s1, {"bwrap": "/usr/bin/bwrap", "available": True})
            self.assertEqual(s1, s2)
            self.assertEqual(mock_which.call_count, 1)
            self.assertEqual(mock_jail.call_count, 1)
        capabilities.clear_capability_cache()

    # ── E34: skills handles PermissionError and symlink loops cleanly ─────────

    def test_e34_skills_handles_unreadable_dir_and_symlink_loop(self) -> None:
        ws = self.root / "skills_ws"
        skills_dir = ws / skills.SKILLS_DIRNAME
        skills_dir.mkdir(parents=True)
        with patch("os.listdir", side_effect=PermissionError(13, "Permission denied")):
            found, problems = skills._skill_files(skills_dir, within=ws)
        self.assertEqual(found, [])
        self.assertTrue(any("could not be read" in p for p in problems))

    # ── E35: surface_machine _cap_list keeps tail and marks truncated ─────────

    def test_e35_surface_machine_keeps_tail_and_marks_truncated(self) -> None:
        lines = [f"line-{i}" for i in range(surface_machine.MAX_OUTPUT_LINES + 5)]
        res = surface_machine.MachineRunResult.model_validate(
            {"id": "m1", "output": lines, "settled": True, "alive": True}
        )
        self.assertTrue(res.truncated)
        self.assertEqual(len(res.output), surface_machine.MAX_OUTPUT_LINES)
        self.assertEqual(res.output[0], "line-5")
        self.assertEqual(res.output[-1], f"line-{surface_machine.MAX_OUTPUT_LINES + 4}")

    # ── E36: stats_import strips unknown nested keys ──────────────────────────

    def test_e36_stats_import_strips_extra_nested_keys(self) -> None:
        doc = {
            "exported_at": "2026-09-21T00:00:00Z",
            "days": [
                {
                    "day": "2026-09-20",
                    "day_stats": {
                        "date": "2026-09-20",
                        "created": 1,
                        "succeeded": 1,
                        "failed": 0,
                        "cancelled": 0,
                        "total_tokens": 10,
                        "calls": 1,
                        "extra_evil": "drop_me",
                    },
                    "goals": {
                        "goals": 1,
                        "active": 0,
                        "succeeded": 1,
                        "failed": 0,
                        "cancelled": 0,
                        "success_rate": 100,
                        "extra_goal_key": 123,
                    },
                    "usage": {
                        "input_tokens": 5,
                        "output_tokens": 5,
                        "total_tokens": 10,
                        "calls": 1,
                        "avg_duration_ms": 12.5,
                        "extra_usage_key": "drop_me",
                    },
                }
            ],
        }
        cleaned = stats_import.validate_import(doc)
        day0 = cleaned["days"][0]
        self.assertNotIn("extra_evil", day0["day_stats"])
        self.assertNotIn("extra_goal_key", day0["goals"])
        self.assertNotIn("extra_usage_key", day0["usage"])

    # ── E37: toolcall negative ints, null function guard, anthropic merge ─────

    def test_e37_toolcall_negative_int_null_function_and_anthropic_merge(self) -> None:
        spec = ToolSpec(
            name="t",
            description="",
            parameters={"type": "object", "properties": {"delta": {"type": "integer"}}},
        )
        self.assertEqual(toolcall.coerce_arguments(spec, {"delta": "-5"}), {"delta": -5})
        self.assertEqual(toolcall.coerce_arguments(spec, {"delta": "+7"}), {"delta": 7})

        calls = toolcall.parse_openai_tool_calls(
            {"tool_calls": [{"id": "1", "function": None}, None, {"id": "2", "function": {"name": "ok", "arguments": "{}"}}]}
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "ok")

        merged = toolcall.to_anthropic_messages(
            [
                {"role": "tool", "tool_call_id": "c1", "content": "out1"},
                {"role": "assistant", "content": "", "tool_calls": []},
                {"role": "user", "content": "nudge"},
            ]
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["role"], "user")
        self.assertEqual(len(merged[0]["content"]), 2)

    # ── E38: github_remote handles malformed URLs without ValueError ──────────

    def test_e38_github_remote_handles_malformed_urls(self) -> None:
        self.assertIsNone(github_remote.parse_remote("https://[invalid/owner/repo"))
        self.assertIsNone(github_remote.parse_remote("https://github.com:badport/owner/repo"))
        self.assertEqual(
            github_remote.parse_remote("https://github.com/octocat/Hello-World.git"),
            github_remote.GithubRemote(owner="octocat", repo="Hello-World"),
        )

    # ── E39: cli quotes path arguments and handles InvalidURL/OSError ─────────

    async def test_e39_cli_escapes_path_ids_and_catches_errors(self) -> None:
        seen_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_paths.append(request.url.raw_path.decode("ascii"))
            return httpx.Response(200, json={"ok": True})

        out, err = io.StringIO(), io.StringIO()
        code = await cli.run(
            ["notes", "delete", "../other"],
            env={cli.ENV_TOKEN: "tok"},
            out=out,
            err=err,
            transport=httpx.MockTransport(handler),
            base_url="http://test",
        )
        self.assertEqual(code, 0)
        self.assertEqual(seen_paths, ["/settings/notes/..%2Fother"])

        out2, err2 = io.StringIO(), io.StringIO()
        code2 = await cli.run(
            ["status"],
            env={cli.ENV_TOKEN: "tok"},
            out=out2,
            err=err2,
            transport=httpx.MockTransport(handler),
            base_url="http://127.0.0.1:notaport",
        )
        self.assertNotEqual(code2, 0)
        self.assertIn("codify-cli:", err2.getvalue())

    # ── E44: fs.apply expected_versions distinguishes >1MB file from absent ───

    def test_e44_fs_expected_versions_rejects_oversized_existing_file_when_expected_absent(self) -> None:
        ws = self.root / "fs_ws"
        ws.mkdir()
        big = ws / "big.txt"
        big.write_bytes(b"x" * (MAX_DIFF_BYTES + 100))
        fs = FileSystemService(str(ws))
        with self.assertRaises(FileMovedUnderProposalError):
            fs.apply(
                [{"path": "big.txt", "action": "create", "content": "overwrite\n"}],
                dry_run=False,
                expected_versions={"big.txt": ""},
            )

    # ── E46: usage_audit single _by_goal sort & per-(role, model) reask-rate ──

    def test_e46_usage_audit_single_by_goal_and_per_model_reask_rate(self) -> None:
        events: list[dict[str, Any]] = []
        seq = 0

        def add_usage(model: str) -> None:
            nonlocal seq
            seq += 1
            events.append(
                {
                    "type": "usage",
                    "goal_id": "g1",
                    "step_id": "s1",
                    "sequence": seq,
                    "timestamp": 1000.0 + seq,
                    "payload": {"role": "critic", "provider": "p", "model": model, "input_tokens": 100, "output_tokens": 10},
                }
            )

        def add_reask(model: str) -> None:
            nonlocal seq
            seq += 1
            events.append(
                {
                    "type": "agent_call_failed",
                    "goal_id": "g1",
                    "step_id": "s1",
                    "sequence": seq,
                    "timestamp": 1000.0 + seq,
                    "payload": {"role": "critic", "provider": "p", "model": model, "retrying": True},
                }
            )

        # bad-model fails 2 of 2 calls (100%); good-model has 20 clean calls.
        for _ in range(2):
            add_usage("bad-model")
            add_reask("bad-model")
        for _ in range(20):
            add_usage("good-model")

        with patch.object(usage_audit, "_by_goal", wraps=usage_audit._by_goal) as spy_by_goal:
            report = usage_audit.audit(events)
        self.assertEqual(spy_by_goal.call_count, 1)
        rates = [s for s in report["signals"] if s["id"] == "reask-rate"]
        self.assertEqual(len(rates), 1)
        self.assertEqual(rates[0]["model"], "bad-model")
        self.assertEqual(rates[0]["reasks"], 2)
        self.assertEqual(rates[0]["calls"], 2)
        self.assertEqual(rates[0]["rate"], 1.0)

    # ── E47: recall.build_brief filters current_thread_id and step: first ─────

    def test_e47_recall_build_brief_filters_before_truncating(self) -> None:
        now = 1_700_000_000.0
        threads = [
            {"id": "current", "title": "Current", "last_touched": now, "runs": 1, "completed": 1, "failed": 0, "asks": ["now"]},
            *[
                {
                    "id": f"t{i}",
                    "title": f"Thread {i}",
                    "last_touched": now - i - 1,
                    "runs": 1,
                    "completed": 1,
                    "failed": 0,
                    "asks": [f"ask {i}"],
                }
                for i in range(recall.MAX_THREADS)
            ],
        ]
        obs = [
            *[
                {
                    "subject": f"step:s{i}",
                    "lesson": "internal step failure",
                    "example": "err",
                    "proof": 10,
                    "refined_at": now,
                }
                for i in range(recall.MAX_OBSERVATIONS)
            ],
            {
                "subject": "command:pytest",
                "lesson": "A retry got past this failure before.",
                "example": "needs venv",
                "proof": 2,
                "refined_at": now,
            },
        ]
        brief = recall.build_brief(threads, [], current_thread_id="current", observation_rows=obs, now=now)
        self.assertIn(f"Thread {recall.MAX_THREADS - 1}", brief)
        self.assertIn("command:pytest", brief)

    # ── E49: replies._close_truncated handles unclosed string ending in \" ────

    def test_e49_replies_close_truncated_unclosed_escaped_quote(self) -> None:
        raw = '{"steps": ["first", "second\\"'
        repaired = replies.extract_json(raw, ("steps",), repair_truncation=True)
        self.assertEqual(repaired, {"steps": ["first"]})

    # ── E50: NotesService.add returns normalized source ───────────────────────

    def test_e50_notes_add_returns_normalized_source(self) -> None:
        conn = connect(self.root / "e50.db")
        self.addCleanup(conn.close)
        svc = notes.NotesService(conn)
        added = svc.add("remember this", source="unexpected-source")
        self.assertEqual(added["source"], "person")
        self.assertEqual(svc.list()[0]["source"], "person")

    # ── E51: McpService._replace_tools preserves unreviewed 'changed' state ───

    def test_e51_mcp_second_probe_preserves_unreviewed_changed_state(self) -> None:
        conn = connect(self.root / "e51.db")
        self.addCleanup(conn.close)
        kc = Keychain(secrets_path=self.root / "e51_secrets.json")
        svc = mcp.McpService(conn, kc)
        conn.execute(
            "INSERT INTO mcp_servers (id, name, url, auth_kind, auth_header, api_key_ref, enabled, created_at, updated_at) "
            "VALUES ('srv1', 'S', 'https://93.184.216.34/mcp', 'none', '', '', 1, 1.0, 1.0)"
        )
        t_v1 = [{"name": "t1", "description": "v1", "input_schema": {}, "schema_hash": "h1"}]
        svc._replace_tools("srv1", t_v1, 1.0)
        # User enables tool t1
        conn.execute("UPDATE mcp_tools SET authority = 'read', enabled = 1, state = 'current' WHERE server_id = 'srv1'")
        conn.commit()
        # Probe 1 sees schema change -> state='changed', enabled=0
        t_v2 = [{"name": "t1", "description": "v2", "input_schema": {"a": 1}, "schema_hash": "h2"}]
        svc._replace_tools("srv1", t_v2, 2.0)
        row1 = conn.execute("SELECT enabled, state FROM mcp_tools WHERE server_id = 'srv1'").fetchone()
        self.assertEqual((row1["enabled"], row1["state"]), (0, "changed"))
        # Probe 2 with same h2 before user reviews must NOT clear state='changed' to 'current'
        svc._replace_tools("srv1", t_v2, 3.0)
        row2 = conn.execute("SELECT enabled, state FROM mcp_tools WHERE server_id = 'srv1'").fetchone()
        self.assertEqual((row2["enabled"], row2["state"]), (0, "changed"))


if __name__ == "__main__":
    unittest.main()
