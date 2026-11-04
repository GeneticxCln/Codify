"""Export and import one JSON profile: how a person carries their setup to another machine or another project.

A profile holds what is *yours* and portable: the tuning settings, how each of the eight roles is set up, the skills
a workspace carries, and (only if asked) what that workspace has learned and the notes kept for every project. It is
a file a person may send to someone, commit, or read in an editor, which is why the rules are about what it can never
do:

* **It carries no credential.** Keys live in the keychain and are not read here. Every text field is also passed
  through the engine's one scrubber (`providers.redact_secrets`, with every stored key as a known secret), so a key a
  prompt or a lesson happened to quote is removed from the file rather than shipped in it.
* **Importing never widens what the engine may do.** Settings that open a door (browser actions, web access, network
  for commands, the sandbox's isolation, the CI check, cross-project notes) are not exported and are ignored if a file
  carries them: a profile from somewhere else must not be able to turn on the network by being opened. The same goes
  for anything that decides *where* text is sent: no `base_url`, no audio server, no checkpoint repository, and no
  provider. A provider name is an address too, with the person's stored key behind it, so a file that says `openai` on
  a role running a local model would move every prompt of that role to a hosted vendor. Neither does it replace a
  role's system prompt, which is instructions for the role from a file that may have come from anyone. A role takes the
  file's tuning (temperature, token and context limits, thinking effort, keep-alive) and its model name only while the
  provider stays the one it already has; the report says what was left.
* **Importing never overwrites.** A skill whose name exists is left alone, a memory subject that exists is left alone,
  a note already kept is left alone. Settings and role configs are the only things replaced, and only the person's
  explicit choice to apply them (`apply`) does that.
* **It goes through the doors that already exist.** Roles through `set_config` (invariant 2: agent configuration is
  written only by a settings route), skills through the creator that writes one new file under `.codify/skills/` and
  nothing else (invariant 9), settings through the same `set_int` / `set_str` that `PUT /settings/engine` uses, so
  every clamp and limit applies. A profile is data; it adds no path to any of them.
* **It is bounded.** At most 50 skills, 200 memory entries and 50 notes, each with a length cap, before anything is
  read. Memory arrives with proof 1 and no evidence, as a claim a person brought rather than one this machine saw.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Callable, Collection
from typing import Any, Literal

from pydantic import BaseModel, Field

from engine.models import (
    BUILTIN_PROVIDERS,
    ROLES,
    AgentConfigUpdate,
    SkillCreate,
    SkillCreated,
    Workspace,
)
from engine.notes import MAX_NOTE_CHARS, MAX_NOTES, NotesService
from engine.providers import Keychain, redact_secrets
from engine.services import AgentRegistryService, ApiError, SettingsService, WorkspaceService
from engine.skills import MAX_DESCRIPTION_CHARS, workspace_skills
from engine.suggestions import one_line

PROFILE_FORMAT = "codify-profile"
PROFILE_VERSION = 1

MAX_PROFILE_SKILLS = 50
MAX_PROFILE_MEMORY = 200
MAX_MEMORY_FIELD = 300

#: The numeric settings a profile carries: tuning, not authority. Anything not named here is not exported and is
#: ignored on import, so a setting added later is left out until someone decides it belongs.
PORTABLE_INT_SETTINGS = (
    "parallel_width", "stats_retention_days", "trace_retention_days", "event_retention_days",
    "conductor_max_turns", "conductor_max_moves", "conductor_drives_execution", "review_deep",
    "session_compact_auto", "session_compact_keep_turns", "suggestions", "suggest_paused_hours",
)
PORTABLE_STRING_SETTINGS = (
    "conductor_provider", "conductor_model", "conductor_fallback_provider", "conductor_fallback_model",
)
#: Said in every export, so a reader of the file knows these were left out on purpose and what to do about them.
LEFT_OUT = {
    "sandbox_network": "lets workspace commands reach the network",
    "sandbox_isolation": "decides whether workspace commands are jailed",
    "sandbox_unjailed_ok": "lets workspace code run when no jail can be built",
    "browser_actions": "lets the assistant click and type in your browser tabs",
    "web_fetch": "lets the assistant read web pages",
    "web_fetch_hosts": "names the sites it may read",
    "suggest_ci": "contacts GitHub without a button being pressed",
    "cross_workspace_notes": "lets notes written for one project reach another",
    "laya_required": "is a machine's own choice about its gate",
    "laya_checkpoint": "names a model repository to download",
    "audio": "voice settings name servers audio is sent to",
    "api keys": "never leave the keychain",
    "role addresses": "a role's base_url decides where your code is sent",
    "role providers": "a provider decides where your code is sent, and which stored key goes with it",
    "role prompts": "a prompt override is instructions for a role, taken from a file",
    "conductor providers": "the conductor's provider decides where your code is sent",
}
#: The role fields a profile exports. `api_key`, `base_url` and the protocol are not among them. Export carries the
#: provider and the prompt so a file reads as a record of the setup; import applies only `ROLE_TUNING_FIELDS` and a
#: model name that stays on the role's own provider.
ROLE_FIELDS = (
    "provider", "model_name", "temperature", "max_tokens", "thinking_effort", "num_ctx", "keep_alive",
    "system_prompt_override", "fallback_provider", "fallback_model_name",
)
#: What an import writes to a role without conditions: numbers and flags about how it runs, not where it runs.
ROLE_TUNING_FIELDS = ("temperature", "max_tokens", "thinking_effort", "num_ctx", "keep_alive")
#: The conductor's routing settings. Export carries them; import leaves them as the person has them.
ROUTING_STRING_SETTINGS = {
    "conductor_provider": "conductor_model",
    "conductor_fallback_provider": "conductor_fallback_model",
}


class ProfileRole(BaseModel):
    model_config = {"extra": "forbid"}
    provider: str | None = Field(None, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    model_name: str | None = Field(None, max_length=128)
    temperature: float | None = Field(None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(None, gt=0, le=200000)
    thinking_effort: str | None = Field(None, max_length=16)
    num_ctx: int | None = Field(None, gt=0, le=1_000_000)
    keep_alive: str | None = Field(None, max_length=32)
    system_prompt_override: str | None = Field(None, max_length=32768)
    fallback_provider: str | None = Field(None, max_length=64, pattern=r"^([a-z][a-z0-9_-]*)?$")
    fallback_model_name: str | None = Field(None, max_length=128)


class ProfileSettings(BaseModel):
    model_config = {"extra": "forbid"}
    ints: dict[str, int] = Field(default_factory=dict, max_length=64)
    strings: dict[str, str] = Field(default_factory=dict, max_length=64)


class ProfileSkill(BaseModel):
    model_config = {"extra": "forbid"}
    name: str = Field(..., min_length=1, max_length=64)
    description: str = Field(..., min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    moves: list[str] = Field(default_factory=list, max_length=24)
    instructions: str = Field(..., min_length=1, max_length=32768)


class ProfileMemory(BaseModel):
    model_config = {"extra": "forbid"}
    subject: str = Field(..., min_length=1, max_length=200)
    lesson: str = Field(..., min_length=1, max_length=2000)
    example: str = Field("", max_length=2000)


class ProfileNote(BaseModel):
    model_config = {"extra": "forbid"}
    text: str = Field(..., min_length=1, max_length=MAX_NOTE_CHARS * 2)


class Profile(BaseModel):
    """The file. `extra=forbid` all the way down: a field this engine does not know is refused, not ignored, so a file
    from a newer build says so instead of quietly applying half of itself."""

    model_config = {"extra": "forbid"}
    format: Literal["codify-profile"]
    version: int = Field(..., ge=1, le=PROFILE_VERSION)
    exported_at: float | None = None
    settings: ProfileSettings = Field(default_factory=ProfileSettings)
    roles: dict[str, ProfileRole] = Field(default_factory=dict, max_length=len(ROLES))
    skills: list[ProfileSkill] = Field(default_factory=list, max_length=MAX_PROFILE_SKILLS)
    memory: list[ProfileMemory] = Field(default_factory=list, max_length=MAX_PROFILE_MEMORY)
    notes: list[ProfileNote] = Field(default_factory=list, max_length=MAX_NOTES)
    left_out: dict[str, str] = Field(default_factory=dict, max_length=64)


class ProfileApply(BaseModel):
    """What the person chose to apply. Skills, memory and notes are opt-in: they are off unless asked for.

    Settings and role tuning are the person's own setup moving between machines. A skill is instructions the conductor
    reads, a lesson is a claim it may act on and a note is text it is shown, each from a file that may have come from
    anyone, so none of them is applied because a file was opened.
    """

    model_config = {"extra": "forbid"}
    settings: bool = True
    roles: bool = True
    skills: bool = False
    memory: bool = False
    notes: bool = False


class ProfileImport(BaseModel):
    model_config = {"extra": "forbid"}
    profile: Profile
    # Where skills and memory land. Required only if the profile carries them and they are to be applied.
    workspace_id: str | None = Field(None, max_length=200)
    apply: ProfileApply = Field(default_factory=ProfileApply)


class ProfileTools:
    """The services a profile reads and writes, held together so the two directions share one scrubber."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: SettingsService,
        registry: AgentRegistryService,
        workspaces: WorkspaceService,
        notes: NotesService,
        keychain: Keychain,
        known_moves: Collection[str] | None = None,
    ) -> None:
        self._db = conn
        self.settings = settings
        self.registry = registry
        self.workspaces = workspaces
        self.notes = notes
        self.keychain = keychain
        self.known_moves = known_moves

    # ── the scrubber ────────────────────────────────────────────────────────────────────────────────────────────

    def _known_secrets(self) -> tuple[str, ...]:
        """Every credential this machine holds, by value, so an echo of one in a prompt or a lesson is found."""
        slugs = set(BUILTIN_PROVIDERS)
        for cfg in self.registry.list_configs():
            slugs.add(cfg.provider)
            if cfg.fallback_provider:
                slugs.add(cfg.fallback_provider)
        found: list[str] = []
        for slug in sorted(slugs):
            value = ""
            try:
                value = self.keychain.get_provider_key(slug)
            except Exception:  # noqa: BLE001 — a keychain that cannot answer has nothing to scrub with
                value = ""
            if value:
                found.append(value)
        # An MCP server's credential is the other secret the engine holds (docs/00 §6.10). It lives in the same store
        # under `mcp/<id>`, and a prompt, lesson or note that quoted it was exported verbatim unless it happened to be
        # spelled `Bearer ...`.
        try:
            refs = [str(r["api_key_ref"]) for r in self._db.execute(
                "SELECT api_key_ref FROM mcp_servers WHERE api_key_ref != ''"
            ).fetchall()]
        except sqlite3.Error:
            refs = []
        for ref in refs:
            try:
                secret = self.keychain.get(ref)
            except Exception:  # noqa: BLE001 — a keychain that cannot answer has nothing to scrub with
                secret = ""
            if secret:
                found.append(secret)
        return tuple(found)

    # ── export ──────────────────────────────────────────────────────────────────────────────────────────────────

    def export(
        self, *, workspace: Workspace | None, include_memory: bool, include_notes: bool,
    ) -> dict[str, Any]:
        secrets = self._known_secrets()

        def clean(text: str) -> str:
            return redact_secrets(text, secrets)

        ints = {key: self.settings.get_int(key) for key in PORTABLE_INT_SETTINGS}
        strings = {key: clean(self.settings.get_str(key)) for key in PORTABLE_STRING_SETTINGS}
        roles: dict[str, Any] = {}
        for cfg in self.registry.list_configs():
            data = cfg.model_dump()
            entry = {name: data.get(name) for name in ROLE_FIELDS}
            if entry.get("system_prompt_override"):
                entry["system_prompt_override"] = clean(str(entry["system_prompt_override"]))
            roles[cfg.role] = {k: v for k, v in entry.items() if v is not None}

        skills: list[dict[str, Any]] = []
        if workspace is not None:
            found, _problems = workspace_skills(workspace.root_path, self.known_moves)
            for skill in found[:MAX_PROFILE_SKILLS]:
                skills.append({
                    "name": skill.name,
                    "description": clean(skill.description),
                    "moves": list(skill.moves),
                    "instructions": clean(skill.body),
                })

        memory: list[dict[str, Any]] = []
        if include_memory and workspace is not None:
            rows = self._db.execute(
                "SELECT subject, lesson, example FROM observations WHERE workspace_id = ? "
                "AND subject NOT LIKE 'step:%' ORDER BY refined_at DESC, subject LIMIT ?",
                (workspace.id, MAX_PROFILE_MEMORY),
            ).fetchall()
            memory = [
                {"subject": clean(r["subject"])[:200], "lesson": clean(r["lesson"])[:2000],
                 "example": clean(r["example"])[:2000]}
                for r in rows if r["lesson"]
            ]

        notes = [{"text": clean(n["text"])} for n in self.notes.list()] if include_notes else []
        return {
            "format": PROFILE_FORMAT,
            "version": PROFILE_VERSION,
            "exported_at": time.time(),
            "settings": {"ints": ints, "strings": strings},
            "roles": roles,
            "skills": skills,
            "memory": memory,
            "notes": notes,
            "left_out": dict(LEFT_OUT),
        }

    # ── import ──────────────────────────────────────────────────────────────────────────────────────────────────

    def apply(
        self,
        body: ProfileImport,
        workspace: Workspace | None,
        make_skill: Callable[[Workspace, SkillCreate], SkillCreated],
    ) -> dict[str, Any]:
        """Apply what the person chose. `make_skill` is the skill creator, handed in by the route.

        It is a parameter and not something this module reaches for: the creator is a person-only door that the
        route module alone may name (docs/00 §6.9), and a profile is exactly the sort of file whose contents must
        not be able to widen that.
        """
        profile, choice = body.profile, body.apply
        secrets = self._known_secrets()
        applied: dict[str, Any] = {"settings": [], "roles": [], "skills": [], "memory": 0, "notes": 0}
        skipped: list[dict[str, str]] = []

        def skip(what: str, reason: str) -> None:
            skipped.append({"what": what, "reason": reason})

        if choice.settings:
            self._apply_settings(profile.settings, applied, skip)
        if choice.roles:
            self._apply_roles(profile.roles, applied, skip)
        if choice.skills and profile.skills:
            if workspace is None:
                skip("skills", "no workspace was named to receive them")
            else:
                self._apply_skills(profile.skills, workspace, make_skill, secrets, applied, skip)
        if choice.memory and profile.memory:
            if workspace is None:
                skip("memory", "no workspace was named to receive it")
            else:
                applied["memory"] = self._apply_memory(profile.memory, workspace, secrets, skip)
        if choice.notes and profile.notes:
            applied["notes"] = self._apply_notes(profile.notes, secrets, skip)
        for what, entry in (("skills", profile.skills), ("memory", profile.memory), ("notes", profile.notes)):
            if entry and not getattr(choice, what):
                skip(what, "present in the file, not applied (you did not choose it)")
        return {"applied": applied, "skipped": skipped}

    def _apply_settings(self, settings: ProfileSettings, applied: dict[str, Any], skip: Any) -> None:
        for key, value in settings.ints.items():
            if key not in PORTABLE_INT_SETTINGS:
                skip(f"setting {key}", LEFT_OUT.get(key, "not a setting a profile carries"))
                continue
            applied["settings"].append(key)
            self.settings.set_int(key, value)
        pair_of = {model: provider for provider, model in ROUTING_STRING_SETTINGS.items()}
        for key, text in settings.strings.items():
            if key not in PORTABLE_STRING_SETTINGS:
                skip(f"setting {key}", LEFT_OUT.get(key, "not a setting a profile carries"))
                continue
            if key in ROUTING_STRING_SETTINGS:
                if text.strip() != self.settings.get_str(key):
                    skip(f"setting {key}", LEFT_OUT["conductor providers"])
                continue
            provider_key = pair_of.get(key)
            if provider_key is not None:
                # A model name belongs to a provider. The file's name is kept only while the file and the person
                # agree on which provider that is.
                asked = settings.strings.get(provider_key)
                if asked is not None and asked.strip() != self.settings.get_str(provider_key):
                    skip(f"setting {key}", "it belongs to a different provider than the one set here")
                    continue
            try:
                self.settings.set_str(key, text)
                applied["settings"].append(key)
            except ApiError as exc:
                skip(f"setting {key}", exc.message)

    def _apply_roles(self, roles: dict[str, ProfileRole], applied: dict[str, Any], skip: Any) -> None:
        for role, spec in roles.items():
            if role not in ROLES:
                skip(f"role {role}", "not one of the eight roles")
                continue
            fields = spec.model_dump(exclude_unset=True)
            current = self.registry.get_config(role)
            update: dict[str, Any] = {name: fields[name] for name in ROLE_TUNING_FIELDS if fields.get(name) is not None}

            # Where a role's text goes, and what it is told, stay as the person set them. Naming the same value the
            # role already has changes nothing and is not reported.
            for name, reason in (
                ("provider", LEFT_OUT["role providers"]),
                ("fallback_provider", LEFT_OUT["role providers"]),
                ("fallback_model_name", "it belongs to the fallback provider, which a file does not choose"),
                ("system_prompt_override", LEFT_OUT["role prompts"]),
            ):
                asked = fields.get(name)
                if asked and asked != (getattr(current, name, None) or ""):
                    skip(f"role {role} {name}", reason)
            model = fields.get("model_name")
            if model is not None:
                asked_provider = fields.get("provider")
                if asked_provider in (None, current.provider):
                    update["model_name"] = model
                else:
                    skip(
                        f"role {role} model_name",
                        f"it is for {asked_provider!r}, and this role stays on {current.provider!r}",
                    )
            if not update:
                continue
            try:
                self.registry.set_config(role, AgentConfigUpdate(**update))
                applied["roles"].append(role)
            except (ApiError, ValueError) as exc:
                skip(f"role {role}", getattr(exc, "message", None) or str(exc).splitlines()[0])

    def _apply_skills(
        self, skills: list[ProfileSkill], workspace: Workspace,
        make_skill: Callable[[Workspace, SkillCreate], SkillCreated], secrets: tuple[str, ...],
        applied: dict[str, Any], skip: Any,
    ) -> None:
        for item in skills:
            try:
                made = make_skill(
                    workspace,
                    SkillCreate(
                        name=item.name, description=redact_secrets(item.description, secrets),
                        moves=item.moves, instructions=redact_secrets(item.instructions, secrets),
                    ),
                )
                applied["skills"].append(made.name)
            except ApiError as exc:
                skip(f"skill {item.name}", "already in this workspace" if exc.code == "skill_exists" else exc.message)

    def _apply_memory(
        self, memory: list[ProfileMemory], workspace: Workspace, secrets: tuple[str, ...], skip: Any,
    ) -> int:
        now = time.time()
        written = 0
        for item in memory:
            subject = one_line(redact_secrets(item.subject, secrets), 200)
            lesson = one_line(redact_secrets(item.lesson, secrets), MAX_MEMORY_FIELD)
            example = one_line(redact_secrets(item.example, secrets), MAX_MEMORY_FIELD)
            if not subject or not lesson or subject.startswith("step:"):
                skip(f"memory {item.subject[:40]!r}", "empty, or a step marker rather than a lasting lesson")
                continue
            if self._db.execute(
                "SELECT 1 FROM observations WHERE workspace_id = ? AND subject = ?", (workspace.id, subject),
            ).fetchone() is not None:
                skip(f"memory {subject[:40]!r}", "this workspace already has its own")
                continue
            self._db.execute(
                "INSERT INTO observations (id, workspace_id, subject, lesson, example, proof, evidence, created_at, "
                "refined_at) VALUES (?, ?, ?, ?, ?, 1, '[]', ?, ?)",
                (str(uuid.uuid4()), workspace.id, subject, lesson, example, now, now),
            )
            written += 1
        self._db.commit()
        return written

    def _apply_notes(self, notes: list[ProfileNote], secrets: tuple[str, ...], skip: Any) -> int:
        kept = 0
        for item in notes:
            try:
                self.notes.add(redact_secrets(item.text, secrets), source="imported")
                kept += 1
            except ApiError as exc:
                skip(f"note {item.text[:40]!r}", exc.message)
        return kept
