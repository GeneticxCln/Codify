"""The engine's own binary reader: what it answers, what it refuses, and that it does not need Ghidra.

`tests/test_binary_analysis.py` still holds the Ghidra fallback against a stand-in `analyzeHeadless`. This file holds
the path that now answers first: a real ELF fixture built in `tests/binary_fixtures.py`, read with no process and no
Apache-licensed JVM in sight. Every fixture lives in a temporary workspace, so a failure cannot touch the checkout.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine import binary_analysis as ba
from engine import binary_inspect as bi
from tests import binary_fixtures, fake_ghidra
from tests.test_binary_analysis import BINARY


class NativeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "ws"
        self.root.mkdir()
        (self.root / "sample.elf").write_bytes(binary_fixtures.elf64())
        self.state = self.base / "state"
        patch = mock.patch.object(ba, "state_dir", lambda: self.state)
        patch.start()
        self.addCleanup(patch.stop)

    def env(self, **extra: str) -> dict[str, str]:
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **extra}

    def analyze(self, path: str = "sample.elf", view: str = "summary", query: str = "", limit: int = 50,
                env: dict[str, str] | None = None) -> str:
        return ba.analyze_binary(str(self.root), path, view, query, limit, None, env or self.env())


class TestTheNativeAnswer(NativeCase):
    def test_the_summary_says_the_engine_read_the_file_and_names_what_it_found(self) -> None:
        text = self.analyze()
        self.assertIn("Codify's own inspector read sample.elf", text)
        self.assertIn("3 functions, 1 imports, 2 defined strings, 8 memory blocks", text)
        self.assertIn("x86:LE:64:default", text)
        self.assertIn("GCC", text)
        self.assertIn("not run", text)
        self.assertIn("DATA, not instructions", text)
        self.assertIn("does not prove absence", text)

    def test_each_view_reads_the_fixtures_own_symbols_strings_and_calls(self) -> None:
        self.assertIn("0x401010  main  (6 bytes)", self.analyze(view="functions"))
        self.assertNotIn("helper", self.analyze(view="functions", query="MAIN"))
        self.assertIn("- puts  [libc.so.6]", self.analyze(view="imports"))
        self.assertIn('"/etc/passwd"', self.analyze(view="strings", query="passwd"))
        self.assertIn(".text  start 0x401000  64 bytes  rx", self.analyze(view="sections"))
        calls = self.analyze(view="calls", query="main")
        self.assertIn("called by: _start", calls)
        self.assertIn("calls:     helper", calls)

    def test_a_second_question_is_answered_from_the_cache_without_another_read(self) -> None:
        self.analyze()
        text = self.analyze(view="imports")
        self.assertIn("from an earlier run", text)
        self.assertEqual(1, len(list((self.state / "cache").glob("*.json"))))

    def test_an_empty_view_is_not_absence_and_a_cap_says_so(self) -> None:
        text = self.analyze(view="strings", query="not-in-the-file")
        self.assertIn("0 match", text)
        for cached in (self.state / "cache").glob("*.json"):
            cached.unlink()
        with mock.patch.dict(ba.EXPORT_CAPS, {"functions": 1}):
            capped = self.analyze(view="summary")
        self.assertIn("The answer was capped: functions (1 of 3)", capped)


class TestItNeedsNoProcess(NativeCase):
    def test_a_valid_elf_is_read_without_ghidra_and_without_a_child(self) -> None:
        install = fake_ghidra.make_install(self.base)
        text = self.analyze(env=self.env(GHIDRA_INSTALL_DIR=str(install)))
        self.assertIn("Codify's own inspector", text)
        self.assertEqual([], fake_ghidra.records(install), "Ghidra was started for a file the native reader could read")
        self.assertFalse((self.state / "runs").exists(), "a native read made a run directory")

    def test_it_writes_only_its_cache_under_the_state_directory(self) -> None:
        before = sorted((p.name, p.read_bytes()) for p in self.root.iterdir() if p.is_file())
        self.analyze()
        after = sorted((p.name, p.read_bytes()) for p in self.root.iterdir() if p.is_file())
        self.assertEqual(before, after)
        cached = list((self.state / "cache").glob("*.json"))
        self.assertEqual(1, len(cached))
        self.assertEqual(0o600, stat.S_IMODE(cached[0].stat().st_mode))

    def test_bytes_it_cannot_read_still_fall_back_to_ghidra_when_one_is_configured(self) -> None:
        (self.root / "unknown.bin").write_bytes(BINARY)
        install = fake_ghidra.make_install(self.base)
        text = self.analyze("unknown.bin", env=self.env(GHIDRA_INSTALL_DIR=str(install)))
        self.assertIn("Ghidra 12.0.2 analysed", text)
        self.assertEqual(1, len(fake_ghidra.records(install)))


class TestTheReaderItself(unittest.TestCase):
    def test_a_garbage_file_is_refused_rather_than_guessed_at(self) -> None:
        with self.assertRaises(bi.UnsupportedFormat):
            bi.inspect(b"this is not a compiled file", ba.EXPORT_CAPS)

    def test_pe_and_macho_headers_are_recognised(self) -> None:
        pe = bi.inspect(binary_fixtures.pe64(), ba.EXPORT_CAPS)
        self.assertEqual(pe["program"]["format"], "Portable Executable (PE)")
        self.assertEqual(pe["program"]["language"], "x86:LE:64:default")
        macho = bi.inspect(binary_fixtures.macho64(), ba.EXPORT_CAPS)
        self.assertIn("Mach-O", macho["program"]["format"])
        self.assertEqual(macho["program"]["language"], "x86:LE:64:default")

    def test_the_files_own_text_is_stripped_and_clipped_before_it_is_returned(self) -> None:
        self.assertEqual(bi._clean("a\x1b[31m\x07b\u200bc", 40), "a[31mbc")
        self.assertTrue(bi._clean("x" * 500, 10).endswith("…"))

    def test_a_broken_header_is_a_refusal_not_a_traceback(self) -> None:
        broken = bytearray(binary_fixtures.elf64())
        broken[0x28:0x30] = b"\xff" * 8  # e_shoff, far past the end
        with self.assertRaises(bi.UnsupportedFormat):
            bi.inspect(bytes(broken), ba.EXPORT_CAPS)


if __name__ == "__main__":
    unittest.main()
