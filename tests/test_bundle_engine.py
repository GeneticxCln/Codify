"""The engine bundle the packages carry: its pin, its lock, and the rules its build keeps.

`scripts/bundle_engine.py` builds the CPython + dependencies + `engine/` that the deb and the
AppImage ship, and `scripts/lock_engine.py` writes the lock it installs. Building one needs a
download, so the gate does not build it (the release does, and `make check-bundle` boots what it
built). What the gate can hold without a network is everything that decides *what* gets built:

* the pin describes one coherent build, of the Python version CI's newest leg tests;
* the lock covers every dependency `engine/requirements.txt` declares, inside its range, pinned,
  hashed and under a licence the packages may redistribute — and was made for the pinned
  interpreter, not an older one;
* a download or a tarball whose hash is not the pin's is deleted, never used;
* extraction refuses a member that would land outside the bundle;
* the copy, the prune and the install argv are what the docstrings say they are.
"""

from __future__ import annotations

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import hashlib
import io
import json
import re
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts import bundle_engine, lock_engine
from scripts.bundle_engine import BundleError, Pin
from tests.test_dependency_parity import _requirements

ROOT = Path(__file__).resolve().parent.parent
LOCK_LINE = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>\S+) --hash=sha256:(?P<sha>[0-9a-f]{64})  # (?P<licence>.+)$")


def _release(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version.split("+")[0])[:4])


def _satisfies(version: str, spec: str) -> bool:
    """`version` against a `>=a,<b` style specifier: the only operators requirements.txt uses."""
    for clause in (c.strip() for c in spec.split(",") if c.strip()):
        match = re.fullmatch(r"(>=|<=|==|<|>)\s*([0-9][0-9.]*)", clause)
        if match is None:
            raise AssertionError(f"specifier {clause!r} needs this comparator extended")
        op, bound = match.groups()
        have, want = _release(version), _release(bound)
        ok = {">=": have >= want, "<=": have <= want, "==": have == want, "<": have < want, ">": have > want}[op]
        if not ok:
            return False
    return True


def _normal(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _locked() -> dict[str, re.Match[str]]:
    entries: dict[str, re.Match[str]] = {}
    for line in bundle_engine.LOCK_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = LOCK_LINE.match(line)
        if match is None:
            raise AssertionError(f"lock line is not `name==version --hash=sha256:…  # licence`: {line!r}")
        entries[_normal(match["name"])] = match
    return entries


def _pin(**changes: str) -> Pin:
    base = {
        "version": "3.14.6",
        "release": "20260718",
        "asset": "cpython-3.14.6+20260718-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
        "sha256": hashlib.sha256(b"the pinned bytes").hexdigest(),
    }
    base.update(changes)
    return Pin(**base)


class TestThePin(unittest.TestCase):
    def test_the_committed_pin_is_one_coherent_build(self) -> None:
        pin = bundle_engine.load_pin()
        self.assertEqual(pin.minor, ".".join(pin.version.split(".")[:2]))
        self.assertEqual(pin.abi, "cp" + pin.minor.replace(".", ""))

    def test_the_bundled_python_is_the_newest_one_ci_tests(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "check.yml").read_text(encoding="utf-8")
        match = re.search(r'^\s*python:\s*\[([^\]]+)\]', workflow, re.M)
        self.assertIsNotNone(match, "check.yml's python matrix moved; this test reads it")
        assert match is not None
        legs = [leg.strip().strip('"') for leg in match.group(1).split(",")]
        newest = max(legs, key=lambda leg: tuple(int(n) for n in leg.split(".")))
        self.assertEqual(
            bundle_engine.load_pin().minor, newest,
            "the packages would ship an interpreter no CI leg runs the suite on",
        )

    def test_it_can_be_fetched_from_the_project_and_its_mirror(self) -> None:
        urls = bundle_engine.asset_urls(_pin())
        self.assertTrue(urls[0].startswith("https://github.com/astral-sh/python-build-standalone/releases/download/20260718/"))
        self.assertTrue(urls[1].startswith("https://releases.astral.sh/github/python-build-standalone/"))
        self.assertTrue(all("cpython-3.14.6%2B20260718-" in url for url in urls))

    def test_a_pin_that_does_not_describe_one_build_is_refused(self) -> None:
        good = json.loads(bundle_engine.PIN_PATH.read_text(encoding="utf-8"))
        for change in ({"sha256": "abc"}, {"asset": good["asset"].replace("3.14.6", "3.14.5")}, {"version": "3.14"}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as scratch:
                path = Path(scratch) / "python.json"
                path.write_text(json.dumps({**good, **change}), encoding="utf-8")
                with self.assertRaises(BundleError):
                    bundle_engine.load_pin(path)


class TestTheLock(unittest.TestCase):
    def test_every_line_is_pinned_hashed_and_licensed(self) -> None:
        locked = _locked()
        self.assertGreater(len(locked), 10)
        for name, entry in locked.items():
            with self.subTest(package=name):
                self.assertTrue(
                    lock_engine.licence_allowed(entry["licence"]),
                    f"{name} is {entry['licence']}, which the packages may not redistribute",
                )

    def test_it_covers_every_dependency_inside_its_range(self) -> None:
        locked = _locked()
        for requirement in _requirements():
            parsed = re.match(r"([A-Za-z0-9._-]+)\s*(.*)", requirement)
            self.assertIsNotNone(parsed, requirement)
            assert parsed is not None
            name, spec = parsed.groups()
            with self.subTest(requirement=requirement):
                self.assertIn(_normal(name), locked, f"{name} is required and the bundle would not have it")
                version = locked[_normal(name)]["version"]
                self.assertTrue(
                    _satisfies(version, spec),
                    f"the lock has {name} {version}, outside {spec}: run `make lock-engine`",
                )

    def test_keyrings_linux_only_dependencies_are_in_it(self) -> None:
        # They are behind `sys_platform == "linux"` markers, so a lock made anywhere else drops them,
        # and a bundled keyring without them cannot reach the Secret Service.
        locked = _locked()
        for name in ("secretstorage", "jeepney"):
            self.assertIn(name, locked)

    def test_it_was_made_for_the_pinned_interpreter(self) -> None:
        first = bundle_engine.LOCK_PATH.read_text(encoding="utf-8").splitlines()[0]
        self.assertIn(
            f"CPython {bundle_engine.load_pin().version} ", first,
            "packaging/python.json moved and the lock was not regenerated for it: run `make lock-engine`",
        )


class TestLicences(unittest.TestCase):
    def test_an_expression_passes_on_its_parts(self) -> None:
        allowed = lock_engine.licence_allowed
        self.assertTrue(allowed("MIT"))
        self.assertTrue(allowed("Apache-2.0 OR BSD-3-Clause"))
        self.assertTrue(allowed("MPL-2.0 AND (Apache-2.0 OR MIT)"))
        self.assertTrue(allowed("MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later"))
        self.assertFalse(allowed("GPL-3.0-only"))
        self.assertFalse(allowed("MIT AND GPL-3.0-only"))
        self.assertFalse(allowed(""))
        self.assertFalse(allowed("BSD 3-Clause License  Copyright (c) 2024"))

    def test_a_wheel_with_no_readable_licence_stops_the_lock(self) -> None:
        self.assertEqual(lock_engine.licence_of("x", {"license_expression": "MIT"}), "MIT")
        self.assertEqual(lock_engine.licence_of("x", {"license": "BSD-3-Clause"}), "BSD-3-Clause")
        self.assertEqual(lock_engine.licence_of("scrapling", {"license": "BSD 3-Clause License\n..."}), "BSD-3-Clause")
        with self.assertRaises(lock_engine.LockError):
            lock_engine.licence_of("x", {"license": "Copyright (c) someone\nAll rights reserved"})

    def test_the_report_becomes_sorted_hashed_lines(self) -> None:
        def item(name: str, sha: str | None, licence: str = "MIT") -> dict[str, object]:
            hashes = {"sha256": sha} if sha else {}
            return {"metadata": {"name": name, "version": "1.0", "license_expression": licence},
                    "download_info": {"archive_info": {"hashes": hashes}}}

        entries = lock_engine.from_report({"install": [item("zed", "b" * 64), item("Alpha", "a" * 64)]})
        self.assertEqual([e.name for e in entries], ["Alpha", "zed"])
        self.assertEqual(entries[0].line(), f"Alpha==1.0 --hash=sha256:{'a' * 64}  # MIT")
        with self.assertRaises(lock_engine.LockError):
            lock_engine.from_report({"install": [item("nohash", None)]})
        with self.assertRaises(lock_engine.LockError):
            lock_engine.from_report({"install": [item("gpl", "c" * 64, "GPL-3.0-only")]})
        self.assertTrue(LOCK_LINE.match(entries[1].line()))


class TestFetchingThePinnedFile(unittest.TestCase):
    def test_a_download_that_is_not_the_pinned_file_is_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            cache = Path(scratch)

            def wrong(_url: str, dest: Path) -> None:
                dest.write_bytes(b"something else")

            with self.assertRaises(BundleError) as caught:
                bundle_engine.fetch_python(_pin(), cache, download=wrong)
            self.assertIn("deleted", str(caught.exception))
            self.assertEqual(list(cache.iterdir()), [])

    def test_the_mirror_is_tried_when_the_first_host_fails(self) -> None:
        tried: list[str] = []

        def flaky(url: str, dest: Path) -> None:
            tried.append(url)
            if len(tried) == 1:
                raise OSError("403")
            dest.write_bytes(b"the pinned bytes")

        with tempfile.TemporaryDirectory() as scratch:
            path = bundle_engine.fetch_python(_pin(), Path(scratch), download=flaky)
            self.assertEqual(path.read_bytes(), b"the pinned bytes")
            self.assertEqual(len(tried), 2)
            # Once it is cached and checked, nothing is downloaded again.
            bundle_engine.fetch_python(_pin(), Path(scratch), download=flaky)
            self.assertEqual(len(tried), 2)

    def test_a_tarball_handed_in_is_held_to_the_same_pin(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            given = Path(scratch) / "given.tar.gz"
            given.write_bytes(b"not the pinned bytes")
            with self.assertRaises(BundleError):
                bundle_engine.fetch_python(_pin(), Path(scratch) / "cache", tarball=given)
            self.assertFalse((Path(scratch) / "cache" / _pin().asset).exists())


def _tar(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class TestExtraction(unittest.TestCase):
    def test_a_member_outside_the_bundle_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            tarball = Path(scratch) / "evil.tar.gz"
            tarball.write_bytes(_tar({"python/bin/python3": b"", "../escaped": b"x"}))
            with self.assertRaises(BundleError):
                bundle_engine.extract_python(tarball, Path(scratch) / "out")
            self.assertFalse((Path(scratch) / "escaped").exists())

    def test_a_tarball_with_no_interpreter_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            tarball = Path(scratch) / "empty.tar.gz"
            tarball.write_bytes(_tar({"python/README": b""}))
            with self.assertRaises(BundleError):
                bundle_engine.extract_python(tarball, Path(scratch) / "out")


class TestWhatIsInstalledAndHow(unittest.TestCase):
    def test_wheels_are_chosen_for_the_glibc_floor_newest_tag_first(self) -> None:
        tags = bundle_engine.platform_tags((2, 35))
        self.assertEqual(tags[0], "manylinux_2_35_x86_64")
        self.assertEqual(tags[-2:], ["manylinux_2_17_x86_64", "manylinux2014_x86_64"])

    def test_the_install_takes_only_the_hashed_wheels_of_the_lock(self) -> None:
        pin = _pin()
        argv = bundle_engine.install_argv(Path("/b/python/bin/python3"), Path("/b/python"), pin, Path("/l"))
        for flag in ("--require-hashes", "--no-deps", "--only-binary=:all:", "--no-compile"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--abi") + 1], "cp314")
        self.assertEqual(argv[argv.index("--python-version") + 1], "3.14")
        self.assertEqual(argv[argv.index("--target") + 1], "/b/python/lib/python3.14/site-packages")
        self.assertEqual(argv[-2:], ["-r", "/l"])
        # The lock is resolved with the same tags, so the file pip picks is the file the lock hashed.
        self.assertTrue(set(bundle_engine.target_flags(pin)) <= set(argv))

    def test_bytecode_never_checks_a_timestamp(self) -> None:
        argv = bundle_engine.compile_argv(Path("py"), [Path("a")])
        self.assertEqual(argv[argv.index("--invalidation-mode") + 1], "unchecked-hash")


class TestTheBundlesContents(unittest.TestCase):
    def test_the_engine_is_copied_without_bytecode_or_its_requirements_file(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            src = Path(scratch) / "engine"
            for rel in ("__main__.py", "requirements.txt", "__pycache__/x.cpython-314.pyc",
                        "builtin_skills/a.md", "ghidra/Export.java", "stale.pyc"):
                (src / rel).parent.mkdir(parents=True, exist_ok=True)
                (src / rel).write_text("x", encoding="utf-8")
            count = bundle_engine.copy_engine(src, Path(scratch) / "out")
            kept = sorted(str(p.relative_to(Path(scratch) / "out")) for p in (Path(scratch) / "out").rglob("*") if p.is_file())
            self.assertEqual(kept, ["__main__.py", "builtin_skills/a.md", "ghidra/Export.java"])
            self.assertEqual(count, 3)

    def test_the_real_engine_keeps_the_data_it_reads_at_run_time(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            out = Path(scratch) / "engine"
            bundle_engine.copy_engine(ROOT / "engine", out)
            for rel in ("__main__.py", "regex_worker.py", "spawn_guard.py"):
                self.assertTrue((out / rel).is_file(), rel)
            for directory in ("builtin_skills", "builtin_profiles", "ghidra"):
                self.assertTrue(any((out / directory).iterdir()), directory)

    def test_the_prune_removes_what_the_engine_never_runs(self) -> None:
        pin = _pin()
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / "python"
            for rel in ("bin/python3", "bin/pip3.14", "bin/idle3.14", "lib/libpython3.14.so.1.0",
                        "lib/python3.14/tkinter/__init__.py", "lib/python3.14/site-packages/pip/__init__.py",
                        "lib/python3.14/site-packages/pip-26.1.2.dist-info/METADATA",
                        "lib/python3.14/site-packages/fastapi/__init__.py", "share/man/x"):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text("x", encoding="utf-8")
            bundle_engine.prune(root, pin)
            left = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
            self.assertEqual(left, ["bin/python3", "lib/python3.14/site-packages/fastapi/__init__.py"])

    def test_the_interpreter_is_one_file_and_the_tree_has_no_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / "python"
            (root / "bin").mkdir(parents=True)
            (root / "bin" / "python3.14").write_bytes(b"interpreter")
            (root / "bin" / "python3").symlink_to("python3.14")
            (root / "bin" / "python").symlink_to("python3.14")
            bundle_engine.single_interpreter(root)
            self.assertEqual(sorted(p.name for p in (root / "bin").iterdir()), ["python3"])
            self.assertFalse((root / "bin" / "python3").is_symlink())
            self.assertEqual((root / "bin" / "python3").read_bytes(), b"interpreter")
            # Any other link would be copied whole by the packager, so the build stops on it.
            (root / "lib").mkdir()
            (root / "lib" / "alias").symlink_to("../bin/python3")
            with self.assertRaises(BundleError):
                bundle_engine.single_interpreter(root)

    def test_the_manifest_names_the_app_version_and_the_lock(self) -> None:
        text = "# header\nfastapi==1.0 --hash=sha256:" + "a" * 64 + "  # MIT\n"
        info = bundle_engine.manifest(_pin(), text, bundle_engine.app_version(), 12)
        conf = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
        self.assertEqual(info["app_version"], conf["version"])
        self.assertEqual(info["packages"], ["fastapi==1.0"])
        self.assertEqual(info["lock_sha256"], hashlib.sha256(text.encode()).hexdigest())

    def test_a_directory_that_is_not_a_bundle_is_never_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            theirs = Path(scratch) / "out"
            theirs.mkdir()
            (theirs / "notes.txt").write_text("mine", encoding="utf-8")
            with self.assertRaises(BundleError):
                bundle_engine._make_room(theirs)
            self.assertTrue((theirs / "notes.txt").is_file())
            (theirs / "BUNDLE.json").write_text("{}", encoding="utf-8")
            bundle_engine._make_room(theirs)
            self.assertFalse(theirs.exists())


if __name__ == "__main__":
    unittest.main()
