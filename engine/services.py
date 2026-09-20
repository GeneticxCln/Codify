from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from engine.db import dumps, row_to_dict
from engine.models import (
    BUILTIN_PROVIDERS,
    PROVIDER_SLUG_RE,
    ROLES,
    AgentConfig,
    AgentConfigUpdate,
    AgentRole,
    Event,
    Goal,
    GoalCreate,
    PlanStep,
    Workspace,
    WorkspaceCreate,
)
from engine.providers import Keychain, ProviderError, ProviderFactory, validate_local_base_url


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: dict | None = None):
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}


class AgentRegistryService:
    def __init__(self, conn, factory: ProviderFactory, keychain: Keychain):
        self._db = conn
        self._factory = factory
        self._keychain = keychain

    def list_configs(self) -> list[AgentConfig]:
        rows = {
            r["role"]: AgentConfig.model_validate(row_to_dict(r))
            for r in self._db.execute("SELECT * FROM agent_configs")
        }
        return [rows[role] for role in ROLES if role in rows]

    def get_config(self, role: str) -> AgentConfig:
        if role not in ROLES:
            raise ApiError(404, "unknown_role", f"Unknown agent role {role}")
        row = self._db.execute("SELECT * FROM agent_configs WHERE role = ?", (role,)).fetchone()
        if row is None:
            raise ApiError(404, "unknown_role", f"Unknown agent role {role}")
        return AgentConfig.model_validate(row_to_dict(row))

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
        if "provider" in updates and updates["provider"] is not None:
            if not PROVIDER_SLUG_RE.match(updates["provider"]):
                raise ApiError(400, "invalid_provider", "provider slug is invalid")
        data.update({k: v for k, v in updates.items() if v is not None or k in ("system_prompt_override", "base_url")})
        provider = data["provider"]
        builtin = BUILTIN_PROVIDERS.get(provider)
        if builtin:
            data["protocol"] = updates.get("protocol") or data.get("protocol") or builtin["protocol"]
            if not data.get("base_url"):
                data["base_url"] = builtin["base_url"]
            if builtin["local_only"]:
                try:
                    validate_local_base_url(data["base_url"])
                except ProviderError as exc:
                    raise ApiError(400, exc.code, exc.message) from exc
        else:
            if not data.get("protocol") or not data.get("base_url"):
                raise ApiError(400, "invalid_provider", "custom provider requires protocol and base_url")
            if data["protocol"] == "ollama":
                try:
                    validate_local_base_url(data["base_url"])
                except ProviderError as exc:
                    raise ApiError(400, exc.code, exc.message) from exc
        if raw_key:
            try:
                data["api_key_ref"] = self._keychain.set(role, raw_key)
            except ProviderError as exc:
                raise ApiError(400, exc.code, exc.message) from exc
        data["updated_at"] = time.time()
        merged = AgentConfig.model_validate(data)
        self._db.execute(
            """UPDATE agent_configs SET display_name=?, provider=?, protocol=?, model_name=?,
               api_key_ref=?, base_url=?, system_prompt_override=?, temperature=?, max_tokens=?, updated_at=?
               WHERE role=?""",
            (
                merged.display_name, merged.provider, merged.protocol, merged.model_name,
                merged.api_key_ref, merged.base_url, merged.system_prompt_override,
                merged.temperature, merged.max_tokens, merged.updated_at, role,
            ),
        )
        self._db.commit()
        return merged

    def get_provider_for(self, role: AgentRole):
        cfg = self.get_config(role)
        return self._factory.build(cfg), cfg

    def provider_catalog(self) -> dict[str, Any]:
        used = {c.provider for c in self.list_configs()}
        return {
            "builtins": [
                {"slug": slug, **meta} for slug, meta in BUILTIN_PROVIDERS.items()
            ],
            "custom": sorted(p for p in used if p not in BUILTIN_PROVIDERS),
        }


class WorkspaceService:
    def __init__(self, conn):
        self._db = conn

    def create(self, body: WorkspaceCreate) -> Workspace:
        root = str(Path(body.root_path).expanduser().resolve())
        if not Path(root).is_dir():
            raise ApiError(400, "invalid_root", "root_path must be an existing directory")
        ws = Workspace(id=str(uuid.uuid4()), name=body.name, root_path=root, created_at=time.time())
        try:
            self._db.execute(
                "INSERT INTO workspaces (id, name, root_path, created_at) VALUES (?, ?, ?, ?)",
                (ws.id, ws.name, ws.root_path, ws.created_at),
            )
            self._db.commit()
        except Exception as exc:
            raise ApiError(409, "duplicate_workspace", str(exc)) from exc
        return ws

    def list(self) -> list[Workspace]:
        return [Workspace.model_validate(row_to_dict(r)) for r in self._db.execute("SELECT * FROM workspaces")]

    def get(self, workspace_id: str) -> Workspace:
        row = self._db.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        if row is None:
            raise ApiError(404, "unknown_workspace", "workspace not found")
        return Workspace.model_validate(row_to_dict(row))


class GoalService:
    def __init__(self, conn):
        self._db = conn

    def create(self, body: GoalCreate) -> Goal:
        row = self._db.execute("SELECT id FROM workspaces WHERE id = ?", (body.workspace_id,)).fetchone()
        if row is None:
            raise ApiError(404, "unknown_workspace", "workspace not found")
        now = time.time()
        goal = Goal(
            id=str(uuid.uuid4()),
            workspace_id=body.workspace_id,
            title=body.title,
            description=body.description,
            status="PLANNING",
            dry_run=body.dry_run,
            version=0,
            created_at=now,
            updated_at=now,
        )
        self._db.execute(
            """INSERT INTO goals (id, workspace_id, title, description, status, dry_run, version, event_seq, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)""",
            (goal.id, goal.workspace_id, goal.title, goal.description, goal.status, int(goal.dry_run), now, now),
        )
        self._db.commit()
        return goal

    def get(self, goal_id: str) -> Goal:
        row = self._db.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if row is None:
            raise ApiError(404, "unknown_goal", "goal not found")
        return Goal.model_validate({k: row[k] for k in Goal.model_fields if k != "dry_run"} | {"dry_run": bool(row["dry_run"])})

    def steps(self, goal_id: str) -> list[PlanStep]:
        rows = self._db.execute("SELECT * FROM plan_steps WHERE goal_id = ? ORDER BY ordinal", (goal_id,))
        out = []
        for r in rows:
            d = row_to_dict(r)
            d["suggested_paths"] = json.loads(d["suggested_paths"] or "[]")
            out.append(PlanStep.model_validate(d))
        return out

    def update_status(self, goal_id: str, expected_version: int, status: str) -> Goal:
        now = time.time()
        cur = self._db.execute(
            "UPDATE goals SET status=?, version=version+1, updated_at=? WHERE id=? AND version=?",
            (status, now, goal_id, expected_version),
        )
        if cur.rowcount != 1:
            g = self.get(goal_id)
            raise ApiError(409, "version_conflict", "version mismatch", {"current": g.model_dump()})
        self._db.commit()
        return self.get(goal_id)

    def next_sequence(self, goal_id: str) -> int:
        row = self._db.execute(
            "UPDATE goals SET event_seq = event_seq + 1 WHERE id = ? RETURNING event_seq",
            (goal_id,),
        ).fetchone()
        if row is None:
            raise ApiError(404, "unknown_goal", "goal not found")
        self._db.commit()
        return int(row[0])

    def publish(self, event: Event) -> Event:
        self._db.execute(
            "INSERT INTO events (id, goal_id, step_id, type, payload, timestamp, sequence) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event.id, event.goal_id, event.step_id, event.type, dumps(event.payload), event.timestamp, event.sequence),
        )
        self._db.commit()
        return event

    def events_after(self, goal_id: str, after: int) -> list[Event]:
        rows = self._db.execute(
            "SELECT * FROM events WHERE goal_id = ? AND sequence > ? ORDER BY sequence", (goal_id, after)
        )
        out = []
        for r in rows:
            d = row_to_dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(Event.model_validate(d))
        return out
