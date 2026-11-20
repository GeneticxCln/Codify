"""Targeted regression tests for Workspace, Sandbox & Analysis Desktools audit fixes (WT-1 through WT-29)."""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import asyncio
import gzip
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import AsyncIterator
from pathlib import Path
from unittest import mock

import httpx

from engine import (
    binary_analysis,
    binary_inspect,
    git_readonly,
    html_text,
    library,
    sandbox,
    scan,
    spawn_guard,
    web_fetch,
)
from engine.fs import (
    FileAlreadyExistsError,
    FileSystemService,
    GitMetadataError,
    protected_root_reason,
)
from engine.git import GitService
from tests import binary_fixtures


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Tester",
        "GIT_AUTHOR_EMAIL": "tester@example.test",
        "GIT_COMMITTER_NAME": "Tester",
        "GIT_COMMITTER_EMAIL": "tester@example.test",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True,
    )


class TestGitAuditFixes(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "note.txt").write_text("hello\n", encoding="utf-8")
        _git(self.repo, "add", "note.txt")
        _git(self.repo, "commit", "-q", "-m", "initial")
        self.git = GitService()

    def test_wt1_push_does_not_run_clean_filter(self) -> None:
        remote = self.base / "remote.git"
        _git(self.base, "init", "-q", "--bare", str(remote))
        _git(self.repo, "remote", "add", "origin", str(remote))
        marker = self.base / "push-filter-ran"
        script = self.base / "evil-clean.sh"
        script.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat\n", encoding="utf-8")
        script.chmod(0o755)
        _git(self.repo, "config", "filter.evil.clean", str(script))
        (self.repo / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
        # Dirty worktree + stale mtime would cause `git status` to invoke filter.evil.clean.
        (self.repo / "note.txt").write_text("CHANGED\n", encoding="utf-8")
        os.utime(self.repo / "note.txt", (1, 1))

        answer = self.git.push(str(self.repo))

        self.assertTrue(answer["ok"], answer.get("reason"))
        self.assertFalse(marker.exists(), "push invoked filter.evil.clean via git status")

    def test_wt2_worktree_config_filter_is_refused(self) -> None:
        marker = self.base / "wt-filter-ran"
        script = self.base / "wt-clean.sh"
        script.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat\n", encoding="utf-8")
        script.chmod(0o755)
        _git(self.repo, "config", "extensions.worktreeConfig", "true")
        (self.repo / ".git" / "config.worktree").write_text(
            f'[filter "evilwt"]\n\tclean = {script}\n', encoding="utf-8",
        )
        (self.repo / ".gitattributes").write_text("*.txt filter=evilwt\n", encoding="utf-8")
        (self.repo / "note.txt").write_text("changed\n", encoding="utf-8")

        state = self.git.status(str(self.repo))
        self.assertIsNone(state["branch"])
        self.assertIn("filter.evilwt.clean", state["reason"] or "")

        commit_hash, reason = self.git.commit_with_reason(str(self.repo), "feat: test", ["note.txt"])
        self.assertIsNone(commit_hash)
        self.assertIn("filter.evilwt.clean", reason or "")
        self.assertFalse(marker.exists())

    def test_wt3_core_worktree_and_bare_refused_and_work_tree_pinned(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("TOP_SECRET_OUTSIDE\n", encoding="utf-8")

        env = git_readonly.runner_env(os.environ, str(self.repo))
        self.assertEqual(str(self.repo), env.get("GIT_WORK_TREE"))
        args = git_readonly.runner_args(["status"], work_tree=str(self.repo))
        self.assertIn(f"--work-tree={self.repo}", args)
        init_args = git_readonly.runner_args(["init"], work_tree=str(self.repo))
        self.assertNotIn(f"--work-tree={self.repo}", init_args)

        _git(self.repo, "config", "core.worktree", str(outside))
        self.assertIsNotNone(git_readonly.untrusted_config(str(self.repo), "status"))
        status = self.git.status(str(self.repo))
        self.assertIsNone(status["branch"])
        self.assertIn("core.worktree", status["reason"] or "")
        out = self.git.read_only(str(self.repo), ["ls-files", "--others"])
        self.assertNotIn("secret.txt", out)
        with self.assertRaises(sandbox.CommandNotAllowed):
            sandbox.SandboxService().run_command(str(self.repo), ["git", "status"], mode="read_only")

        _git(self.repo, "config", "--unset", "core.worktree")
        _git(self.repo, "config", "core.bare", "true")
        self.assertIsNotNone(git_readonly.untrusted_config(str(self.repo), "status"))
        self.assertIsNotNone(self.git._untrusted_config_reason(str(self.repo), "status"))

    def test_wt11_clean_nothing_to_commit_returns_none_reason(self) -> None:
        commit_hash, reason = self.git.commit_with_reason(str(self.repo), "feat: noop", ["note.txt"])
        self.assertIsNone(commit_hash)
        self.assertIsNone(reason)

    def test_wt12_commit_rejects_dot_empty_and_git_metadata_paths(self) -> None:
        (self.repo / "unrelated.txt").write_text("user wip\n", encoding="utf-8")
        commit_hash, reason = self.git.commit_with_reason(
            str(self.repo), "feat: bad paths", [".", "", "./", ".git/config"],
        )
        self.assertIsNone(commit_hash)
        self.assertIsNone(reason)
        porcelain = _git(self.repo, "status", "--porcelain").stdout
        self.assertIn("?? unrelated.txt", porcelain)

    def test_wt13_fifo_git_config_is_refused_without_hanging(self) -> None:
        fifo_repo = self.base / "fifo_repo"
        (fifo_repo / ".git").mkdir(parents=True)
        os.mkfifo(fifo_repo / ".git" / "config")

        started = time.monotonic()
        reason = git_readonly.untrusted_config(str(fifo_repo), "status")
        elapsed = time.monotonic() - started

        self.assertIsNotNone(reason)
        self.assertIn("regular file", reason or "")
        self.assertLess(elapsed, 2.0)
        self.assertIsNotNone(self.git._untrusted_config_reason(str(fifo_repo), "status"))

    def test_wt24_git_grep_threads_flag_refused(self) -> None:
        with self.assertRaises(git_readonly.GitRefusal):
            git_readonly.validate(["grep", "--threads=999", "hello"], lambda _w: True)


class TestSandboxAndSpawnGuardAuditFixes(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()

    def test_wt4_library_service_passes_settings_to_sandbox(self) -> None:
        class RequiredSettings:
            def get_str(self, key: str) -> str:
                return "required" if key == "sandbox_isolation" else ""

            def get_int(self, _key: str) -> int:
                return 0

        lib = library.LibraryService(str(self.base), settings=RequiredSettings())
        with mock.patch("shutil.which", side_effect=lambda name: None if name == "bwrap" else "/bin/ls"):
            with self.assertRaises(sandbox.CommandNotAllowed) as ctx:
                lib.run(["ls"])
        self.assertIn("sandbox isolation is required", str(ctx.exception))

    def test_wt5_jail_binds_nested_submodule_git_and_gitfile_target_ro(self) -> None:
        ws = self.base / "ws"
        ws.mkdir()
        (ws / ".git-real").mkdir()
        (ws / ".git").write_text("gitdir: .git-real\n", encoding="utf-8")
        sub = ws / "packages" / "submod"
        sub.mkdir(parents=True)
        (sub / ".git").mkdir()

        fake_settings = mock.MagicMock()
        fake_settings.get_str.return_value = "required"
        svc = sandbox.SandboxService(settings=fake_settings)
        with (
            mock.patch("shutil.which", return_value="/usr/bin/bwrap"),
            mock.patch("engine.sandbox.bwrap_can_jail", return_value=True),
        ):
            argv, _ = svc._jail_argv(["/bin/true"], ws, network=False)
        assert argv is not None
        ro_binds = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--ro-bind"]
        self.assertIn(str(ws / ".git"), ro_binds)
        self.assertIn(str(ws / ".git-real"), ro_binds)
        self.assertIn(str(sub / ".git"), ro_binds)

    def test_wt14_tool_binds_ignores_relative_and_file_path_entries(self) -> None:
        fake_home = self.base / "home"
        fake_home.mkdir()
        rel_dir = fake_home / "cwd_inside_home"
        rel_dir.mkdir()
        file_entry = fake_home / "not_a_dir"
        file_entry.write_text("x", encoding="utf-8")
        valid_dir = fake_home / "tools"
        valid_dir.mkdir()

        old_cwd = os.getcwd()
        os.chdir(rel_dir)
        self.addCleanup(os.chdir, old_cwd)
        with mock.patch.dict(os.environ, {"PATH": f".:bin:{file_entry}:{valid_dir}"}, clear=False):
            args = sandbox.SandboxService._tool_binds(Path("/usr/bin/true"), fake_home)
        ro_binds = [args[i + 1] for i, tok in enumerate(args) if tok == "--ro-bind"]
        self.assertNotIn(str(rel_dir), ro_binds)
        self.assertNotIn(str(file_entry), ro_binds)
        self.assertIn(str(valid_dir), ro_binds)

    def test_wt15_jail_home_computed_dynamically_from_current_home(self) -> None:
        dynamic_home = self.base / "dynamic_home"
        dynamic_home.mkdir()
        ws = dynamic_home / "proj"
        ws.mkdir()

        fake_settings = mock.MagicMock()
        fake_settings.get_str.return_value = "required"
        svc = sandbox.SandboxService(settings=fake_settings)
        with (
            mock.patch.dict(os.environ, {"HOME": str(dynamic_home)}),
            mock.patch("shutil.which", return_value="/usr/bin/bwrap"),
            mock.patch("engine.sandbox.bwrap_can_jail", return_value=True),
        ):
            argv, _ = svc._jail_argv(["/bin/true"], ws, network=False)
        assert argv is not None
        self.assertIn(str(dynamic_home), argv)
        tmpfs_idx = argv.index("--tmpfs")
        # Find the tmpfs for dynamic_home specifically.
        home_tmpfs = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--tmpfs"]
        self.assertIn(str(dynamic_home), home_tmpfs)
        self.assertGreaterEqual(tmpfs_idx, 0)

    def test_wt25_npm_script_disallows_leading_hyphen(self) -> None:
        fs = FileSystemService(str(self.base))
        for bad in ("-r", "--if-present", "-"):
            with self.subTest(bad=bad):
                with self.assertRaises(sandbox.CommandNotAllowed):
                    sandbox.validate_argv(["npm", "run", bad], fs)
        sandbox.validate_argv(["npm", "run", "test:unit-1"], fs)

    def test_wt26_spawn_guard_strips_internal_env_vars_from_child(self) -> None:
        code = (
            "import json, os; "
            f"print(json.dumps({{'pid': {spawn_guard.ENV_PARENT_PID!r} in os.environ, "
            f"'lim': {spawn_guard.ENV_LIMITS!r} in os.environ}}))"
        )
        done = subprocess.run(
            spawn_guard.guarded_argv([sys.executable, "-c", code]),
            env=spawn_guard.guarded_env({}, spawn_guard.Limits(core_bytes=0)),
            capture_output=True, text=True, timeout=15, start_new_session=True, check=True,
        )
        seen = json.loads(done.stdout.strip())
        self.assertEqual({"pid": False, "lim": False}, seen)


class TestFileSystemAuditFixes(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.ws = self.base / "ws"
        self.ws.mkdir()
        self.fs = FileSystemService(str(self.ws))

    def test_wt6_creating_empty_file_reports_changed_true(self) -> None:
        dry = self.fs.apply([{"path": "pkg/__init__.py", "action": "create", "content": ""}], dry_run=True)
        self.assertTrue(dry[0]["changed"])
        real = self.fs.apply([{"path": "pkg/__init__.py", "action": "create", "content": ""}], dry_run=False)
        self.assertTrue(real[0]["changed"])
        self.assertTrue((self.ws / "pkg" / "__init__.py").is_file())
        # Second identical write of an already-existing empty file is unchanged.
        again = self.fs.apply([{"path": "pkg/__init__.py", "action": "update", "content": ""}], dry_run=False)
        self.assertFalse(again[0]["changed"])

    def test_wt16_protected_root_resolves_symlinked_credential_and_autorun_dirs(self) -> None:
        fake_home = self.base / "home"
        fake_home.mkdir()
        real_ssh = self.base / "real_ssh_storage"
        real_ssh.mkdir()
        (fake_home / ".ssh").symlink_to(real_ssh, target_is_directory=True)
        real_cfg = self.base / "real_cfg_storage"
        real_cfg.mkdir()
        (fake_home / ".config").symlink_to(real_cfg, target_is_directory=True)

        with mock.patch.dict(os.environ, {"HOME": str(fake_home)}):
            self.assertIsNotNone(protected_root_reason(real_ssh))
            self.assertIsNotNone(protected_root_reason(real_cfg))
            self.assertIsNotNone(protected_root_reason(real_cfg / "autostart"))

    def test_wt17_intermediate_dir_symlink_and_write_atomic_git_guard(self) -> None:
        real_dir = self.ws / "real_dir"
        real_dir.mkdir()
        (self.ws / "link_dir").symlink_to(real_dir, target_is_directory=True)

        with self.assertRaises(ValueError):
            self.fs.apply([{"path": "link_dir/file.txt", "action": "create", "content": "hi"}], dry_run=False)
        self.assertFalse((real_dir / "file.txt").exists())

        with self.assertRaises(FileAlreadyExistsError):
            self.fs.create_text("link_dir/skill.md", "hi")

        (self.ws / ".git").mkdir()
        with self.assertRaises(GitMetadataError):
            self.fs._write_atomic(self.ws / ".git" / "config", b"bad")


class TestHtmlWebLibraryScanAuditFixes(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()

    def test_wt7_deep_descendant_selector_is_fast_and_exact(self) -> None:
        depth = 40
        markup = "<div>" * depth + "<p>leaf</p>" + "</div>" * depth
        root = html_text.parse(markup)
        selector = " ".join(["div"] * 12)

        started = time.monotonic()
        matched = html_text.select(root, selector)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.5, f"selector matching took {elapsed:.2f}s (expected linear)")
        self.assertEqual(depth - 11, len(matched))

    def test_wt8_rule_to_worker_includes_severity_and_scan_tree_avoids_disk_reload(self) -> None:
        (self.base / "app.py").write_text("x = low_hit()\ny = high_hit()\n", encoding="utf-8")
        low_rule = scan.Rule("r-low", "Low", "low", "", "low_hit", "", ("py",), "code", "", "")
        high_rule = scan.Rule("r-high", "High", "high", "", "high_hit", "", ("py",), "code", "", "")
        self.assertEqual("high", high_rule.to_worker()["severity"])
        self.assertEqual("low", low_rule.to_worker()["severity"])

        with mock.patch.object(scan, "load_profiles", side_effect=AssertionError("must not reload profiles")):
            res = scan.scan_tree(
                str(self.base), [low_rule.to_worker(), high_rule.to_worker()],
                None, False, False, 5.0,
            )
        self.assertEqual(["r-high", "r-low"], [f["rule"] for f in res["findings"]])

    def test_wt18_read_line_window_reports_retained_line_count_after_char_truncation(self) -> None:
        big = self.base / "big.txt"
        big.write_text("\n".join(f"line-{i:03d}-" + "x" * 120 for i in range(1, 201)) + "\n", encoding="utf-8")
        lib = library.LibraryService(str(self.base))
        res = lib.read("big.txt", offset=1, limit=200)
        self.assertLess(res["lines"], 200)
        self.assertEqual(res["lines"], res["text"].count("\n") + (0 if res["text"].endswith("\n") else 1))
        rendered = library.format_read(res)
        self.assertIn(f"lines 1-{res['lines']} of 200", rendered)

    def test_wt19_and_wt28_web_fetch_malformed_urls_and_compressed_drain(self) -> None:
        html = '<html><body><a href="http://[bad">broken</a><a href="https://example.com/ok">ok</a></body></html>'
        extracted = web_fetch.extract(html, "https://example.com/", None, 1000)
        self.assertEqual([("ok", "https://example.com/ok")], extracted.links)

        policy = web_fetch.FetchPolicy(mode=web_fetch.MODE_ANY)

        async def resolver(_host: str, _port: int) -> list[str]:
            return ["93.184.216.34"]

        def bad_redirect(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "http://[bad"})

        with self.assertRaises(web_fetch.FetchRefused):
            asyncio.run(
                web_fetch.fetch(
                    "https://example.com/", policy,
                    resolve=resolver, transport=httpx.MockTransport(bad_redirect), verify=False,
                )
            )

        # WT-28: gzip bomb streaming through aiter_raw is capped cleanly without unbounded flush.
        class RawGzipStream(httpx.AsyncByteStream):
            def __init__(self, payload: bytes) -> None:
                self._payload = payload

            async def __aiter__(self) -> AsyncIterator[bytes]:
                for i in range(0, len(self._payload), 128):
                    yield self._payload[i:i + 128]

        bomb_gz = gzip.compress(b"A" * (web_fetch.MAX_BODY_BYTES + 200_000))
        normal_gz = gzip.compress(b"<html><body><p>hello compressed</p></body></html>")

        def gzip_handler(req: httpx.Request) -> httpx.Response:
            data = bomb_gz if "bomb" in str(req.url) else normal_gz
            return httpx.Response(
                200,
                headers={"content-type": "text/html", "content-encoding": "gzip"},
                stream=RawGzipStream(data),
            )

        fetched_ok = asyncio.run(
            web_fetch.fetch(
                "https://example.com/ok", policy,
                resolve=resolver, transport=httpx.MockTransport(gzip_handler), verify=False,
            )
        )
        self.assertIn("hello compressed", fetched_ok.text)
        self.assertFalse(fetched_ok.body_capped)

        fetched_bomb = asyncio.run(
            web_fetch.fetch(
                "https://example.com/bomb", policy,
                resolve=resolver, transport=httpx.MockTransport(gzip_handler), verify=False,
            )
        )
        self.assertTrue(fetched_bomb.body_capped)

    def test_wt20_duplicate_rule_ids_across_profiles_are_disambiguated(self) -> None:
        prof_dir = self.base / ".codify" / "profiles"
        prof_dir.mkdir(parents=True)
        (prof_dir / "alpha.json").write_text(json.dumps({
            "name": "alpha",
            "description": "Alpha",
            "rules": [{"id": "shared-id", "title": "Alpha Rule", "severity": "high", "pattern": "ALPHA_HIT", "languages": ["py"]}],
        }), encoding="utf-8")
        (prof_dir / "beta.json").write_text(json.dumps({
            "name": "beta",
            "description": "Beta",
            "rules": [{"id": "shared-id", "title": "Beta Rule", "severity": "low", "pattern": "BETA_HIT", "languages": ["py"]}],
        }), encoding="utf-8")
        (self.base / "app.py").write_text("ALPHA_HIT()\nBETA_HIT()\n", encoding="utf-8")

        report = scan.run_scan(str(self.base), profile="all")
        self.assertIn("[HIGH] alpha:shared-id", report)
        self.assertIn("[LOW] beta:shared-id", report)
        self.assertIn("Alpha Rule", report)
        self.assertIn("Beta Rule", report)

    def test_wt27_case_insensitive_skip_dirs(self) -> None:
        (self.base / ".GIT").mkdir()
        (self.base / ".GIT" / "config").write_text("SECRET_TOKEN_HERE\n", encoding="utf-8")
        (self.base / "NODE_MODULES").mkdir()
        (self.base / "NODE_MODULES" / "pkg.js").write_text("SECRET_TOKEN_HERE\n", encoding="utf-8")
        (self.base / "src.py").write_text("ok = 1\n", encoding="utf-8")

        lib = library.LibraryService(str(self.base))
        tree = lib.tree(depth=3)
        self.assertEqual(["src.py"], tree["files"])
        search = lib.search("SECRET_TOKEN_HERE")
        self.assertEqual([], search["matches"])
        self.assertIn(".git", search.get("skipped_dirs", []))
        self.assertIn("node_modules", search.get("skipped_dirs", []))


class TestBinaryAuditFixes(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()

    def test_wt9_overlapping_elf_sections_do_not_amplify(self) -> None:
        # Build a crafted ELF64 with 2,000 overlapping executable/data sections pointing to the same 64 KiB blob.
        blob_off = 0x100
        blob_size = 65_536
        sh_off = blob_off + blob_size
        sh_num = 2_000
        names = b"\x00.text\x00.shstrtab\x00"
        names_off = sh_off + sh_num * 64
        buf = bytearray(names_off + len(names))
        buf[blob_off:blob_off + blob_size] = (b"ABCD\x00\xe8\x00\x00\x00\x00" * (blob_size // 10 + 1))[:blob_size]
        buf[names_off:names_off + len(names)] = names
        ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\x00" * 8
        struct.pack_into(
            "<16sHHIQQQIHHHHHH", buf, 0,
            ident, 2, 62, 1, 0x401000, 0, sh_off, 0, 64, 0, 0, 64, sh_num, 1,
        )
        # Section 0: NULL; Section 1: .shstrtab; Sections 2..1999: overlapping .text
        struct.pack_into("<IIQQQQIIQQ", buf, sh_off + 64, 7, 3, 0, 0, names_off, len(names), 0, 0, 1, 0)
        for idx in range(2, sh_num):
            struct.pack_into(
                "<IIQQQQIIQQ", buf, sh_off + idx * 64,
                1, 1, 0x6, 0x401000 + idx * 0x10, blob_off, blob_size, 0, 0, 16, 0,
            )

        started = time.monotonic()
        doc = binary_inspect.inspect(bytes(buf), binary_analysis.EXPORT_CAPS)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0, f"overlapping ELF scan took {elapsed:.2f}s")
        self.assertLessEqual(doc["totals"]["blocks"], binary_inspect.MAX_SECTIONS)
        # Strings from the single 64 KiB blob must not be amplified by 1,000x.
        self.assertLess(doc["totals"]["strings"], 20_000)

    def test_wt10_macho64_initprot_bounds_and_zerofill(self) -> None:
        # 64-bit Mach-O with one LC_SEGMENT_64 (__DATA) having maxprot=7 (rwx), initprot=3 (rw),
        # one regular section and one S_ZEROFILL (__bss) section, plus a truncated LC_SYMTAB.
        cmd_seg_size = 72 + 2 * 80  # 232 bytes
        cmd_bad_symtab = 12  # < 24, must be skipped safely
        total_cmds = cmd_seg_size + cmd_bad_symtab
        buf = bytearray(32 + total_cmds + 64)
        # Header: magic, cputype(x86_64), cpusubtype, filetype(exec), ncmds=2, sizeofcmds, flags, reserved
        struct.pack_into("<IiiIIIII", buf, 0, 0xFEEDFACF, 0x01000007, 3, 2, 2, total_cmds, 0, 0)
        off = 32
        struct.pack_into("<II", buf, off, 0x19, cmd_seg_size)
        buf[off + 8:off + 14] = b"__DATA"
        struct.pack_into("<QQQQ", buf, off + 24, 0x100004000, 0x2000, 32 + total_cmds, 64)
        struct.pack_into("<iiII", buf, off + 56, 7, 3, 2, 0)  # maxprot=rwx(7), initprot=rw(3), nsects=2
        # Section 0: __data (regular)
        s0 = off + 72
        buf[s0:s0 + 6] = b"__data"
        buf[s0 + 16:s0 + 22] = b"__DATA"
        struct.pack_into("<QQI", buf, s0 + 32, 0x100004000, 16, 32 + total_cmds)
        struct.pack_into("<I", buf, s0 + 64, 0x0)  # S_REGULAR
        buf[32 + total_cmds:32 + total_cmds + 16] = b"real_macho_str\x00\x00"
        # Section 1: __bss (S_ZEROFILL with file_at=0)
        s1 = s0 + 80
        buf[s1:s1 + 5] = b"__bss"
        buf[s1 + 16:s1 + 22] = b"__DATA"
        struct.pack_into("<QQI", buf, s1 + 32, 0x100005000, 0x100, 0)
        struct.pack_into("<I", buf, s1 + 64, 0x1)  # S_ZEROFILL
        # Truncated LC_SYMTAB (cmdsize=12 < 24)
        struct.pack_into("<III", buf, off + cmd_seg_size, 0x2, cmd_bad_symtab, 0)

        doc = binary_inspect.inspect(bytes(buf), binary_analysis.EXPORT_CAPS)
        blocks = {b["name"]: b for b in doc["blocks"]}
        self.assertEqual("rw", blocks["__DATA,__data"]["perms"])
        self.assertEqual("rw", blocks["__DATA,__bss"]["perms"])
        string_vals = [s["value"] for s in doc["strings"]]
        self.assertIn("real_macho_str", string_vals)
        # __bss must NOT have rescanned the Mach-O header (which holds "__DATA").
        self.assertNotIn("__DATA", string_vals)

    def test_wt21_elf_comment_with_zero_sh_addr_detects_compiler(self) -> None:
        raw = bytearray(binary_fixtures.elf64())
        # Section 3 in binary_fixtures.elf64() is `.comment` at 0x400 + 3*64; set its sh_addr (+16) to 0.
        struct.pack_into("<Q", raw, 0x400 + 3 * 64 + 16, 0)
        doc = binary_inspect.inspect(bytes(raw), binary_analysis.EXPORT_CAPS)
        self.assertEqual("GCC", doc["program"]["compiler"])

    def test_wt22_and_wt23_pe_coff_long_symbols_aux_optional_bounds_and_last_fn_cap(self) -> None:
        # Build a PE64 with:
        # - optional_size = 112 (0 data directories; directory_count claims 16 — must be bounds-checked!)
        # - 1 executable .text section [VA 0x1000, size 0x40]
        # - COFF symbol table with:
        #   - sym 0: long name "very_long_function_name" at VA 0x1000, NumberOfAuxSymbols = 1
        #   - sym 1: auxiliary record (must be skipped!)
        #   - sym 2: short name "target" at VA 0x1020
        # - In .text: call from 0x1000 to 0x1020; call from 0x1020 to 0x999999 (way outside .text — must NOT map to target!)
        buf = bytearray(0x400)
        buf[0:2] = b"MZ"
        struct.pack_into("<I", buf, 0x3C, 0x40)
        buf[0x40:0x44] = b"PE\x00\x00"
        sym_ptr = 0x240
        sym_count = 3
        struct.pack_into("<HHIIIHH", buf, 0x44, 0x8664, 1, 0, sym_ptr, sym_count, 112, 0x0022)
        opt = 0x58
        struct.pack_into("<H", buf, opt, 0x20B)
        struct.pack_into("<I", buf, opt + 16, 0x1000)
        struct.pack_into("<Q", buf, opt + 24, 0x140000000)
        struct.pack_into("<I", buf, opt + 108, 16)  # claims 16 dirs, but optional_size=112 has 0 bytes for dirs
        sec_tbl = opt + 112
        buf[sec_tbl:sec_tbl + 5] = b".text"
        struct.pack_into("<IIII", buf, sec_tbl + 8, 0x40, 0x1000, 0x40, 0x200)
        struct.pack_into("<I", buf, sec_tbl + 36, 0x60000020)  # RX
        # .text bytes at 0x200: at +0, E8 rel32(+0x1B) -> target at +0x20; at +0x20, E8 rel32(+0x5000) -> outside .text
        buf[0x200] = 0xE8
        struct.pack_into("<i", buf, 0x201, 0x20 - 5)
        buf[0x220] = 0xE8
        struct.pack_into("<i", buf, 0x221, 0x5000)
        # COFF symbols at 0x240
        str_tbl = sym_ptr + sym_count * 18
        long_name = b"very_long_function_name\x00"
        struct.pack_into("<I", buf, str_tbl, 4 + len(long_name))
        buf[str_tbl + 4:str_tbl + 4 + len(long_name)] = long_name
        # sym 0: long name (0x00000000 + offset 4), val=0, sec=1, type=0x20, storage=2, num_aux=1
        struct.pack_into("<IIIhHBB", buf, sym_ptr, 0, 4, 0, 1, 0x20, 2, 1)
        # sym 1: bogus auxiliary record that looks like a function if NOT skipped
        buf[sym_ptr + 18:sym_ptr + 24] = b"AUXBAD"
        struct.pack_into("<IhHBB", buf, sym_ptr + 18 + 8, 0x10, 1, 0x20, 2, 0)
        # sym 2: short name "target" at offset 0x20, sec=1, type=0x20
        buf[sym_ptr + 36:sym_ptr + 42] = b"target"
        struct.pack_into("<IhHBB", buf, sym_ptr + 36 + 8, 0x20, 1, 0x20, 2, 0)

        doc = binary_inspect.inspect(bytes(buf), binary_analysis.EXPORT_CAPS)
        funcs = {f["name"]: f for f in doc["functions"]}
        self.assertIn("very_long_function_name", funcs)
        self.assertNotIn("AUXBAD", funcs)
        self.assertIn("target", funcs)
        # WT-23: target's size is capped to .text end (0x40 - 0x20 = 0x20), not infinity.
        self.assertEqual(0x20, funcs["target"]["size"])
        self.assertEqual(["target"], funcs["very_long_function_name"]["callees"])
        self.assertEqual([], funcs["target"]["callees"])
        # Direct unit check of _owner on size==0 outside any section: only exact start matches.
        self.assertEqual(0x1000, binary_inspect._owner([0x1000], {0x1000: 0}, 0x1000))
        self.assertIsNone(binary_inspect._owner([0x1000], {0x1000: 0}, 0x1004))

    def test_wt29_inspector_version_keys_and_validates_native_cache(self) -> None:
        ws = self.base / "ws"
        ws.mkdir()
        (ws / "sample.elf").write_bytes(binary_fixtures.elf64())
        state = self.base / "state"
        with mock.patch.object(binary_analysis, "state_dir", lambda: state):
            first = binary_analysis.analyse(str(ws), "sample.elf")
            self.assertFalse(first["cached"])
            second = binary_analysis.analyse(str(ws), "sample.elf")
            self.assertTrue(second["cached"])
            # Bumping INSPECTOR_VERSION misses the old cache and re-runs native inspection.
            with mock.patch.object(binary_inspect, "INSPECTOR_VERSION", binary_inspect.INSPECTOR_VERSION + 1):
                third = binary_analysis.analyse(str(ws), "sample.elf")
            self.assertFalse(third["cached"])


if __name__ == "__main__":
    unittest.main()
