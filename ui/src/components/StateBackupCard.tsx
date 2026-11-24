import React, { useCallback, useEffect, useState } from "react";
import { AlertCircle, Check, Download, HardDrive, RotateCcw, Upload } from "lucide-react";

import {
  cancelStateRestore,
  createStateBackup,
  downloadStateBackup,
  fetchStateBackupStatus,
  stageStateRestore,
} from "../api.ts";
import type { StateBackupStatus } from "../types.ts";
import { readRejection } from "../rejection.ts";
import { Button } from "./ui/Button";

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 1024) return `${Math.max(0, Math.round(bytes || 0))} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function bytesToBase64(bytes: Uint8Array): string {
  const chunk = 0x8000;
  const parts: string[] = [];
  for (let i = 0; i < bytes.length; i += chunk) {
    parts.push(String.fromCharCode(...bytes.subarray(i, i + chunk)));
  }
  return btoa(parts.join(""));
}

/**
 * Settings → Sessions: online SQLite backup of `codify.db` and staged restore on next start.
 *
 * API keys are excluded by design: they live in the OS keyring (or `secrets.json`) and are
 * neither written into backups nor touched when a backup is swapped in on restart.
 */
export const StateBackupCard: React.FC = () => {
  const [status, setStatus] = useState<StateBackupStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async (showSpinner = false) => {
    if (showSpinner) setLoading(true);
    try {
      const next = await fetchStateBackupStatus();
      setStatus(next);
      setError(null);
    } catch (err) {
      setError(readRejection(err, "Could not read backup status"));
    } finally {
      if (showSpinner) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(true);
  }, [load]);

  const handleBackup = async () => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const created = await createStateBackup();
      await load(false);
      setNotice(`Saved ${created.filename} (${formatBytes(created.size_bytes)}) to ${created.path}`);
    } catch (err) {
      setError(readRejection(err, "Could not back up Codify's state"));
    } finally {
      setBusy(false);
    }
  };

  const handleStageSaved = async (filename: string) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const staged = await stageStateRestore({ filename });
      await load(false);
      setNotice(`Staged ${staged.source} (${formatBytes(staged.size_bytes)}) — restart Codify to finish restoring.`);
    } catch (err) {
      setError(readRejection(err, "Could not stage backup for restore"));
    } finally {
      setBusy(false);
    }
  };

  const handleRestoreFile = () => {
    if (busy) return;
    const input = window.document.createElement("input");
    input.type = "file";
    input.accept = ".db,.sqlite,.sqlite3,application/x-sqlite3,application/octet-stream";
    input.onchange = async () => {
      const file = input.files?.[0];
      if (!file) return;
      setBusy(true);
      setError(null);
      setNotice(null);
      try {
        const buf = new Uint8Array(await file.arrayBuffer());
        const staged = await stageStateRestore({
          data_base64: bytesToBase64(buf),
          source_name: file.name,
        });
        await load(false);
        setNotice(`Staged ${staged.source} (${formatBytes(staged.size_bytes)}) — restart Codify to finish restoring.`);
      } catch (err) {
        setError(readRejection(err, `Could not stage "${file.name}" for restore`));
      } finally {
        setBusy(false);
      }
    };
    input.click();
  };

  const handleCancelRestore = async () => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await cancelStateRestore();
      await load(false);
      setNotice("Cancelled the pending restore — your current database will stay in place.");
    } catch (err) {
      setError(readRejection(err, "Could not cancel staged restore"));
    } finally {
      setBusy(false);
    }
  };

  const handleDownload = async (filename: string) => {
    setBusy(true);
    setError(null);
    try {
      const blob = await downloadStateBackup(filename);
      const url = URL.createObjectURL(blob);
      const anchor = window.document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      anchor.click();
      const timer = setTimeout(() => URL.revokeObjectURL(url), 10_000);
      (timer as unknown as { unref?: () => void })?.unref?.();
    } catch (err) {
      setError(readRejection(err, `Could not download ${filename}`));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-xl border border-codify-border bg-codify-surface p-4 space-y-3">
      <div className="flex items-start gap-3">
        <HardDrive className="w-4 h-4 mt-0.5 text-codify-info shrink-0" />
        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-semibold text-codify-primary">Backup and restore</h3>
          <p className="text-xs text-codify-muted mt-1 leading-relaxed">
            Writes a consistent snapshot of <span className="font-mono">codify.db</span> using SQLite&apos;s online
            backup API, safe while Codify is running. Restoring validates the snapshot and swaps it in on the next
            start.{" "}
            <span className="text-codify-secondary">
              API keys are excluded — they stay in your OS keyring.
            </span>
          </p>
          {status && (
            <p className="text-2xs text-codify-muted font-mono mt-1 break-all">
              Store: {status.db_path} ({formatBytes(status.db_bytes)})
            </p>
          )}
        </div>
      </div>

      {loading ? (
        <p className="text-xs text-codify-muted">Reading backup status…</p>
      ) : (
        <div className="space-y-3 pt-1">
          <div className="flex flex-wrap items-center gap-2">
            <Button tone="primary" disabled={busy} onClick={() => void handleBackup()}>
              <HardDrive className="w-3.5 h-3.5" />
              <span>{busy ? "Working…" : "Back up"}</span>
            </Button>
            <Button tone="subtle" disabled={busy} onClick={handleRestoreFile}>
              <Upload className="w-3.5 h-3.5" />
              <span>Restore from file…</span>
            </Button>
          </div>

          {status?.pending_restore && (
            <div
              data-pending-restore="true"
              className="rounded-lg border border-codify-border bg-codify-bg p-3 flex flex-wrap items-center justify-between gap-2"
            >
              <div className="text-xs text-codify-primary">
                <span className="font-semibold text-codify-warning">Restore staged for next start:</span>{" "}
                <span className="font-mono">{status.pending_restore.source}</span>{" "}
                <span className="text-codify-muted">({formatBytes(status.pending_restore.size_bytes)})</span>
                <div className="text-2xs text-codify-muted mt-0.5">
                  Restart Codify to swap this backup into place. Keys in your keyring are kept.
                </div>
              </div>
              <Button tone="subtle" size="sm" disabled={busy} onClick={() => void handleCancelRestore()}>
                Cancel restore
              </Button>
            </div>
          )}

          {status && status.backups.length > 0 && (
            <div className="space-y-1.5">
              <div className="text-2xs font-semibold uppercase tracking-wider text-codify-muted">
                Saved snapshots ({status.backups.length})
              </div>
              <div className="max-h-44 overflow-y-auto space-y-1">
                {status.backups.map((b) => (
                  <div
                    key={b.filename}
                    data-backup-row={b.filename}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-codify-border bg-codify-bg px-2.5 py-1.5 text-xs"
                  >
                    <div className="min-w-0">
                      <div className="font-mono text-codify-primary truncate" title={b.path}>
                        {b.filename}
                      </div>
                      <div className="text-2xs text-codify-muted">
                        {formatBytes(b.size_bytes)} · {new Date(b.created_at * 1000).toLocaleString()}
                      </div>
                    </div>
                    <div className="flex items-center gap-1.5 shrink-0">
                      <Button
                        tone="subtle"
                        size="sm"
                        disabled={busy}
                        onClick={() => void handleStageSaved(b.filename)}
                      >
                        <RotateCcw className="w-3 h-3" />
                        <span>Restore</span>
                      </Button>
                      <Button
                        tone="ghost"
                        size="sm"
                        disabled={busy}
                        onClick={() => void handleDownload(b.filename)}
                        aria-label={`Download ${b.filename}`}
                      >
                        <Download className="w-3 h-3" />
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {error && (
        <div className="flex items-start gap-2 text-xs text-codify-danger">
          <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
          <span>{error}</span>
        </div>
      )}
      {notice && (
        <div className="flex items-start gap-2 text-xs text-codify-success">
          <Check className="w-4 h-4 shrink-0 mt-0.5" />
          <span className="break-all">{notice}</span>
        </div>
      )}
    </div>
  );
};
