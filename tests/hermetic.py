"""Point the whole suite at a throwaway state directory, whatever launches it.

Tests are scratch runs, and several build a bare `Keychain()` — exactly what the
engine does — so the suite used to resolve the developer's real `~/.codify`.
`test_provider_fallback` saves a role key to prove the fallback does not carry the
credential, and rewrote a real `secrets.json` on every `make test`. That is where
stray role keys in a developer's store came from.

`tests/__init__.py` is the obvious home for this, but it is *not* imported by
`unittest discover -s tests`, which loads each module as a top level module
(`test_api`, not `tests.test_api`). So every test module starts with one line:

    from tests import hermetic  # noqa: F401

Importing this module activates the redirect (idempotent), and it runs before the
engine is asked for any path. An externally set `CODIFY_HOME` / `CODIFY_DB` /
`CODIFY_SECRETS` is respected: CI, and a developer redirecting the store on purpose,
outrank a default. The two parent-pid variables are the exception and are always
cleared — see `disarm_parent_watchdogs`, which is the part of this that stops a
test process from being killed by a watchdog it inherited. So is `CODIFY_LAYA_SDK`:
the gate's optional SDK is pinned off on every run, because an install that is
recommended to developers must not change what the suite proves — see
`pin_laya_sdk_off`. Run the suite from the project root — `make test` does.
"""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import tempfile

from engine import home
from engine.laya import SDK_DEVICE_ENV, SDK_DISABLE_ENV, SDK_TIMEOUT_ENV, SDK_WARM_ENV
from engine.spawn_guard import ENV_PARENT_PID as ENV_SANDBOX_PARENT_PID
from engine.watchdog import ENV_PARENT_PID

_scratch: str | None = None


class _DropSlowCallbackNotes(logging.Filter):
    """Drop asyncio's "Executing <Task ...> took 0.130 seconds" warnings, and only those.

    `IsolatedAsyncioTestCase` runs every test on a loop in debug mode, and debug mode logs any step that takes
    over 100 ms. Under a loaded machine that is most `asyncSetUp`s: about three hundred lines per run, each a
    page-wide task repr, which buried the one line a failed run needs. The rest of debug mode stays on and
    stays loud (a coroutine that was never awaited, a callback that raised); a slow step is not a failure of
    anything these tests assert.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not (message.startswith("Executing ") and " took " in message and message.endswith(" seconds"))


def activate() -> str:
    """Redirect state to a temp directory once. Returns the directory in force."""
    global _scratch
    if _scratch is not None:
        return _scratch

    # Before either branch below, and on every run: an inherited parent pid is
    # the one inherited value that can kill this process.
    disarm_parent_watchdogs()
    pin_laya_sdk_off()
    quiet_slow_callback_notes()

    if home.is_isolated() or os.environ.get(home.ENV_DB):
        # Already redirected by the caller — do not second-guess an explicit choice.
        _scratch = str(home.codify_home())
        return _scratch

    _scratch = tempfile.mkdtemp(prefix="codify-tests-")
    os.environ[home.ENV_HOME] = _scratch
    # Leaving a directory behind on every run is its own small mess.
    atexit.register(shutil.rmtree, _scratch, True)
    return _scratch


def disarm_parent_watchdogs() -> None:
    """Clear the two variables that make a process watch a parent and act on it.

    This is the one part of the redirect that is not about *where state lands*,
    and it is here because of what it prevents. `engine.app.serve` arms the
    parent watchdog from the environment, with no code call and nothing to opt
    out of: a test that boots the engine inherits whatever `CODIFY_PARENT_PID`
    the developer's shell happens to carry. When that pid is gone — or was never
    this process's parent at all — `watchdog.terminate` runs, and it arms the hard
    deadline and sends this process SIGTERM. A green suite would end, mid-file, at
    a signal, with an exit status that says nothing useful.

    The same applies to the sandbox guard's copy: a stale `CODIFY_SANDBOX_PARENT_PID`
    makes a guard started by a test believe it outlived its engine.

    Both are cleared rather than honoured, unlike `CODIFY_HOME` above, because
    there is no reading of "a developer deliberately armed the test runner's
    watchdog" that anyone wants. A test that wants the watchdog asks for it by
    name — `tests/test_watchdog.py` passes the pid explicitly.
    """
    for name in (ENV_PARENT_PID, ENV_SANDBOX_PARENT_PID):
        os.environ.pop(name, None)


def quiet_slow_callback_notes() -> None:
    """Put `_DropSlowCallbackNotes` on asyncio's logger, once however many modules import this."""
    logger = logging.getLogger("asyncio")
    if not any(isinstance(f, _DropSlowCallbackNotes) for f in logger.filters):
        logger.addFilter(_DropSlowCallbackNotes())


def pin_laya_sdk_off() -> None:
    """Force the gate's LLM fallback for every test, whatever the host installed.

    The Laya SDK is optional by design, and that is exactly what makes it a
    hazard here. A developer who follows the install advice and runs
    `pip install laya` gets torch, a GPU stack and a `Router` that downloads
    checkpoints and preloads them — inside `make test`, on the first gate
    decision. Before this pin, that install silently rewrote what the suite
    proved: the fallback tests saw `engine == "sdk"`, the skip tests saw a
    verdict, and `test_executor` spent its budget on checkpoint fetches and
    CUDA OOM messages instead of the orchestration it is there to check.

    So the dependency on the host's optional packages is taken the same way the
    watchdog variables are: removed, not honoured. A test that wants the SDK
    path asks for it by name — `tests/test_laya.py` installs a fake `laya` module
    and passes `disabled=False` — and one test asserts this pin, so it cannot be
    dropped by someone tidying the environment.
    """
    os.environ[SDK_DISABLE_ENV] = "0"
    # The two knobs that shape how the SDK would run are removed for the same
    # reason: `CODIFY_LAYA_DEVICE=cpu` in a developer's shell would hand a fake
    # `Router` a `device` argument it was never written to take, and a short
    # `CODIFY_LAYA_TIMEOUT_S` would turn a slow test machine into a skipped gate.
    for name in (SDK_DEVICE_ENV, SDK_TIMEOUT_ENV, SDK_WARM_ENV):
        os.environ.pop(name, None)


HOME = activate()
