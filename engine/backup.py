"""Consistent online backup and next-start restore for `codify.db`.

Rules enforced here:

- **Online backup uses SQLite's own backup API (`sqlite3.Connection.backup`).**
  Copying `codify.db` with `shutil.copy` while the engine is running in WAL mode
  races active writers and omits uncheckpointed `-wal` frames. `Connection.backup`
  takes a consistent snapshot safely alongside live readers and writers, then
  switches the snapshot copy to rollback journal mode so the backup is a single
  self-contained `.db` file with `0600` permissions.
- **Restore swaps in on the next start, never mid-process.** Every service on
  `app.state` holds `_PerThreadConnection` handles to the open database. Replacing
  the file underneath live handles splits state across old and new inodes. Instead,
  `POST /state/restore` validates the candidate SQLite file and stages it at
  `<db_path>.restore`; `apply_pending_restore` swaps it into `<db_path>` at the
  very start of `db.connect()`, unlinking any stale `-wal`/`-shm` files before any
  handle opens.
- **Credentials are excluded.** Provider keys live in the OS keyring (or the
  separate `secrets.json` fallback store) and are neither copied into backups nor
  altered by a restore.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import datetime as dt
import json
import os
import re
import secrets
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any

from engine import home
from engine.services import ApiError

SQLITE_MAGIC = b"SQLite format 3\x00"
MAX_RESTORE_UPLOAD_BYTES = 64 * 1024 * 1024  # 64 MiB ceiling for uploaded external .db files
REQUIRED_TABLES = frozenset({"workspaces", "goals", "agent_configs", "engine_settings"})
BACKUP_FILENAME_RE = re.compile(r"^codify-backup-[0-9]{8}-[0-9]{6}(?:-[0-9]+)?\.db$")


def resolve_db_path(conn: sqlite3.Connection | None = None) -> Path:
    """The path of `conn`'s main database file, falling back to `home.db_path()`."""
    if conn is not None:
        with contextlib.suppress(sqlite3.Error, KeyError, IndexError, TypeError):
            for row in conn.execute("PRAGMA database_list").fetchall():
                name = row["name"] if isinstance(row, sqlite3.Row) else row[1]
                file_col = row["file"] if isinstance(row, sqlite3.Row) else row[2]
                if name == "main" and file_col:
                    return Path(str(file_col))
    return home.db_path()


def backups_dir_for(db_path: Path) -> Path:
    return db_path.parent / "backups"


def staged_restore_path(db_path: Path) -> Path:
    return Path(f"{db_path}.restore")


def staged_meta_path(db_path: Path) -> Path:
    return Path(f"{db_path}.restore.json")


def _refuse_symlink(path: Path, label: str = "backup file") -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise ApiError(422, "invalid_backup", f"{label} must not be a symbolic link")


def _open_private_temp(directory: Path, prefix: str) -> Path:
    home.ensure_private_dir(directory)
    tmp = directory / f".{prefix}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    os.close(fd)
    return tmp


def validate_backup_file(path: Path, *, max_schema_version: int) -> None:
    """Verify `path` is an intact Codify SQLite database compatible with this build."""
    _refuse_symlink(path)
    try:
        st = os.stat(path)
    except OSError as exc:
        raise ApiError(422, "invalid_backup", f"backup file cannot be read: {exc}") from exc
    if not stat.S_ISREG(st.st_mode) or st.st_size < len(SQLITE_MAGIC):
        raise ApiError(422, "invalid_backup", "file is not a valid SQLite database")
    try:
        with path.open("rb") as fh:
            header = fh.read(len(SQLITE_MAGIC))
    except OSError as exc:
        raise ApiError(422, "invalid_backup", f"backup file cannot be read: {exc}") from exc
    if header != SQLITE_MAGIC:
        raise ApiError(422, "invalid_backup", "file does not have a SQLite 3 header")

    conn: sqlite3.Connection | None = None
    try:
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        stamped_row = conn.execute("PRAGMA user_version").fetchone()
        stamped = int(stamped_row[0]) if stamped_row else 0
        if stamped > max_schema_version:
            raise ApiError(
                409,
                "newer_schema",
                f"backup was written by a newer Codify (schema {stamped}; this build supports up to {max_schema_version})",
            )
        tables = {
            str(r[0])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        missing = sorted(REQUIRED_TABLES - tables)
        if missing:
            raise ApiError(
                422,
                "invalid_backup",
                f"SQLite file is missing Codify tables ({', '.join(missing)})",
            )
        check_row = conn.execute("PRAGMA quick_check(1)").fetchone()
        verdict = str(check_row[0]) if check_row else ""
        if verdict.lower() != "ok":
            raise ApiError(422, "invalid_backup", f"SQLite integrity check failed: {verdict}")
    except ApiError:
        raise
    except sqlite3.DatabaseError as exc:
        raise ApiError(422, "invalid_backup", f"corrupt or unreadable SQLite database: {exc}") from exc
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()


def _copy_via_sqlite_backup(src_path: Path, dst_path: Path) -> None:
    """Copy `src_path` into `dst_path` via `sqlite3.Connection.backup` as a single rollback-mode file."""
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    try:
        src.execute("PRAGMA busy_timeout = 5000")
        src.backup(dst)
        dst.execute("PRAGMA journal_mode = DELETE")
        dst.commit()
    finally:
        with contextlib.suppress(Exception):
            dst.close()
        with contextlib.suppress(Exception):
            src.close()
    for suffix in ("-wal", "-shm"):
        with contextlib.suppress(OSError):
            Path(f"{dst_path}{suffix}").unlink()
    home.tighten_to_owner(dst_path)


def _next_backup_filename(bdir: Path, now: float) -> str:
    stamp = dt.datetime.fromtimestamp(now, tz=dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = f"codify-backup-{stamp}.db"
    if not (bdir / base).exists():
        return base
    seq = 2
    while (bdir / f"codify-backup-{stamp}-{seq}.db").exists():
        seq += 1
    return f"codify-backup-{stamp}-{seq}.db"


def create_backup(db_path: Path) -> dict[str, Any]:
    """Write a consistent point-in-time copy of `db_path` into `<db_path.parent>/backups/`."""
    _refuse_symlink(db_path, "database file")
    if not db_path.is_file():
        raise ApiError(422, "invalid_backup", "database file does not exist yet")
    bdir = backups_dir_for(db_path)
    home.ensure_private_dir(bdir)
    now = time.time()
    filename = _next_backup_filename(bdir, now)
    target = bdir / filename
    tmp = _open_private_temp(bdir, "backup")
    try:
        _copy_via_sqlite_backup(db_path, tmp)
        os.replace(tmp, target)
        tmp = Path("")  # moved
    finally:
        if tmp and str(tmp) != "" and tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()
    home.tighten_to_owner(target)
    st = target.stat()
    return {
        "filename": filename,
        "path": str(target),
        "size_bytes": int(st.st_size),
        "created_at": float(st.st_mtime),
    }


def backup_file_path(db_path: Path, filename: str) -> Path:
    """Resolve a named backup inside `<db_path.parent>/backups/`, refusing traversal or symlinks."""
    clean = (filename or "").strip()
    if not BACKUP_FILENAME_RE.fullmatch(clean):
        raise ApiError(422, "invalid_backup", "invalid backup filename")
    target = backups_dir_for(db_path) / clean
    _refuse_symlink(target)
    if not target.is_file():
        raise ApiError(404, "unknown_backup", f"backup '{clean}' not found")
    return target


def list_backups(db_path: Path) -> list[dict[str, Any]]:
    bdir = backups_dir_for(db_path)
    if not bdir.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for child in bdir.iterdir():
        if not BACKUP_FILENAME_RE.fullmatch(child.name):
            continue
        try:
            st = os.lstat(child)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        entries.append({
            "filename": child.name,
            "path": str(child),
            "size_bytes": int(st.st_size),
            "created_at": float(st.st_mtime),
        })
    entries.sort(key=lambda item: (item["created_at"], item["filename"]), reverse=True)
    return entries


def pending_restore_info(db_path: Path) -> dict[str, Any] | None:
    staged = staged_restore_path(db_path)
    try:
        st = os.lstat(staged)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    source = staged.name
    staged_at = float(st.st_mtime)
    meta_path = staged_meta_path(db_path)
    with contextlib.suppress(OSError, ValueError, TypeError):
        meta_st = os.lstat(meta_path)
        if stat.S_ISREG(meta_st.st_mode):
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(meta, dict):
                if isinstance(meta.get("source"), str) and meta["source"].strip():
                    source = meta["source"].strip()
                if isinstance(meta.get("staged_at"), (int, float)):
                    staged_at = float(meta["staged_at"])
    return {
        "source": source,
        "size_bytes": int(st.st_size),
        "staged_at": staged_at,
    }


def status(db_path: Path) -> dict[str, Any]:
    db_bytes = 0
    with contextlib.suppress(OSError):
        if db_path.is_file():
            db_bytes = int(db_path.stat().st_size)
    return {
        "db_path": str(db_path),
        "db_bytes": db_bytes,
        "backups_dir": str(backups_dir_for(db_path)),
        "backups": list_backups(db_path),
        "pending_restore": pending_restore_info(db_path),
    }


def _write_staged_meta(db_path: Path, source: str, staged_at: float) -> None:
    meta_file = staged_meta_path(db_path)
    _refuse_symlink(meta_file, "restore metadata")
    tmp = _open_private_temp(db_path.parent, "restore-meta")
    try:
        tmp.write_text(
            json.dumps({"source": source, "staged_at": staged_at}, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(tmp, meta_file)
        home.tighten_to_owner(meta_file)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


def stage_restore_from_backup(
    db_path: Path, filename: str, *, max_schema_version: int,
) -> dict[str, Any]:
    """Validate a saved backup and stage it as `<db_path>.restore` for next engine start."""
    src_file = backup_file_path(db_path, filename)
    validate_backup_file(src_file, max_schema_version=max_schema_version)
    staged = staged_restore_path(db_path)
    _refuse_symlink(staged, "staged restore")
    tmp = _open_private_temp(db_path.parent, "restore")
    try:
        _copy_via_sqlite_backup(src_file, tmp)
        validate_backup_file(tmp, max_schema_version=max_schema_version)
        os.replace(tmp, staged)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()
    home.tighten_to_owner(staged)
    now = time.time()
    _write_staged_meta(db_path, filename, now)
    info = pending_restore_info(db_path)
    if info is None:
        raise ApiError(422, "invalid_backup", "staged restore file could not be verified")
    return info


def stage_restore_from_base64(
    db_path: Path,
    data_base64: str,
    source_name: str | None,
    *,
    max_schema_version: int,
) -> dict[str, Any]:
    """Decode, validate, and stage an uploaded SQLite backup file for next engine start."""
    try:
        raw = base64.b64decode(data_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ApiError(422, "invalid_backup", "uploaded backup is not valid base64 data") from exc
    if not raw:
        raise ApiError(422, "invalid_backup", "uploaded backup file is empty")
    if len(raw) > MAX_RESTORE_UPLOAD_BYTES:
        raise ApiError(
            422,
            "invalid_backup",
            f"uploaded backup exceeds {MAX_RESTORE_UPLOAD_BYTES // (1024 * 1024)} MiB limit",
        )
    if raw[: len(SQLITE_MAGIC)] != SQLITE_MAGIC:
        raise ApiError(422, "invalid_backup", "uploaded file does not have a SQLite 3 header")

    staged = staged_restore_path(db_path)
    _refuse_symlink(staged, "staged restore")
    raw_tmp = _open_private_temp(db_path.parent, "restore-upload")
    clean_tmp = _open_private_temp(db_path.parent, "restore-clean")
    try:
        raw_tmp.write_bytes(raw)
        validate_backup_file(raw_tmp, max_schema_version=max_schema_version)
        # Re-copy through SQLite's backup API so any WAL/journal state in the uploaded
        # file is normalized into a clean rollback-mode single file.
        _copy_via_sqlite_backup(raw_tmp, clean_tmp)
        validate_backup_file(clean_tmp, max_schema_version=max_schema_version)
        os.replace(clean_tmp, staged)
    finally:
        for candidate in (raw_tmp, clean_tmp):
            if candidate.exists():
                with contextlib.suppress(OSError):
                    candidate.unlink()
    home.tighten_to_owner(staged)
    label = os.path.basename((source_name or "").strip()) or "uploaded-backup.db"
    now = time.time()
    _write_staged_meta(db_path, label[:120], now)
    info = pending_restore_info(db_path)
    if info is None:
        raise ApiError(422, "invalid_backup", "staged restore file could not be verified")
    return info


def cancel_pending_restore(db_path: Path) -> bool:
    """Remove any staged `<db_path>.restore` before restart. Idempotent."""
    staged = staged_restore_path(db_path)
    meta = staged_meta_path(db_path)
    removed = False
    for target in (staged, meta):
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode):
            raise ApiError(422, "invalid_backup", "refusing to unlink symlink at restore path")
        with contextlib.suppress(FileNotFoundError):
            target.unlink()
            if target == staged:
                removed = True
    return removed


def apply_pending_restore(db_path: Path, *, max_schema_version: int) -> bool:
    """Swap `<db_path>.restore` into `db_path` before `db.connect` opens the store.

    Returns True when a staged backup was swapped in, False when none was pending
    or when the staged file failed validation (in which case the bad staged file is
    discarded and the existing `db_path` is left untouched so boot still succeeds).
    """
    staged = staged_restore_path(db_path)
    meta = staged_meta_path(db_path)
    try:
        st = os.lstat(staged)
    except FileNotFoundError:
        return False
    except OSError:
        return False

    if not stat.S_ISREG(st.st_mode):
        return False
    try:
        if db_path.exists() and stat.S_ISLNK(os.lstat(db_path).st_mode):
            return False
    except OSError:
        return False

    try:
        validate_backup_file(staged, max_schema_version=max_schema_version)
    except ApiError:
        with contextlib.suppress(OSError):
            staged.unlink()
        with contextlib.suppress(OSError):
            meta.unlink()
        return False

    # Critical: unlink any `-wal` and `-shm` sidecars belonging to the previous
    # database BEFORE replacing `db_path`, otherwise SQLite would replay old WAL
    # frames over the freshly restored database on open.
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_path}{suffix}")
        with contextlib.suppress(FileNotFoundError):
            if not stat.S_ISLNK(os.lstat(sidecar).st_mode):
                sidecar.unlink()

    os.replace(staged, db_path)
    home.tighten_to_owner(db_path)
    with contextlib.suppress(OSError):
        meta.unlink()
    return True
