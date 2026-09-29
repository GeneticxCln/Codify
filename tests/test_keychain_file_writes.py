"""The local secret store is written safely under contention and failure (review of 2026-09-29).

Three defects in `Keychain._write_file` and the read-modify-write around it:

* **A descriptor closed twice.** `os.fdopen(fd)` takes ownership of `fd`; the `except` that follows a failed
  `replace` then closed the same number again. In a process with other threads, that number may already be
  someone else's socket or file.
* **A lock that serialized nothing.** `flock` was taken on the temporary file *after* it had been opened with
  `O_TRUNC` — so a second writer truncated the first one's half-written temp — and the read that decides what
  to write sat outside the lock entirely, so two saves still lost one key (last writer wins). The lock has to
  cover read, modify and write, and it has to be on something that is not renamed away under it.
* **A temp file left behind.** A failed replace left `secrets.json.tmp` — plaintext keys, 0600, and never
  cleaned up.

The store is exercised through its public methods on real files in a temporary directory.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

from engine.providers import Keychain, ProviderError


class KeychainCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "state"
        self.path = self.dir / "secrets.json"

    def keychain(self) -> Keychain:
        # An explicit path is an explicit store: the OS keyring is never consulted.
        return Keychain(secrets_path=self.path)

    def stored(self) -> dict[str, str]:
        data: dict[str, str] = json.loads(self.path.read_text(encoding="utf-8"))
        return data


class TestAFailedWrite(KeychainCase):
    def test_a_failed_replace_closes_no_descriptor_it_does_not_own(self) -> None:
        kc = self.keychain()
        not_open: list[int] = []
        real_close = os.close

        def checked_close(fd: int) -> None:
            # Every descriptor this store closes by hand must still be open when it does: one that a file
            # object has already closed is a number the kernel may have handed to someone else since.
            try:
                os.fstat(fd)
            except OSError:
                not_open.append(fd)
            real_close(fd)

        with mock.patch("os.replace", side_effect=OSError("disk said no")), \
                mock.patch("pathlib.Path.replace", side_effect=OSError("disk said no")), \
                mock.patch("os.close", checked_close):
            with self.assertRaises(ProviderError) as caught:
                kc.set_provider_key("openai", "sk-secret-value")

        self.assertEqual("secrets_unwritable", caught.exception.code)
        self.assertEqual([], not_open, f"os.close was called on descriptors that were already closed: {not_open}")

    def test_a_failed_write_leaves_no_temp_file_and_no_partial_store(self) -> None:
        kc = self.keychain()
        kc.set_provider_key("openai", "first-key-value")

        with mock.patch("os.replace", side_effect=OSError("disk said no")), \
                mock.patch("pathlib.Path.replace", side_effect=OSError("disk said no")):
            with self.assertRaises(ProviderError):
                kc.set_provider_key("groq", "second-key-value")

        leftovers = sorted(p.name for p in self.dir.iterdir() if p.name.endswith(".tmp") or ".tmp" in p.suffixes)
        self.assertEqual([], leftovers, "plaintext keys were left in a temp file")
        self.assertEqual({"providers/openai": "first-key-value"}, self.stored(), "the store changed on a failed save")


class TestConcurrentSaves(KeychainCase):
    def test_saves_from_many_threads_lose_nothing(self) -> None:
        threads_n, each = 8, 20
        errors: list[BaseException] = []
        start = threading.Barrier(threads_n)

        def worker(n: int) -> None:
            kc = self.keychain()
            try:
                start.wait(timeout=10)
                for i in range(each):
                    kc.set_provider_key(f"p{n}x{i}", f"key-{n}-{i}")
            except BaseException as exc:  # noqa: BLE001 — the test reports whatever a worker hit
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual([], errors)
        expected = {f"providers/p{n}x{i}": f"key-{n}-{i}" for n in range(threads_n) for i in range(each)}
        self.assertEqual(expected, self.stored(), "a concurrent save dropped another save's key")

    def test_a_forget_racing_saves_neither_resurrects_nor_drops_anything(self) -> None:
        kc = self.keychain()
        kc.set_provider_key("keep", "keep-value")
        kc.set_provider_key("gone", "gone-value")
        errors: list[BaseException] = []

        def saver() -> None:
            k = self.keychain()
            try:
                for i in range(30):
                    k.set_provider_key(f"new{i}", f"v{i}")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def forgetter() -> None:
            try:
                self.keychain().forget("providers/gone")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=saver), threading.Thread(target=forgetter), threading.Thread(target=saver)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual([], errors)
        data = self.stored()
        self.assertNotIn("providers/gone", data, "a save resurrected a forgotten key")
        self.assertEqual("keep-value", data["providers/keep"])
        self.assertTrue(all(f"providers/new{i}" in data for i in range(30)))


class TestThePermissions(KeychainCase):
    def test_the_store_the_lock_and_the_directory_are_private(self) -> None:
        self.keychain().set_provider_key("openai", "sk-secret-value")

        self.assertEqual(0o600, stat.S_IMODE(self.path.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(self.dir.stat().st_mode))
        for extra in self.dir.iterdir():
            if extra != self.path:
                self.assertEqual(0o600, stat.S_IMODE(extra.stat().st_mode), f"{extra.name} is not private")


if __name__ == "__main__":
    unittest.main()
