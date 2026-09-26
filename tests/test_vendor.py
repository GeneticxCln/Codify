"""The vendoring policy, enforced rather than promised.

`benchmarks/vendor.py` exists to make one decision hard to make by accident:
putting a third-party repository's code into this project. Everything here runs
offline against a tarball built in the test, because a policy that can only be
checked by reaching the network is a policy nobody re-checks.

The order matters: the licence gate and the pin are checked *before* a byte is
fetched, so a refusal costs nothing and cannot be reached only after a successful
download.
"""

from __future__ import annotations

import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from tests import hermetic  # noqa: F401

from benchmarks import vendor
from benchmarks.runner import BenchmarkError, resolve_repo
from benchmarks.vendor import PERMISSIVE_LICENSES, VendorError

GOOD_SPEC = "acme/widget@1a2b3c4d5e6f"


def _tarball(files: dict[str, str], root: str = "widget-1a2b3c4") -> Path:
    """A GitHub-shaped .tar.gz: one wrapper directory, then the given members."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, body in files.items():
            data = body.encode("utf-8")
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    path = Path(tempfile.mkdtemp(prefix="vendor-src-")) / "src.tar.gz"
    path.write_bytes(buffer.getvalue())
    return path


DEFAULT_FILES = {
    "README.md": "# widget\n",
    "src/widget.py": "def greet():\n    return 'hi'\n",
    "src/widget_test.py": "def test_greet():\n    pass\n",
    "src/__pycache__/widget.cpython-310.pyc": "junk",
    ".git/config": "[core]\n",
    "node_modules/left-pad/index.js": "module.exports = 1\n",
    "dist/widget.min.js": "var a=1\n",
    "src/widget.pyc": "junk",
    "docs/CHANGELOG.md": "# changes\n",
}


class VendorTests(unittest.TestCase):
    """A full fetch, then each rule on its own."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps({"version": 1, "repos": []}), encoding="utf-8")

    def _vendor(self, **kwargs: object) -> dict[str, object]:
        params: dict[str, object] = {
            "license_id": "MIT",
            "why": "a cross-file rename task needs a real package",
            "source": _tarball(DEFAULT_FILES),
            "root": self.root,
            "manifest_path": self.manifest,
            "now": "2026-01-01",
        }
        params.update(kwargs)
        return vendor.vendor(GOOD_SPEC, **params)  # type: ignore[arg-type]

    # -- the whole thing --------------------------------------------
    def test_a_vendored_repo_is_usable_by_the_runner(self) -> None:
        self._vendor()
        resolved = resolve_repo("widget", root=self.root)
        self.assertTrue((resolved / "src" / "widget.py").is_file())
        self.assertTrue((resolved / vendor.NOTICE_NAME).is_file())

    def test_the_wrapper_directory_is_stripped(self) -> None:
        self._vendor()
        dest = self.root / "repos" / "widget"
        self.assertTrue((dest / "README.md").is_file())
        self.assertFalse((dest / "widget-1a2b3c4").exists())

    # -- pruning ----------------------------------------------------
    def test_build_output_and_vcs_state_are_pruned(self) -> None:
        self._vendor()
        dest = self.root / "repos" / "widget"
        for pruned in (
            ".git", "node_modules", "dist", "src/__pycache__", "src/widget.pyc",
        ):
            with self.subTest(pruned=pruned):
                self.assertFalse((dest / pruned).exists(), f"{pruned} was vendored")
        self.assertTrue((dest / "src" / "widget.py").is_file())

    def test_subpath_keeps_only_that_subtree(self) -> None:
        self._vendor(subpath="src")
        dest = self.root / "repos" / "widget"
        self.assertTrue((dest / "widget.py").is_file())
        self.assertFalse((dest / "README.md").exists())
        self.assertFalse((dest / "docs").exists())

    def test_a_subpath_that_keeps_nothing_is_an_error_not_an_empty_repo(self) -> None:
        with self.assertRaises(VendorError) as ctx:
            self._vendor(subpath="no-such-dir")
        self.assertIn("kept no files", str(ctx.exception))

    def test_a_failed_vendor_leaves_no_half_written_repo(self) -> None:
        with self.assertRaises(VendorError):
            self._vendor(subpath="no-such-dir")
        self.assertFalse((self.root / "repos" / "widget").exists())

    # -- refusal ----------------------------------------------------
    def test_a_copyleft_licence_is_refused_before_anything_is_fetched(self) -> None:
        with self.assertRaises(VendorError) as ctx:
            self._vendor(license_id="GPL-3.0")
        self.assertIn("not on the permissive list", str(ctx.exception))
        self.assertEqual(json.loads(self.manifest.read_text(encoding="utf-8"))["repos"], [])

    def test_a_source_available_licence_is_refused_too(self) -> None:
        for license_id in ("BUSL-1.1", "SSPL-1.0", "AGPL-3.0", "MPL-2.0", "LGPL-3.0"):
            with self.subTest(license=license_id):
                with self.assertRaises(VendorError):
                    self._vendor(license_id=license_id)

    def test_every_listed_licence_is_actually_permissive(self) -> None:
        """The list is a policy, so a careless addition is a real regression."""
        for license_id in PERMISSIVE_LICENSES:
            with self.subTest(license=license_id):
                lowered = license_id.lower()
                for copyleft in ("gpl", "agpl", "lgpl", "mpl", "sspl", "busl", "cddl", "eupl"):
                    self.assertNotIn(copyleft, lowered, f"{license_id} is not permissive")

    def test_a_branch_rather_than_a_pin_is_refused(self) -> None:
        for spec in ("acme/widget", "acme/widget@main", "acme/widget@abc"):
            with self.subTest(spec=spec):
                with self.assertRaises(VendorError):
                    vendor.parse_spec(spec)

    def test_a_malformed_upstream_is_refused(self) -> None:
        for spec in ("widget@1a2b3c4d", "a/b/c@1a2b3c4d", "@1a2b3c4d"):
            with self.subTest(spec=spec):
                with self.assertRaises(VendorError):
                    vendor.parse_spec(spec)

    def test_a_vendor_without_a_reason_is_refused(self) -> None:
        for why in ("", "   "):
            with self.subTest(why=why):
                with self.assertRaises(VendorError) as ctx:
                    self._vendor(why=why)
                self.assertIn("--why", str(ctx.exception))

    def test_a_parent_directory_member_is_refused(self) -> None:
        """A tarball is untrusted input even when it came from a cache."""
        evil = _tarball({"../escaped.py": "pwned = 1\n"})
        with self.assertRaises(VendorError) as ctx:
            self._vendor(source=evil)
        self.assertIn("parent-directory", str(ctx.exception))
        self.assertFalse((self.root / "escaped.py").exists())

    def test_an_absolute_member_is_refused(self) -> None:
        # root="" builds an unwrapped member, which is the shape a hostile
        # mirror would serve and the one a well-behaved archive never has.
        evil = _tarball({"/etc/cron.d/pwn": "* * * * * root sh\n"}, root="")
        with self.assertRaises(VendorError) as ctx:
            self._vendor(source=evil)
        self.assertIn("absolute path", str(ctx.exception))

    def test_an_oversized_file_is_refused(self) -> None:
        big = _tarball({"src/huge.bin": "x" * (vendor.MAX_FILE_BYTES + 1)})
        with self.assertRaises(VendorError) as ctx:
            self._vendor(source=big)
        self.assertIn("vendor a subtree", str(ctx.exception))

    def test_a_file_that_is_not_a_tarball_is_an_error_not_a_crash(self) -> None:
        junk = self.root / "junk.tar.gz"
        junk.write_bytes(b"this is not gzip")
        with self.assertRaises(VendorError) as ctx:
            self._vendor(source=junk)
        self.assertIn("not a readable", str(ctx.exception))

    def test_a_missing_source_is_an_error_not_a_crash(self) -> None:
        with self.assertRaises(VendorError) as ctx:
            self._vendor(source=self.root / "absent.tar.gz")
        self.assertIn("not a file", str(ctx.exception))

    # -- provenance -------------------------------------------------
    def test_the_notice_carries_upstream_sha_licence_and_reason(self) -> None:
        self._vendor(why="the rename task needs two call sites")
        notice = (self.root / "repos" / "widget" / vendor.NOTICE_NAME).read_text("utf-8")
        self.assertIn("https://github.com/acme/widget", notice)
        self.assertIn("1a2b3c4d5e6f", notice)
        self.assertIn("MIT", notice)
        self.assertIn("the rename task needs two call sites", notice)

    def test_the_manifest_entry_is_enough_to_recreate_the_snapshot(self) -> None:
        entry = self._vendor(subpath="src")
        recorded = json.loads(self.manifest.read_text(encoding="utf-8"))["repos"]
        self.assertEqual(len(recorded), 1)
        for key in ("name", "upstream", "sha", "license", "subpath", "why", "fetched"):
            with self.subTest(key=key):
                self.assertIn(key, entry)
        self.assertEqual(recorded[0]["subpath"], "src")

    def test_revendoring_replaces_rather_than_duplicates(self) -> None:
        self._vendor()
        self._vendor(why="a better reason the second time")
        recorded = json.loads(self.manifest.read_text(encoding="utf-8"))["repos"]
        self.assertEqual([r["name"] for r in recorded], ["widget"])
        self.assertEqual(recorded[0]["why"], "a better reason the second time")

    def test_repos_stay_sorted(self) -> None:
        for spec, files in (("acme/alpha@1a2b3c4", {"a.py": "a = 1\n"}),
                            ("acme/zeta@1a2b3c4", {"z.py": "z = 1\n"})):
            vendor.vendor(
                spec, license_id="MIT", why="ordering", source=_tarball(files),
                root=self.root, manifest_path=self.manifest, now="2026-01-01",
            )
        recorded = json.loads(self.manifest.read_text(encoding="utf-8"))["repos"]
        self.assertEqual([r["name"] for r in recorded], ["alpha", "zeta"])

    def test_the_shipped_manifest_vendors_nothing_by_default(self) -> None:
        """The default posture is absence: no code, and the manifest says why."""
        manifest = json.loads(
            (Path(vendor.__file__).resolve().parent / "manifest.json").read_text("utf-8")
        )
        self.assertEqual(manifest.get("repos"), [])
        self.assertIn("vendor.py", manifest["_read_this_first"])


class VendorErrorIsBenchmarkError(unittest.TestCase):
    """A vendoring refusal reaches the runner as a diagnosis, never a crash."""

    def test_resolve_repo_explains_how_to_vendor(self) -> None:
        with self.assertRaises(BenchmarkError) as ctx:
            resolve_repo("not-vendored", root=Path("/nonexistent-bench-root"))
        message = str(ctx.exception)
        self.assertIn("benchmarks.vendor", message)
        self.assertIn("docs/08", message)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
