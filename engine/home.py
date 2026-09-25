"""Where this engine keeps its state — resolved in exactly one place.

Codify writes three things outside the project: the SQLite store, the loopback
boot token, and — when no OS keyring is usable — the secrets file. Each used to
resolve `~/.codify` on its own, which made a "scratch" run only *half* hermetic.
Pointing `CODIFY_DB` at a temp directory still let a saved API key reach the
developer's real OS keychain, and `CODIFY_SECRETS` said nothing about the
database — so a verification run could believe it was isolated while writing a
fake provider key into the real store. It did exactly that during the model-menu
work.

Precedence, narrowest first:

1. the store's own override — `CODIFY_DB`, `CODIFY_SECRETS`,
2. `CODIFY_HOME` — the whole state directory,
3. `~/.codify`.

The boot token follows the same directory, with one override of its own:
`CODIFY_BOOT_TOKEN` replaces the token's value rather than relocating it. The
token is state, and where a *default* lives is this module's business.

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
import secrets
from pathlib import Path

ENV_HOME = "CODIFY_HOME"
ENV_DB = "CODIFY_DB"
ENV_SECRETS = "CODIFY_SECRETS"
ENV_BOOT_TOKEN = "CODIFY_BOOT_TOKEN"  # noqa: S105 — a variable name, not a credential
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
    if override:
        if override == ":memory:":
            raise RuntimeError("CODIFY_DB=:memory: is not supported — every connection would get a different empty store")
        return Path(override).expanduser()
    return codify_home() / "codify.db"


def secrets_path() -> Path:
    """The file backend for credentials: `CODIFY_SECRETS`, else inside the home."""
    override = _env(ENV_SECRETS)
    return Path(override).expanduser() if override else codify_home() / "secrets.json"


def boot_token_path() -> Path:
    """The persisted boot token: inside the state directory, like the database.

    `CODIFY_BOOT_TOKEN` is not consulted here. That variable overrides the
    token's *value* (see `boot_token`); where a default lives is this module's
    job, and the token is state.
    """
    return codify_home() / "boot_token"


def boot_token() -> str:
    """The loopback bearer token, stable across restarts of this state dir.

    The engine announces `CODIFY_ENGINE token=…` on stdout and every request has
    to present it. A fresh token per boot meant that any client which cached one
    was rejected with 401 after every restart, and a client with no way to read
    the new handshake — a browser tab pointed at a dev engine, say — stayed
    broken until it was reloaded by hand. The desktop shell papers over this by
    re-reading the handshake from the live process over IPC; nothing can do that
    for a standalone tab. So the token is created once and kept beside the
    database.

    A stable token is a longer-lived credential than a per-boot one. That is
    affordable because the server only ever binds `127.0.0.1`: anything able to
    present this token could read this file, and the database next to it, without
    it. `CODIFY_BOOT_TOKEN` still overrides the value for a caller that wants a
    token scoped to a single process.

    Failing to persist is not fatal. A state directory that cannot be created or
    written must not stop the engine booting; that run gets a per-boot token,
    which is exactly what every run used to get.
    """
    path = boot_token_path()
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if existing:
        return existing
    fresh = secrets.token_hex(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # O_EXCL: two engines booting at once have to present one token, so
            # the loser adopts the winner's file rather than replacing it.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            adopted = path.read_text(encoding="utf-8").strip()
            if adopted:
                return adopted
            # An empty file is a boot that died between creating and filling
            # it. No client can have read one, so repairing it invalidates
            # nothing.
            fd = os.open(path, os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(fresh)
    except OSError:
        return fresh
    return fresh


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
