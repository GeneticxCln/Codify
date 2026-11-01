import React, { useEffect, useRef, useState } from "react";
import { AlertCircle, Plus, RefreshCw, Search, X } from "lucide-react";
import {
  createAgentSkill,
  getAgentSkill,
  getAgentSkills,
  importAgentSkill,
  setAgentSkillEnabled,
} from "../api";
import { readRejection } from "../rejection";
import type { AgentSkill, AgentSkillCreated, AgentSkillDetail } from "../types";
import { Checkbox } from "./ui/Checkbox";

/**
 * Settings → Agent skills.
 *
 * The conductor is shown every enabled skill's name and one-line description, and
 * fetches a body only when it calls `use_skill`. This pane decides which names are
 * offered at all. A disabled skill is not a deleted file and not a new capability:
 * it is absent from the menu, so the conductor cannot choose it by accident.
 *
 * The switch belongs to the selected workspace, not the install: two repositories
 * may reasonably want different recipes. The full instructions are fetched only
 * when a person previews one, for the same reason the conductor fetches them only
 * when it chooses one — a list does not need every body.
 *
 * The creator is the person's door to a new `.codify/skills/<name>.md`. It is not
 * an agent capability: `createAgentSkill` and `importAgentSkill` are the only two
 * routes that reach the engine's create-only writer (`docs/00` §6.9).
 */
type SkillFilter = "all" | "enabled" | "disabled" | "builtin" | "workspace";
type CreatorMode = "write" | "upload";

interface Draft {
  name: string;
  description: string;
  moves: string[];
  instructions: string;
}

const EMPTY_DRAFT: Draft = { name: "", description: "", moves: [], instructions: "" };
const MAX_SKILL_CHARS = 32_768;
const MAX_SKILL_UPLOAD_BYTES = MAX_SKILL_CHARS * 4;

const FILTERS: ReadonlyArray<{ value: SkillFilter; label: string }> = [
  { value: "all", label: "All" },
  { value: "enabled", label: "Enabled" },
  { value: "disabled", label: "Disabled" },
  { value: "builtin", label: "Built-in" },
  { value: "workspace", label: "Workspace" },
];

/**
 * How many skills make a search and filter row worth its height.
 *
 * Below this the whole list fits in a glance, and a field above two rows asks the
 * person to type the name of something already on screen. A workspace with nine
 * built-ins starts with the controls, and one with two does not.
 */
const FILTER_FROM = 6;

function matchesQuery(skill: AgentSkill, tokens: string[]): boolean {
  if (tokens.length === 0) return true;
  const haystack = `${skill.name} ${skill.description} ${skill.moves.join(" ")}`.toLowerCase();
  return tokens.every((token) => haystack.includes(token));
}

function matchesFilter(skill: AgentSkill, filter: SkillFilter): boolean {
  if (filter === "enabled") return skill.enabled;
  if (filter === "disabled") return !skill.enabled;
  if (filter === "builtin") return skill.source === "built-in";
  if (filter === "workspace") return skill.source === "workspace";
  return true;
}

export const AgentSkillsPane: React.FC<{ workspaceId?: string }> = ({ workspaceId }) => {
  const [skills, setSkills] = useState<AgentSkill[] | null>(null);
  const [availableMoves, setAvailableMoves] = useState<string[]>([]);
  const [problems, setProblems] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<Record<string, boolean>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<SkillFilter>("all");
  const [refreshing, setRefreshing] = useState(false);
  const [previewName, setPreviewName] = useState<string | null>(null);
  const [preview, setPreview] = useState<AgentSkillDetail | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [creatorOpen, setCreatorOpen] = useState(false);
  const [creatorMode, setCreatorMode] = useState<CreatorMode>("write");
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [upload, setUpload] = useState<{ filename: string; content: string } | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  /** Which preview request is current, so a slow answer cannot replace a newer one. */
  const previewRequest = useRef(0);
  /** Which list request is current; a slow refresh must not replace a newer load. */
  const listRequest = useRef(0);

  const loadSkills = async (resetFilters: boolean, announce: boolean): Promise<boolean> => {
    if (!workspaceId) return false;
    const request = ++listRequest.current;
    setRefreshing(true);
    setError(null);
    setMessage(null);
    try {
      const result = await getAgentSkills(workspaceId);
      if (listRequest.current !== request) return false;
      setSkills(result.skills);
      setAvailableMoves(result.available_moves ?? []);
      setProblems(result.problems ?? []);
      if (resetFilters) {
        setQuery("");
        setFilter("all");
      }
      if (previewName && !result.skills.some((skill) => skill.name === previewName)) {
        setPreviewName(null);
        setPreview(null);
        setPreviewError(null);
      }
      if (announce) setMessage("Skills refreshed from this workspace.");
      return true;
    } catch (err: unknown) {
      if (listRequest.current === request) {
        setError(readRejection(err, "Could not refresh the skills."));
      }
      return false;
    } finally {
      if (listRequest.current === request) setRefreshing(false);
    }
  };

  useEffect(() => {
    listRequest.current += 1;
    previewRequest.current += 1;
    setSkills(null);
    setAvailableMoves([]);
    setProblems([]);
    setError(null);
    setMessage(null);
    setQuery("");
    setFilter("all");
    setRefreshing(false);
    setPreviewName(null);
    setPreview(null);
    setPreviewError(null);
    setCreatorOpen(false);
    setCreatorMode("write");
    setDraft({ ...EMPTY_DRAFT, moves: [] });
    setUpload(null);
    setUploadError(null);
    setCreateError(null);
    setCreating(false);

    if (!workspaceId) {
      return () => {
        listRequest.current += 1;
      };
    }

    void loadSkills(true, false);
    return () => {
      listRequest.current += 1;
    };
  }, [workspaceId]);

  const save = async (skill: AgentSkill, enabled: boolean) => {
    if (!workspaceId) return;
    setMessage(null);
    setError(null);
    setSaving((current) => ({ ...current, [skill.name]: true }));
    setSkills((current) =>
      current?.map((entry) => (entry.name === skill.name ? { ...entry, enabled } : entry)) ?? current,
    );
    try {
      const result = await setAgentSkillEnabled(skill.name, enabled, workspaceId);
      setSkills((current) =>
        current?.map((entry) =>
          entry.name === result.name ? { ...entry, enabled: result.enabled } : entry,
        ) ?? current,
      );
      setMessage(
        enabled
          ? `${result.name} is on for this workspace — the conductor can choose it again.`
          : `${result.name} is off for this workspace — the conductor will not be offered it.`,
      );
    } catch (err: unknown) {
      // Roll back only the switch that failed. Replacing the whole list would undo a
      // second toggle that succeeded while this request was in flight.
      setSkills((current) =>
        current?.map((entry) =>
          entry.name === skill.name ? { ...entry, enabled: skill.enabled } : entry,
        ) ?? current,
      );
      setError(readRejection(err, `Could not change ${skill.name}.`));
    } finally {
      setSaving((current) => {
        const next = { ...current };
        delete next[skill.name];
        return next;
      });
    }
  };

  const togglePreview = async (skill: AgentSkill) => {
    if (!workspaceId) return;
    if (previewName === skill.name) {
      previewRequest.current += 1;
      setPreviewName(null);
      setPreview(null);
      setPreviewError(null);
      return;
    }
    const request = ++previewRequest.current;
    setPreviewName(skill.name);
    setPreview(null);
    setPreviewError(null);
    try {
      const result = await getAgentSkill(skill.name, workspaceId);
      if (previewRequest.current !== request) return;
      setPreview(result);
    } catch (err: unknown) {
      if (previewRequest.current !== request) return;
      setPreviewError(readRejection(err, `Could not read ${skill.name}.`));
    }
  };

  const clearFilters = () => {
    setQuery("");
    setFilter("all");
  };

  const openCreator = () => {
    setCreatorOpen(true);
    setCreatorMode("write");
    setDraft({ ...EMPTY_DRAFT, moves: [] });
    setUpload(null);
    setUploadError(null);
    setCreateError(null);
    setMessage(null);
  };

  const closeCreator = () => {
    if (creating) return;
    setCreatorOpen(false);
    setCreatorMode("write");
    setDraft({ ...EMPTY_DRAFT, moves: [] });
    setUpload(null);
    setUploadError(null);
    setCreateError(null);
  };

  const afterCreate = async (created: AgentSkillCreated) => {
    setCreatorOpen(false);
    setCreatorMode("write");
    setDraft({ ...EMPTY_DRAFT, moves: [] });
    setUpload(null);
    setUploadError(null);
    setCreateError(null);
    const refreshed = await loadSkills(true, false);
    setMessage(
      `${created.name} was created at ${created.path} and is on for this workspace. ` +
        `It is available to the next conductor turn.` +
        (created.shadows ? " It replaces the built-in skill of that name here." : "") +
        (refreshed ? "" : " Refresh the list to see it."),
    );
  };

  const submitCreate = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!workspaceId || creating) return;
    setCreateError(null);
    setMessage(null);
    if (!draft.name.trim() || !draft.description.trim() || !draft.instructions.trim()) {
      setCreateError("Give the skill a name, a one-line description and instructions.");
      return;
    }
    setCreating(true);
    try {
      const created = await createAgentSkill(workspaceId, {
        name: draft.name,
        description: draft.description,
        moves: draft.moves,
        instructions: draft.instructions,
      });
      await afterCreate(created);
    } catch (err: unknown) {
      setCreateError(readRejection(err, "Could not create the skill."));
    } finally {
      setCreating(false);
    }
  };

  const handleFile = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const input = event.currentTarget;
    const file = input.files?.[0];
    input.value = "";
    setUpload(null);
    setUploadError(null);
    setCreateError(null);
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".md")) {
      setUploadError("Choose a .md file — a skill is Markdown, not a script or an archive.");
      return;
    }
    if (file.size > MAX_SKILL_UPLOAD_BYTES) {
      setUploadError(
        `That file is ${file.size.toLocaleString()} bytes; a skill is at most ${MAX_SKILL_CHARS.toLocaleString()} characters.`,
      );
      return;
    }
    let content: string;
    try {
      content = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
    } catch {
      setUploadError("That file is not valid UTF-8 text.");
      return;
    }
    if (!content.trim()) {
      setUploadError("That file is empty.");
      return;
    }
    if (content.length > MAX_SKILL_CHARS) {
      setUploadError(`That file is over the ${MAX_SKILL_CHARS.toLocaleString()}-character skill limit.`);
      return;
    }
    if (content.includes("\u0000")) {
      setUploadError("That file is binary, not Markdown text.");
      return;
    }
    setUpload({ filename: file.name, content });
  };

  const submitUpload = async () => {
    if (!workspaceId || creating) return;
    setCreateError(null);
    setUploadError(null);
    setMessage(null);
    if (!upload) {
      setUploadError("Choose a .md file first.");
      return;
    }
    setCreating(true);
    try {
      const created = await importAgentSkill(workspaceId, upload);
      await afterCreate(created);
    } catch (err: unknown) {
      setUploadError(readRejection(err, "Could not import the skill."));
    } finally {
      setCreating(false);
    }
  };

  if (!workspaceId) {
    return (
      <p className="text-xs leading-relaxed text-codify-muted">
        Select a project first — skill switches and new skills belong to one workspace, because the
        same skill can be useful in one repository and noise in another.
      </p>
    );
  }

  if (skills === null) {
    return (
      <p className="text-xs text-codify-muted">
        {error ?? "Reading the skills this workspace can offer…"}
      </p>
    );
  }

  const tokens = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const visible = skills.filter(
    (skill) => matchesQuery(skill, tokens) && matchesFilter(skill, filter),
  );
  const filterable = skills.length >= FILTER_FROM;
  const activeFilter = FILTERS.find((entry) => entry.value === filter) ?? FILTERS[0];

  return (
    <div className="space-y-3">
      <div className="rounded-xl border border-codify-border bg-codify-surface p-3.5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h3 className="text-xs font-semibold text-codify-secondary">
              Skills the conductor may choose from
            </h3>
            <p className="mt-1 text-xs leading-relaxed text-codify-muted">
              Enabled skills are offered by name and one-line description. The conductor fetches a
              skill's full instructions only when it calls <code>use_skill</code>, so it chooses what
              it needs instead of carrying every skill in every prompt. A disabled skill is not
              offered at all.
            </p>
            <p className="mt-1.5 text-xs leading-relaxed text-codify-muted">
              These switches belong to the selected project. A workspace adds its own
              <code> .codify/skills/ </code> to the list; turning one off does not delete or change
              the file.
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            <button
              type="button"
              onClick={openCreator}
              className="flex items-center gap-1 rounded-lg border border-codify-border bg-codify-bg px-2.5 py-1.5 text-2xs font-semibold text-codify-secondary transition-colors hover:border-codify-accent hover:text-codify-primary"
            >
              <Plus className="h-3.5 w-3.5" /> New skill
            </button>
            <button
              type="button"
              onClick={() => void loadSkills(false, true)}
              disabled={refreshing}
              aria-label="Refresh skills"
              title="Refresh skills"
              className="rounded-lg border border-codify-border bg-codify-bg p-1.5 text-codify-muted transition-colors hover:border-codify-accent hover:text-codify-secondary disabled:cursor-wait disabled:opacity-60"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${refreshing ? "animate-spin" : ""}`} />
            </button>
          </div>
        </div>
      </div>

      {creatorOpen && (
        <div
          role="region"
          aria-label="New skill"
          className="rounded-xl border border-codify-accent/50 bg-codify-bg p-3.5"
        >
          <div className="flex items-start justify-between gap-3">
            <div>
              <h4 className="text-xs font-semibold text-codify-secondary">New skill</h4>
              <p className="mt-1 text-xs leading-relaxed text-codify-muted">
                Write one or upload a Markdown file. It becomes
                <code> .codify/skills/&lt;name&gt;.md </code> in this workspace, on by default, and
                the conductor can choose it on its next turn.
              </p>
            </div>
            <button
              type="button"
              onClick={closeCreator}
              disabled={creating}
              aria-label="Close new skill"
              className="shrink-0 rounded-lg p-1 text-codify-muted hover:bg-codify-raised hover:text-codify-secondary disabled:opacity-50"
            >
              <X className="h-4 w-4" />
            </button>
          </div>

          <div role="group" aria-label="Skill source" className="mt-3 flex gap-1">
            {(["write", "upload"] as const).map((mode) => (
              <button
                key={mode}
                type="button"
                aria-pressed={creatorMode === mode}
                onClick={() => {
                  setCreatorMode(mode);
                  setCreateError(null);
                  setUploadError(null);
                }}
                disabled={creating}
                className={
                  "rounded-lg border px-2.5 py-1 text-2xs font-semibold transition-colors disabled:opacity-50 " +
                  (creatorMode === mode
                    ? "border-codify-accent bg-codify-accent/20 text-codify-accent-ink"
                    : "border-codify-border text-codify-muted hover:text-codify-secondary")
                }
              >
                {mode === "write" ? "Write" : "Upload"}
              </button>
            ))}
          </div>

          {creatorMode === "write" ? (
            <form onSubmit={(event) => void submitCreate(event)} className="mt-3 space-y-2.5">
              <label className="block text-2xs font-medium text-codify-secondary">
                Name
                <input
                  aria-label="New skill name"
                  value={draft.name}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, name: event.target.value }))
                  }
                  placeholder="release-check"
                  autoComplete="off"
                  spellCheck={false}
                  disabled={creating}
                  className="mt-1 block w-full rounded-lg border border-codify-border bg-codify-surface px-2.5 py-1.5 font-mono text-xs text-codify-secondary placeholder-codify-muted focus:border-codify-accent focus:outline-hidden"
                />
                <span className="mt-0.5 block text-2xs font-normal text-codify-muted">
                  Lowercase letters, digits and dashes; it becomes the filename.
                </span>
              </label>
              <label className="block text-2xs font-medium text-codify-secondary">
                Description
                <input
                  aria-label="New skill description"
                  value={draft.description}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, description: event.target.value }))
                  }
                  placeholder="Checks a release before tagging."
                  maxLength={200}
                  disabled={creating}
                  className="mt-1 block w-full rounded-lg border border-codify-border bg-codify-surface px-2.5 py-1.5 text-xs text-codify-secondary placeholder-codify-muted focus:border-codify-accent focus:outline-hidden"
                />
              </label>
              <div>
                <span className="text-2xs font-medium text-codify-secondary">Moves</span>
                <p className="mt-0.5 text-2xs text-codify-muted">
                  What the instructions are written around. This is a hint, not a grant.
                </p>
                {availableMoves.length > 0 ? (
                  <div className="mt-1.5 flex max-h-28 flex-wrap gap-x-3 gap-y-1 overflow-y-auto rounded-lg border border-codify-border bg-codify-surface p-2">
                    {availableMoves.map((move) => (
                      <Checkbox
                        key={move}
                        checked={draft.moves.includes(move)}
                        disabled={creating}
                        onChange={() =>
                          setDraft((current) => ({
                            ...current,
                            moves: current.moves.includes(move)
                              ? current.moves.filter((entry) => entry !== move)
                              : [...current.moves, move],
                          }))
                        }
                        className="font-mono text-2xs text-codify-muted"
                      >
                        {move}
                      </Checkbox>
                    ))}
                  </div>
                ) : (
                  <p className="mt-1 text-2xs text-codify-muted">
                    The engine has not reported any moves; leave this empty.
                  </p>
                )}
              </div>
              <label className="block text-2xs font-medium text-codify-secondary">
                Instructions
                <textarea
                  aria-label="New skill instructions"
                  value={draft.instructions}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, instructions: event.target.value }))
                  }
                  placeholder={"1. Read the release manifest.\n2. Run the narrowest check.\n3. Report what passed and what did not."}
                  maxLength={MAX_SKILL_CHARS}
                  rows={6}
                  disabled={creating}
                  className="mt-1 block w-full resize-y rounded-lg border border-codify-border bg-codify-surface px-2.5 py-1.5 font-mono text-xs leading-relaxed text-codify-secondary placeholder-codify-muted focus:border-codify-accent focus:outline-hidden"
                />
                <span className="mt-0.5 block text-2xs font-normal text-codify-muted">
                  {draft.instructions.length.toLocaleString()} / {MAX_SKILL_CHARS.toLocaleString()}{" "}
                  characters
                </span>
              </label>
              <div className="flex flex-wrap items-center gap-2 pt-1">
                <button
                  type="submit"
                  disabled={
                    creating ||
                    !draft.name.trim() ||
                    !draft.description.trim() ||
                    !draft.instructions.trim()
                  }
                  className="rounded-lg bg-codify-accent px-3 py-1.5 text-2xs font-semibold text-codify-bg transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {creating ? "Creating…" : "Create skill"}
                </button>
                <button
                  type="button"
                  onClick={closeCreator}
                  disabled={creating}
                  className="rounded-lg px-2.5 py-1.5 text-2xs font-medium text-codify-muted hover:text-codify-secondary disabled:opacity-50"
                >
                  Cancel
                </button>
              </div>
            </form>
          ) : (
            <div className="mt-3 space-y-2.5">
              <label className="block rounded-lg border border-dashed border-codify-border bg-codify-surface p-3 text-xs text-codify-muted">
                <span className="font-medium text-codify-secondary">Choose a Markdown skill</span>
                <span className="mt-1 block">
                  One .md file. A header with <code>name</code>, <code>description</code> and{" "}
                  <code>moves</code> is optional; without one, the filename is the name and the first
                  line is the description.
                </span>
                <input
                  type="file"
                  accept=".md,text/markdown"
                  aria-label="Upload a skill file"
                  onChange={(event) => void handleFile(event)}
                  disabled={creating}
                  className="mt-2 block w-full text-2xs text-codify-muted file:mr-2 file:rounded-md file:border file:border-codify-border file:bg-codify-bg file:px-2 file:py-1 file:text-2xs file:text-codify-secondary"
                />
              </label>
              {upload && (
                <p role="status" className="text-2xs text-codify-muted">
                  Ready to upload <span className="font-mono text-codify-secondary">{upload.filename}</span>.
                </p>
              )}
              {uploadError && (
                <p role="alert" className="text-xs text-codify-danger">
                  {uploadError}
                </p>
              )}
              <div className="flex flex-wrap items-center gap-2 pt-1">
                <button
                  type="button"
                  onClick={() => void submitUpload()}
                  disabled={creating || !upload}
                  className="rounded-lg bg-codify-accent px-3 py-1.5 text-2xs font-semibold text-codify-bg transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {creating ? "Uploading…" : "Upload skill"}
                </button>
                <button
                  type="button"
                  onClick={closeCreator}
                  disabled={creating}
                  className="rounded-lg px-2.5 py-1.5 text-2xs font-medium text-codify-muted hover:text-codify-secondary disabled:opacity-50"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {createError && (
            <p role="alert" className="mt-2 text-xs text-codify-danger">
              {createError}
            </p>
          )}
        </div>
      )}

      {problems.length > 0 && (
        <div className="flex items-start gap-2 rounded-xl border border-codify-warning/60 bg-codify-warning/20 p-3 text-xs text-codify-warning-ink">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{problems.join(" · ")}</span>
        </div>
      )}

      {filterable && (
        <div className="rounded-xl border border-codify-border bg-codify-bg p-3">
          <div className="flex items-center gap-2 rounded-lg border border-codify-border bg-codify-surface px-2.5 py-1.5 focus-within:border-codify-accent">
            <Search className="h-3.5 w-3.5 shrink-0 text-codify-muted" />
            <input
              type="search"
              aria-label="Search skills"
              placeholder="Search skills by name, description or move"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              autoComplete="off"
              spellCheck={false}
              className="min-w-0 flex-1 bg-transparent text-xs text-codify-secondary placeholder-codify-muted focus:outline-hidden"
            />
            {query && (
              <button
                type="button"
                onClick={() => setQuery("")}
                className="shrink-0 text-2xs text-codify-muted hover:text-codify-secondary"
              >
                Clear
              </button>
            )}
          </div>
          <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
            <div role="group" aria-label="Filter skills" className="flex flex-wrap gap-1">
              {FILTERS.map((entry) => (
                <button
                  key={entry.value}
                  type="button"
                  aria-pressed={filter === entry.value}
                  onClick={() => setFilter(entry.value)}
                  className={
                    "rounded-full border px-2.5 py-0.5 text-2xs font-medium transition-colors " +
                    (filter === entry.value
                      ? "border-codify-accent bg-codify-accent/20 text-codify-accent-ink"
                      : "border-codify-border text-codify-muted hover:border-codify-accent hover:text-codify-secondary")
                  }
                >
                  {entry.label}
                </button>
              ))}
            </div>
            <span role="status" className="text-2xs text-codify-muted">
              {visible.length} of {skills.length} skills
              {filter !== "all" ? ` · ${activeFilter.label}` : ""}
            </span>
          </div>
        </div>
      )}

      <div className="space-y-2">
        {visible.map((skill) => {
          const open = previewName === skill.name;
          const detail = preview?.name === skill.name ? preview : null;
          const panelId = `skill-${skill.name}-instructions`;
          return (
            <div
              key={skill.name}
              className="flex flex-col gap-1.5 rounded-xl border border-codify-border bg-codify-bg p-3.5"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <Checkbox
                    checked={skill.enabled}
                    disabled={Boolean(saving[skill.name])}
                    onChange={(next) => void save(skill, next)}
                  >
                    {skill.name}
                  </Checkbox>
                  <p className="mt-1 text-xs leading-relaxed text-codify-muted">{skill.description}</p>
                  <button
                    type="button"
                    aria-expanded={open}
                    aria-controls={open ? panelId : undefined}
                    aria-label={`${open ? "Hide" : "Preview"} ${skill.name} instructions`}
                    onClick={() => void togglePreview(skill)}
                    className="mt-2 text-2xs font-medium text-codify-info-ink hover:text-codify-accent"
                  >
                    {open ? "Hide instructions" : "Preview instructions"}
                  </button>
                </div>
                <span className="shrink-0 rounded-full border border-codify-border px-2 py-0.5 text-2xs text-codify-muted">
                  {skill.source === "workspace" ? "Workspace" : "Built-in"}
                </span>
              </div>

              {open && (
                <div
                  id={panelId}
                  className="mt-1 rounded-lg border border-codify-border bg-codify-surface p-3"
                >
                  {previewError ? (
                    <p className="text-xs text-codify-danger" role="status">{previewError}</p>
                  ) : detail ? (
                    <>
                      <div className="flex flex-wrap items-center justify-between gap-2 text-2xs text-codify-muted">
                        <span className="font-semibold text-codify-secondary">Instructions</span>
                        <span>
                          {detail.moves.length > 0
                            ? `Moves: ${detail.moves.join(", ")}`
                            : "No moves declared"}
                        </span>
                      </div>
                      {detail.notes.length > 0 && (
                        <p className="mt-1.5 text-2xs text-codify-warning-ink">
                          {detail.notes.join(" · ")}
                        </p>
                      )}
                      <pre className="mt-2 max-h-80 overflow-y-auto whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-codify-secondary">
                        {detail.body}
                      </pre>
                      <p className="mt-2 text-2xs text-codify-muted">
                        Read-only preview. The file is not changed and the conductor still loads the
                        body only when it chooses this skill.
                      </p>
                    </>
                  ) : (
                    <p className="text-xs text-codify-muted">Reading the instructions…</p>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {visible.length === 0 && skills.length > 0 && (
        <div className="rounded-xl border border-codify-border bg-codify-bg p-3 text-xs text-codify-muted">
          <p>No skills match this search or filter.</p>
          <button
            type="button"
            onClick={clearFilters}
            className="mt-1.5 font-medium text-codify-info-ink hover:text-codify-accent"
          >
            Clear search and filters
          </button>
        </div>
      )}
      {skills.length === 0 && (
        <p className="text-xs text-codify-muted">No skills are available in this workspace.</p>
      )}
      {error && <p className="text-xs text-codify-danger" role="status">{error}</p>}
      {message && <p className="text-xs text-codify-muted" role="status">{message}</p>}
    </div>
  );
};
