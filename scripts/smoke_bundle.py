"""Boot the engine a bundle or a package carries, ask it for `/health`, and check it wrote nothing beside itself.

    python3 scripts/smoke_bundle.py build/engine-bundle             # what `make bundle-engine` built
    python3 scripts/smoke_bundle.py --deb dist/Codify_1.3.0_amd64.deb
    python3 scripts/smoke_bundle.py --appimage dist/Codify_1.3.0_amd64.AppImage --version 1.3.0

The release runs it on both packages before it publishes them (`docs/18` §3), because it is the
only place the question can be answered: the gate never builds a package, and a package whose
engine cannot start is a window that says "the engine started but exited" on every machine it
is installed on. A deb is unpacked with `dpkg-deb -x` and an AppImage with its own
`--appimage-extract`; neither is installed, mounted or run as the app.

What counts as a pass is what the shell needs, started the way the shell starts it
(`engine_protocol::engine_env`): the bundle's own `python/bin/python3 -m engine`, from the bundle,
with `PYTHONPATH` set to it, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONNOUSERSITE=1`, no `PYTHONHOME`,
and a throwaway `CODIFY_HOME`. Then:

* a `CODIFY_ENGINE token=… port=…` line on stdout within the boot timeout;
* `GET /health` with that token answering 200;
* not one file in the bundle created, removed or changed while it ran — a deb's files belong to
  root and an AppImage is read-only, so a write that works here fails there;
* with `--version`, a `BUNDLE.json` that says the package's own version, so a package can never
  carry an engine bundled for another release.

Exits 0 on a pass and 1 with the reason. Not part of `check` or `ci`: it needs a built bundle.
"""

from __future__ import annotations

import argparse
import json
import os
import selectors
import shutil
import signal
import subprocess  # noqa: S404 — argv lists only, never a shell
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.preview_engine import read_handshake  # noqa: E402 — run as a file, the checkout has to be on the path first

BOOT_TIMEOUT_S = 60.0
STOP_TIMEOUT_S = 15.0
UNPACK_TIMEOUT_S = 300


class SmokeError(RuntimeError):
    """A bundle that does not do what the shell needs, with the sentence that says why."""


def snapshot(tree: Path) -> dict[str, tuple[int, int]]:
    """Every file and directory under `tree`, with its size and modification time."""
    found: dict[str, tuple[int, int]] = {}
    for path in tree.rglob("*"):
        info = path.lstat()
        found[str(path.relative_to(tree))] = (info.st_size if path.is_file() else -1, info.st_mtime_ns)
    return found


def changes(before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]) -> list[str]:
    created = [f"created {p}" for p in sorted(after.keys() - before.keys())]
    removed = [f"removed {p}" for p in sorted(before.keys() - after.keys())]
    changed = [f"changed {p}" for p in sorted(before.keys() & after.keys()) if before[p] != after[p]]
    return created + removed + changed


def engine_env(bundle: Path, home: Path) -> dict[str, str]:
    """The environment the shell gives a bundled engine, with state kept out of the real home."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH")}
    env.update(
        PYTHONPATH=str(bundle),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONNOUSERSITE="1",
        CODIFY_HOME=str(home),
        # The engine's parent watchdog (`engine/watchdog.py`): if this script dies, so does it.
        CODIFY_PARENT_PID=str(os.getpid()),
    )
    return env


def _wait_for_handshake(engine: subprocess.Popen[bytes], timeout_s: float) -> tuple[str, int]:
    if engine.stdout is None:
        raise SmokeError("the engine was started without a stdout to read its handshake from")
    deadline = time.monotonic() + timeout_s
    buffered = b""
    with selectors.DefaultSelector() as selector:
        selector.register(engine.stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            if not selector.select(timeout=max(0.0, deadline - time.monotonic())):
                break
            chunk = os.read(engine.stdout.fileno(), 65536)
            if not chunk:
                raise SmokeError(f"the engine exited with {engine.wait()} before its handshake")
            buffered += chunk
            *lines, buffered = buffered.split(b"\n")
            for line in lines:
                found = read_handshake(line.decode("utf-8", "replace"))
                if found is not None:
                    return found
    raise SmokeError(f"no handshake within {timeout_s:.0f}s")


# No proxy for the loopback engine, whatever the environment says: `urlopen` honours HTTP_PROXY,
# and a proxy cannot reach this machine's 127.0.0.1.
_LOOPBACK = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _health(port: int, token: str) -> int:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/health", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with _LOOPBACK.open(request, timeout=10) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError) as exc:
        raise SmokeError(f"GET /health on port {port} could not connect: {exc}") from exc


def _stop(engine: subprocess.Popen[bytes]) -> None:
    if engine.poll() is None:
        engine.send_signal(signal.SIGTERM)
        try:
            engine.wait(timeout=STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            engine.kill()
            engine.wait()
    if engine.stdout is not None:
        engine.stdout.close()


def smoke(bundle: Path, *, version: str | None = None, boot_timeout_s: float = BOOT_TIMEOUT_S) -> str:
    """Boot `bundle`'s engine and check it; returns a one-line account of the pass."""
    python = bundle / "python" / "bin" / "python3"
    if not python.is_file() or not (bundle / "engine" / "__main__.py").is_file():
        raise SmokeError(f"{bundle} is not an engine bundle (no python/bin/python3 and engine/__main__.py)")
    if version is not None:
        carried = json.loads((bundle / "BUNDLE.json").read_text(encoding="utf-8")).get("app_version")
        if carried != version:
            raise SmokeError(f"the bundle was built for {carried!r}, and this package is {version!r}")
    before = snapshot(bundle)
    with tempfile.TemporaryDirectory(prefix="smoke-bundle-home-") as home:
        # To a file, not a pipe: nobody reads stderr while waiting for the handshake, and an
        # engine that filled a pipe would block before printing it.
        log = Path(home) / "engine.stderr"
        with log.open("wb") as stderr:
            engine = subprocess.Popen(  # noqa: S603 — the bundle's own interpreter running `-m engine`, as the shell does; no shell
                [str(python), "-m", "engine"],
                cwd=bundle,
                env=engine_env(bundle, Path(home)),
                stdout=subprocess.PIPE,
                stderr=stderr,
            )
        try:
            token, port = _wait_for_handshake(engine, boot_timeout_s)
            status = _health(port, token)
        except SmokeError as exc:
            _stop(engine)
            lines = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            tail = "\n    ".join(lines[-15:]) or "(nothing)"
            raise SmokeError(f"{exc}; its stderr ended:\n    {tail}") from None
        finally:
            _stop(engine)
    if status != 200:
        raise SmokeError(f"GET /health answered {status}, not 200")
    written = changes(before, snapshot(bundle))
    if written:
        raise SmokeError("the engine wrote into its own bundle: " + "; ".join(written[:8]))
    return f"engine booted on port {port}, /health 200, the bundle unchanged"


def find_bundle(unpacked: Path) -> Path:
    """The one `engine-bundle` directory inside an unpacked package."""
    found = sorted(path.parent for path in unpacked.rglob("engine-bundle/BUNDLE.json"))
    if len(found) != 1:
        raise SmokeError(f"expected one engine-bundle in the package, found {len(found)}")
    return found[0]


def unpack_deb(deb: Path, dest: Path) -> Path:
    done = subprocess.run(  # noqa: S603 — fixed argv: dpkg-deb unpacking a package file into a temp directory; nothing installed
        ["dpkg-deb", "-x", str(deb), str(dest)],  # noqa: S607 — dpkg-deb resolves on PATH by design, as git does
        timeout=UNPACK_TIMEOUT_S,
        check=False,
    )
    if done.returncode != 0:
        raise SmokeError(f"dpkg-deb could not unpack {deb.name} (exit {done.returncode})")
    return dest


def unpack_appimage(appimage: Path, dest: Path) -> Path:
    copy = dest / appimage.name
    shutil.copyfile(appimage, copy)
    copy.chmod(0o755)
    done = subprocess.run(  # noqa: S603 — the AppImage's own runtime, asked only to extract itself into `dest` (`--appimage-extract` never starts the app)
        [str(copy), "--appimage-extract"], cwd=dest, timeout=UNPACK_TIMEOUT_S, check=False,
        stdout=subprocess.DEVNULL,
    )
    if done.returncode != 0:
        raise SmokeError(f"{appimage.name} could not extract itself (exit {done.returncode})")
    return dest / "squashfs-root"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("bundle", nargs="?", type=Path, help="a built bundle directory")
    what.add_argument("--deb", type=Path, help="a .deb to unpack and check")
    what.add_argument("--appimage", type=Path, help="an .AppImage to extract and check")
    parser.add_argument("--version", default=None, help="the version BUNDLE.json must say")
    args = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory(prefix="smoke-bundle-") as scratch:
            if args.deb is not None:
                bundle = find_bundle(unpack_deb(args.deb.resolve(), Path(scratch)))
            elif args.appimage is not None:
                bundle = find_bundle(unpack_appimage(args.appimage.resolve(), Path(scratch)))
            else:
                bundle = args.bundle.resolve()
            account = smoke(bundle, version=args.version)
    except SmokeError as exc:
        print(f"smoke-bundle: {exc}", file=sys.stderr)
        return 1
    print(f"smoke-bundle: {account}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
