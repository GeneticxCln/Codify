"""What this engine process actually is, and what that interpreter can import.

Every optional capability in this project is installed into *an* environment, and
the engine runs in whichever one something happened to spawn. Those two are not
guaranteed to be the same place, and nothing raises when they differ — the feature
just quietly does not work. The Laya gate is the sharpest case: `pip install laya`
into `.venv` makes `make test` take the in-process SDK path, while the desktop
shell, resolving `python3` from the login shell's PATH, spawns `/usr/bin/python3`
and keeps paying an LLM call per goal for an SDK that is installed in the same
checkout. No error, no warning, no event — just a gate that is 600x slower than it
should be and costs tokens to run.

So the engine says what it is. `GET /settings/runtime` returns this report, the
boot log carries the same facts on stderr, and the mismatch the interpreter choice
can cause is the one thing reported as a warning rather than a fact.

Two rules shape the shape of the report:

**A fact and a verdict are different fields.** `importable` says whether this
interpreter can import `laya`; `disabled_by_env` says whether the gate was told not
to use it. Reporting one number for "is the SDK on" is how a UI ends up
contradicting itself, because the gate's own answer also depends on the environment.

**Only mismatches warn.** "The SDK is not installed" is the documented default
state and belongs in the gate card, which already says so. A warning that is
present on a fresh checkout is noise, and noise is what makes people stop reading
warnings. What is *not* a normal state is this checkout's own virtualenv existing
while the engine runs somewhere else — that is a configuration that cannot do what
the checkout implies, and it is the one this module exists to catch.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import platform
import sys
from pathlib import Path
from typing import Any

from engine.laya import SDK_DISABLE_ENV

# Mirrors `requires-python` in pyproject.toml, which is also ruff's target-version
# and mypy's `python_version`. Hardcoded rather than parsed out of the file: the
# engine has to answer this on a machine where pyproject.toml may not be next to it
# at all, and a check that cannot run is not a check. The three are edited together.
MIN_PYTHON = (3, 10)

# The one path this report and the shell agree on. `engine_protocol::engine_interpreter`
# in the Tauri shell picks exactly this interpreter when it exists, so the two halves
# are describing the same decision — if that path moves, it moves in both places.
VENV_RELATIVE = Path(".venv") / "bin" / "python3"


def project_root() -> Path:
    """The checkout this engine was imported from.

    From the module's own location, not the working directory: the shell sets cwd
    to the checkout, but `python -m engine` run from anywhere with PYTHONPATH set
    has no such guarantee, and a report that named the wrong root would put the
    virtualenv warning on a directory with no virtualenv in it.
    """
    return Path(__file__).resolve().parent.parent


def venv_interpreter(root: Path | None = None) -> Path:
    """Where the checkout's own interpreter would be, whether or not it exists."""
    return (root or project_root()) / VENV_RELATIVE


def interpreter_report() -> dict[str, Any]:
    """Which interpreter this process is, in the terms a person can act on.

    `executable` is the answer to "why is the SDK missing" — it is the thing that
    has to match wherever the package was installed. The venv fields are what say
    whether that interpreter belongs to this checkout.
    """
    in_venv = sys.prefix != sys.base_prefix
    return {
        "executable": sys.executable,
        "version": platform.python_version(),
        # Carried as numbers rather than re-parsed out of `version`: the floor
        # check below compares against these, and a comparison that reads the
        # string back is a comparison that can be wrong in a way nothing asserts.
        "version_info": [sys.version_info[0], sys.version_info[1]],
        "implementation": platform.python_implementation(),
        "in_virtualenv": in_venv,
        # The venv's own root, or the same as `base_prefix` when there is none.
        "prefix": sys.prefix,
        "base_prefix": sys.base_prefix,
    }


def laya_sdk_report() -> dict[str, Any]:
    """Whether *this* interpreter can import the gate's SDK, and why not if it can't.

    A real import rather than `find_spec`, because a package that is present but
    broken is the case worth reporting and a spec lookup calls it present. It is
    cheap: `laya/__init__` is ~55 ms and does not pull torch, which only arrives
    with `Router(preload=True)` — the same import `LayaService.sdk_available` makes
    on every gate decision anyway.

    `importable` is a property of the interpreter alone. Whether the gate will
    *use* it is `LayaService.status()`, which also honours `CODIFY_LAYA_SDK`.
    """
    import_error: str | None = None
    version: str | None = None
    try:
        importlib.import_module("laya")
    except Exception as exc:  # a broken install raises, and it is the reason we report
        import_error = f"{type(exc).__name__}: {exc}"
    else:
        try:
            version = importlib.metadata.version("laya")
        except importlib.metadata.PackageNotFoundError:  # pragma: no cover - importable but unlabelled
            version = None
    return {
        "importable": import_error is None,
        "import_error": import_error,
        "version": version,
        # Named for the variable it reads, so the fix is visible in the report.
        "disabled_by_env": os.environ.get(SDK_DISABLE_ENV, "").strip().lower()
        in ("0", "false", "no", "off"),
    }


def _warnings(
    interpreter: dict[str, Any], sdk: dict[str, Any], root: Path,
) -> list[str]:
    """The things that are wrong right now. Empty on a healthy install.

    Ordered worst first: a checkout that cannot use its own virtualenv is a
    configuration fault, an interpreter below the declared floor is a support
    question, and neither is the SDK simply being absent.
    """
    out: list[str] = []
    venv = venv_interpreter(root)
    if not sdk["importable"] and venv.is_file() and not interpreter["in_virtualenv"]:
        # The exact failure this module was written for, in the words of someone
        # holding the two facts they need: the interpreter that is running, and the
        # one the checkout would have used.
        out.append(
            f"The Laya SDK is not importable by this engine ({interpreter['executable']}), "
            f"but this checkout has its own interpreter at {venv}. Install the SDK into "
            f"that environment, or start the engine with it — otherwise the gate uses the "
            f"fallback model for every goal."
        )
    if tuple(interpreter["version_info"]) < MIN_PYTHON:
        required = ".".join(str(part) for part in MIN_PYTHON)
        out.append(
            f"This engine is running Python {interpreter['version']}; the project declares "
            f">={required}."
        )
    return out


def capability_report(root: Path | None = None) -> dict[str, Any]:
    """The whole self-check: what this process is, what it can import, what is wrong.

    One call, so the facts and the warnings cannot come from different moments — a
    report whose warning describes a different interpreter than the one it names is
    worse than no report.
    """
    resolved = root or project_root()
    interpreter = interpreter_report()
    sdk = laya_sdk_report()
    return {
        "interpreter": interpreter,
        "project_root": str(resolved),
        "checkout_interpreter": str(venv_interpreter(resolved)),
        "laya_sdk": sdk,
        "warnings": _warnings(interpreter, sdk, resolved),
    }


def startup_lines() -> list[str]:
    """The same report, flattened for the boot log.

    stderr, because the shell parses stdout for the handshake and a line of prose
    there would be read as a malformed boot. Written at startup rather than only on
    request so the facts are in `codify.log` before anyone thinks to open Settings.
    """
    report = capability_report()
    interpreter = report["interpreter"]
    where = interpreter["prefix"] if interpreter["in_virtualenv"] else "system Python"
    lines = [
        f"interpreter {interpreter['executable']} — Python "
        f"{interpreter['version']} ({interpreter['implementation']}, {where})"
    ]
    sdk = report["laya_sdk"]
    if sdk["importable"]:
        version = f" {sdk['version']}" if sdk["version"] else ""
        lines.append(f"laya SDK importable{version}")
    else:
        lines.append(f"laya SDK not importable — {sdk['import_error']}")
    if sdk["disabled_by_env"]:
        lines.append(f"laya SDK disabled by {SDK_DISABLE_ENV}")
    lines.extend(f"warning: {warning}" for warning in report["warnings"])
    return lines
