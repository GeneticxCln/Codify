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

    # Every known key with its (default, clamp). Anything read must be listed
    # here: an unknown key is not a setting, it is a typo.
    SPEC: dict[str, tuple[int, callable]] = {
        # How many steps of a parallel goal may run at once (see
        # executor.DEFAULT_PARALLEL_WIDTH). Clamped to a band a machine can take.
        "parallel_width": (4, lambda v: max(1, min(v, 16))),
    }

    def __init__(self, conn):
        self._db = conn

    def get_int(self, key: str) -> int:
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

    @staticmethod
    def _normalize_target(
        data: dict,
        updates: dict,
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
        merged = AgentConfig.model_validate(data)
        self._db.execute(
            """UPDATE agent_configs SET display_name=?, provider=?, protocol=?, model_name=?,
               api_key_ref=?, base_url=?, system_prompt_override=?, temperature=?, max_tokens=?,
               fallback_provider=?, fallback_model_name=?, fallback_protocol=?, fallback_base_url=?,
               updated_at=?
               WHERE role=?""",
            (
                merged.display_name, merged.provider, merged.protocol, merged.model_name,
                merged.api_key_ref, merged.base_url, merged.system_prompt_override,
                merged.temperature, merged.max_tokens,
                merged.fallback_provider, merged.fallback_model_name,
                merged.fallback_protocol, merged.fallback_base_url,
                merged.updated_at, role,
            ),
        )
        self._db.commit()
        return merged

    def get_provider_for(self, role: AgentRole):
        cfg = self.get_config(role)
        return self._factory.build(cfg), cfg

    def build_provider(self, cfg: AgentConfig):
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


class WorkspaceService:
    def __init__(self, conn):
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


class GoalService:
    def __init__(self, conn):
        self._db = conn

    def create(self, body: GoalCreate) -> Goal:
        row = self._db.execute("SELECT id FROM workspaces WHERE id = ?", (body.workspace_id,)).fetchone()
        if row is None:
            raise ApiError(404, "unknown_workspace", "workspace not found")
        now = time.time()
        title, description = split_title_description(body.title, body.description)
        goal = Goal(
            id=str(uuid.uuid4()),
            workspace_id=body.workspace_id,
            title=title,
            description=description,
            status="PLANNING",
            dry_run=body.dry_run,
            plan_only=body.plan_only,
            parallel=body.parallel,
            version=0,
            created_at=now,
            updated_at=now,
            provider=body.provider,
            model=body.model,
        )
        self._db.execute(
            """INSERT INTO goals (id, workspace_id, title, description, status, dry_run, plan_only, parallel, version, event_seq, created_at, updated_at, provider, model)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?)""",
            (goal.id, goal.workspace_id, goal.title, goal.description, goal.status, int(goal.dry_run), int(goal.plan_only), int(goal.parallel), now, now, goal.provider, goal.model),
        )
        self._db.commit()
        return goal

    def get(self, goal_id: str) -> Goal:
        row = self._db.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if row is None:
            raise ApiError(404, "unknown_goal", "goal not found")
        data = {k: row[k] for k in row.keys() if k in Goal.model_fields}
        data["dry_run"] = bool(row["dry_run"])
        data["plan_only"] = bool(row["plan_only"])
        data["parallel"] = bool(row["parallel"])
        return Goal.model_validate(data)

    def recent_run_models(self, limit: int = 5, scan_events: int = 300) -> list[dict]:
        """The models that *actually* answered recently, newest first.

        Read from `agent_assigned` events rather than from `goals.provider/model`,
        which record what the command bar asked for. Since per-role configs became
        authoritative (see `01` §2.2), those two are not the same thing: a goal can
        name one model and be executed by four others. Ordering a menu by "the last
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
        out: list[dict] = []
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

    def steps(self, goal_id: str) -> list[PlanStep]:
        rows = self._db.execute("SELECT * FROM plan_steps WHERE goal_id = ? ORDER BY ordinal", (goal_id,))
        out = []
        for r in rows:
            d = row_to_dict(r)
            d["suggested_paths"] = json.loads(d["suggested_paths"] or "[]")
            out.append(PlanStep.model_validate(d))
        return out

    def update_step(self, goal_id: str, step_id: str, expected_version: int, patch: dict) -> PlanStep:
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
        changes: dict[str, dict] = {
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

    def fail_orphaned_active_goals(self) -> list[tuple[str, str, str]]:
        """Mark goals left PLANNING/RUNNING by a dead process as FAILED.

        Called once at boot, before any request can see the stale state. Returns
        (goal_id, previous_status, message) so the caller can explain each rescue
        in the goal's own event log. Version is bumped unconditionally here —
        no client holds a view of a goal from a dead process, so there is
        nothing to conflict with. Publishes nothing: the terminal status event
        comes from the executor's `_fail`, so the stream shows one coherent
        story (log line, then failure) rather than two competing writers.
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
                continue  # concurrent change mid-boot; not ours to fight
            rescued.append((
                goal_id,
                previous,
                f"the engine restarted while this goal was {previous} — marking it failed; retry to run it again",
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

    def set_parallel(self, goal_id: str, enabled: bool) -> None:
        row = self._db.execute(
            "UPDATE goals SET parallel = ?, updated_at = ? WHERE id = ?",
            (int(enabled), time.time(), goal_id),
        )
        if row.rowcount != 1:
            raise ApiError(404, "unknown_goal", "goal not found")
        self._db.commit()

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
        self, goal_id: str, expected_version: int, status: str, step_id: str | None = None
    ) -> Goal:
        """Set the goal status and announce it.

        The event is published here rather than by callers so *every* status
        change reaches live streams — pause and cancel used to write the DB and
        stay silent, leaving open chats to discover the change by polling.
        """
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
            payload={"status": status, "version": updated.version},
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
