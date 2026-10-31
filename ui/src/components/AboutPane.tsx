import React, { useCallback, useEffect, useState } from "react";
import { Info, Keyboard, FolderOpen, Cpu, Globe, RefreshCw, Download } from "lucide-react";

import {
  checkEngineHealth,
  checkForUpdate,
  downloadUpdate,
  getAppFacts,
  getEngineInfo,
  underShell,
  type AppFacts,
  type HealthStatus,
  type UpdateCheck,
  type UpdateDownload,
} from "../api";
import { readRejection } from "../rejection";
import { SHORTCUT_HELP } from "../shortcuts";
import type { UpdateProgress } from "../shellEvents";
import { size, updateLine, updatePercent } from "../updateProgress";
import { EngineRuntimeCard } from "./EngineRuntimeCard";
import { Button } from "./ui/Button";
import { Panel } from "./ui/Panel";

const REPO = "github.com/GeneticxCln/Codify";
const RELEASES = "github.com/GeneticxCln/Codify/releases";

/** One `label: value` line, the shape every section here uses. */
const Row: React.FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => (
  <div className="flex flex-wrap items-baseline gap-x-2 text-xs">
    <span className="text-codify-muted">{label}</span>
    <span className="text-codify-primary break-all">{children}</span>
  </div>
);

/** What the engine's health answer means, in words. */
function healthWords(health: HealthStatus | null): string {
  if (health === null) return "checking…";
  if (!health.ok) return "not answering";
  return health.authenticated ? "running" : "running, but this window's token is stale";
}

/**
 * A package's size in units a person reads, because the raw byte count is not one.
 *
 * Decimal megabytes, which are the ones a release page and a file manager print: 18,000,000
 * bytes is "18 MB" there and would be "17 MB" in binary mebibytes, and a size that disagrees
 * with the page the same file is on reads as a different file.
 */
function megabytes(bytes: number | null): string {
  if (bytes === null || bytes <= 0) return "";
  return ` — about ${size(bytes)}`;
}

/** The version a release is, for a sentence: its tag, or its number, or what we have. */
function releaseName(check: UpdateCheck): string {
  return check.tag ?? check.latest ?? "a newer release";
}

/**
 * What the update button is doing, in one value rather than four flags.
 *
 * `failed` keeps the check it was working from, so a download that failed still offers the
 * download again; `answered` with no asset is a real answer — up to date, a checkout, a
 * release with no package for this machine — and renders the shell's own sentence.
 */
type UpdateState =
  | { kind: "idle" }
  | { kind: "checking" }
  | { kind: "answered"; check: UpdateCheck }
  | { kind: "downloading"; check: UpdateCheck; progress: UpdateProgress | null }
  | { kind: "downloaded"; check: UpdateCheck; download: UpdateDownload }
  | { kind: "failed"; check: UpdateCheck | null; message: string };

/**
 * How far a download has come, as a bar under the sentence that started it.
 *
 * The bar and the caption are drawn from the same two numbers, so they cannot disagree. A
 * percentage is reported to assistive technology only when there is one: a download whose size
 * nobody announced has no figure to give, and inventing one would read as a number that is
 * wrong. That case is the slow pulse in the accent colour instead — motion-safe, because a
 * person who asked for no motion gets a still bar and the caption says the same thing in words.
 */
const DownloadBar: React.FC<{
  asset: string;
  progress: UpdateProgress | null;
  expected: number | null;
}> = ({ asset, progress, expected }) => {
  const percent = updatePercent(progress, expected);
  return (
    <div
      role="progressbar"
      aria-label={`Downloading ${asset}`}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={percent ?? undefined}
      className="h-1.5 w-full overflow-hidden rounded-sm border border-codify-border bg-codify-raised"
    >
      <div
        className={
          percent === null
            ? "h-full w-full bg-codify-accent/40 motion-safe:animate-pulse"
            : "h-full bg-codify-accent"
        }
        style={percent === null ? undefined : { width: `${percent}%` }}
      />
    </div>
  );
};

/** The update section: a check a person starts, and the package it hands them. */
const UpdatePanel: React.FC = () => {
  const [state, setState] = useState<UpdateState>({ kind: "idle" });

  const check = useCallback(async () => {
    setState({ kind: "checking" });
    try {
      setState({ kind: "answered", check: await checkForUpdate() });
    } catch (err) {
      setState({ kind: "failed", check: null, message: readRejection(err) });
    }
  }, []);

  const fetch = useCallback(async (known: UpdateCheck) => {
    setState({ kind: "downloading", check: known, progress: null });
    try {
      const download = await downloadUpdate((progress) => {
        // The only download on screen is this one: the button is disabled while this runs, so there
        // is no second download whose receipts could arrive here, and the subscription ends with
        // the call that started it.
        setState({ kind: "downloading", check: known, progress });
      });
      setState({ kind: "downloaded", check: known, download });
    } catch (err) {
      setState({ kind: "failed", check: known, message: readRejection(err) });
    }
  }, []);

  const busy = state.kind === "checking" || state.kind === "downloading";
  // The offer survives a *failed download*: that failure is about the download, and the release
  // is still there to try again. It does not survive a failed check, which has no release at all.
  const known =
    state.kind === "answered" || state.kind === "downloading" || state.kind === "failed"
      ? state.check
      : null;

  return (
    <Panel
      title={
        <>
          <RefreshCw className="w-3.5 h-3.5" /> Updates
        </>
      }
    >
      <div className="space-y-2">
        {state.kind === "idle" && (
          <p className="text-xs text-codify-muted leading-relaxed">
            Codify can ask GitHub whether a newer release exists, and hand you the package for
            this install. Nothing is checked until you ask: one request to{" "}
            <span className="font-mono">{REPO}</span>, with no key and nothing about this
            machine, and it installs nothing — the package is written to your Downloads folder
            and the command to run is printed for you.
          </p>
        )}

        {state.kind === "checking" && (
          <p className="text-xs text-codify-muted">Checking {REPO}…</p>
        )}

        {known?.asset && (
          <>
            <Row label="Available">
              {releaseName(known)} — you are running {known.current}
            </Row>
            {known.published_at && (
              <Row label="Published">{known.published_at.slice(0, 10)}</Row>
            )}
            <p className="text-2xs text-codify-muted leading-relaxed">
              {known.asset}
              {megabytes(known.asset_bytes)} is the package for this install. Its page, for
              reading, is <span className="font-mono">{known.url}</span>.
            </p>
          </>
        )}

        {state.kind === "answered" && !state.check.asset && (
          <p className="text-xs text-codify-secondary leading-relaxed">
            {state.check.message ?? `You are running ${state.check.current}.`}
          </p>
        )}

        {state.kind === "downloading" && known?.asset && (
          <>
            <p className="text-xs text-codify-muted">
              Downloading {known.asset}
              {megabytes(known.asset_bytes)}. A package is tens of megabytes, so this can take a
              minute.
            </p>
            <DownloadBar
              asset={known.asset}
              progress={state.progress}
              expected={known.asset_bytes}
            />
            <p className="text-2xs text-codify-muted">
              {updateLine(state.progress, known.asset_bytes)}
            </p>
          </>
        )}

        {state.kind === "downloaded" && (
          <>
            <Row label="Written">
              {state.download.path} ({state.download.bytes.toLocaleString()} bytes)
            </Row>
            {/* The command, as text in its own block: it is long, it is meant to be copied, and
                the shell that downloaded the file runs none of it (docs/18 §4). */}
            <pre className="text-2xs font-mono whitespace-pre-wrap break-all text-codify-secondary bg-codify-raised border border-codify-border rounded-lg p-2">
              {state.download.next_step}
            </pre>
          </>
        )}

        {state.kind === "failed" && (
          <p className="text-xs text-codify-danger leading-relaxed">{state.message}</p>
        )}

        {/* A shell that is not here cannot know a version, fetch a file or name a folder, and
            the sentence says that rather than showing a button that would fail. */}
        {!underShell() ? (
          <p className="text-2xs text-codify-muted leading-relaxed">
            The update check needs the desktop shell, so it cannot run in this browser preview.
            The releases are at <span className="font-mono">{RELEASES}</span>.
          </p>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <Button
              tone={known?.asset ? "subtle" : "primary"}
              onClick={() => void check()}
              disabled={busy}
            >
              <RefreshCw className="w-3.5 h-3.5" />
              {state.kind === "idle" ? "Check for updates" : "Check again"}
            </Button>
            {known?.asset && state.kind !== "downloaded" && (
              <Button tone="primary" onClick={() => void fetch(known)} disabled={busy}>
                <Download className="w-3.5 h-3.5" />
                {state.kind === "downloading" ? "Downloading…" : `Download ${known.asset}`}
              </Button>
            )}
          </div>
        )}
      </div>
    </Panel>
  );
};

/**
 * Settings → About: what this app is and what it is running on.
 *
 * Says only what the app can truthfully know: the shell's own version when there is a shell
 * (`getAppFacts`), the engine's interpreter and health from the routes that already report
 * them, the port it is connected to, where the data lives, and the keyboard shortcuts
 * straight from the table the keyboard layer is tested against (`SHORTCUT_HELP`). The engine
 * has no version of its own to print, so none is invented: the engine ships in the same
 * checkout as the app, and the interpreter line is what tells two installs apart.
 *
 * The boot token is never shown. The port is not a secret and is what a person needs when
 * something else has to find the engine.
 *
 * It is the one place in Settings that *writes* something — the release package, into the
 * person's Downloads folder, when they press Download — and it still writes no
 * configuration: agent config comes from the settings routes alone (docs/02 §1). The update
 * check is also the one request this app makes that is not a model provider's, so it is
 * named in the sentence above it and made only on a click (docs/18 §4).
 *
 * A download is minutes long, so the button says how far it has come while it runs, from the
 * shell's own announcements (`update-progress` → `updateProgress.ts`). Nothing else in the pane
 * is live: what it is, what it is running on, and what to do with the file once it lands.
 */
export const AboutPane: React.FC = () => {
  const [app, setApp] = useState<AppFacts | null | undefined>(undefined);
  const [health, setHealth] = useState<HealthStatus | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getAppFacts().then((facts) => {
      if (!cancelled) setApp(facts);
    });
    void checkEngineHealth().then((status) => {
      if (!cancelled) setHealth(status);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const port = getEngineInfo().port;

  return (
    <div className="space-y-4">
      <Panel title={<><Info className="w-3.5 h-3.5" /> Codify</>}>
        <div className="space-y-1.5">
          <p className="text-xs text-codify-muted leading-relaxed">
            A local-first, multi-agent AI coding assistant. Your code, your keys and your history stay on
            this machine. It talks to the model providers you configure; when you allow web pages in Settings,
            the assistant can fetch them; and GitHub is asked for a release only when you press Check for updates.
          </p>
          {app === undefined ? (
            <Row label="Version">checking…</Row>
          ) : app ? (
            <>
              <Row label="Version">{app.version}</Row>
              {app.tauri && <Row label="Desktop shell">Tauri {app.tauri}</Row>}
            </>
          ) : underShell() ? (
            // A shell is here and would not say what it is: say that, rather than print a blank version.
            <Row label="Version">unavailable: the desktop shell did not report one</Row>
          ) : (
            <Row label="Running as">a standalone browser preview, with no desktop shell</Row>
          )}
          <Row label="Project">{REPO}</Row>
          <Row label="Licence">MIT</Row>
        </div>
      </Panel>

      <UpdatePanel />

      <Panel title={<><Cpu className="w-3.5 h-3.5" /> Engine</>}>
        <div className="space-y-2">
          <Row label="Status">{healthWords(health)}</Row>
          <Row label="Address">127.0.0.1:{port}, on this machine only</Row>
          <EngineRuntimeCard />
        </div>
      </Panel>

      <Panel title={<><FolderOpen className="w-3.5 h-3.5" /> Where things live</>}>
        <div className="space-y-1.5">
          <Row label="Data folder">~/.codify (or $CODIFY_HOME when it is set)</Row>
          <p className="text-2xs text-codify-muted leading-relaxed">
            One SQLite database (codify.db), the boot token, and the secrets file when no OS keychain is
            available. Nothing else is written outside the workspaces you open.
          </p>
        </div>
      </Panel>

      <Panel title={<><Keyboard className="w-3.5 h-3.5" /> Keyboard shortcuts</>}>
        <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-xs">
          {SHORTCUT_HELP.map((shortcut) => (
            <React.Fragment key={shortcut.keys}>
              <dt className="font-mono text-codify-primary">{shortcut.keys}</dt>
              <dd className="text-codify-secondary">{shortcut.does}</dd>
            </React.Fragment>
          ))}
        </dl>
      </Panel>

      <Panel title={<><Globe className="w-3.5 h-3.5" /> Documentation</>}>
        <p className="text-xs text-codify-muted leading-relaxed">
          The specification is in the repository's <span className="font-mono">docs/</span> folder, from the
          architecture overview (<span className="font-mono">docs/00</span>) to the workspace shell
          (<span className="font-mono">docs/09</span>).
        </p>
      </Panel>
    </div>
  );
};
