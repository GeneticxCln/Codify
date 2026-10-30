"""`scripts/release_notes.py`: the tag, the three version files and the published file names must agree.

The script exists because a release whose tag says `v1.0.0` while `tauri.conf.json` says `0.2.0` makes every copy of
that build offer an update to itself forever, and because a file named in a release page that the release does not
contain is a broken download for whoever clicks it. It reads a checkout and prints notes, so most of these tests hand
it checkouts of their own; the last two run it against the real one, which is the only way to catch a version bump
that reached two of the three files.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.release_notes import ARCH, disagreements, main, notes, tag_version, versions

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# A Cargo.toml with a dependency that names its own version inline, which is the shape the
# `[package]` version's regex has to survive: `regex.search` would find `version = "2"` first.
CARGO_TOML = """[package]
name = "codify-desktop"
version = "{version}"

[dependencies]
tauri = {{ version = "2", features = ["unstable"] }}
"""


def checkout(root: Path, *, conf: str = "1.0.0", crate: str = "1.0.0", ui: str = "1.0.0") -> Path:
    """A miniature checkout: the three files that carry a version, and nothing else."""
    (root / "src-tauri").mkdir(parents=True, exist_ok=True)
    (root / "ui").mkdir(parents=True, exist_ok=True)
    (root / "src-tauri" / "tauri.conf.json").write_text(
        json.dumps({"version": conf}), encoding="utf-8"
    )
    (root / "src-tauri" / "Cargo.toml").write_text(
        CARGO_TOML.format(version=crate), encoding="utf-8"
    )
    (root / "ui" / "package.json").write_text(json.dumps({"version": ui}), encoding="utf-8")
    return root


def run(tag: str, root: Path) -> tuple[int, str, str]:
    """The script as the workflow runs it: exit status, stdout, stderr."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main([tag, "--root", str(root)])
    return code, out.getvalue(), err.getvalue()


class TestTheVersionsAndTheTag(unittest.TestCase):
    def test_three_files_that_agree_with_the_tag_are_published(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            code, out, err = run("v1.0.0", checkout(Path(scratch)))
        self.assertEqual(0, code, err)
        self.assertEqual("", err)
        self.assertIn("Codify 1.0.0", out)

    def test_a_file_left_behind_is_named_with_the_fix(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            code, out, err = run("v1.0.0", checkout(Path(scratch), ui="0.2.0"))
        self.assertEqual(1, code)
        self.assertEqual("", out, "a refusal printed something that looks like notes")
        self.assertIn("ui/package.json says 0.2.0", err)
        self.assertIn("set all three files to 1.0.0", err)
        self.assertNotIn("tauri.conf.json says", err, "a file that agrees was reported")

    def test_every_file_is_reported_when_none_of_them_agree(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            found = disagreements("v2.0.0", versions(checkout(Path(scratch))))
        self.assertEqual(3, len(found), found)

    def test_a_tag_without_the_leading_v_is_the_same_release(self) -> None:
        self.assertEqual(tag_version("v1.0.0"), tag_version("1.0.0"))
        with tempfile.TemporaryDirectory() as scratch:
            found = versions(checkout(Path(scratch)))
        self.assertEqual([], disagreements("1.0.0", found))

    def test_a_tag_that_is_not_a_version_is_refused(self) -> None:
        # `nightly` is a tag this repository could have; a package built from it could be
        # compared with nothing, so the release stops rather than shipping an unnameable version.
        with tempfile.TemporaryDirectory() as scratch:
            code, out, err = run("nightly", checkout(Path(scratch)))
        self.assertEqual(1, code)
        self.assertEqual("", out)
        self.assertIn("does not name a version", err)

    def test_a_missing_file_is_a_failure_and_not_a_skipped_entry(self) -> None:
        # "two of the three agree" must not be a state this can reach.
        with tempfile.TemporaryDirectory() as scratch:
            root = checkout(Path(scratch))
            (root / "ui" / "package.json").unlink()
            with self.assertRaises(SystemExit) as raised:
                versions(root)
        self.assertIn("ui/package.json", str(raised.exception))
        self.assertIn("cannot read", str(raised.exception))

    def test_a_version_file_with_no_version_is_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = checkout(Path(scratch))
            (root / "src-tauri" / "tauri.conf.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(SystemExit) as raised:
                versions(root)
        self.assertIn("carries no version", str(raised.exception))

    def test_the_crate_version_is_the_packages_and_not_a_dependencys(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            found = dict(versions(checkout(Path(scratch))))
        self.assertEqual("1.0.0", found["src-tauri/Cargo.toml"])


class TestTheNotes(unittest.TestCase):
    def test_the_notes_name_exactly_the_files_the_release_publishes(self) -> None:
        text = notes("v1.0.0", "1.0.0")
        self.assertIn(f"Codify_1.0.0_{ARCH}.deb", text)
        self.assertIn(f"Codify_1.0.0_{ARCH}.AppImage", text)
        self.assertIn("SHA256SUMS", text)
        # The version is in every file name it names, so notes for a new release cannot be
        # the old ones.
        self.assertNotIn("0.2.0", text)

    def test_the_notes_say_what_they_do_not_do(self) -> None:
        text = notes("v1.0.0", "1.0.0")
        # What a person would otherwise have to guess: that the package carries the engine (and
        # what it leaves out), and that it does not update itself.
        self.assertIn("Each package carries the engine", text)
        self.assertIn("CODIFY_ROOT", text)
        self.assertIn("installs nothing", text)
        self.assertIn("sudo apt install", text)


class TestTheRealCheckout(unittest.TestCase):
    def test_the_three_version_files_agree_with_each_other(self) -> None:
        """Not with a tag — with each other: a bump that reached two files is the drift this catches."""
        found = versions(PROJECT_ROOT)
        self.assertEqual(
            {version for _, version in found},
            {found[0][1]},
            "the version files disagree: " + ", ".join(f"{n}={v}" for n, v in found),
        )

    def test_the_repository_is_in_a_state_a_tag_could_be_cut_from(self) -> None:
        """And that the version it carries is one the notes can name, at the real checkout."""
        found = versions(PROJECT_ROOT)
        version = found[0][1]
        self.assertEqual([], disagreements(f"v{version}", found))
        code, out, _ = run(f"v{version}", PROJECT_ROOT)
        self.assertEqual(0, code)
        self.assertIn(f"Codify_{version}_{ARCH}.deb", out)

    def test_the_workflow_runs_this_script_and_publishes_both_packages(self) -> None:
        """The three files are only checked if a release actually calls the check.

        Read as text, the way the UI suite reads its components: no YAML parser is worth
        adding for four substrings, and a workflow that stopped calling this script would
        quietly make every test above a fact about nothing.
        """
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("python3 scripts/release_notes.py", workflow)
        self.assertIn("--notes-file release-notes.md", workflow)
        self.assertIn("Codify_*.deb", workflow)
        self.assertIn("Codify_*.AppImage", workflow)
        self.assertIn("cargo tauri build", workflow)

    def test_the_workflow_ships_the_engine_and_boots_it_in_both_packages(self) -> None:
        """A package without its engine, or with one that cannot start, must not be published.

        The bundle is built, added through the overlay config (not tauri.conf.json, so that
        `cargo check` never needs it), and each package's engine is booted before the publish
        step — in that order, because a smoke after the upload would check a release that is
        already out.
        """
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        steps = [
            "python3 scripts/bundle_engine.py",
            "cargo tauri build --config tauri.bundle.conf.json",
            'python3 scripts/smoke_bundle.py --deb dist/Codify_*.deb --version "$VERSION"',
            'python3 scripts/smoke_bundle.py --appimage dist/Codify_*.AppImage --version "$VERSION"',
            "- name: Publish the release",
        ]
        positions = [workflow.find(step) for step in steps]
        self.assertNotIn(-1, positions, dict(zip(steps, positions, strict=True)))
        self.assertEqual(positions, sorted(positions), "the steps are out of order")
        overlay = json.loads(
            (PROJECT_ROOT / "src-tauri" / "tauri.bundle.conf.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            overlay["bundle"]["resources"], {"../build/engine-bundle": "engine-bundle"}
        )
        conf = json.loads(
            (PROJECT_ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("resources", conf["bundle"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
