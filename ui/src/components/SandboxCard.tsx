import React, { useEffect, useState } from "react";
import { AlertTriangle, ShieldCheck } from "lucide-react";

import { getEngineRuntime, getEngineSettings, saveEngineSettings } from "../api";
import { readRejection } from "../rejection.ts";
import type { EngineRuntime } from "../types";
import { Checkbox } from "./ui/Checkbox";
import { SelectMenu } from "./SelectMenu";

const MODES = [
  { value: "auto", label: "Auto — use bubblewrap when available" },
  { value: "required", label: "Required — refuse commands without bubblewrap" },
  { value: "off", label: "Off — run commands unjailed" },
] as const;

/**
 * How workspace commands are contained (`sandbox_isolation`, docs/03 §1.4).
 *
 * The mode is a setting; whether this host can make a jail is a fact from `/settings/runtime`.
 * Both are shown together because `auto` on a host without bubblewrap is the one combination a
 * person needs to see before a command runs: the project's own code is refused until they tick the acknowledgement
 * below, because running it with no jail is not containment.
 */
export const SandboxCard: React.FC = () => {
  const [mode, setMode] = useState<string | null>(null);
  const [network, setNetwork] = useState<boolean | null>(null);
  // Whether `auto` may run the project's own code when this host cannot build a jail. Absent on an engine
  // that predates the switch, which reads as off.
  const [unjailedOk, setUnjailedOk] = useState(false);
  const [jail, setJail] = useState<NonNullable<EngineRuntime["sandbox"]> | null>(null);
  const [runtimeFailed, setRuntimeFailed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    let cancelled = false;
    getEngineSettings()
      .then((settings) => {
        if (cancelled || !settings?.sandbox_isolation) return;
        setMode(settings.sandbox_isolation.value);
        setNetwork(settings.sandbox_network ? settings.sandbox_network.value === 1 : false);
        setUnjailedOk(settings.sandbox_unjailed_ok ? settings.sandbox_unjailed_ok.value === 1 : false);
      })
      .catch(() => {
        // A card whose save would be refused is worse than no card; it stays absent.
      });
    getEngineRuntime()
      .then((report) => {
        if (!cancelled) setJail(report.sandbox ?? null);
      })
      .catch(() => {
        // The mode still works; only the host badge is unavailable, and it says so.
        if (!cancelled) setRuntimeFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (mode === null || network === null) return null;

  const save = async (
    patch: { sandbox_isolation?: string; sandbox_network?: boolean; sandbox_unjailed_ok?: boolean },
    previous: () => void,
  ) => {
    setSaving(true);
    setMessage(null);
    try {
      const result = await saveEngineSettings(patch);
      if (patch.sandbox_isolation !== undefined) {
        setMode(String(result.saved.sandbox_isolation));
      }
      if (patch.sandbox_network !== undefined) {
        setNetwork(Number(result.saved.sandbox_network) === 1);
      }
      if (patch.sandbox_unjailed_ok !== undefined) {
        setUnjailedOk(Number(result.saved.sandbox_unjailed_ok) === 1);
      }
      setMessage({ ok: true, text: "Saved — this applies to the next workspace command." });
    } catch (error: unknown) {
      previous();
      setMessage({ ok: false, text: readRejection(error, "Could not change sandbox isolation.") });
    } finally {
      setSaving(false);
    }
  };

  const changeMode = (next: string) => {
    const previous = mode;
    setMode(next);
    void save({ sandbox_isolation: next }, () => setMode(previous));
  };

  const changeNetwork = (next: boolean) => {
    const previous = network;
    setNetwork(next);
    void save({ sandbox_network: next }, () => setNetwork(previous));
  };

  const changeUnjailedOk = (next: boolean) => {
    const previous = unjailedOk;
    setUnjailedOk(next);
    void save({ sandbox_unjailed_ok: next }, () => setUnjailedOk(previous));
  };

  const missingJail = jail !== null && !jail.available;

  return (
    <div className="flex flex-col gap-2.5 bg-codify-bg border border-codify-border rounded-xl p-3.5">
      <div className="flex items-center gap-2 text-xs font-semibold text-codify-secondary">
        <ShieldCheck className="w-3.5 h-3.5 text-codify-success" />
        Workspace command jail
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor="sandbox-isolation" className="text-xs text-codify-secondary font-medium">
          Isolation mode
        </label>
        <SelectMenu
          id="sandbox-isolation"
          label="Workspace command isolation"
          value={mode}
          disabled={saving}
          onChange={changeMode}
          options={MODES}
          className="flex items-center justify-between gap-2 bg-codify-surface border border-codify-border rounded-lg px-2.5 py-1.5 text-xs text-codify-secondary focus:outline-hidden focus:border-codify-accent disabled:opacity-40 disabled:cursor-not-allowed"
        />
      </div>

      <Checkbox checked={network} disabled={saving} onChange={changeNetwork}>
        Allow test commands to reach the network
      </Checkbox>

      <p className="text-xs text-codify-muted leading-relaxed">
        The jail makes the workspace the only writable place, hides this run&apos;s state and the
        person&apos;s home, and blocks the network unless enabled above. It still runs the
        repository&apos;s own code, so approving a goal in an untrusted repository remains the
        decision that matters.
      </p>

      {missingJail && mode !== "off" && (
        <p className="text-xs text-codify-warning-ink bg-codify-warning/20 border border-codify-warning/60 rounded-sm p-2 flex items-start gap-1.5">
          <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
          <span>
            {mode === "required"
              ? "Bubblewrap is not installed on this host, so required mode refuses every workspace command."
              : unjailedOk
                ? "Bubblewrap is not installed on this host. Auto mode runs the project's own code (tests, scripts) unjailed because you allowed it below; choose Required to refuse it instead."
                : "Bubblewrap is not installed on this host. Auto mode refuses to run the project's own code (tests, scripts) until you allow unjailed runs below; choose Required to refuse it for good."}
          </span>
        </p>
      )}
      {missingJail && mode === "auto" && (
        <Checkbox checked={unjailedOk} disabled={saving} onChange={changeUnjailedOk}>
          Allow the project&apos;s own code to run on this machine without a jail
        </Checkbox>
      )}
      {jail?.available && (
        <p className="text-xs text-codify-muted">
          Bubblewrap: <span className="font-mono text-codify-secondary">{jail.bwrap}</span>
        </p>
      )}
      {runtimeFailed && (
        <p className="text-xs text-codify-muted">
          Could not read this host&apos;s jail capability from /settings/runtime.
        </p>
      )}
      {message && (
        <p className={`text-xs ${message.ok ? "text-codify-success" : "text-codify-danger"}`} role="status">
          {message.text}
        </p>
      )}
    </div>
  );
};
