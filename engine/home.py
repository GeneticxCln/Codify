"""Where this engine keeps its state — resolved in exactly one place.

Codify writes two things outside the project: the SQLite store and, when no OS
keyring is usable, the secrets file. Each used to resolve `~/.codify` on its own,
which made a "scratch" run only *half* hermetic. Pointing `CODIFY_DB` at a temp
directory still let a saved API key reach the developer's real OS keychain, and
`CODIFY_SECRETS` said nothing about the database — so a verification run could
believe it was isolated while writing a fake provider key into the real store. It
did exactly that during the model-menu work.

Precedence, narrowest first:

1. the store's own override — `CODIFY_DB`, `CODIFY_SECRETS`,
2. `CODIFY_HOME` — the whole state directory,
3. `~/.codify`.

A redirected run also stops using the OS keychain (`keyring_allowed`), because a
scratch run that can still read or write the real credential store is not a
scratch run. `CODIFY_DB` is deliberately excluded from that rule: relocating a
database says nothing about where credentials belong, and silently moving a
user's keys because they moved their database would be a surprise rather than a
safety feature. `startup_notice` states that half-redirected case instead of
leaving it implicit.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "CODIFY_HOME"
ENV_DB = "CODIFY_DB"
ENV_SECRETS = "CODIFY_SECRETS"
DEFAULT_DIR = ".codify"


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def codify_home() -> Path:
    """The state directory: `CODIFY_HOME` when set, else `~/.codify`."""
    override = _env(ENV_HOME)
    return Path(override).expanduser() if override else Path.home() / DEFAULT_DIR


def db_path() -> Path:
    """The SQLite store: `CODIFY_DB`, else inside the state directory."""
    override = _env(ENV_DB)
    return Path(override).expanduser() if override else codify_home() / "codify.db"


def secrets_path() -> Path:
    """The file backend for credentials: `CODIFY_SECRETS`, else inside the home."""
    override = _env(ENV_SECRETS)
    return Path(override).expanduser() if override else codify_home() / "secrets.json"


def is_isolated() -> bool:
    """True when this process was pointed away from the default state directory.

    Any redirection of *credentials* counts, because that is what isolation has to
    protect. `CODIFY_DB` alone does not — see the module docstring.
    """
    return bool(_env(ENV_HOME) or _env(ENV_SECRETS))


def keyring_allowed(explicit_secrets_path: bool = False) -> bool:
    """May this process use the OS keychain?

    Not when the store was redirected — `CODIFY_HOME`, `CODIFY_SECRETS`, or a path
    handed straight to `Keychain`. A run that names its own store is asking us to
    use *that* store, and it must not be able to touch the real one.
    """
    return not (explicit_secrets_path or is_isolated())


def startup_notice() -> str:
    """One line, for stderr: where this run's state actually lands.

    Printed at boot so an isolated run is visibly isolated, and so the
    half-redirected case — database moved, credentials not — is stated rather than
    discovered later by finding a test key in a real keychain.
    """
    where = f"database {db_path()}"
    if is_isolated():
        return (
            f"CODIFY_HOME {codify_home()} — isolated: {where}, credentials "
            f"{secrets_path()} (the OS keychain is left untouched)"
        )
    if _env(ENV_DB):
        return (
            f"warning: {ENV_DB} is set but {ENV_HOME} is not — the database is redirected "
            f"while credentials still resolve to the OS keychain and {secrets_path()}. "
            f"Set {ENV_HOME} to redirect both."
        )
    return f"state dir {codify_home()}: {where}"
