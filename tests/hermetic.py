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
outrank a default. Run the suite from the project root — `make test` does.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

from engine import home

_scratch: str | None = None


def activate() -> str:
    """Redirect state to a temp directory once. Returns the directory in force."""
    global _scratch
    if _scratch is not None:
        return _scratch

    if home.is_isolated() or os.environ.get(home.ENV_DB):
        # Already redirected by the caller — do not second-guess an explicit choice.
        _scratch = str(home.codify_home())
        return _scratch

    _scratch = tempfile.mkdtemp(prefix="codify-tests-")
    os.environ[home.ENV_HOME] = _scratch
    # Leaving a directory behind on every run is its own small mess.
    atexit.register(shutil.rmtree, _scratch, True)
    return _scratch


HOME = activate()
