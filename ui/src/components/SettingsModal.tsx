import { readRejection } from "../rejection.ts";
import React, { useState, useEffect, useCallback, useMemo, useRef } from "react";
import {
  AgentRole,
  ModelCatalog,
  ModelOption,
  ProviderKeyStatus,
  ProviderModelStatus,
  RecentRunModel,
  SettingsTab,
} from "../types";
import { fetchModelCatalog, fetchProviderKeys, saveProviderKey } from "../api";
import { SettingsPanel } from "./SettingsPanel";
import { AppearancePane } from "./AppearancePane";
import { AudioPane } from "./AudioPane";
import { AboutPane } from "./AboutPane";
import { ProviderRow } from "./ProviderRow";
import { useAgentConfigs } from "../hooks/useAgentConfigs";
import { checkedLabel, discoveredFooter, needsOwnRefresh } from "../providerSetup";
import {
  loadSeen,
  mergeAll,
  newModelIds,
  saveSeen,
  type SeenIndex,
} from "../modelFreshness";
import { X, Key, Sliders, HardDrive, Lock, AlertCircle, RefreshCw, Palette, Mic, Info } from "lucide-react";

/**
 * How often the panel re-asks while it is open, and how long after a check a
 * window focus is ignored.
 *
 * A minute matches the engine's own cache TTL (`DEFAULT_TTL_S`), so every tick is
 * a real answer rather than an echo of the last one. The cooldown is the other
 * half: alt-tabbing fires a burst of focus events, and each burst is eight
 * providers asked in parallel — a reader switching windows twice would queue
 * more provider traffic than the minute is worth.
 */
const AUTO_REFRESH_MS = 60_000;
const FOCUS_COOLDOWN_MS = 10_000;

interface SettingsModalProps {
  isOpen: boolean;
  onClose: () => void;
  /** Which tab to open on (defaults to keys). */
  initialTab?: SettingsTab;
  /**
   * Models that recently answered, handed down so a role's model field badges the
   * ids the app has actually run — the same hint the chat's menu shows.
   */
  recentRuns?: RecentRunModel[];
  /**
   * Bumped by the engine-level stream whenever a provider's model list moves.
   *
   * The panel owns its own fetch rather than being handed the catalogue, so this
   * is how it learns there is something to re-read — and the re-read is
   * `refresh: false`, because the engine only announces a change *after* the
   * fresh answer is in its cache. Asking the eight providers again here would
   * spend a rate limit to be told what we were just told.
   */
  catalogTick?: number;
  /**
   * Is the engine pushing the catalogue to us?
   *
   * While it is, this panel owns no timer at all: the engine's watcher sweeps on
   * the catalogue's own cache period and one sweep serves every screen, so a
   * timer here would ask eight providers a second time for an answer the app
   * already has. While it is not — the socket dropped, or a build without the
   * channel — the panel falls back to refreshing itself, which is a stale list
   * versus one extra discovery, and the list is the worse of the two.
   */
  catalogLive?: boolean;
}

/**
 * The settings screen.
 *
 * It is a full-height panel, not a dialog: eight role cards and eight providers do
 * not fit in a 2xl box with a 26rem scroll region, and shrinking them into one
 * made every card half cut off. Left column = where credentials live, right
 * column = which agent gets which model, both scrolling independently.
 */
export const SettingsModal: React.FC<SettingsModalProps> = ({
  isOpen,
  onClose,
  initialTab = "keys",
  recentRuns = [],
  catalogTick = 0,
  catalogLive = false,
}) => {
  const [tab, setTab] = useState<SettingsTab>(initialTab);
  const [keys, setKeys] = useState<ProviderKeyStatus[]>([]);
  // No draft keys here any more. Each `ProviderRow` owns the text in its own
  // field, so this component no longer holds a second copy of a secret it does
  // not use — and a modal-level draft map was the wrong place for one: it
  // outlived the row that filled it, and survived a tab switch with the value
  // still in it.
  const [saving, setSaving] = useState<Record<string, boolean>>({});
  const [savedSuccess, setSavedSuccess] = useState<Record<string, boolean>>({});
  const [error, setError] = useState<string | null>(null);
  // Discovered models, so a freshly saved key immediately shows what that
  // provider offers — and a failing provider shows why it offered nothing.
  const [models, setModels] = useState<ModelOption[]>([]);
  const [modelStatus, setModelStatus] = useState<ProviderModelStatus[]>([]);

  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  /** Which request is in flight, so the footer can say what it is waiting on. */
  const [checking, setChecking] = useState<"manual" | "auto" | null>(null);
  /**
   * When the engine last *asked* the providers, in epoch seconds. This is the
   * engine's own `fetched_at`, not the moment the answer reached the UI, so a
   * cached reply reports when it was really fetched rather than claiming to have
   * just been.
   */
  const [lastChecked, setLastChecked] = useState<number | null>(null);
  /** In-flight and last-start, as refs: a ref is the only thing a burst sees. */
  const inFlight = useRef(false);
  const lastFetchAt = useRef(0);
  /** An explicit refresh that arrived during a request, to run the moment it ends. */
  const askAgain = useRef(false);
  /**
   * The current `loadCatalog`, for the re-run below. A ref, because the function
   * refers to itself and a `useCallback` cannot hold the value it is producing.
   * Assigned in the body rather than in an effect: an effect would run *after*
   * the effect that opens the panel, so the first request on the first open
   * would re-run through a seed instead of the real function. Writing a ref is
   * idempotent, so a second render in strict mode changes nothing.
   */
  const loadCatalogRef = useRef<(refresh?: boolean, cause?: "manual" | "auto") => Promise<void>>(
    async () => {},
  );
  /**
   * Ticks on its own so "checked 12s ago" becomes "checked 48s ago" without a
   * re-render of the whole panel every second.
   */
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!isOpen) return;
    const id = window.setInterval(() => setNow(Date.now() / 1000), 10_000);
    return () => window.clearInterval(id);
  }, [isOpen]);

  /**
   * What the last visit to this panel showed, per provider — the baseline the
   * "new" marker is measured against.
   *
   * Read once, on purpose. A baseline that moved with every fetch would make the
   * marker blink out the moment it appeared, which is the same as not having one.
   * The stored copy is what the *next* launch compares against, so a model stays
   * badged for as long as this session lasts and then stops being news.
   */
  const [seen] = useState<SeenIndex>(loadSeen);

  // The agent store, owned here rather than in SettingsPanel, so the provider
  // tab can say how many roles a model would touch and then write to them. One
  // store for the whole screen: eight role cards each holding their own copy
  // meant a save in one left the others stale, and this tab would have had a
  // third, disagreeing one.
  const store = useAgentConfigs();

  /** The footer's freshness line, or nothing before the first answer arrives. */
  const checked = checkedLabel(lastChecked, now, checking !== null);

  /** Each provider's slice of the discovered catalogue, computed once. */
  const modelsByProvider = useMemo(() => {
    const by = new Map<string, ModelOption[]>();
    for (const m of models) {
      const list = by.get(m.provider) ?? [];
      list.push(m);
      by.set(m.provider, list);
    }
    return by;
  }, [models]);

  /** What each provider has newly released, against the last visit. */
  const newIdsByProvider = useMemo(() => {
    const by = new Map<string, string[]>();
    for (const m of models) {
      const list = by.get(m.provider) ?? [];
      list.push(m.id);
      by.set(m.provider, list);
    }
    const out = new Map<string, string[]>();
    for (const [provider, ids] of by) {
      out.set(provider, newModelIds(seen, provider, ids));
    }
    return out;
  }, [models, seen]);

  const roleProviders = useMemo(
    () =>
      store.configs.map((c) => ({
        role: c.role as AgentRole,
        provider: c.provider,
        model_name: c.model_name,
      })),
    [store.configs],
  );

  /**
   * Write one model to the named roles, one request each.
   *
   * `PUT /settings/agents/{role}` is the only route that may change agent
   * config (docs/00 §6.2), so a provider row has no "set the default" of its
   * own to call — it calls the one legal route, per role. The protocol travels
   * with it because a model is only callable on the dialect its provider speaks,
   * and `openai_compat` is not a synonym for "any endpoint".
   */
  const applyModel = async (
    provider: string,
    model: string,
    roles: AgentRole[],
  ): Promise<void> => {
    const target = models.find((m) => m.provider === provider && m.id === model);
    for (const role of roles) {
      await store.update(role, {
        provider,
        ...(target?.protocol ? { protocol: target.protocol } : {}),
        model_name: model,
      });
    }
  };

  const loadCatalog = useCallback(
    async (refresh = false, cause: "manual" | "auto" = "manual") => {
      // One discovery at a time. A second burst stacked on the first asks eight
      // providers twice for the same answer, and the slower of the two is what
      // the reader ends up looking at.
      //
      // The two callers are not treated the same, because what they want is not.
      // An automatic one can be dropped: the request already on its way is
      // seconds old at worst, and the next tick is a minute away. An explicit one
      // cannot — a key saved a moment ago invalidated the engine's cache, and the
      // in-flight answer may predate the key, which is how "paste a key and the
      // list fills in" (docs/06 §1) turns into an empty list for a minute.
      if (inFlight.current) {
        if (cause === "auto") return;
        askAgain.current = true;
        return;
      }
      inFlight.current = true;
      lastFetchAt.current = Date.now();
      setCatalogLoading(true);
      setChecking(cause);
      setCatalogError(null);
      try {
        const catalog: ModelCatalog = await fetchModelCatalog(refresh);
        setModels(catalog.models ?? []);
        setModelStatus(catalog.providers ?? []);
        setLastChecked(catalog.fetched_at ?? null);
        // Fold what just came back into the stored baseline, so the models on
        // screen right now are not "new" the next time this panel is opened.
        // A provider that refused to answer contributes nothing: recording its
        // empty list as what it serves is how a rejected key turns into every
        // model on the provider being announced as new.
        const idsByProvider = new Map<string, string[]>();
        for (const m of catalog.models ?? []) {
          const list = idsByProvider.get(m.provider) ?? [];
          list.push(m.id);
          idsByProvider.set(m.provider, list);
        }
        saveSeen(
          mergeAll(
            seen,
            (catalog.providers ?? []).map((p) => ({
              provider: p.provider,
              ok: p.ok,
              ids: idsByProvider.get(p.provider) ?? [],
            })),
          ),
        );
      } catch (err: any) {
        setCatalogError(readRejection(err, "Could not discover models from the engine."));
      } finally {
        inFlight.current = false;
        setCatalogLoading(false);
        setChecking(null);
        if (askAgain.current) {
          askAgain.current = false;
          void loadCatalogRef.current(true, "manual");
        }
      }
    },
    [seen],
  );
  loadCatalogRef.current = loadCatalog;

  /**
   * The engine reported it has a current catalogue — a change, or just a check.
   * Re-read it from the cache the engine just filled: a provider that gains a
   * model mid-session should appear in this row now, badged as new, without
   * waiting for anything.
   *
   * The record of "what was new" is deliberately not touched here
   * (`modelFreshness`): it is still the last *visit*, so a release that lands
   * mid-session is badged, and it stops being news on the next launch rather than
   * the next check.
   */
  useEffect(() => {
    // The fallback, and only the fallback. While the engine pushes, the watcher
    // sweeps on the catalogue's own cache period and one sweep serves every open
    // screen — a timer here would ask eight providers a second time for an answer
    // this app already has. When the socket is down the calculation reverses: a
    // stale list is worse than an extra discovery, so the panel asks.
    if (!needsOwnRefresh({ live: catalogLive, isOpen })) return;
    const onFocus = () => {
      if (Date.now() - lastFetchAt.current < FOCUS_COOLDOWN_MS) return;
      void loadCatalog(true, "auto");
    };
    const tick = window.setInterval(() => {
      // A hidden window is a window nobody is reading, and polling it spends
      // provider quota to update a list that is not on screen. The focus handler
      // covers the moment it comes back.
      if (document.hidden) return;
      void loadCatalog(true, "auto");
    }, AUTO_REFRESH_MS);
    window.addEventListener("focus", onFocus);
    return () => {
      window.clearInterval(tick);
      window.removeEventListener("focus", onFocus);
    };
  }, [isOpen, catalogLive, loadCatalog]);

  useEffect(() => {
    setTab(initialTab);
  }, [initialTab, isOpen]);

  useEffect(() => {
    if (isOpen && tab === "keys") {
      loadKeys();
    }
    if (isOpen) {
      loadCatalog(true);
    }
  }, [isOpen, tab, loadCatalog]);

  /**
   * The engine announced that a provider's list moved. Re-read it from the cache
   * the engine just filled: a provider that gains a model mid-session should
   * appear in this row now, badged as new, without waiting for the next tick.
   */
  useEffect(() => {
    if (!isOpen || catalogTick === 0) return;
    void loadCatalog(false);
  }, [isOpen, catalogTick, loadCatalog]);

  // Escape closes, like every other overlay in the app.
  useEffect(() => {
    if (!isOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isOpen, onClose]);

  const loadKeys = async () => {
    try {
      const data = await fetchProviderKeys();
      setKeys(data);
    } catch (err: any) {
      setError(err.message || "Failed to load provider credentials");
    }
  };

  if (!isOpen) return null;

  const storage = keys[0]?.storage;
  const storageDetail = keys[0]?.storage_detail;
  // Three ways to end up in a file, and only one of them is a missing keyring.
  // An older engine does not send the reason, so the absent case keeps the old copy.
  const storageReason = keys[0]?.storage_reason ?? (storage === "file" ? "no_keyring" : "keyring");
  const needsKeyCount = keys.filter((k) => k.needs_key && !k.has_key).length;

  return (
    <div className="fixed inset-0 bg-black/75 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-codify-surface border border-codify-border rounded-2xl shadow-2xl w-full max-w-6xl h-[90vh] flex flex-col overflow-hidden">
        {/* Header */}
        <div className="flex items-start justify-between border-b border-codify-border px-6 py-4 flex-shrink-0">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl bg-codify-info/20 border border-codify-info/30 flex items-center justify-center text-codify-info">
              <Key className="w-4 h-4" />
            </div>
            <div>
              <h2 className="text-base font-bold text-codify-primary">Settings</h2>
              <p className="text-xs text-codify-muted">
                {tab === "keys"
                  ? needsKeyCount > 0
                    ? `${needsKeyCount} of ${keys.length} providers still need a key — their models cannot be listed or called until then.`
                    : "Every keyed provider has a credential. Models are discovered live from each provider."
                  : tab === "appearance"
                    ? "Themes swap the app's surface and text colours at runtime — no restart, no rebuild."
                    : tab === "about"
                      ? "What this app is, what it is running on, and the keyboard shortcuts."
                      : tab === "audio"
                        ? "The microphone, dictation into the prompt, and answers read aloud — through the speech providers you choose."
                        : "Each role's model, endpoint, temperature, and prompt. Roles run in the order of the pipeline."}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-codify-muted hover:text-codify-secondary p-1.5 rounded-lg hover:bg-codify-raised transition-colors"
            title="Close (Esc)"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Tab bar */}
        <div className="flex items-center gap-1 px-6 pt-3 flex-shrink-0">
          <button
            type="button"
            onClick={() => setTab("keys")}
            className={`flex items-center gap-1.5 px-3.5 py-1.5 text-xs font-semibold rounded-lg transition-colors ${
              tab === "keys"
                ? "bg-codify-raised text-codify-primary border border-codify-border"
                : "text-codify-muted hover:text-codify-secondary"
            }`}
          >
            <Key className="w-3.5 h-3.5" /> Provider Keys
            {needsKeyCount > 0 && (
              <span className="ml-0.5 text-2xs px-1.5 rounded-full bg-codify-warning/40 border border-codify-warning/60 text-codify-warning">
                {needsKeyCount}
              </span>
            )}
          </button>
          <button
            type="button"
            onClick={() => setTab("agents")}
            className={`flex items-center gap-1.5 px-3.5 py-1.5 text-xs font-semibold rounded-lg transition-colors ${
              tab === "agents"
                ? "bg-codify-raised text-codify-primary border border-codify-border"
                : "text-codify-muted hover:text-codify-secondary"
            }`}
          >
            <Sliders className="w-3.5 h-3.5" /> Agent Roles
          </button>
          <button
            type="button"
            onClick={() => setTab("audio")}
            className={`flex items-center gap-1.5 px-3.5 py-1.5 text-xs font-semibold rounded-lg transition-colors ${
              tab === "audio"
                ? "bg-codify-raised text-codify-primary border border-codify-border"
                : "text-codify-muted hover:text-codify-secondary"
            }`}
          >
            <Mic className="w-3.5 h-3.5" /> Audio
          </button>
          <button
            type="button"
            onClick={() => setTab("appearance")}
            className={`flex items-center gap-1.5 px-3.5 py-1.5 text-xs font-semibold rounded-lg transition-colors ${
              tab === "appearance"
                ? "bg-codify-raised text-codify-primary border border-codify-border"
                : "text-codify-muted hover:text-codify-secondary"
            }`}
          >
            <Palette className="w-3.5 h-3.5" /> Appearance
          </button>
          <button
            type="button"
            onClick={() => setTab("about")}
            className={`flex items-center gap-1.5 px-3.5 py-1.5 text-xs font-semibold rounded-lg transition-colors ${
              tab === "about"
                ? "bg-codify-raised text-codify-primary border border-codify-border"
                : "text-codify-muted hover:text-codify-secondary"
            }`}
          >
            <Info className="w-3.5 h-3.5" /> About
          </button>
        </div>

        {/* Body — the only scrolling region, full panel height */}
        <div className="flex-1 min-h-0 overflow-y-auto px-6 py-4">
          {tab === "keys" && (
            <div className="space-y-4">
              {error && (
                <div className="flex items-start gap-2 p-3 bg-codify-danger/20 border border-codify-danger/60 rounded-xl text-xs text-codify-danger">
                  <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
                  <span>{error}</span>
                </div>
              )}
              {catalogError && (
                <div className="flex items-start gap-2 p-3 bg-codify-warning/20 border border-codify-warning/60 rounded-xl text-xs text-codify-warning">
                  <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" />
                  <span>{catalogError}</span>
                </div>
              )}

              {/* Where a key actually goes. Saying "your OS keychain" when the key
                  is really written to a file would be a lie the user cannot see. */}
              {storage && (
                <div className="flex items-start gap-2 text-xs text-codify-muted bg-codify-bg border border-codify-border rounded-xl px-3.5 py-2.5">
                  {storage === "keyring" ? (
                    <Lock className="w-3.5 h-3.5 flex-shrink-0 mt-0.5 text-codify-success" />
                  ) : (
                    <HardDrive className="w-3.5 h-3.5 flex-shrink-0 mt-0.5 text-codify-warning" />
                  )}
                  <span className="leading-relaxed">
                    {storageReason === "keyring" ? (
                      <>Keys are stored in your OS keychain.</>
                    ) : storageReason === "isolated_run" ? (
                      <>
                        Keys are stored in <span className="font-mono text-codify-secondary">{storageDetail}</span>
                        . That is by design here, not a missing keychain: this engine was started with its
                        own state directory, so it cannot read or write the real one.
                      </>
                    ) : (
                      <>
                        Keys are stored in <span className="font-mono text-codify-secondary">{storageDetail}</span>{" "}
                        — this machine has no usable OS keychain, so Codify keeps them in an owner-only
                        file instead. Install <span className="font-mono text-codify-secondary">keyring</span>{" "}
                        (with a Secret Service / Keychain backend) to move them there.
                      </>
                    )}{" "}
                    Keys are also read from environment variables (
                    <span className="font-mono">OPENAI_API_KEY</span>,{" "}
                    <span className="font-mono">ANTHROPIC_API_KEY</span>, …) when none is stored.
                  </span>
                </div>
              )}

              {/* One row per provider: the credential, and the model, in the order
                  a person decides them. Each row owns its own staged model, so
                  two providers can be half-configured at once without either
                  losing what was typed. */}
              <div className="space-y-3">
                {keys.map((k) => (
                  <ProviderRow
                    key={k.provider}
                    keyStatus={k}
                    status={modelStatus.find((p) => p.provider === k.provider)}
                    models={modelsByProvider.get(k.provider) ?? []}
                    newIds={newIdsByProvider.get(k.provider) ?? []}
                    roleProviders={roleProviders}
                    onSaveKey={async (provider, key) => {
                      setSaving((prev) => ({ ...prev, [provider]: true }));
                      setError(null);
                      try {
                        await saveProviderKey(provider, key);
                        setSavedSuccess((prev) => ({ ...prev, [provider]: true }));
                        await loadKeys();
                        // The engine invalidates its catalog on key save, so ask
                        // again — that is what fills the picker beside this field
                        // without a second visit.
                        await loadCatalog(true);
                        setTimeout(
                          () => setSavedSuccess((prev) => ({ ...prev, [provider]: false })),
                          2500,
                        );
                      } finally {
                        setSaving((prev) => ({ ...prev, [provider]: false }));
                      }
                    }}
                    onApplyModel={applyModel}
                    onRefresh={() => loadCatalog(true)}
                    refreshing={catalogLoading}
                    savingKey={saving[k.provider]}
                    keySaved={savedSuccess[k.provider]}
                  />
                ))}
              </div>
            </div>
          )}

          {tab === "agents" && (
            <SettingsPanel
              embedded
              models={models}
              providerStatus={modelStatus}
              recentRuns={recentRuns}
              onRefreshModels={() => loadCatalog(true)}
              refreshingModels={catalogLoading}
            />
          )}

          {tab === "audio" && (
            <AudioPane
              models={models}
              providerStatus={modelStatus}
              onRefreshModels={() => loadCatalog(true)}
              refreshingModels={catalogLoading}
            />
          )}

          {tab === "appearance" && <AppearancePane />}

          {tab === "about" && <AboutPane />}
        </div>

        {/* Footer */}
        <div className="border-t border-codify-border px-6 py-3 flex items-center justify-between flex-shrink-0">
          <span className="flex items-center gap-2 text-xs text-codify-muted">
            {discoveredFooter(models.length, modelStatus.filter((s) => s.ok).length)}
            {/* The list re-discovers on its own now, so it can change while
                somebody is reading it. Saying when the last answer arrived is
                what keeps that from reading as a model quietly disappearing. */}
            {checked && (
              <span
                className={
                  checking === "auto" ? "text-codify-info/90 flex items-center gap-1" : undefined
                }
                title={
                  checking === "auto"
                    ? "Re-checking the providers on their own — this panel re-asks every minute and whenever the window comes back"
                    : "When the providers were last asked what they serve"
                }
              >
                {checkedLabel(lastChecked, now, checking !== null)}
              </span>
            )}
            {catalogError && (
              <span className="text-codify-warning/90" title={catalogError}>
                — last check failed
              </span>
            )}
            <button
              type="button"
              onClick={() => loadCatalog(true)}
              disabled={catalogLoading}
              title="Ask every configured provider what it serves right now"
              className="flex items-center gap-1 text-codify-muted hover:text-codify-secondary disabled:opacity-50 cursor-pointer"
            >
              <RefreshCw className={catalogLoading ? "w-3 h-3 animate-spin" : "w-3 h-3"} />
              Refresh
            </button>
          </span>
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-1.5 bg-codify-raised hover:bg-codify-border text-codify-secondary rounded-lg text-xs font-semibold transition-colors"
          >
            Done
          </button>
        </div>
      </div>
    </div>
  );
};
