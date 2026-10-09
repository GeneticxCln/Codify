from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, get_args
from collections.abc import Callable

import sqlite3

from engine.db import dumps, row_to_dict
from engine.fs import (
    BINARY_SNIFF_BYTES,
    MAX_EDIT_BYTES,
    FileAccessError,
    FileChangedError,
    FileNotTextError,
    FileSystemService,
    FileTooLargeError,
    NotAFileError,
    PathEscapeError,
    ProtectedRootError,
    looks_binary,
    protected_root_reason,
)
from engine.library import LibraryService
from engine.models import (
    BUILTIN_PROVIDERS,
    PAUSE_CODES,
    PROVIDER_SLUG_RE,
    ROLES,
    AgentConfig,
    AgentConfigUpdate,
    AgentRole,
    Conversation,
    ConversationCreate,
    ConversationTurn,
    ConversationUpdate,
    Event,
    Goal,
    GoalConversationUpdate,
    GoalCreate,
    GoalStatus,
    PlanStep,
    ShellTab,
    ShellTabWrite,
    TurnCreate,
    Workspace,
    WorkspaceFile,
    WorkspaceFileList,
    WorkspaceFileSaved,
    WorkspaceCreate,
)
from engine.providers import (
    BaseProvider,
    Keychain,
    ProviderError,
    ProviderFactory,
    key_destination_problem,
    validate_local_base_url,
)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: dict[str, Any] | None = None):
        # str(exc) must be the message: goal failures, logs, and error cards all
        # stringify the exception, and without super().__init__ every one of them
        # rendered as an empty string with the real message buried in attributes.
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}


class SettingsService:
    """Engine-wide settings persisted in the store (`engine_settings` key/value).

    Deliberately tiny and stringly: one row per key, values validated by the
    caller (each key owns its clamp), so adding a setting never needs a
    migration. Reads are unlogged and cheap — the executor asks on every
    parallel batch.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._db = conn

    # Every known key with its (default, clamp). Anything read must be listed
    # here: an unknown key is not a setting, it is a typo.
    SPEC: dict[str, tuple[int, Callable[[int], int]]] = {
        # How many steps of a parallel goal may run at once (see
        # executor.DEFAULT_PARALLEL_WIDTH). Clamped to a band a machine can take.
        "parallel_width": (4, lambda v: max(1, min(v, 16))),
        # How many daily stats snapshots to keep (stats_snapshots). 0 keeps
        # everything — the honest default for a machine with disk to spare;
        # the upper bound exists so a fat-fingered 999999 cannot be stored as
        # "effectively forever" when the user meant bounded.
        "stats_retention_days": (90, lambda v: max(0, min(v, 730))),
        # How many days a goal's recording is kept (trace_calls). A recording
        # is a copy of the model's output about the user's code, so the default
        # is bounded rather than "forever"; 0 keeps everything for someone
        # deliberately collecting replays. Same 730 ceiling as snapshots, for
        # the same fat-finger reason.
        "trace_retention_days": (30, lambda v: max(0, min(v, 730))),
        # How many tool-calling turns one conductor run may take before the
        # engine stops it and answers with what it has. Bounded because a model
        # that has lost the thread will call tools forever, and an unbounded
        # loop is a way to spend a key and a machine's time on nothing.
        "conductor_max_turns": (14, lambda v: max(1, min(v, 40))),
        # How many *stage moves* one conductor run may make (recon, design, plan,
        # write, verify, review, summarize). A separate budget because the two
        # are not the same currency: a model call costs seconds, while a stage
        # move is a whole sub-agent run that can take minutes and, for `write`,
        # touch files. Counting them together would let eight cheap file reads
        # starve the change the user actually asked for.
        "conductor_max_moves": (12, lambda v: max(0, min(v, 60))),
        # Whether an approved plan is executed by the conductor or by the
        # engine's own sequence. 1 leaves the conductor driving, which is the
        # point of having one; 0 is the escape hatch for a model that is not yet
        # good enough to be trusted with the order, and it needs no rebuild.
        "conductor_drives_execution": (1, lambda v: 1 if v else 0),
        # Read each answer aloud as it arrives (Settings → Audio). Off unless asked for.
        "tts_auto_read": (0, lambda v: 1 if v else 0),
        # Whether the conductor's `fetch_page` may read the web: 0 not at all, 1 only the sites in
        # `web_fetch_hosts`, 2 any public site. 2 on a fresh install: Codify runs on local models, and an
        # assistant that cannot look something up is the one that is held back. What that costs is that the
        # address it asks for leaves the machine from the engine itself and can carry what a turn has read
        # (engine/web_fetch.py, docs/12, docs/03 §1.6); every request is announced in the transcript before it
        # is made, and a person narrows it to a list or switches it off in Settings. Only a person writes
        # this, never a goal or a turn (docs/00 §6.2). An install that already stored a choice keeps it.
        "web_fetch": (2, lambda v: max(0, min(v, 2))),
    }

    # The conductor's model, and why it is here rather than in `agent_configs`:
    # `agent_configs` is keyed by role and iterated by `config_problems`, the
    # preflight and the Settings screen, so a row in it *is* a ninth AgentRole
    # and docs/00 §6.1 forbids that. The conductor is a loop, not a stage. These
    # keys are where it is configured instead. Empty means "not chosen", which
    # the caller resolves to the scribe's configuration — the one role whose job
    # is already writing prose for a person.
    #
    # The fallback pair is the same argument one level down: a conductor whose
    # only model is down has no answer to give at all, and the roles have had a
    # fallback target since before the conductor existed. Two keys rather than
    # one, because a fallback with a provider and no model is the same inert
    # half-pair the primary would be.
    STRING_SPEC: dict[str, str] = {
        "conductor_provider": "",
        "conductor_model": "",
        "conductor_fallback_provider": "",
        "conductor_fallback_model": "",
        # Voice (engine/speech.py). Empty means "not chosen", and a feature with an
        # empty provider or model says so rather than guessing one.
        "stt_provider": "",
        "stt_model": "",
        "stt_language": "",
        "tts_provider": "",
        "tts_model": "",
        "tts_voice": "",
        "audio_input": "",
        "stt_base_url": "",
        "tts_base_url": "",
        # The sites `fetch_page` may read when `web_fetch` is 1: site names, comma-separated, each one
        # covering its subdomains. Normalised on the way in (engine/web_fetch.py `parse_hosts`), so what
        # is stored is what the fetch reads.
        "web_fetch_hosts": "",
    }

    def get_str(self, key: str) -> str:
        """A known string key's value, stripped. The key must exist here."""
        if key not in self.STRING_SPEC:
            raise KeyError(key)
        row = self._db.execute(
            "SELECT value FROM engine_settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return self.STRING_SPEC[key]
        return str(row["value"]).strip()

    def set_str(self, key: str, value: str) -> str:
        if key not in self.STRING_SPEC:
            raise ApiError(400, "unknown_setting", f"unknown engine setting: {key}")
        clean = str(value).strip()[:200]
        self._db.execute(
            """INSERT INTO engine_settings (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                             updated_at = excluded.updated_at""",
            (key, clean, time.time()),
        )
        self._db.commit()
        return clean

    def get_int(self, key: str) -> int:
        """A known key's value, clamped — the key must exist in SPEC."""
        default, clamp = self.SPEC[key]
        row = self._db.execute(
            "SELECT value FROM engine_settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return default
        try:
            return clamp(int(str(row["value"]).strip()))
        except (TypeError, ValueError):
            # A value that cannot parse is treated as unset, never as a crash:
            # the settings screen wrote it, so a bad save must not wedge goals.
            return default

    def set_int(self, key: str, value: int) -> int:
        if key not in self.SPEC:
            raise ApiError(400, "unknown_setting", f"unknown engine setting: {key}")
        default, clamp = self.SPEC[key]
        clamped = clamp(int(value))
        self._db.execute(
            """INSERT INTO engine_settings (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
            (key, str(clamped), time.time()),
        )
        self._db.commit()
        return clamped


def custom_provider_address(
    provider: str, rows: list[AgentConfig]
) -> tuple[str, str, str | None] | None:
    """Where a custom provider slug points: `(protocol, base_url, api_key_ref)`, or None.

    A built-in slug is explained by the catalogue; a custom one is only a label, and its endpoint,
    protocol and credential live on the role row (or fallback columns) that introduced it. Anything
    else that names a provider without holding an address of its own (the conductor's pair, a speech
    setting) means that row's address, whichever role holds it. The credential reference comes along
    only from a primary row, where it belongs to the provider being named; a fallback column carries
    none.
    """
    for row in rows:
        if row.provider == provider and row.base_url:
            return row.protocol, row.base_url, row.api_key_ref
    for row in rows:
        if row.fallback_provider == provider and row.fallback_base_url:
            return row.fallback_protocol or "openai_compat", row.fallback_base_url, None
    return None


class AgentRegistryService:
    def __init__(
        self, conn: sqlite3.Connection, factory: ProviderFactory, keychain: Keychain
    ) -> None:
        self._db = conn
        self._factory = factory
        self._keychain = keychain

    def list_configs(self) -> list[AgentConfig]:
        rows = {
            r["role"]: AgentConfig.model_validate(row_to_dict(r))
            for r in self._db.execute("SELECT * FROM agent_configs")
        }
        return [rows[role] for role in ROLES if role in rows]

    def provider_key_status(self) -> dict[str, dict[str, Any]]:
        """Each known provider: does it need a credential, and is one stored.

        Shaped for `engine/role_repair.py`, which is the only consumer. It
        answers the "needs a credential" half from the provider's own
        declaration rather than from a guess, because a custom slug with no
        catalog entry would otherwise be assumed to need a key it does not.
        """
        status = {
            slug: {
                "needs_key": bool(meta["needs_key"]),
                "has_key": self._keychain.has_provider_key(slug),
            }
            for slug, meta in BUILTIN_PROVIDERS.items()
        }
        # A custom provider is any slug a role points at (either target) that the catalogue does not list.
        # It has no declaration of whether it needs a key, so the protocol answers, as everywhere else
        # (`role_repair.target_needs_key`); whether one is stored is a plain question of the keychain.
        for cfg in self.list_configs():
            for slug, protocol in ((cfg.provider, cfg.protocol), (cfg.fallback_provider, cfg.fallback_protocol)):
                if slug and slug not in status:
                    status[slug] = {
                        "needs_key": (protocol or "openai_compat") != "ollama",
                        "has_key": self._keychain.has_provider_key(slug),
                    }
        return status

    def configs_with_key_state(self) -> list[dict[str, Any]]:
        """The role rows as dicts, each carrying `has_role_key`: whether the role's own credential resolves.

        The one place that answers it, so the goal preflight and the Repair endpoint cannot disagree. A
        custom provider's key is stored for the role (`api_key_ref`), not under the provider's name, and
        judging a role from the provider table alone called every such role uncallable.
        """
        rows: list[dict[str, Any]] = []
        for cfg in self.list_configs():
            row = cfg.model_dump()
            row["has_role_key"] = bool(cfg.api_key_ref and self._keychain.get(cfg.api_key_ref))
            rows.append(row)
        return rows

    def get_config(self, role: str) -> AgentConfig:
        if role not in ROLES:
            raise ApiError(404, "unknown_role", f"Unknown agent role {role}")
        row = self._db.execute("SELECT * FROM agent_configs WHERE role = ?", (role,)).fetchone()
        if row is None:
            raise ApiError(404, "unknown_role", f"Unknown agent role {role}")
        return AgentConfig.model_validate(row_to_dict(row))

    @staticmethod
    def _normalize_target(
        data: dict[str, Any],
        updates: dict[str, Any],
        *,
        provider_field: str,
        protocol_field: str,
        base_url_field: str,
        previous_provider: str | None,
    ) -> None:
        """Resolve a (provider, protocol, base_url) triple from the provider catalog.

        One implementation for both targets a role can be called on, because the
        rules are not obvious and a second copy would drift: a provider is not a
        wire format (`anthropic` reached through an OpenAI-compatible proxy is
        legitimate), and a provider switch must not carry the previous provider's
        endpoint over with it — that sent one wire format to the other's socket.
        """
        provider = data[provider_field]
        builtin = BUILTIN_PROVIDERS.get(provider)
        protocol_given = bool(updates.get(protocol_field))
        provider_in_patch = provider_field in updates
        provider_switched = provider_in_patch and provider != previous_provider
        if builtin:
            if protocol_given:
                # An explicit protocol in the patch always wins.
                data[protocol_field] = updates[protocol_field]
            elif provider_in_patch:
                # Provider given and no protocol: inherit the NEW provider's
                # default. Keeping the old protocol here silently sent e.g.
                # Anthropic wire format to an Ollama endpoint.
                data[protocol_field] = builtin["protocol"]
            else:
                data[protocol_field] = data.get(protocol_field) or builtin["protocol"]
            if updates.get(base_url_field):
                pass  # a non-empty endpoint in the patch always wins
            elif provider_switched or not data.get(base_url_field):
                data[base_url_field] = builtin["base_url"]
            if builtin["local_only"]:
                try:
                    validate_local_base_url(data[base_url_field])
                except ProviderError as exc:
                    raise ApiError(400, exc.code, exc.message) from exc
        else:
            if not data.get(protocol_field) or not data.get(base_url_field):
                raise ApiError(400, "invalid_provider", "custom provider requires protocol and base_url")
            if data[protocol_field] == "ollama":
                try:
                    validate_local_base_url(data[base_url_field])
                except ProviderError as exc:
                    raise ApiError(400, exc.code, exc.message) from exc

    def set_config(self, role: str, patch: AgentConfigUpdate) -> AgentConfig:
        cfg = self.get_config(role)
        data = cfg.model_dump()
        updates = patch.model_dump(exclude_unset=True)
        raw_key = updates.pop("api_key", None)
        if "system_prompt_override" in updates and updates["system_prompt_override"] is not None:
            text = updates["system_prompt_override"].strip()
            if len(text) > 32768:
                raise ApiError(400, "prompt_too_long", "system_prompt_override exceeds 32768 characters")
            updates["system_prompt_override"] = text or None
        for field in ("provider", "fallback_provider"):
            if updates.get(field) and not PROVIDER_SLUG_RE.match(updates[field]):
                raise ApiError(400, "invalid_provider", "provider slug is invalid")
        # Nullable fields where an explicit null is meaningful: it clears the
        # field. Everything else treats null as "no change". `fallback_provider`
        # is null-ed to remove a fallback, and doing that must also drop the
        # protocol and endpoint that belonged to it.
        CLEARABLE = (
            "system_prompt_override", "base_url",
            "fallback_provider", "fallback_protocol", "fallback_base_url",
            # Sending null must mean "go back to the provider's default window",
            # not "keep whatever was stored": the UI writes null to clear it.
            "num_ctx",
            # Same for the residency window — "unset" is a real setting, and a
            # user who cleared the field wants Ollama's own five minutes back.
            "keep_alive",
        )
        data.update({k: v for k, v in updates.items() if v is not None or k in CLEARABLE})
        if not (data.get("fallback_provider") or "").strip():
            # Removed, not half-configured: the endpoint and model that belonged
            # to the old target go with it, or a later provider switch would
            # inherit a URL nothing points at.
            data["fallback_provider"] = None
            data["fallback_protocol"] = None
            data["fallback_base_url"] = None
            data["fallback_model_name"] = ""
        self._normalize_target(
            data, updates,
            provider_field="provider", protocol_field="protocol", base_url_field="base_url",
            previous_provider=cfg.provider,
        )
        if data.get("fallback_provider"):
            self._normalize_target(
                data, updates,
                provider_field="fallback_provider",
                protocol_field="fallback_protocol",
                base_url_field="fallback_base_url",
                previous_provider=cfg.fallback_provider,
            )
        # A key is never saved toward, or pointed at, a destination that would send it in the
        # clear — and this runs *before* the key is stored, so a refused save leaves no secret
        # behind. Only refused when there is a key: a keyless server on the LAN is a real setup
        # and has nothing to protect. `ProviderFactory.build` holds the same rule when a
        # request is made, so this is the early, readable half of it.
        switched = data.get("provider") != cfg.provider
        for label, protocol, base_url, key in (
            (
                "base_url", data.get("protocol"), data.get("base_url"),
                raw_key
                # A role key does not follow a provider switch (see below), so it is not in play.
                or ("" if switched else self._keychain.get(data.get("api_key_ref")))
                or self._keychain.get_provider_key(data["provider"]),
            ),
            (
                "fallback_base_url", data.get("fallback_protocol"), data.get("fallback_base_url"),
                self._keychain.get_provider_key(data["fallback_provider"]) if data.get("fallback_provider") else "",
            ),
        ):
            if key and protocol != "ollama" and base_url:
                problem = key_destination_problem(base_url)
                if problem is not None:
                    raise ApiError(400, "invalid_base_url", f"{label}: {problem}")
        if raw_key:
            try:
                data["api_key_ref"] = self._keychain.set(role, raw_key)
            except ProviderError as exc:
                raise ApiError(400, exc.code, exc.message) from exc
        elif data.get("provider") != cfg.provider and data.get("api_key_ref"):
            # A provider switch must not carry the previous provider's credential
            # over with it — same rule as `_normalize_target` applies to the
            # endpoint. `api_key_ref` was stored under the OLD provider; keeping
            # it would hand that key to the new provider's endpoint (a live
            # credential leak, e.g. an Anthropic key POSTed to OpenAI). An
            # explicit `api_key` in the same patch wins: the branch above ran
            # first and re-pointed the ref at a key the user just supplied.
            data["api_key_ref"] = None
        data["updated_at"] = time.time()
        try:
            merged = AgentConfig.model_validate(data)
        except Exception as exc:
            raise ApiError(422, "invalid_config", f"merged agent config is invalid: {exc}") from exc
        self._db.execute(
            """UPDATE agent_configs SET display_name=?, provider=?, protocol=?, model_name=?,
               api_key_ref=?, base_url=?, system_prompt_override=?, temperature=?, max_tokens=?,
               fallback_provider=?, fallback_model_name=?, fallback_protocol=?, fallback_base_url=?,
               num_ctx=?, keep_alive=?, updated_at=?
               WHERE role=?""",
            (
                merged.display_name, merged.provider, merged.protocol, merged.model_name,
                merged.api_key_ref, merged.base_url, merged.system_prompt_override,
                merged.temperature, merged.max_tokens,
                merged.fallback_provider, merged.fallback_model_name,
                merged.fallback_protocol, merged.fallback_base_url,
                merged.num_ctx, merged.keep_alive, merged.updated_at, role,
            ),
        )
        self._db.commit()
        return merged

    def get_provider_for(self, role: AgentRole) -> tuple[BaseProvider, AgentConfig]:
        cfg = self.get_config(role)
        return self._factory.build(cfg), cfg

    def build_provider(self, cfg: AgentConfig) -> BaseProvider:
        """Build a provider from a config that may not be the stored primary one.

        The fallback is the same role with a different target, so it needs the same
        constructor — through the registry, so both go through the one factory.
        """
        return self._factory.build(cfg)

    def fallback_config_for(self, cfg: AgentConfig) -> AgentConfig | None:
        """The role's config as it would look on its fallback target.

        A fallback is the same role with a different `provider`/`model_name`, so it
        is built from the same config with those three fields swapped — one code
        path for credentials, protocol, and endpoint, rather than a second
        `AgentConfig` constructor that could disagree with the first. The role's
        key is deliberately not carried over: `api_key_ref` belongs to the primary
        provider, and `ProviderFactory` would then look up a key for the fallback
        under the primary's ref. Temperature and max tokens stay the role's own,
        because they describe the job, not the model answering it.
        """
        if not cfg.has_fallback:
            return None
        return cfg.model_copy(update={
            "provider": cfg.fallback_provider,
            "protocol": cfg.fallback_protocol or "openai_compat",
            "model_name": cfg.fallback_model_name,
            "base_url": cfg.fallback_base_url,
            "api_key_ref": None,
        })

    def provider_catalog(self) -> dict[str, Any]:
        used = {c.provider for c in self.list_configs()}
        return {
            "builtins": [
                {"slug": slug, **meta} for slug, meta in BUILTIN_PROVIDERS.items()
            ],
            "custom": sorted(p for p in used if p not in BUILTIN_PROVIDERS),
        }


# A goal that reaches one of these stays there — except by the moves in `REOPENED_BY`, which are
# `POST /goals/{id}/apply` (a completed dry run, or a failed one) and the step retry, both of which
# re-open a finished goal as RUNNING. Nothing re-opens CANCELLED: retry, apply and start all refuse it.
TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
REOPENED_BY: dict[str, frozenset[str]] = {
    "COMPLETED": frozenset({"RUNNING"}),
    "FAILED": frozenset({"RUNNING"}),
}


def may_change_status(current: str, target: str) -> bool:
    if current not in TERMINAL_STATUSES or target == current:
        return True
    return target in REOPENED_BY.get(current, frozenset())


class WorkspaceService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._db = conn

    def create(self, body: WorkspaceCreate) -> Workspace:
        root = str(Path(body.root_path).expanduser().resolve())
        if not Path(root).is_dir():
            raise ApiError(400, "invalid_root", "root_path must be an existing directory")
        # `/` (or any filesystem ancestor of everything) makes every
        # path-containment check vacuous: nothing can escape a root that
        # contains the whole machine. A workspace must be a directory the user
        # deliberately chose, and no legitimate project IS the filesystem root.
        if Path(root).parent == Path(root):
            raise ApiError(
                400, "invalid_root",
                "root_path refuses to be the filesystem root — a workspace that "
                "contains every path makes containment checks meaningless",
            )
        protected = protected_root_reason(Path(root))
        if protected is not None:
            raise ApiError(400, "invalid_root", f"root_path {root} is not accepted: {protected}")
        ws = Workspace(id=str(uuid.uuid4()), name=body.name, root_path=root, created_at=time.time())
        try:
            self._db.execute(
                "INSERT INTO workspaces (id, name, root_path, created_at) VALUES (?, ?, ?, ?)",
                (ws.id, ws.name, ws.root_path, ws.created_at),
            )
            self._db.commit()
        except Exception as exc:
            if "UNIQUE constraint failed: workspaces.root_path" in str(exc):
                raise ApiError(409, "duplicate_workspace", f"workspace already exists for {root}") from exc
            raise ApiError(409, "duplicate_workspace", "workspace could not be created (duplicate id or path)") from exc
        return ws

    def list_workspaces(self) -> list[Workspace]:
        return [Workspace.model_validate(row_to_dict(r)) for r in self._db.execute("SELECT * FROM workspaces")]

    def get(self, workspace_id: str) -> Workspace:
        row = self._db.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        if row is None:
            raise ApiError(404, "unknown_workspace", "workspace not found")
        return Workspace.model_validate(row_to_dict(row))

    def goal_count(self, workspace_id: str) -> int:
        """How many goals point at this workspace — the cost of deleting it."""
        row = self._db.execute(
            "SELECT count(*) FROM goals WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    def set_design_contract(self, workspace_id: str, path: str) -> Workspace:
        """Pin the brand contract the design agent must obey, or clear it with "".

        Validated here rather than at read time because this is a settings action
        with a user watching: an escape is refused, and a path that is not a
        readable text file is refused, while the screen is still open. A pin that
        only failed mid-goal would surface as a mysterious absence of brand, in a
        transcript nowhere near the setting that caused it.

        The directory is never modified — pinning reads the file once to prove it
        is usable, and the engine re-reads it per goal.
        """
        workspace = self.get(workspace_id)  # 404 if unknown
        relative = (path or "").strip()
        if relative:
            fs = FileSystemService(workspace.root_path)
            try:
                target = fs.resolve(relative)
            except PathEscapeError as exc:
                raise ApiError(
                    400, "design_contract_escape",
                    f"{relative!r} resolves outside this workspace — a brand contract "
                    f"must be a file inside {workspace.root_path}",
                ) from exc
            if not target.is_file():
                raise ApiError(
                    400, "design_contract_missing",
                    f"no file at {relative!r} in this workspace — pin a contract that exists",
                )
            try:
                sniff = target.read_bytes()[:BINARY_SNIFF_BYTES]
            except OSError as exc:
                raise ApiError(
                    400, "design_contract_unreadable",
                    f"{relative!r} could not be read: {exc}",
                ) from exc
            if looks_binary(sniff):
                raise ApiError(
                    400, "design_contract_binary",
                    f"{relative!r} is not text — a brand contract must be readable as text",
                )
        self._db.execute(
            "UPDATE workspaces SET design_contract_path = ? WHERE id = ?",
            (relative, workspace_id),
        )
        self._db.commit()
        return self.get(workspace_id)

    # ── the editor's files ─────────────────────────────────────────────────────
    #
    # Three methods, and the only ones that read a workspace file for a *person* rather than a model. `save_file` is
    # the person's Save: the one writer besides the fixer (docs/00 §6.9), reached from exactly one route and named by
    # no agent-side module (`test_invariants_at_their_boundary.TestAPersonsSaveIsTheOneOtherDoor`).

    # They take the `Workspace`, not its id, so the route can look the workspace up on the event loop (the connection is
    # not shared across threads) and then hand the file work, which can be a walk of ten thousand files or a megabyte
    # read, to a worker thread without a database call inside it.

    def list_files(self, workspace: Workspace, limit: int) -> WorkspaceFileList:
        found = LibraryService(workspace.root_path).list_files(limit)
        return WorkspaceFileList(files=found["files"], truncated=found["truncated"], limit=found["limit"])

    def read_file(self, workspace: Workspace, path: str) -> WorkspaceFile:
        try:
            got = FileSystemService(workspace.root_path).read_editable(path)
        except (PathEscapeError, NotAFileError, FileTooLargeError, FileNotTextError, FileAccessError) as exc:
            raise self._file_refusal(exc, path) from exc
        return WorkspaceFile(path=got.path, content=got.content, version=got.version, size=got.size)

    def save_file(self, workspace: Workspace, path: str, content: str, base_version: str) -> WorkspaceFileSaved:
        try:
            saved = FileSystemService(workspace.root_path).save_text(path, content, base_version)
        except (
            PathEscapeError, NotAFileError, FileTooLargeError, FileNotTextError, FileAccessError, FileChangedError,
        ) as exc:
            raise self._file_refusal(exc, path) from exc
        return WorkspaceFileSaved(path=saved.path, version=saved.version, size=saved.size)

    @staticmethod
    def _file_refusal(exc: Exception, path: str) -> ApiError:
        """One sentence and one code per way a file door can say no, so the editor can say the right thing.

        The order matters: `ProtectedRootError` and `GitMetadataError` are kinds of `PathEscapeError`.
        """
        label = repr(path) if "\x00" in path else path
        if isinstance(exc, ProtectedRootError):
            return ApiError(400, "workspace_protected", f"this workspace is not a place files may be saved: {exc.reason}")
        if isinstance(exc, PathEscapeError):
            return ApiError(400, "file_escape", f"{label} is outside this workspace, or is git's own metadata")
        if isinstance(exc, NotAFileError):
            return ApiError(404, "file_missing", f"there is no file at {label} in this workspace")
        if isinstance(exc, FileTooLargeError):
            return ApiError(
                422, "file_too_large",
                f"{label} is {exc.size:,} bytes; the editor opens and saves files up to {MAX_EDIT_BYTES:,} bytes",
            )
        if isinstance(exc, FileNotTextError):
            if exc.reason == "binary":
                return ApiError(422, "file_binary", f"{label} is binary (it holds a NUL byte), not text an editor can hold")
            return ApiError(422, "file_not_text", f"{label} is not valid UTF-8 text")
        if isinstance(exc, FileChangedError):
            return ApiError(
                409, "file_changed",
                f"{label} changed on disk since it was opened. Reload it to see the new version, or save over it.",
                {"current_version": exc.current_version},
            )
        if isinstance(exc, FileAccessError):
            return ApiError(422, "file_access", f"{label} could not be read or written: {exc.detail}")
        raise exc

    def delete(self, workspace_id: str, *, delete_goals: bool = False) -> dict[str, Any]:
        """Forget a workspace, and optionally the goal history recorded against it.

        Only Codify's records are removed. The directory at `root_path` is never
        touched — nothing here reads or writes the user's files, and a workspace
        row is just a path this engine was pointed at. Saying so in the response
        is part of the contract: "delete workspace" that also deleted a project
        would be a very different button.

        `goals.workspace_id` deliberately carries **no** ON DELETE CASCADE, so a
        workspace with history is refused rather than silently taking the goals
        with it. The caller has to ask for the cascade explicitly, and the
        refusal names the count so the UI can show what the second click costs.
        Without that, the FK violation surfaced as a bare IntegrityError — an
        HTTP 500 whose message is "FOREIGN KEY constraint failed" and which
        tells the user nothing about what to do next.
        """
        workspace = self.get(workspace_id)  # 404 if unknown
        goals = self.goal_count(workspace_id)
        if goals and not delete_goals:
            raise ApiError(
                409, "workspace_not_empty",
                f"this workspace still has {goals} goal{'' if goals == 1 else 's'} recorded "
                "against it — delete them too, or delete the workspace afterwards",
                {"goals": goals, "workspace_id": workspace_id},
            )
        if goals:
            # Refuse while anything is still in flight: a PLANNING or RUNNING
            # goal has a live coroutine that will keep publishing events for a
            # row that no longer exists. Cancel it first.
            live = self._db.execute(
                """SELECT id, title, status FROM goals
                   WHERE workspace_id = ? AND status IN ('PLANNING', 'RUNNING')
                   LIMIT 1""",
                (workspace_id,),
            ).fetchone()
            if live is not None:
                phase = "planning" if live["status"] == "PLANNING" else "running"
                raise ApiError(
                    409, "workspace_has_active_goals",
                    f"goal {live['title']!r} is still {phase}; "
                    "cancel it before deleting its workspace",
                    {"goal_id": live["id"], "workspace_id": workspace_id},
                )
            events = self._db.execute(
                """SELECT count(*) FROM events
                   WHERE goal_id IN (SELECT id FROM goals WHERE workspace_id = ?)""",
                (workspace_id,),
            ).fetchone()[0]
            self._db.execute("DELETE FROM goals WHERE workspace_id = ?", (workspace_id,))
        else:
            events = 0
        self._db.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
        self._db.commit()
        return {
            "deleted": True,
            "workspace_id": workspace_id,
            "name": workspace.name,
            "goals": goals,
            "events": int(events),
            # Stated explicitly so no caller has to guess whether "delete
            # workspace" means "delete the user's project".
            "files_touched": False,
        }


GOAL_TITLE_MAX = 200


def split_title_description(title: str, description: str) -> tuple[str, str]:
    """Fit an arbitrary user prompt into the (title<=200, description<=20000) schema.

    Chat UIs send the whole prompt as `title`. If it fits, keep it as-is.
    Otherwise truncate the first line as the title and put the full prompt in
    the description so the planner still sees every word.
    """
    title = title or ""
    description = description or ""
    if len(title) <= GOAL_TITLE_MAX:
        return title, description
    # Prefer a natural break (first line/paragraph) within the 200-char cap.
    head = title[:GOAL_TITLE_MAX]
    first_line = title.split("\n", 1)[0]
    if 0 < len(first_line) <= GOAL_TITLE_MAX:
        head = first_line
    else:
        # No newline to cut on: trim to the last word boundary.
        cut = head.rfind(" ", GOAL_TITLE_MAX // 2)
        if cut > 0:
            head = head[:cut]
    full = f"{title}\n\n{description}" if description else title
    return head.strip(), full[:20000]


class ConversationService:
    """Threads of turns — the unit a tab points at.

    Lives beside `GoalService` because the two are deliberately separate: a goal
    is a run with a plan and a verifier, a conversation is the question several
    runs answer. Collapsing them would mean every tab is one goal and the app
    cannot hold two lines of inquiry in the same workspace, which is exactly the
    shape the single `messages` array had.

    Nothing here writes a goal. A turn is derived from the goal that answers it,
    so there is no second record to drift out of step with the first.
    """

    # Every read of a conversation carries its parent's *name*, joined rather
    # than left for the client to resolve.
    #
    # The panel lists one workspace's live threads and hides archived ones, so a
    # child whose parent has been archived cannot look that parent up in the list
    # it was given — the id is still on the row and the name is still in the
    # database, but nothing the panel can see connects them. The label then
    # degrades to a generic word and never recovers. Joining here makes the
    # lineage a fact the response carries, so archiving a parent costs the child
    # nothing and `parent_title` is `None` only when the parent row is truly
    # gone.
    #
    # Both sides of the join share `workspace_id` and `archived`, so every
    # column a caller filters or orders by has to be qualified with `c.`. An
    # unqualified one is not a second bug but a hard `ambiguous column name`
    # error from SQLite, which is how this shape usually announces itself.
    _SELECT_WITH_PARENT = (
        "SELECT c.id, c.workspace_id, c.title, c.archived, c.parent_id, "
        "c.created_at, c.updated_at, p.title AS parent_title "
        "FROM conversations c LEFT JOIN conversations p ON p.id = c.parent_id"
    )

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._db = conn

    @staticmethod
    def _row_to_conversation(row: sqlite3.Row) -> Conversation:
        return Conversation(
            id=row["id"],
            workspace_id=row["workspace_id"],
            title=row["title"],
            archived=bool(row["archived"]),
            parent_id=row["parent_id"],
            # Present on every read that goes through `_SELECT_WITH_PARENT`, and
            # absent from a bare `SELECT *`. The `in` check rather than `row["…"]`
            # is what lets both shapes share this one mapper: a missing column
            # would otherwise raise `IndexError` on a query that never promised
            # the join, which is a 500 from an unrelated change.
            parent_title=(
                row["parent_title"] if "parent_title" in row.keys() else None
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create(self, body: ConversationCreate) -> Conversation:
        row = self._db.execute(
            "SELECT id FROM workspaces WHERE id = ?", (body.workspace_id,)
        ).fetchone()
        if row is None:
            raise ApiError(404, "unknown_workspace", "workspace not found")
        now = time.time()
        # A title the client sends is normalised the same way a goal's is, so
        # "New chat" is a placeholder the UI shows and not a row the engine
        # invented. An empty title is the ordinary case.
        title = body.title.strip()[:200] if body.title else ""
        # A parent has to be a real thread in the same workspace, or the child
        # is claiming a lineage that does not exist. Checked here rather than
        # trusted, because `parent_id` is client input and a parent from another
        # workspace would be a thread hanging off a tree the caller cannot see.
        parent_id = body.parent_id
        if parent_id is not None:
            parent = self._db.execute(
                "SELECT workspace_id FROM conversations WHERE id = ?",
                (parent_id,),
            ).fetchone()
            if parent is None:
                raise ApiError(404, "unknown_conversation", "parent conversation not found")
            if parent["workspace_id"] != body.workspace_id:
                raise ApiError(
                    422,
                    "parent_workspace_mismatch",
                    "a thread can only be started on a thread in the same workspace",
                )
        convo = Conversation(
            id=str(uuid.uuid4()),
            workspace_id=body.workspace_id,
            title=title,
            archived=False,
            parent_id=parent_id,
            created_at=now,
            updated_at=now,
        )
        self._db.execute(
            """INSERT INTO conversations
                   (id, workspace_id, title, archived, parent_id, created_at, updated_at)
               VALUES (?, ?, ?, 0, ?, ?, ?)""",
            (
                convo.id,
                convo.workspace_id,
                convo.title,
                convo.parent_id,
                convo.created_at,
                convo.updated_at,
            ),
        )
        self._db.commit()
        # Re-read rather than returning the object built above, so a thread born
        # on another one comes back carrying that parent's name. Returning the
        # hand-built value would make the response shape depend on *how* the row
        # was created, and a client that reads `parent_title` would see it
        # missing on exactly the response where it is most wanted.
        return self.get(convo.id)

    def _get_row(self, conversation_id: str) -> sqlite3.Row:
        row: sqlite3.Row | None = self._db.execute(
            self._SELECT_WITH_PARENT + " WHERE c.id = ?", (conversation_id,)
        ).fetchone()
        if row is None:
            raise ApiError(404, "unknown_conversation", "conversation not found")
        return row

    def get(self, conversation_id: str) -> Conversation:
        return self._row_to_conversation(self._get_row(conversation_id))

    def list_conversations(
        self,
        workspace_id: str | None = None,
        include_archived: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Conversation]:
        """Threads, most recently touched first.

        Archived threads are hidden by default: a tab list is a list of things the
        user is working on, and burying the live ones under months of finished
        threads is the same mistake `list_goals` avoids by putting active goals
        first. They are still there — `include_archived` reads them.
        """
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        where: list[str] = []
        params: list[Any] = []
        # Qualified with `c.`: the join brings a second table that has its own
        # `workspace_id` and `archived`, and an unqualified name is ambiguous.
        if workspace_id is not None:
            where.append("c.workspace_id = ?")
            params.append(workspace_id)
        if not include_archived:
            where.append("c.archived = 0")
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        params.extend([limit, offset])
        rows = self._db.execute(
            self._SELECT_WITH_PARENT
            + clause
            + " ORDER BY c.updated_at DESC, c.id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [self._row_to_conversation(r) for r in rows]

    def rename(self, conversation_id: str, body: ConversationUpdate) -> Conversation:
        # A rename is the only mutable thing about a thread, and it cannot move
        # it: `ConversationUpdate` forbids extra fields, so a body that tried to
        # set `workspace_id` or `archived` is a 422 rather than a silent rewrite.
        self._get_row(conversation_id)
        now = time.time()
        title = body.title.strip()[:200]
        self._db.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title, now, conversation_id),
        )
        self._db.commit()
        return self.get(conversation_id)

    def set_archived(self, conversation_id: str, archived: bool) -> Conversation:
        self._get_row(conversation_id)
        self._db.execute(
            "UPDATE conversations SET archived = ? WHERE id = ?",
            (int(archived), conversation_id),
        )
        self._db.commit()
        return self.get(conversation_id)

    def delete(self, conversation_id: str) -> dict[str, Any]:
        """Drop the thread, keep the runs.

        A goal's history is the audit trail of what the engine did, and it is not
        undone by someone closing a tab. `ON DELETE SET NULL` on
        `goals.conversation_id` means the goals survive as single-turn threads
        rather than being swept out of the history with the thread that named
        them — the same reasoning as docs/03 §1.7's commit scope.
        """
        self._get_row(conversation_id)
        self._db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        self._db.commit()
        return {"deleted": True}

    def turns(self, conversation_id: str) -> list[ConversationTurn]:
        """The thread's turns, oldest first, as the transcript reads them.

        Derived rather than stored. The prompt is the goal's description, falling
        back to its title for a goal created before descriptions carried the full
        text — never a second copy of the user's words.
        """
        self._get_row(conversation_id)
        rows = self._db.execute(
            """SELECT id, conversation_id, title, description, status, created_at
               FROM goals WHERE conversation_id = ?
               ORDER BY created_at ASC, id ASC""",
            (conversation_id,),
        ).fetchall()
        out: list[ConversationTurn] = []
        for row in rows:
            prompt = (row["description"] or "").strip() or row["title"]
            out.append(
                ConversationTurn(
                    goal_id=row["id"],
                    conversation_id=row["conversation_id"],
                    prompt=prompt,
                    status=row["status"],
                    created_at=row["created_at"],
                )
            )
        return out


class ShellTabService:
    """The open tabs, and the order they are in.

    The only view state the engine holds, and it holds the smallest useful part
    of it: which tabs exist, and in what order. That is the part two windows
    must agree about, and it is why this is a table of rows rather than a blob
    — a shared strip has more than one writer, and a writer that could only
    replace the whole thing would be a writer that closes the other window's
    tabs every time it saved.

    What a tab is *showing* stays in the window that shows it, as an opaque JSON
    `payload`. The engine checks that it is JSON and how big it is, and stops
    there on purpose. It could learn the shape (a `conversation_id`, a `url` and
    a history stack) and this would still be a worse design: a second version of
    the app would have to migrate rows it cannot read, and a strip whose tabs are
    half a shape the engine understands is a strip the engine can corrupt.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._db = conn

    @staticmethod
    def _row_to_tab(row: sqlite3.Row) -> ShellTab:
        return ShellTab(
            key=row["key"],
            position=row["position"],
            kind=row["kind"],
            payload=row["payload"],
            updated_at=row["updated_at"],
        )

    def list_tabs(self) -> list[ShellTab]:
        """The strip, in the order it is read.

        `position` ascending, `key` as the tiebreak so two rows claiming the same
        slot have a defined order rather than whatever SQLite returned. The
        client re-sorts and re-pushes on its next change, so this is a stable
        read, not a claim about uniqueness.
        """
        rows = self._db.execute(
            "SELECT key, position, kind, payload, updated_at "
            "FROM shell_tabs ORDER BY position ASC, key ASC"
        ).fetchall()
        return [self._row_to_tab(row) for row in rows]

    def upsert(self, body: ShellTabWrite) -> list[ShellTab]:
        """Record one tab and return the whole strip.

        The payload is validated as JSON before it is stored, because the one
        promise this service makes about a payload is that it *is* a payload: a
        row holding text that is not JSON would fail in every client that read
        it, and the failure would surface in a window rather than here, where it
        can be refused with a sentence. Its shape is not checked, and cannot be:
        see the class docstring.

        Returning the strip rather than the one tab is the same round trip as a
        `GET`, and it is what lets a client adopt everything else has done in the
        same breath as its own write — the two windows in this design do not wait
        for each other, they meet in this response.
        """
        try:
            json.loads(body.payload)
        except (TypeError, ValueError) as exc:
            raise ApiError(
                422,
                "invalid_tab_payload",
                f"a tab's payload must be JSON: {exc}",
            ) from None
        self._db.execute(
            "INSERT INTO shell_tabs (key, position, kind, payload, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "position = excluded.position, kind = excluded.kind, "
            "payload = excluded.payload, updated_at = excluded.updated_at",
            (body.key, body.position, body.kind, body.payload, time.time()),
        )
        self._db.commit()
        return self.list_tabs()

    def remove(self, key: str) -> list[ShellTab]:
        """Forget one tab, and return the strip that is left.

        Idempotent, deliberately, and this is the one place where "refuse loudly"
        would be wrong. With two windows on one strip, a tab can be closed in
        both — the second close is not a bug in the client, it is the shared
        strip working — so a 404 here would make the ordinary case an error the
        user has to see. The response is the truth about what is left, which is
        what the caller actually needed.
        """
        self._db.execute("DELETE FROM shell_tabs WHERE key = ?", (key,))
        self._db.commit()
        return self.list_tabs()


class GoalService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._db = conn

    def create(self, body: GoalCreate) -> Goal:
        row = self._db.execute("SELECT id FROM workspaces WHERE id = ?", (body.workspace_id,)).fetchone()
        if row is None:
            raise ApiError(404, "unknown_workspace", "workspace not found")
        now = time.time()
        title, description = split_title_description(body.title, body.description)
        # A conversation is not a free-floating label: it has to exist, and it has
        # to be in the same workspace. One check, shared with
        # `attach_conversation`, so creation and later attachment cannot drift.
        self._check_link(body.conversation_id, body.workspace_id)
        goal = Goal(
            id=str(uuid.uuid4()),
            workspace_id=body.workspace_id,
            conversation_id=body.conversation_id,
            title=title,
            description=description,
            status="PLANNING",
            dry_run=body.dry_run,
            plan_only=body.plan_only,
            parallel=body.parallel,
            mode=body.mode,
            trace=body.trace,
            version=0,
            created_at=now,
            updated_at=now,
            provider=body.provider,
            model=body.model,
        )
        self._db.execute(
            """INSERT INTO goals (id, workspace_id, conversation_id, title, description, status, dry_run, plan_only, parallel, mode, trace, version, event_seq, created_at, updated_at, provider, model)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?)""",
            (goal.id, goal.workspace_id, goal.conversation_id, goal.title, goal.description, goal.status, int(goal.dry_run), int(goal.plan_only), int(goal.parallel), goal.mode, int(goal.trace), now, now, goal.provider, goal.model),
        )
        if goal.conversation_id is not None:
            # The thread is what a tab orders by, so a new turn has to move it.
            # Without this a conversation that opened last week would stay pinned
            # below one nobody has touched since.
            self._db.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, goal.conversation_id),
            )
        self._db.commit()
        return goal

    def _check_link(self, conversation_id: str | None, workspace_id: str) -> None:
        """Validate a goal↔thread link, and the one place that validation lives.

        Shared by `create` and `attach_conversation` because the two must not
        drift: a mismatch check that guarded creation but not later attachment
        would let a client move a run under another workspace's name — the
        reach-around docs/09 §3.1 exists to forbid. Without it a tab could
        show one workspace's work under another's.
        """
        if conversation_id is None:
            return
        row = self._db.execute(
            "SELECT workspace_id FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise ApiError(404, "unknown_conversation", "conversation not found")
        if row["workspace_id"] != workspace_id:
            raise ApiError(
                422,
                "conversation_workspace_mismatch",
                "conversation belongs to a different workspace",
            )

    def attach_conversation(self, goal_id: str, body: GoalConversationUpdate) -> Goal:
        """Move an existing goal into a thread — what `POST /goals` cannot do.

        The restore path needs this: a goal that predates conversations has
        `conversation_id` NULL, and the UI creating a thread client-side left
        the link in the panel rather than the store — one restart later the run
        was its own thread again (docs/09 §6).

        Re-attaching overwrites: moving a run from thread A to thread B is the
        same act as the first attach, and refusing it would file history
        permanently wherever it first landed. The goal's `version` is not
        bumped — that counter guards status/step concurrency
        (`update_goal`'s check-and-increment), and a thread link is not that.
        """
        goal = self.get(goal_id)  # 404 unknown_goal
        self._check_link(body.conversation_id, goal.workspace_id)
        now = time.time()
        self._db.execute(
            "UPDATE goals SET conversation_id = ? WHERE id = ?",
            (body.conversation_id, goal_id),
        )
        # The panel orders threads by last touch, and putting a run into a
        # thread is a touch — same as creating one into it.
        self._db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, body.conversation_id),
        )
        self._db.commit()
        return self.get(goal_id)

    def get(self, goal_id: str) -> Goal:
        row = self._db.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if row is None:
            raise ApiError(404, "unknown_goal", "goal not found")
        data = {k: row[k] for k in row.keys() if k in Goal.model_fields}
        data["dry_run"] = bool(row["dry_run"])
        data["plan_only"] = bool(row["plan_only"])
        data["parallel"] = bool(row["parallel"])
        data["trace"] = bool(row["trace"])
        return Goal.model_validate(data)

    def create_turn(self, conversation_id: str, body: TurnCreate) -> Goal:
        """One turn: a goal row that will never have a plan.

        A goal rather than a new table, and the reason is `events.goal_id` being
        `NOT NULL`. The event log is the WebSocket, the audit trail, the usage
        books and the stats feed, so a turn stored anywhere else would have
        nowhere to write a single streamed token. `mode="chat"` is what keeps it
        out of the pipeline: `POST /goals` spawns `run_planning` and this route
        spawns `run_chat`, and `GoalCreate` refuses `mode="chat"` so there is
        exactly one door.

        The thread must exist and must belong to a real workspace, checked
        through `_check_link` for the same reason `create` uses it: attachment
        and creation cannot be allowed to disagree about what a valid link is.
        """
        row = self._db.execute(
            "SELECT workspace_id FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if row is None:
            raise ApiError(404, "unknown_conversation", "conversation not found")
        workspace_id = str(row["workspace_id"])
        ws_row = self._db.execute(
            "SELECT id FROM workspaces WHERE id = ?", (workspace_id,)
        ).fetchone()
        if ws_row is None:
            raise ApiError(404, "unknown_workspace", "workspace not found")
        prompt = body.prompt.strip()
        if not prompt:
            raise ApiError(422, "empty_prompt", "a turn needs something to say")
        now = time.time()
        # The title is the prompt truncated the way every other goal's is —
        # a turn with a 20,000-character title would not fit the column's
        # contract, and the thread's own name is the UI's business, not this.
        title = prompt[:200]
        goal = Goal(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            title=title,
            description=prompt,
            status="PLANNING",
            mode="chat",
            trace=body.trace,
            version=0,
            created_at=now,
            updated_at=now,
            provider=body.provider,
            model=body.model,
        )
        self._db.execute(
            """INSERT INTO goals (id, workspace_id, conversation_id, title, description,
                                  status, dry_run, plan_only, parallel, mode, trace,
                                  version, event_seq, created_at, updated_at, provider, model)
               VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, 0, 0, ?, ?, ?, ?)""",
            (goal.id, goal.workspace_id, goal.conversation_id, goal.title, goal.description,
             goal.status, goal.mode, int(goal.trace), goal.created_at, goal.updated_at,
             goal.provider, goal.model),
        )
        self._db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )
        self._db.commit()
        return goal

    def turn_history(self, conversation_id: str, limit: int = 12) -> list[dict[str, str]]:
        """This thread's earlier turns as (prompt, reply) pairs, oldest first.

        Derived from rows that already exist rather than stored, so there is no
        second copy of a conversation that could disagree with the transcript.
        Only `mode="chat"` goals are read: a pipeline goal's "reply" is a commit
        subject and a step summary, and feeding those to a turn as though they
        were things a person said is how a thread fills up with noise.

        A turn whose reply was never written — cancelled, or failed mid-call —
        is still returned, with an empty reply. Dropping it would lose the fact
        that the user asked something, and the next turn's model would then be
        told about a conversation with a hole in it.
        """
        rows = self._db.execute(
            """SELECT id, title, description FROM goals
               WHERE conversation_id = ? AND mode = 'chat'
               ORDER BY created_at DESC, id DESC LIMIT ?""",
            (conversation_id, max(0, int(limit))),
        ).fetchall()
        out: list[dict[str, str]] = []
        for r in reversed(rows):
            reply = self._db.execute(
                """SELECT payload FROM events
                   WHERE goal_id = ? AND type = 'log'
                   ORDER BY sequence DESC LIMIT 40""",
                (r["id"],),
            ).fetchall()
            text = ""
            for ev in reversed(reply):
                payload = json.loads(ev["payload"] or "{}")
                if payload.get("turn"):
                    text = str(payload.get("message") or "")
                    break
            out.append(
                {
                    "prompt": (r["description"] or r["title"] or "").strip(),
                    "reply": text.strip(),
                }
            )
        return out

    def list_goals(
        self,
        workspace_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Goal]:
        """Goals, newest first, for the UI's history.

        Every goal is already persisted; this is the read that was missing, which
        left a restart showing nothing about any of them. `status` is validated
        against GoalStatus's values (a bad one is a 422, not an empty list), and
        the page is bounded because a long-lived install keeps every goal it ever
        ran. Active goals still lead a filtered-by-nothing list: the one a user
        just dispatched must not sink under a wall of finished runs.
        """
        if status is not None and status not in get_args(GoalStatus):
            raise ApiError(422, "invalid_status", f"unknown goal status: {status}")
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        where = []
        params: list[Any] = []
        if workspace_id is not None:
            where.append("workspace_id = ?")
            params.append(workspace_id)
        if status is not None:
            where.append("status = ?")
            params.append(status)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        params.extend([limit, offset])
        rows = self._db.execute(
            """SELECT * FROM goals"""
            + clause
            + """ ORDER BY
                  CASE WHEN status IN ('PLANNING', 'PENDING', 'RUNNING', 'PAUSED') THEN 0 ELSE 1 END,
                  created_at DESC, id DESC
                LIMIT ? OFFSET ?""",
            params,
        )
        out = []
        for row in rows:
            data = {k: row[k] for k in row.keys() if k in Goal.model_fields}
            data["dry_run"] = bool(row["dry_run"])
            data["plan_only"] = bool(row["plan_only"])
            data["parallel"] = bool(row["parallel"])
            out.append(Goal.model_validate(data))
        return out

    def recent_run_models(self, limit: int = 5, scan_events: int = 300) -> list[dict[str, Any]]:
        """The models that *actually* answered recently, newest first.

        Read from `agent_assigned` events rather than from `goals.provider/model`,
        which record what the command bar asked for. Since per-role configs became
        authoritative (see `01` §2.2), those two are not the same thing: a goal can
        name one model and be executed by four others. (A *turn's* pick does route
        the turn's own calls, `docs/09` §10.20, but not the roles it may start.)
        Ordering a menu by "the last
        one you ran" while reading the intent would label a model the engine never
        called — the exact shape of false claim this file keeps removing.

        Ordered by the event's own timestamp rather than by goal creation: two goals
        can start in the same second, and "which answered most recently" is a question
        about the events, not about which goal is newer.
        """
        rows = self._db.execute(
            """SELECT e.payload AS payload, e.timestamp AS timestamp
               FROM events e
               WHERE e.type = 'agent_assigned'
               ORDER BY e.timestamp DESC, e.sequence DESC
               LIMIT ?""",
            (max(1, scan_events),),
        )
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                continue
            provider = (payload.get("provider") or "").strip()
            model = (payload.get("model") or "").strip()
            # A role with no model chosen is announced with an empty id; it is not
            # a model that ran, so it must not reach the list.
            if not provider or not model or (provider, model) in seen:
                continue
            seen.add((provider, model))
            out.append({
                "provider": provider,
                "model": model,
                "role": payload.get("role"),
                "ran_at": row["timestamp"],
            })
            if len(out) >= max(1, limit):
                break
        return out

    def recall_events(
        self,
        workspace_id: str,
        *,
        window_days: int = 0,
        limit: int | None = None,
        now: float | None = None,
        with_ids: bool = False,
    ) -> list[dict[str, Any]]:
        """Recent outcome events for one workspace, newest first.

        The query half of the `recall` tool (`engine/recall.py` holds the
        projection and the wording). It lives here rather than in a service of
        its own because this class already owns `events` and already answers
        questions across them for the statistics screen.

        Two things are load-bearing and neither is a detail:

        **The workspace scope is in the join.** Filtering in Python would have
        read the other workspace's rows to decide not to return them — and a
        user with two repositories open must not have one's failures recalled
        into the other's turns, so the scoping has to be the thing that keeps
        them out rather than the thing that discards them.

        **The type list is the allow-list.** It is interpolated from
        `recall.RECALLABLE`'s keys rather than repeated here, so a type added
        to the allow-list cannot be forgotten in the query — and a type left
        out of the allow-list is unreachable no matter what is in the table.
        """
        from engine.recall import MAX_SCAN_EVENTS, RECALLABLE

        kinds = list(RECALLABLE)
        placeholders = ",".join("?" for _ in kinds)
        cutoff: float | None = None
        if window_days > 0:
            cutoff = (time.time() if now is None else now) - window_days * 86_400.0
        scan = max(1, int(limit or MAX_SCAN_EVENTS))
        rows = self._db.execute(
            f"""SELECT e.goal_id AS goal_id, e.step_id AS step_id, e.type AS type,
                       e.payload AS payload, e.timestamp AS timestamp,
                       e.sequence AS sequence, g.title AS goal_title
                FROM events e
                JOIN goals g ON g.id = e.goal_id
                WHERE g.workspace_id = ?
                  AND e.type IN ({placeholders})
                  AND (? IS NULL OR e.timestamp >= ?)
                ORDER BY e.timestamp DESC, e.sequence DESC
                LIMIT ?""",  # noqa: S608 — placeholders are `?`; kinds is a fixed allow-list
            (workspace_id, *kinds, cutoff, cutoff, scan),
        ).fetchall()
        rows = [dict(row) for row in rows]
        if with_ids:
            ids = self._db.execute(
                """SELECT goal_id, step_id, type, sequence, id FROM events
                   WHERE goal_id IN (
                     SELECT id FROM goals WHERE workspace_id = ?
                   )""",
                (workspace_id,),
            ).fetchall()
            by_key: dict[tuple[str, str, str, int], str] = {}
            for r in ids:
                by_key[(r["goal_id"], r["step_id"] or "", r["type"], r["sequence"])] = r["id"]
            for row in rows:
                key = (row["goal_id"], row["step_id"] or "", row["type"], row["sequence"])
                row["id"] = by_key.get(key, "")
        return rows

    def observation_rows(
        self,
        workspace_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """The durable beliefs about this workspace, most recently refined first.

        The read half of the observations store (docs/10 §6): one row per
        (workspace, subject), refined by the consolidation pass after each
        run. `evidence` is the newest supporting event ids, so a claim can be
        checked against the rows that back it; `recall.search_observations`
        caps and formats what a model sees.
        """
        cap = max(1, min(int(limit or 50), 200))
        rows = self._db.execute(
            """SELECT subject, lesson, example, proof, evidence, refined_at
               FROM observations WHERE workspace_id = ?
               ORDER BY refined_at DESC, subject LIMIT ?""",
            (workspace_id, cap),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                evidence = json.loads(row["evidence"] or "[]")
            except (TypeError, ValueError):
                evidence = []
            out.append({
                "subject": row["subject"],
                "lesson": row["lesson"],
                "example": row["example"],
                "proof": row["proof"],
                "evidence": evidence if isinstance(evidence, list) else [],
                "refined_at": row["refined_at"],
            })
        return out

    def record_observations(self, workspace_id: str, observations: list[dict[str, Any]]) -> int:
        """Refine the store with what one consolidation pass distilled.

        The write half of the observations store, and the only path that
        writes it: the engine's post-run pass calls this with
        `distill_observations`' output, never a model. Refinement, not
        appending — the shape docs/10 §6 takes from Hindsight: a subject seen
        again gets its proof count *added* and its evidence refreshed, not a
        second row. The lesson and example take the newest scan's wording, so
        a recovery flipping a lesson positive survives the next scan.

        `step:`-subjects are skipped: they are pairing plumbing, and one retry
        is not a durable belief about the workspace. Returns how many rows
        were written or refined.
        """
        now = time.time()
        written = 0
        for obs in observations:
            subject = str(obs.get("subject") or "")
            if not subject or subject.startswith("step:"):
                continue
            existing = self._db.execute(
                "SELECT proof, evidence FROM observations WHERE workspace_id = ? AND subject = ?",
                (workspace_id, subject),
            ).fetchone()
            evidence = [str(e) for e in (obs.get("evidence") or []) if str(e)]
            if existing is None:
                self._db.execute(
                    """INSERT INTO observations
                       (id, workspace_id, subject, lesson, example, proof, evidence,
                        created_at, refined_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), workspace_id, subject,
                     str(obs.get("lesson") or ""), str(obs.get("example") or ""),
                     max(1, int(obs.get("proof") or 1)),
                     json.dumps(evidence), now, now),
                )
            else:
                try:
                    prior = json.loads(existing["evidence"] or "[]")
                except (TypeError, ValueError):
                    prior = []
                merged = list(dict.fromkeys([*evidence, *[str(e) for e in prior]]))
                self._db.execute(
                    """UPDATE observations SET lesson = ?, example = ?,
                       proof = proof + ?, evidence = ?, refined_at = ?
                       WHERE workspace_id = ? AND subject = ?""",
                    (str(obs.get("lesson") or ""), str(obs.get("example") or ""),
                     max(1, int(obs.get("proof") or 1)),
                     json.dumps(merged[:20]), now, workspace_id, subject),
                )
            written += 1
        self._db.commit()
        return written

    def thread_recall(
        self,
        workspace_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """This workspace's conversation threads, with their run outcomes.

        The query half of the `recall_threads` tool (`engine/recall.py` holds
        the projection and the wording), and a sibling of `recall_events`:
        where that reads *events* — what happened inside a run — this reads the
        other unit of history the database already keeps, the conversation. It
        lives here for the same reason `recall_events` does: this class owns
        the goals/conversations schema and already answers cross-goal questions
        for the statistics screen.

        Deliberately not `turn_history`: that serves a *continuing* thread with
        its own (prompt, reply) pairs, chat goals only. This serves a *new*
        thread with its workspace neighbours — every thread, pipeline runs and
        chat turns both, because "three threads asked about this workspace and
        two of their runs completed" is history too.
        """
        from engine.recall import MAX_ASK_CHARS

        cap = max(1, min(int(limit or 50), 200))
        rows = self._db.execute(
            """SELECT c.id AS id, c.title AS title, c.updated_at AS last_touched,
                      COUNT(g.id) AS runs,
                      SUM(CASE WHEN g.status = 'COMPLETED' THEN 1 ELSE 0 END) AS completed,
                      SUM(CASE WHEN g.status = 'FAILED' THEN 1 ELSE 0 END) AS failed,
                      SUM(CASE WHEN g.status = 'CANCELLED' THEN 1 ELSE 0 END) AS cancelled
                         FROM conversations c
                LEFT JOIN goals g ON g.conversation_id = c.id
               WHERE c.workspace_id = ? AND c.archived = 0
               GROUP BY c.id, c.title, c.updated_at
               ORDER BY c.updated_at DESC, c.id DESC
               LIMIT ?""",
            (workspace_id, cap),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            asks = self._db.execute(
                """SELECT description FROM goals
                   WHERE conversation_id = ?
                   ORDER BY created_at DESC, id DESC LIMIT ?""",
                (row["id"], 4),
            ).fetchall()
            out.append({
                "id": row["id"],
                "title": row["title"],
                "last_touched": row["last_touched"],
                "runs": row["runs"],
                "completed": row["completed"],
                "failed": row["failed"],
                "cancelled": row["cancelled"],
                "asks": [
                    (r["description"] or "")[:MAX_ASK_CHARS]
                    for r in asks
                ],
            })
        return out

    def steps(self, goal_id: str) -> list[PlanStep]:
        rows = self._db.execute("SELECT * FROM plan_steps WHERE goal_id = ? ORDER BY ordinal", (goal_id,))
        out = []
        for r in rows:
            d = row_to_dict(r)
            d["suggested_paths"] = json.loads(d["suggested_paths"] or "[]")
            out.append(PlanStep.model_validate(d))
        return out

    def update_step(self, goal_id: str, step_id: str, expected_version: int, patch: dict[str, Any]) -> PlanStep:
        """Edit a plan step's title/description/paths before execution.

        Only allowed while the goal is PENDING (plan produced, execution not
        started) and every step is still PENDING — once the executor touches
        a step, the plan is the record of what ran. Bumps the goal version
        and emits a plan_updated event so live streams can refresh.
        """
        # The route already validated the patch against PlanStepUpdate; here we
        # just drop unset (None) fields — explicit nulls mean "no change".
        goal = self.get(goal_id)
        if goal.status != "PENDING":
            raise ApiError(409, "illegal_status", f"plan steps can only be edited while the goal is PENDING (currently {goal.status})")
        existing = self.steps(goal_id)
        step = next((s for s in existing if s.id == step_id), None)
        if step is None:
            raise ApiError(404, "unknown_step", "step not found")
        if any(s.status != "PENDING" for s in existing):
            raise ApiError(409, "step_not_pending", "plan steps can only be edited while every step is PENDING")

        data = {k: v for k, v in patch.items() if v is not None}
        if not data:
            raise ApiError(422, "empty_patch", "no editable fields provided")
        # Capture what the patch *edited* before `data` is completed from the
        # stored step below — after that, every key looks changed.
        updates = set(data)
        # The transcript must be able to show what an edit changed, not just
        # that something changed: suggested_paths gate parallel batching, so a
        # path edit is execution-relevant drift and gets before/after in the
        # event. Unedited fields carry identical before/after — the UI decides
        # what is worth showing by comparing them.
        new_paths = None
        if "suggested_paths" in data:
            new_paths = [p.strip() for p in data["suggested_paths"] if p and p.strip()]
        changes: dict[str, dict[str, Any]] = {
            "suggested_paths": {
                "before": list(step.suggested_paths),
                "after": new_paths if new_paths is not None else list(step.suggested_paths),
            },
            "title": {"before": step.title, "after": data.get("title", step.title)},
            "description": {
                "before": step.description,
                "after": data.get("description", step.description),
            },
        }
        if new_paths is not None:
            data["suggested_paths"] = json.dumps(new_paths)
        else:
            data["suggested_paths"] = json.dumps(step.suggested_paths)
        if "title" not in data:
            data["title"] = step.title
        if "description" not in data:
            data["description"] = step.description

        # Editing the plan is an execution-relevant mutation: validate the
        # caller's view is current and bump the version atomically, so a
        # client can't start execution from a stale plan (same contract as
        # start/pause).
        cur = self._db.execute(
            "UPDATE goals SET version=version+1, updated_at=? WHERE id=? AND version=?",
            (time.time(), goal_id, expected_version),
        )
        if cur.rowcount != 1:
            g = self.get(goal_id)
            raise ApiError(409, "version_conflict", "version mismatch", {"current": g.model_dump()})

        cur = self._db.execute(
            "UPDATE plan_steps SET title=?, description=?, suggested_paths=? WHERE id=? AND goal_id=?",
            (data["title"], data["description"], data["suggested_paths"]
             if isinstance(data["suggested_paths"], str) else json.dumps(data["suggested_paths"]),
             step_id, goal_id),
        )
        if cur.rowcount != 1:
            self._db.rollback()
            raise ApiError(404, "unknown_step", "step not found")
        self._db.commit()

        event = Event(
            id=str(uuid.uuid4()),
            goal_id=goal_id,
            step_id=step_id,
            type="plan_updated",
            # Report only the fields the patch *edited*, not the merged record.
            # `data` is completed from the stored step before this point, so a
            # title-only edit used to announce all three fields as changed and
            # any client refetching on that signal redrew fields that had not
            # moved — clobbering an in-progress edit in another field.
            # `changes` carries before/after per field so the chat transcript
            # can show the drift itself: suggested_paths gate parallel
            # batching, and an edit there is exactly what the transcript needs
            # when explaining why a batch was later refused.
            payload={
                "step_id": step_id,
                "step_title": step.title,
                "fields": sorted(updates),
                "changes": changes,
            },
            timestamp=time.time(),
            sequence=self.next_sequence(goal_id),
        )
        self.publish(event)
        return next(s for s in self.steps(goal_id) if s.id == step_id)

    def fail_orphaned_active_goals(self, why: str = "the engine restarted") -> list[tuple[str, str, str]]:
        """Mark goals left PLANNING/RUNNING by a process that is gone as FAILED.

        Called twice, with two different reasons, and the reason is the whole point of
        the parameter. At boot the cause is an engine that died without anyone seeing
        it go; on a graceful shutdown it is this engine, deliberately, with a run cut
        short. Both leave a goal whose coroutine is not there any more, both are
        repaired before any client can read the stale status, and a user reading the
        event log later deserves to be told which of the two happened to them.

        Returns (goal_id, previous_status, message) so the caller can explain each
        rescue in the goal's own event log. Version is bumped unconditionally here —
        no client holds a view of a goal from a process that is going away, so there
        is nothing to conflict with. Publishes nothing: the terminal status event
        comes from the executor's `_fail`, so the stream shows one coherent story
        (log line, then failure) rather than two competing writers.
        """
        rows = self._db.execute(
            "SELECT id, status, version FROM goals WHERE status IN ('PLANNING', 'RUNNING')"
        ).fetchall()
        rescued: list[tuple[str, str, str]] = []
        now = time.time()
        for r in rows:
            goal_id, previous, version = r["id"], r["status"], r["version"]
            cur = self._db.execute(
                "UPDATE goals SET status='FAILED', version=version+1, updated_at=? WHERE id=? AND version=?",
                (now, goal_id, version),
            )
            if cur.rowcount != 1:
                continue  # concurrent change while we are deciding; not ours to fight
            rescued.append((
                goal_id,
                previous,
                f"{why} while this goal was {previous} — marking it failed; retry to run it again",
            ))
        self._db.commit()
        return rescued

    def set_dry_run(self, goal_id: str, enabled: bool) -> None:
        row = self._db.execute(
            "UPDATE goals SET dry_run = ?, updated_at = ? WHERE id = ?",
            (int(enabled), time.time(), goal_id),
        )
        if row.rowcount != 1:
            raise ApiError(404, "unknown_goal", "goal not found")
        self._db.commit()

    def set_trace(self, goal_id: str, enabled: bool) -> Goal:
        """Turn recording for this goal on or off (docs/04 §8).

        Settable only while the goal has not started: a recording that begins
        halfway through a run is a trace of half a run, and the replay it
        claims to support would be missing the calls that shaped the first
        half. Turning it *off* is always allowed, because deleting what has
        already been recorded is the user's call and not a state change.
        """
        if not enabled:
            row = self._db.execute(
                "UPDATE goals SET trace = 0, updated_at = ? WHERE id = ?",
                (time.time(), goal_id),
            )
        else:
            row = self._db.execute(
                "UPDATE goals SET trace = 1, updated_at = ? WHERE id = ? AND status = 'PLANNING'",
                (time.time(), goal_id),
            )
        if row.rowcount != 1:
            current = self._db.execute(
                "SELECT status FROM goals WHERE id = ?", (goal_id,)
            ).fetchone()
            if current is None:
                raise ApiError(404, "unknown_goal", "goal not found")
            raise ApiError(
                409, "trace_locked",
                f"tracing can only be enabled while a goal is PLANNING, and this one is {current[0]}",
            )
        self._db.commit()
        return self.get(goal_id)

    def set_plan_only(self, goal_id: str, enabled: bool) -> Goal:
        """Flip plan_only after creation (used when enabling execution on a
        plan-only goal). Emits a goal_status event so live streams refresh."""
        row = self._db.execute(
            "UPDATE goals SET plan_only = ?, updated_at = ? WHERE id = ?",
            (int(enabled), time.time(), goal_id),
        )
        if row.rowcount != 1:
            raise ApiError(404, "unknown_goal", "goal not found")
        self._db.commit()
        return self.get(goal_id)

    def update_status(
        self, goal_id: str, expected_version: int, status: str, step_id: str | None = None,
        *, reason_code: str | None = None, reason: str | None = None,
    ) -> Goal:
        """Set the goal status and announce it.

        `reason_code` and `reason` are for an engine-initiated pause and nothing else: a code from the closed
        set in `models.PAUSE_CODES` and the plain-language sentence that goes with it, published on the same
        `goal_status` event. They come together, only with `PAUSED`, and every other status change carries
        neither key, so the event's shape for everything but a pause is what it always was.

        The event is published here rather than by callers so *every* status
        change reaches live streams — pause and cancel used to write the DB and
        stay silent, leaving open chats to discover the change by polling.

        A goal that has reached a terminal status stays there, except by the two
        documented moves that re-open it (`may_change_status`): `CANCELLED` is a
        person's decision, and `run_chat` used to end with an unconditional
        `COMPLETED` that overwrote it. Repeating the status a goal already has is
        not a move and is allowed.
        """
        if (reason_code is None) != (reason is None):
            raise ValueError("a pause reason and its code go together")
        if reason_code is not None and (reason_code not in PAUSE_CODES or status != "PAUSED"):
            raise ValueError(f"{reason_code!r} is not a pause code, or the status is not PAUSED")
        row = self._db.execute("SELECT status FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if row is None:
            raise ApiError(404, "unknown_goal", "goal not found")
        if not may_change_status(row["status"], status):
            raise ApiError(
                409, "illegal_status", f"a {row['status']} goal cannot become {status}",
            )
        now = time.time()
        cur = self._db.execute(
            "UPDATE goals SET status=?, version=version+1, updated_at=? WHERE id=? AND version=?",
            (status, now, goal_id, expected_version),
        )
        if cur.rowcount != 1:
            g = self.get(goal_id)
            raise ApiError(409, "version_conflict", "version mismatch", {"current": g.model_dump()})
        self._db.commit()
        updated = self.get(goal_id)
        self.publish(Event(
            id=str(uuid.uuid4()),
            goal_id=goal_id,
            step_id=step_id,
            type="goal_status",
            payload={
                "status": status, "version": updated.version,
                **({"reason_code": reason_code, "reason": reason} if reason_code else {}),
            },
            timestamp=now,
            sequence=self.next_sequence(goal_id),
        ))
        return updated

    def has_proposed_files(self, goal_id: str) -> bool:
        """True when a dry-run stored at least one file operation for this goal."""
        row = self._db.execute(
            "SELECT 1 FROM proposed_files WHERE goal_id = ? LIMIT 1", (goal_id,)
        ).fetchone()
        return row is not None

    def proposed_content(self, goal_id: str, step_id: str, path: str) -> str | None:
        """The content a dry run proposed for one path, or None if it proposed none.

        The bytes Apply replays, read back for the one consumer that has to judge
        an artifact that is not on disk: the verifier reviewing a design
        deliverable that was planned but never written. Reading it here rather
        than from a diff is the point — the stored proposal is the full final
        content (`_store_proposed_files` resolves an edit before storing it), so
        what the verifier reviews is byte-for-byte what Apply will write.

        The path is matched case-insensitively, the same rule the rest of the
        engine uses to recognise a `DESIGN.md`, so a plan that capitalized the
        name still finds its own proposal. A step stores at most one proposal per
        path (`_store_proposed_files` clears its own rows first), so the ordering
        here is only there to make that independent of insertion order.
        """
        row = self._db.execute(
            "SELECT content FROM proposed_files WHERE goal_id = ? AND step_id = ?"
            " AND lower(path) = lower(?) ORDER BY rowid DESC LIMIT 1",
            (goal_id, step_id, path),
        ).fetchone()
        if row is None:
            return None
        content = row["content"]
        return str(content) if content is not None else None

    # Statuses with a live coroutine attached: a planning run, or a step driver.
    # Deleting one of these leaves that coroutine publishing events for a row
    # that no longer exists, so the route refuses and the user cancels first.
    ACTIVE_DRIVER_STATUSES = ("PLANNING", "RUNNING")

    def delete(self, goal_id: str) -> dict[str, Any]:
        """Delete a goal and everything recorded against it.

        The event log, plan steps, and any dry-run proposals go with it via the
        schema's ON DELETE CASCADE — they describe this run and are meaningless
        without it. The counts come back in the response rather than being
        logged and forgotten, so the UI can say what it actually removed instead
        of a bare "deleted".

        Nothing on disk is touched. A goal's record says *what* Codify did; the
        files a fixer wrote are the user's, and undoing a write is a different
        and much more dangerous operation than forgetting a log.
        """
        goal = self.get(goal_id)  # 404 if unknown
        if goal.status in self.ACTIVE_DRIVER_STATUSES:
            raise ApiError(
                409, "goal_in_progress",
                f"this goal is {goal.status.lower()} — cancel it before deleting it",
                {"goal_id": goal_id, "status": goal.status},
            )
        counts = {
            "steps": int(self._db.execute(
                "SELECT count(*) FROM plan_steps WHERE goal_id = ?", (goal_id,)
            ).fetchone()[0]),
            "events": int(self._db.execute(
                "SELECT count(*) FROM events WHERE goal_id = ?", (goal_id,)
            ).fetchone()[0]),
            "proposed_files": int(self._db.execute(
                "SELECT count(*) FROM proposed_files WHERE goal_id = ?", (goal_id,)
            ).fetchone()[0]),
        }
        self._db.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
        self._db.commit()
        return {
            "deleted": True,
            "goal_id": goal_id,
            "title": goal.title,
            "workspace_id": goal.workspace_id,
            **counts,
            "files_touched": False,
        }

    def compact_superseded_deltas(
        self, goal_id: str, step_id: str | None, role: str, before_sequence: int
    ) -> int:
        """Blank one finished stream's intermediate snapshots. Returns how many.

        A streamed reply is stored as *snapshots*, each the full text so far, so
        that any single event is self-contained (a client that connects mid-run
        repaints the current text instead of replaying fragments). The price is
        that every snapshot repeats all the ones before it: stored quadratically,
        a 561-token reply left 88 events and 106 KB. Once the stream's final
        snapshot has landed, the earlier ones say nothing it does not.

        **The rows stay; their text goes.** A goal's event sequence is dense from
        1 (`tests/stream_isolation.py`): a client detects a lost event by a gap,
        so deleting the rows would read as data loss. Blanking the text keeps
        every sequence number and every row and drops the quadratic part; the UI
        renders the newest snapshot per stream, which is the final one.

        Scoped tightly, because this rewrites audit rows: the goal, the step (a
        parallel step's stream is its own), the role, and only rows that carry a
        `final: false` flag and have not been compacted. The conductor's prose
        deltas carry no flag and are left alone, as is anything above
        `before_sequence` (a later stream of the same role at the same step).
        """
        cur = self._db.execute(
            "UPDATE events SET payload = json_set(payload, '$.text', '', '$.compacted', json('true')) "
            "WHERE goal_id = ? AND type = 'model_delta' "
            "AND sequence < ? AND step_id IS ? "
            "AND json_extract(payload, '$.role') = ? "
            "AND json_extract(payload, '$.final') = 0 "
            "AND json_extract(payload, '$.compacted') IS NULL",
            (goal_id, before_sequence, step_id, role),
        )
        self._db.commit()
        return cur.rowcount

    def next_sequence(self, goal_id: str) -> int:
        try:
            row = self._db.execute(
                "UPDATE goals SET event_seq = event_seq + 1 WHERE id = ? RETURNING event_seq",
                (goal_id,),
            ).fetchone()
        except Exception:
            # SQLite < 3.35 has no UPDATE...RETURNING. Fall back to the
            # portable read-modify-write (single-writer engine, same result).
            cur = self._db.execute("SELECT event_seq FROM goals WHERE id = ?", (goal_id,)).fetchone()
            if cur is None:
                raise ApiError(404, "unknown_goal", "goal not found")  # noqa: B904 — API rewrite
            nxt = int(cur[0]) + 1
            self._db.execute("UPDATE goals SET event_seq = ? WHERE id = ?", (nxt, goal_id))
            self._db.commit()
            return nxt
        if row is None:
            raise ApiError(404, "unknown_goal", "goal not found")
        self._db.commit()
        return int(row[0])

    def current_sequence(self, goal_id: str) -> int:
        """The goal's highest event sequence, without consuming one.

        The stage timer needs a watermark to ask "what was published while this
        stage ran" without allocating a sequence number it will not use — and
        `next_sequence` cannot be pressed for that, because a gap in the log
        would be a hole in the transcript's ordering.
        """
        row = self._db.execute(
            "SELECT event_seq FROM goals WHERE id = ?", (goal_id,)
        ).fetchone()
        if row is None:
            raise ApiError(404, "unknown_goal", "goal not found")
        return int(row[0])

    def publish(self, event: Event) -> Event:
        self._db.execute(
            "INSERT INTO events (id, goal_id, step_id, type, payload, timestamp, sequence) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event.id, event.goal_id, event.step_id, event.type, dumps(event.payload), event.timestamp, event.sequence),
        )
        self._db.commit()
        return event

    def latest_event(self, goal_id: str, type_: str) -> Event | None:
        """The goal's newest event of one type, or None.

        For state that is a *snapshot* carried by events (the conductor's todo list is the one: every change
        republishes the whole list, so the newest event is the list). Reading the whole log to find the last
        one of a type would make a long goal pay for its history every time a run starts.
        """
        row = self._db.execute(
            "SELECT * FROM events WHERE goal_id = ? AND type = ? ORDER BY sequence DESC LIMIT 1",
            (goal_id, type_),
        ).fetchone()
        if row is None:
            return None
        d = row_to_dict(row)
        try:
            d["payload"] = json.loads(d["payload"])
            return Event.model_validate(d)
        except Exception:
            return None

    def events_after(self, goal_id: str, after: int, limit: int | None = None) -> list[Event]:
        """The goal's events with a sequence above `after`, in order.

        `limit` is for the **streaming** readers, and without it the WebSocket
        tick was quadratic: it re-read and re-validated the goal's entire
        remaining history every 250 ms and then threw most of it away with a
        `[:500]` slice in Python — the one caller that wants a page of events
        paid for the whole log, twice a second, on a goal that only gets longer.

        The guarantee that makes a limit safe here is that `after` advances only
        to the last event actually **sent**, so a bounded read is still lossless
        — the next tick starts exactly where this one stopped. A row that cannot
        be parsed is skipped without advancing the watermark, exactly as before.
        """
        sql = "SELECT * FROM events WHERE goal_id = ? AND sequence > ? ORDER BY sequence"
        params: tuple[Any, ...] = (goal_id, after)
        if limit is not None:
            sql += " LIMIT ?"
            params = (goal_id, after, limit)
        rows = self._db.execute(sql, params)
        out = []
        for r in rows:
            d = row_to_dict(r)
            try:
                d["payload"] = json.loads(d["payload"])
            except (TypeError, ValueError):
                continue
            try:
                out.append(Event.model_validate(d))
            except Exception:
                continue
        return out
