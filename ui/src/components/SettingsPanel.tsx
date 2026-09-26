import React, { useEffect, useMemo, useState } from "react";
import {
  AgentCallStat,
  AgentRole,
  LayaStatus,
  ModelOption,
  ProviderModelStatus,
  RecentRunModel,
  RepairReport,
  RoleInfo,
} from "../types";
import { fetchAgentCallStats, fetchRoles, getEngineSettings, getLayaStatus, repairAgentConfigs, saveEngineSettings } from "../api";
import { useAgentConfigs } from "../hooks/useAgentConfigs";
import { buildModelSignals } from "../modelSignals";
import { findStaleFallback, findStaleModel, StaleModel } from "../staleModel";
import { AgentConfigCard } from "./AgentConfigCard";
import { Sliders, ShieldCheck, Zap, AlertTriangle, Cpu, Wand2, Wrench, Workflow } from "lucide-react";

interface SettingsPanelProps {
  /** Render compactly inside the settings modal instead of as a full page. */
  embedded?: boolean;
  /** Discovered models, offered per role so the field autocompletes real ids. */
  models?: ModelOption[];
  /** Per-provider discovery outcome, so a stale role model can be flagged. */
  providerStatus?: ProviderModelStatus[];
  /** Ask every provider what it serves right now, from inside a model field. */
  onRefreshModels?: () => void;
  refreshingModels?: boolean;
  /**
   * Models that recently answered, so a role's model field can show *why* an id
   * looks familiar. The roles half of these signals is built here from the store,
   * which is the live copy — the panel must not re-derive it from a prop that can
   * be a save behind.
   */
  recentRuns?: RecentRunModel[];
}

/**
 * Which engine will actually gate goals, stated plainly.
 *
 * The gate has three possible engines (real SDK / fallback model / skipped), and
 * a toggle that silently did nothing would be worse than no toggle at all — so
 * the status card always names the one in force, and why.
 */
const LayaGateStatus: React.FC = () => {
  const [status, setStatus] = useState<LayaStatus | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    getLayaStatus()
      .then((s) => {
        if (!cancelled) setStatus(s ?? null);
      })
      .catch(() => {
        if (!cancelled) setStatus(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading) return null;

  if (!status) {
    return (
      <div className="flex items-start gap-2 text-xs text-gray-400 bg-codify-surface border border-codify-border rounded-lg p-3">
        <AlertTriangle className="w-4 h-4 text-amber-400 flex-shrink-0 mt-0.5" />
        <span>Gate status unavailable — the engine did not answer /settings/laya.</span>
      </div>
    );
  }

  const sdk = status.sdk_installed && !status.sdk_disabled;
  const tone = sdk
    ? "text-emerald-300"
    : status.sdk_disabled
    ? "text-amber-300"
    : "text-gray-300";

  return (
    <div className="flex flex-col gap-1.5 bg-codify-surface border border-codify-border rounded-lg p-3">
      <div className={`flex items-center gap-2 text-xs font-semibold ${tone}`}>
        <Zap className="w-4 h-4" />
        Pre-flight gate:{" "}
        {sdk ? "Laya SDK (in-process, no tokens)" : "fallback model below, or skipped if it is unreachable"}
      </div>
      <p className="text-xs text-gray-400 flex items-start gap-1.5">
        <Cpu className="w-3.5 h-3.5 flex-shrink-0 mt-0.5" />
        <span>
          Runs before the planner: typed intent / risk / prompt-injection decisions. Injection at or
          above {status.policy.injection_block_threshold} blocks the goal before any model or
          filesystem work happens. Risk ≥ {status.policy.risk_warn_level} and ambiguity ≥{" "}
          {status.policy.clarify_warn_threshold} only warn.
        </span>
      </p>
      {status.sdk_disabled && (
        <p className="text-xs text-amber-400">
          SDK disabled by CODIFY_LAYA_SDK=0 — the gate will use this role's provider below.
        </p>
      )}
      {!status.sdk_installed && !status.sdk_disabled && (
        <p className="text-xs text-gray-500">
          Install the SDK (`pip install laya` + weights) to run the gate in-process;{" "}
          {status.sdk_error ? `last error: ${status.sdk_error}` : "no local weights installed yet."}
        </p>
      )}
    </div>
  );
};

export const SettingsPanel: React.FC<SettingsPanelProps> = ({
  embedded = false,
  models = [],
  providerStatus = [],
  onRefreshModels,
  refreshingModels = false,
  recentRuns = [],
}) => {
  // One config store for the whole screen, so every card and the summary below
  // read the same state and a save updates all of them.
  const store = useAgentConfigs();

  // The same ordering signals the chat's menu uses, built from *this* screen's
  // store rather than a prop: assigning a model to a role here must move it in
  // both lists immediately, and a prop that arrives one save late would not.
  const signals = useMemo(
    () => buildModelSignals(store.configs, recentRuns),
    [store.configs, recentRuns]
  );

  // What each slot is for, from the engine. The order comes from the configs
  // themselves (already in pipeline order), so there is no second list of roles
  // here to drift from the engine's.
  const [roleInfo, setRoleInfo] = useState<Record<string, RoleInfo>>({});
  const [rolesError, setRolesError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    fetchRoles()
      .then((roles) => {
        if (cancelled) return;
        setRoleInfo(Object.fromEntries(roles.map((r) => [r.role, r])));
        setRolesError(null);
      })
      .catch((err: any) => {
        if (cancelled) return;
        setRolesError(err?.message || "Could not load role descriptions.");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // What actually happened to each role the last time it ran (the roadmap's
  // per-agent latency/error stats). Re-read when a save lands so a
  // test-connection result and the next goal run both show up without leaving
  // the screen; an engine without the endpoint simply yields nothing.
  const [callStats, setCallStats] = useState<Record<string, AgentCallStat>>({});
  const [callStatsError, setCallStatsError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    fetchAgentCallStats()
      .then((stats) => {
        if (cancelled) return;
        setCallStats(Object.fromEntries(stats.map((s) => [s.role, s])));
        setCallStatsError(null);
      })
      .catch((err: any) => {
        // Stats are informational — a failure leaves the cards without the
        // "last call" line rather than failing the screen, but it is recorded
        // instead of swallowed so a broken endpoint is visible.
        if (cancelled) return;
        setCallStatsError(err?.message || "Could not load per-role call stats.");
      });
    return () => {
      cancelled = true;
    };
  }, [store.configs]);

  const  orderedRoles =  useMemo<AgentRole[]>(
    () => store.configs.map((c) => c.role),
    [store.configs]
  );

  // Which roles point at a model their provider no longer reports. Empty while
  // configs are still loading — an unknown is not a problem.
  const staleByRole = useMemo(() => {
    const verdicts = new Map<AgentRole, StaleModel>();
    for (const role of orderedRoles) {
      const verdict = findStaleModel(
        store.configs.find((c) => c.role === role),
        models,
        providerStatus
      );
      if (verdict) verdicts.set(role, verdict);
    }
    return verdicts;
  }, [orderedRoles, store.configs, models, providerStatus]);

  const staleRoles = [...staleByRole.entries()];

  // The fallback target rots the same way and is even easier to miss: it is only
  // used once something else has already failed.
  const staleFallbackByRole = useMemo(() => {
    const verdicts = new Map<AgentRole, StaleModel>();
    for (const role of orderedRoles) {
      const verdict = findStaleFallback(
        store.configs.find((c) => c.role === role),
        models,
        providerStatus
      );
      if (verdict) verdicts.set(role, verdict);
    }
    return verdicts;
  }, [orderedRoles, store.configs, models, providerStatus]);

  // Roles with no model chosen. The seed deliberately names none (no model list
  // is compiled into the build), so this is the state a fresh install is in.
  // A role with a usable fallback is not "waiting for a model" in the sense this
  // count means — but it is not configured either, so it is reported separately
  // rather than silently dropped from the total.
  const unconfigured = orderedRoles.filter((role) => {
    const cfg = store.configs.find((c) => c.role === role);
    return !(cfg?.model_name || "").trim() && !(cfg?.fallback_model_name || "").trim();
  });
  const fallbackOnly = orderedRoles.filter((role) => {
    const cfg = store.configs.find((c) => c.role === role);
    return !(cfg?.model_name || "").trim() && Boolean((cfg?.fallback_model_name || "").trim());
  });

  const [bulkChoice, setBulkChoice] = useState("");
  // Fix the roles that cannot run, in one action, without touching the ones that
  // work. The engine decides which is which and returns the reason for every
  // change *and* every skip, so this card shows the outcome instead of asserting it.
  const [repairBusy, setRepairBusy] = useState(false);
  const [repairReport, setRepairReport] = useState<RepairReport | null>(null);
  const [repairError, setRepairError] = useState<string | null>(null);
  const repairRoles = async () => {
    setRepairBusy(true);
    setRepairError(null);
    try {
      const report = await repairAgentConfigs();
      setRepairReport(report);
      // The engine changed configs underneath the cards and invalidated its catalog;
      // re-read both so the screen shows what is actually stored now.
      await store.refresh();
      onRefreshModels?.();
    } catch (err: any) {
      setRepairError(err?.message || "Could not repair the role configs");
    } finally {
      setRepairBusy(false);
    }
  };

  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkError, setBulkError] = useState<string | null>(null);

  // ── Engine settings (persisted in the store, not env vars) ─────────────
  const [parallelWidth, setParallelWidth] = useState<number | null>(null);
  const [parallelWidthBounds, setParallelWidthBounds] = useState({ min: 1, max: 16 });
  const [widthDraft, setWidthDraft] = useState<string>("");
  const [widthSaving, setWidthSaving] = useState(false);
  const [widthMsg, setWidthMsg] = useState<{ ok: boolean; text: string } | null>(null);
  // Snapshot retention (Settings → Engine): how many daily stats snapshots to
  // keep. 0 means everything — the default is bounded (90) so an unattended
  // machine's table grows by a decision, not an accident.
  const [retention, setRetention] = useState<number | null>(null);
  const [retentionBounds, setRetentionBounds] = useState({ min: 0, max: 730 });
  const [retentionDraft, setRetentionDraft] = useState<string>("");
  const [retentionSaving, setRetentionSaving] = useState(false);
  const [retentionMsg, setRetentionMsg] = useState<{ ok: boolean; text: string } | null>(null);
  // Recording retention — the same shape as the one above, for a different
  // table. Kept separate rather than folded into a generic "retention" field
  // because the two policies govern different data and a user lowering one
  // should not silently lower the other.
  const [traceRetention, setTraceRetention] = useState<number | null>(null);
  const [traceRetentionBounds, setTraceRetentionBounds] = useState({ min: 0, max: 730 });
  const [traceRetentionDraft, setTraceRetentionDraft] = useState<string>("");
  const [traceRetentionSaving, setTraceRetentionSaving] = useState(false);
  const [traceRetentionMsg, setTraceRetentionMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [engineSettingsError, setEngineSettingsError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getEngineSettings()
      .then((s) => {
        if (cancelled || !s) return;
        setParallelWidth(s.parallel_width.value);
        setParallelWidthBounds({ min: s.parallel_width.min, max: s.parallel_width.max });
        setWidthDraft(String(s.parallel_width.value));
        if (s.stats_retention_days) {
          setRetention(s.stats_retention_days.value);
          setRetentionBounds({ min: s.stats_retention_days.min, max: s.stats_retention_days.max });
          setRetentionDraft(String(s.stats_retention_days.value));
        }
        // Optional on purpose: an engine that predates the setting answers
        // without it, and a hidden field is better than a broken save.
        if (s.trace_retention_days) {
          setTraceRetention(s.trace_retention_days.value);
          setTraceRetentionBounds({
            min: s.trace_retention_days.min,
            max: s.trace_retention_days.max,
          });
          setTraceRetentionDraft(String(s.trace_retention_days.value));
        }
        setEngineSettingsError(null);
      })
      .catch((err: any) => {
        if (cancelled) return;
        setEngineSettingsError(err?.message || "Could not load engine settings.");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const saveParallelWidth = async () => {
    const parsed = parseInt(widthDraft, 10);
    if (!Number.isFinite(parsed)) {
      setWidthMsg({ ok: false, text: "Enter a whole number." });
      return;
    }
    setWidthSaving(true);
    setWidthMsg(null);
    try {
      const res = await saveEngineSettings({ parallel_width: parsed });
      const saved = res.saved.parallel_width;
      setParallelWidth(saved);
      setWidthDraft(String(saved));
      setWidthMsg(
        saved === parsed
          ? { ok: true, text: "Saved — the next parallel batch uses it, no restart needed." }
          : { ok: true, text: `Clamped to ${saved} (allowed ${parallelWidthBounds.min}–${parallelWidthBounds.max}).` }
      );
    } catch (err: any) {
      setWidthMsg({ ok: false, text: err?.message || "Could not save the parallel width." });
    } finally {
      setWidthSaving(false);
    }
  };

  const saveRetention = async () => {
    const parsed = parseInt(retentionDraft, 10);
    if (!Number.isFinite(parsed)) {
      setRetentionMsg({ ok: false, text: "Enter a whole number (0 keeps everything)." });
      return;
    }
    setRetentionSaving(true);
    setRetentionMsg(null);
    try {
      const res = await saveEngineSettings({ stats_retention_days: parsed });
      const saved = res.saved.stats_retention_days;
      setRetention(saved);
      setRetentionDraft(String(saved));
      setRetentionMsg(
        saved === parsed
          ? {
              ok: true,
              text:
                saved === 0
                  ? "Saved — every daily snapshot is kept."
                  : `Saved — the ${saved} most recent days are kept; older ones go on the next stats view.`,
            }
          : { ok: true, text: `Clamped to ${saved} (allowed ${retentionBounds.min}–${retentionBounds.max}).` }
      );
    } catch (err: any) {
      setRetentionMsg({ ok: false, text: err?.message || "Could not save the retention setting." });
    } finally {
      setRetentionSaving(false);
    }
  };

  const saveTraceRetention = async () => {
    const parsed = parseInt(traceRetentionDraft, 10);
    if (!Number.isFinite(parsed)) {
      setTraceRetentionMsg({ ok: false, text: "Enter a whole number (0 keeps every recording)." });
      return;
    }
    setTraceRetentionSaving(true);
    setTraceRetentionMsg(null);
    try {
      const res = await saveEngineSettings({ trace_retention_days: parsed });
      const saved = res.saved.trace_retention_days;
      setTraceRetention(saved);
      setTraceRetentionDraft(String(saved));
      setTraceRetentionMsg(
        saved === parsed
          ? {
              ok: true,
              text:
                saved === 0
                  ? "Saved — recordings are kept until you delete them."
                  : `Saved — recordings older than ${saved} days go on the next Stats view.`,
            }
          : { ok: true, text: `Clamped to ${saved} (allowed ${traceRetentionBounds.min}–${traceRetentionBounds.max}).` }
      );
    } catch (err: any) {
      setTraceRetentionMsg({ ok: false, text: err?.message || "Could not save the recording retention setting." });
    } finally {
      setTraceRetentionSaving(false);
    }
  };

  // Point every role at one discovered model — the fast path out of the
  // unconfigured state, using ids the providers actually report.
  const applyToEveryRole = async () => {
    const option = models.find((m) => `${m.provider}:${m.id}` === bulkChoice);
    if (!option) return;
    setBulkBusy(true);
    setBulkError(null);
    try {
      for (const role of orderedRoles) {
        await store.update(role, {
          provider: option.provider,
          ...(option.protocol ? { protocol: option.protocol } : {}),
          model_name: option.id,
        });
      }
    } catch (err: any) {
      setBulkError(err?.message || "Failed to apply the model to every role");
    } finally {
      setBulkBusy(false);
    }
  };

  return (
    <div
      className={
        embedded
          ? "flex flex-col gap-4 py-1"
          : "max-w-5xl mx-auto py-6 px-4 flex flex-col gap-6"
      }
    >
      {!embedded && (
        <div className="flex flex-col gap-1 border-b border-codify-border pb-4">
          <h2 className="text-xl font-bold text-gray-100 flex items-center gap-2">
            <Sliders className="w-5 h-5 text-blue-400" />
            Sub-Agent Configuration
          </h2>
          <p className="text-sm text-gray-400 flex items-center gap-2">
            <ShieldCheck className="w-4 h-4 text-green-400" />
            Settings is the exclusive mutator for sub-agent models and credentials. Keys go to your
            OS keychain when one is available, otherwise to an owner-only file — the Provider Keys
            tab states which is in force.
          </p>
        </div>
      )}

      <LayaGateStatus />

      {rolesError && (
        <div className="flex items-start gap-2 text-xs text-amber-300 bg-amber-950/30 border border-amber-800/60 rounded-lg p-3">
          <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
          <span className="leading-relaxed">Role descriptions unavailable — {rolesError}</span>
        </div>
      )}

      {callStatsError && (
        <div className="flex items-start gap-2 text-xs text-gray-400 bg-codify-surface border border-codify-border rounded-lg p-3">
          <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5 text-gray-500" />
          <span className="leading-relaxed">Per-role call stats unavailable — {callStatsError}</span>
        </div>
      )}

      {engineSettingsError && (
        <div className="flex items-start gap-2 text-xs text-amber-300 bg-amber-950/30 border border-amber-800/60 rounded-lg p-3">
          <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
          <span className="leading-relaxed">Engine settings unavailable — {engineSettingsError}</span>
        </div>
      )}

      <div className="flex flex-col gap-2.5 bg-codify-bg border border-codify-border rounded-xl p-3.5">
        <div className="flex items-center gap-2 text-xs font-semibold text-gray-200">
          <Workflow className="w-3.5 h-3.5 text-purple-400" />
          Engine
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label
            htmlFor="parallel-width"
            className="text-xs text-gray-300 font-medium"
            title="How many steps of a parallel goal may run at once"
          >
            Parallel step width
          </label>
          <input
            id="parallel-width"
            type="number"
            min={parallelWidthBounds.min}
            max={parallelWidthBounds.max}
            value={widthDraft}
            onChange={(e) => {
              setWidthDraft(e.target.value);
              setWidthMsg(null);
            }}
            disabled={parallelWidth === null || widthSaving}
            className="w-20 bg-codify-surface border border-codify-border rounded-lg px-2.5 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-blue-500 font-mono disabled:opacity-40"
          />
          <button
            type="button"
            onClick={saveParallelWidth}
            disabled={
              parallelWidth === null ||
              widthSaving ||
              widthDraft === String(parallelWidth)
            }
            className="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 text-white text-xs font-semibold rounded-lg transition-colors disabled:opacity-40"
          >
            {widthSaving ? "Saving..." : "Save"}
          </button>
          {widthMsg && (
            <span className={`text-xs ${widthMsg.ok ? "text-green-400" : "text-red-400"}`}>
              {widthMsg.text}
            </span>
          )}
        </div>
        <p className="text-xs text-gray-400 leading-relaxed">
          How many steps of a parallel goal may run at once; a wider plan runs in waves of this size
          instead of opening every model session simultaneously. Stored with the engine's settings
          (the <span className="font-mono">CODIFY_PARALLEL_WIDTH</span> env var, if set, overrides
          this field), and applies from the next batch without a restart.
        </p>

        <div className="flex flex-wrap items-center gap-2 border-t border-codify-border pt-2.5">
          <label
            htmlFor="stats-retention"
            className="text-xs text-gray-300 font-medium"
            title="How many daily statistics snapshots to keep"
          >
            Stats history to keep (days)
          </label>
          <input
            id="stats-retention"
            type="number"
            min={retentionBounds.min}
            max={retentionBounds.max}
            value={retentionDraft}
            onChange={(e) => {
              setRetentionDraft(e.target.value);
              setRetentionMsg(null);
            }}
            disabled={retention === null || retentionSaving}
            className="w-20 bg-codify-surface border border-codify-border rounded-lg px-2.5 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-blue-500 font-mono disabled:opacity-40"
          />
          <button
            type="button"
            onClick={saveRetention}
            disabled={
              retention === null ||
              retentionSaving ||
              retentionDraft === String(retention)
            }
            className="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 text-white text-xs font-semibold rounded-lg transition-colors disabled:opacity-40"
          >
            {retentionSaving ? "Saving..." : "Save"}
          </button>
          {retentionMsg && (
            <span className={`text-xs ${retentionMsg.ok ? "text-green-400" : "text-red-400"}`}>
              {retentionMsg.text}
            </span>
          )}
        </div>
        <p className="text-xs text-gray-400 leading-relaxed">
          How many of the daily statistics snapshots (the “Day by day” chart in Stats) to keep.
          <span className="text-gray-300"> 0 keeps everything.</span> Lowering the number prunes the
          oldest days on the next stats view; the newest days always survive.
        </p>

        {traceRetention !== null && (
          <>
            <div className="flex flex-wrap items-center gap-2 border-t border-codify-border pt-2.5">
              <label
                htmlFor="trace-retention"
                className="text-xs text-gray-300 font-medium"
                title="How long a run's recording is kept"
              >
                Recordings to keep (days)
              </label>
              <input
                id="trace-retention"
                type="number"
                min={traceRetentionBounds.min}
                max={traceRetentionBounds.max}
                value={traceRetentionDraft}
                onChange={(e) => {
                  setTraceRetentionDraft(e.target.value);
                  setTraceRetentionMsg(null);
                }}
                disabled={traceRetentionSaving}
                className="w-20 bg-codify-surface border border-codify-border rounded-lg px-2.5 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-blue-500 font-mono disabled:opacity-40"
              />
              <button
                type="button"
                onClick={saveTraceRetention}
                disabled={
                  traceRetentionSaving ||
                  traceRetentionDraft === String(traceRetention)
                }
                className="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 text-white text-xs font-semibold rounded-lg transition-colors disabled:opacity-40"
              >
                {traceRetentionSaving ? "Saving..." : "Save"}
              </button>
              {traceRetentionMsg && (
                <span className={`text-xs ${traceRetentionMsg.ok ? "text-green-400" : "text-red-400"}`}>
                  {traceRetentionMsg.text}
                </span>
              )}
            </div>
            <p className="text-xs text-gray-400 leading-relaxed">
              How long a run’s recording (the model calls of a goal armed with Record) is kept.
              <span className="text-gray-300"> 0 keeps every recording.</span> Older ones are
              forgotten on the next Stats view. Deleting a recording early is always allowed from
              the run’s own card.
            </p>
          </>
        )}
      </div>

      <div className="flex flex-col gap-2.5 bg-codify-bg border border-codify-border rounded-xl p-3.5">
        <div className="flex items-center gap-2 text-xs font-semibold text-gray-200">
          <Wrench className="w-3.5 h-3.5 text-emerald-400" />
          Fix the roles that can't run
        </div>
        <p className="text-xs text-gray-400 leading-relaxed">
          Points a role at a model this engine has discovered when it has no model chosen, when its
          provider needs a credential none is stored for, or when its provider no longer serves the
          model it is set to. <span className="text-gray-300">Roles that already work are not
          touched</span> — unlike the override below, which deliberately applies to all of them.
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={repairRoles}
            disabled={repairBusy}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white text-xs font-semibold rounded-lg transition-colors"
          >
            <Wrench className={repairBusy ? "w-3.5 h-3.5 animate-pulse" : "w-3.5 h-3.5"} />
            {repairBusy ? "Checking every role..." : "Fix roles that can't run"}
          </button>
          {repairReport && (
            <button
              type="button"
              onClick={() => setRepairReport(null)}
              className="text-xs text-gray-400 hover:text-gray-200"
            >
              Dismiss
            </button>
          )}
        </div>

        {repairError && (
          <span className="text-xs text-red-400 font-medium">{repairError}</span>
        )}

        {repairReport && (
          <div className="flex flex-col gap-1.5 border-t border-codify-border pt-2">
            {repairReport.changed ? (
              <>
                <span className="text-xs text-gray-300">
                  Pointed {repairReport.repaired.length} role
                  {repairReport.repaired.length === 1 ? "" : "s"} at{" "}
                  <span className="font-mono text-emerald-300">
                    {repairReport.target?.provider}/{repairReport.target?.model}
                  </span>{" "}
                  — {repairReport.target_reason}.
                </span>
                <div className="flex flex-col gap-0.5">
                  {repairReport.repaired.map((row) => (
                    <span key={row.role} className="text-xs text-gray-400">
                      <span className="font-mono text-gray-200">{row.role}</span> — {row.reason}
                    </span>
                  ))}
                </div>
              </>
            ) : repairReport.unfixable.length > 0 ? (
              /* A broken install and a broken install nobody could fix are not the
                 same result, and this one must not read as a pass. */
              <>
                <span className="text-xs text-amber-300">
                  Nothing was changed: {repairReport.unfixable.length} role
                  {repairReport.unfixable.length === 1 ? "" : "s"} need a model and none could be
                  pointed at{repairReport.target_reason ? ` — ${repairReport.target_reason}` : ""}.
                </span>
                <div className="flex flex-col gap-0.5">
                  {repairReport.unfixable.map((row) => (
                    <span key={row.role} className="text-xs text-amber-300/80">
                      <span className="font-mono">{row.role}</span> — {row.reason}
                    </span>
                  ))}
                </div>
              </>
            ) : (
              <span className="text-xs text-gray-300">
                Nothing needed fixing
                {repairReport.target_reason ? ` — ${repairReport.target_reason}` : ""}.
              </span>
            )}

            {repairReport.left_alone.length > 0 && (
              <div className="flex flex-col gap-0.5">
                <span className="text-2xs uppercase tracking-wider text-gray-500">
                  Left alone ({repairReport.left_alone.length})
                </span>
                {repairReport.left_alone.map((row) => (
                  <span key={row.role} className="text-xs text-gray-500">
                    <span className="font-mono text-gray-400">{row.role}</span> — {row.reason}
                  </span>
                ))}
              </div>
            )}

            {repairReport.notes.map((note, i) => (
              <span key={i} className="text-xs text-amber-400/90 flex items-start gap-1.5">
                <AlertTriangle className="w-3 h-3 flex-shrink-0 mt-0.5" />
                {note}
              </span>
            ))}
          </div>
        )}
      </div>

      <div className="flex flex-col gap-2.5 bg-codify-bg border border-codify-border rounded-xl p-3.5">
        <div className="flex items-center gap-2 text-xs font-semibold text-gray-200">
          <Wand2 className="w-3.5 h-3.5 text-blue-400" />
          Use one model for every role
        </div>
        <p className="text-xs text-gray-400 leading-relaxed">
          No model list ships with Codify — every id below was discovered from a provider you
          configured. Roles start with none chosen, so a fresh install has to say which model it
          runs.
          {unconfigured.length > 0 && (
            <span className="text-amber-300">
              {" "}
              {unconfigured.length} of {orderedRoles.length} still need one: {unconfigured.join(", ")}.
            </span>
          )}
          {fallbackOnly.length > 0 && (
            <span className="text-teal-300">
              {" "}
              {fallbackOnly.join(", ")} {fallbackOnly.length === 1 ? "has" : "have"} no primary model
              and will run on the configured fallback{fallbackOnly.length === 1 ? "" : "s"} instead.
            </span>
          )}
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <select
            value={bulkChoice}
            onChange={(e) => setBulkChoice(e.target.value)}
            className="flex-1 min-w-[16rem] bg-codify-surface border border-codify-border rounded-lg px-3 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-blue-500 font-mono"
          >
            <option value="">
              {models.length > 0 ? "Choose a discovered model..." : "No models discovered yet"}
            </option>
            {models.map((m) => (
              <option key={`${m.provider}:${m.id}`} value={`${m.provider}:${m.id}`}>
                {m.provider} · {m.id}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={applyToEveryRole}
            disabled={!bulkChoice || bulkBusy}
            className="px-3 py-1.5 bg-blue-600 hover:bg-blue-500 text-white text-xs font-semibold rounded-lg transition-colors disabled:opacity-40"
          >
            {bulkBusy ? "Applying..." : `Apply to all ${orderedRoles.length} roles`}
          </button>
          {bulkError && <span className="text-xs text-red-400">{bulkError}</span>}
        </div>
      </div>

      {/* Summary first: the role cards live in a scroll container, so a warning
          on the fifth of eight cards is invisible without knowing to look. */}
      {staleRoles.length > 0 && (
        <div className="flex items-start gap-2 text-xs text-amber-300 bg-amber-950/30 border border-amber-800/60 rounded-lg p-3">
          <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
          <span className="leading-relaxed">
            {staleRoles.length} of {orderedRoles.length} roles point at a model their provider no
            longer reports:{" "}
            {staleRoles.map(([role, s]) => `${role} (${s.model})`).join(", ")}. Each affected card
            below is outlined in amber.
          </span>
        </div>
      )}

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4 items-start">
        {orderedRoles.map((role) => (
          <AgentConfigCard
            key={role}
            role={role}
            store={store}
            models={models}
            providerStatus={providerStatus}
            stale={staleByRole.get(role) ?? null}
            staleFallback={staleFallbackByRole.get(role) ?? null}
            info={roleInfo[role]}
            callStat={callStats[role] ?? null}
            signals={signals}
            onRefreshModels={onRefreshModels}
            refreshingModels={refreshingModels}
          />
        ))}
      </div>
    </div>
  );
};
