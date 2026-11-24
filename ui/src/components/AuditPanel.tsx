import React, { useCallback, useEffect, useRef, useState } from "react";
import { FileCode2, FolderGit2, GitCommitHorizontal, RefreshCw } from "lucide-react";

import { fetchAuditChanges, listWorkspaces } from "../api";
import type { AuditChangeGoal, AuditChangesResponse, Workspace } from "../types";
import { SEARCHABLE_FROM, SelectMenu } from "./SelectMenu";
import { Badge } from "./ui/Badge";
import { statusTone } from "../statusTone";

const INITIAL_FILES_SHOWN = 6;

interface AuditPanelProps {
  /** Jump to a goal in the transcript when clicked. */
  onOpenGoal?: (goalId: string) => void;
}

const GoalChangeCard: React.FC<{
  entry: AuditChangeGoal;
  onOpenGoal?: (goalId: string) => void;
}> = ({ entry, onOpenGoal }) => {
  const [showAllFiles, setShowAllFiles] = useState(false);
  const visibleFiles = showAllFiles ? entry.files : entry.files.slice(0, INITIAL_FILES_SHOWN);
  const hiddenFiles = Math.max(0, entry.files.length - visibleFiles.length);

  return (
    <div className="bg-codify-bg border border-codify-border rounded-xl p-3 flex flex-col gap-2">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 text-2xs text-codify-muted mb-0.5">
            <FolderGit2 className="w-3 h-3 shrink-0" />
            <span className="truncate font-medium text-codify-secondary">{entry.workspace_name}</span>
            <span>·</span>
            <span>{new Date(entry.updated_at * 1000).toLocaleString()}</span>
          </div>
          {onOpenGoal ? (
            <button
              type="button"
              onClick={() => onOpenGoal(entry.goal_id)}
              className="text-left text-xs font-semibold text-codify-primary hover:underline transition-colors cursor-pointer break-words"
            >
              {entry.title || "(untitled goal)"}
            </button>
          ) : (
            <div className="text-xs font-semibold text-codify-primary break-words">
              {entry.title || "(untitled goal)"}
            </div>
          )}
        </div>
        <div className="flex items-center gap-1 shrink-0">
          {entry.dry_run && <Badge tone="neutral">DRY RUN</Badge>}
          <Badge tone={statusTone(entry.status)}>{entry.status}</Badge>
        </div>
      </div>

      {entry.commits.length > 0 && (
        <div className="flex flex-col gap-1 border-t border-codify-border/60 pt-2">
          <div className="text-2xs font-semibold uppercase tracking-wider text-codify-muted">
            Commits ({entry.commits.length})
          </div>
          {entry.commits.map((c, idx) => (
            <div
              key={`${c.step_id ?? "step"}-${c.sha ?? idx}`}
              className="flex items-baseline gap-1.5 text-xs text-codify-secondary"
            >
              <GitCommitHorizontal className="w-3 h-3 shrink-0 text-codify-success self-center" />
              {c.sha && (
                <span className="font-mono text-2xs text-codify-muted shrink-0">{c.sha}</span>
              )}
              <span className="break-words">{c.message}</span>
            </div>
          ))}
        </div>
      )}

      {entry.files.length > 0 && (
        <div className="flex flex-col gap-1 border-t border-codify-border/60 pt-2">
          <div className="text-2xs font-semibold uppercase tracking-wider text-codify-muted">
            Files ({entry.files_count})
          </div>
          <div className="flex flex-wrap gap-1">
            {visibleFiles.map((filePath) => (
              <span
                key={filePath}
                title={filePath}
                className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md bg-codify-surface border border-codify-border font-mono text-2xs text-codify-secondary max-w-full truncate"
              >
                <FileCode2 className="w-2.5 h-2.5 shrink-0 text-codify-muted" />
                <span className="truncate">{filePath}</span>
              </span>
            ))}
          </div>
          {hiddenFiles > 0 && (
            <button
              type="button"
              onClick={() => setShowAllFiles(true)}
              className="self-start text-2xs text-codify-accent-ink hover:underline cursor-pointer"
            >
              Show {hiddenFiles} more file{hiddenFiles === 1 ? "" : "s"}
            </button>
          )}
        </div>
      )}
    </div>
  );
};

/**
 * App-wide audit view: cross-project feed of what agents changed (commits, files, and goals),
 * derived from persisted per-goal audit events and plan steps.
 */
export const AuditPanel: React.FC<AuditPanelProps> = ({ onOpenGoal }) => {
  const [projects, setProjects] = useState<Workspace[]>([]);
  const [project, setProject] = useState<string>("");
  const [feed, setFeed] = useState<AuditChangesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const reqSeqRef = useRef(0);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    listWorkspaces()
      .then((list) => {
        if (!cancelled) setProjects(list);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  const load = useCallback(async (wsId: string) => {
    const req = ++reqSeqRef.current;
    setLoading(true);
    setError(null);
    setFeed(null);
    try {
      const data = await fetchAuditChanges(wsId || null);
      if (!mountedRef.current || reqSeqRef.current !== req) return;
      setFeed(data);
    } catch (err: unknown) {
      if (!mountedRef.current || reqSeqRef.current !== req) return;
      setError(err instanceof Error ? err.message : "Could not load audit changes.");
    } finally {
      if (mountedRef.current && reqSeqRef.current === req) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void load(project);
  }, [project, load]);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <SelectMenu
          label="Audit project"
          value={project}
          onChange={setProject}
          options={[
            { value: "", label: "All projects" },
            ...projects.map((p) => ({ value: p.id, label: p.name })),
          ]}
          searchable={projects.length + 1 >= SEARCHABLE_FROM}
          searchPlaceholder="Search projects…"
          className="flex-1 min-w-0 flex items-center justify-between gap-2 rounded-lg border border-codify-border bg-codify-bg px-2.5 py-1.5 text-xs text-codify-secondary focus:outline-hidden focus:border-codify-accent cursor-pointer"
        />
        <button
          type="button"
          onClick={() => void load(project)}
          disabled={loading}
          aria-label="Reload audit log"
          className="p-1.5 rounded-lg border border-codify-border bg-codify-bg text-codify-muted hover:text-codify-primary disabled:opacity-40 cursor-pointer"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
        </button>
      </div>

      {feed && (
        <div className="grid grid-cols-3 gap-2">
          <div className="bg-codify-bg border border-codify-border rounded-xl p-2.5">
            <div className="text-2xs uppercase tracking-wider text-codify-muted">Goals</div>
            <div className="text-base font-bold font-mono text-codify-primary">{feed.total_goals}</div>
          </div>
          <div className="bg-codify-bg border border-codify-border rounded-xl p-2.5">
            <div className="text-2xs uppercase tracking-wider text-codify-muted">Commits</div>
            <div className="text-base font-bold font-mono text-codify-success">{feed.total_commits}</div>
          </div>
          <div className="bg-codify-bg border border-codify-border rounded-xl p-2.5">
            <div className="text-2xs uppercase tracking-wider text-codify-muted">Files</div>
            <div className="text-base font-bold font-mono text-codify-primary">{feed.total_files}</div>
          </div>
        </div>
      )}

      {loading && (
        <p className="text-xs text-codify-muted px-1 py-4">Loading audit log…</p>
      )}

      {error && (
        <p className="text-xs text-codify-danger px-1 py-2" role="alert">
          {error}
        </p>
      )}

      {!loading && !error && feed && feed.entries.length === 0 && (
        <p className="text-xs text-codify-muted px-1 py-4 leading-relaxed">
          No agent file changes or commits recorded{project ? " for this project" : " across your projects"} yet.
        </p>
      )}

      {!loading && !error && feed && feed.entries.length > 0 && (
        <div className="flex flex-col gap-2">
          {feed.entries.map((entry) => (
            <GoalChangeCard key={entry.goal_id} entry={entry} onOpenGoal={onOpenGoal} />
          ))}
        </div>
      )}
    </div>
  );
};
