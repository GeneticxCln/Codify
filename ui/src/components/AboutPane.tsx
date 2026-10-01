import React, { useEffect, useState } from "react";
import { Info, Keyboard, FolderOpen, Cpu, Globe } from "lucide-react";

import {
  checkEngineHealth,
  getAppFacts,
  getEngineInfo,
  underShell,
  type AppFacts,
  type HealthStatus,
} from "../api";
import { SHORTCUT_HELP } from "../shortcuts";
import { EngineRuntimeCard } from "./EngineRuntimeCard";
import { Panel } from "./ui/Panel";

const REPO = "github.com/GeneticxCln/Codify";

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
 * Settings → About: what this app is and what it is running on.
 *
 * Read-only, as docs/02 §3.3 requires, and it says only what the app can truthfully know: the shell's
 * own version when there is a shell (`getAppFacts`), the engine's interpreter and health from the
 * routes that already report them, the port it is connected to, where the data lives, and the
 * keyboard shortcuts straight from the table the keyboard layer is tested against (`SHORTCUT_HELP`).
 * The engine has no version of its own to print, so none is invented: the engine ships in the same
 * checkout as the app, and the interpreter line is what tells two installs apart.
 *
 * The boot token is never shown. The port is not a secret and is what a person needs when something
 * else has to find the engine.
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
            this machine; the only requests that leave it go to the model providers you configure.
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
