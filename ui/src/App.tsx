import React, {
  useState,
  useEffect,
  useCallback,
  useMemo,
  useRef,
  useSyncExternalStore,
} from "react";
import {
  Workspace,
  ChatMessage,
  Conversation,
  EngineInfo,
  Event,
  Goal,
  GoalMode,
  ModelCatalog,
  ModelOption,
  AgentConfig,
  RecentRunModel,
  SettingsTab,
  ShellTabRow,
} from "./types";
import { buildModelSignals } from "./modelSignals";
import { openEngineStream } from "./engineStream";
import {
  listWorkspaces,
  fetchAgentConfigs,
  fetchRecentRunModels,
  createWorkspace,
  setWorkspaceDesignContract,
  createTurn,
  getGoal,
  listGoals,
  getGoalEvents,
  startGoal,
  pauseGoal,
  cancelGoal,
  setGoalTrace,
  deleteGoal,
  deleteWorkspace,
  retryStep,
  patchStep,
  getEngineInfo,
  setEngineInfo,
  resyncEngineInfoFromStorage,
  tauriInvoke,
  browseWorkspace,
  fetchModelCatalog,
  checkEngineHealth,
  refreshEngineInfoFromIpc,
  engineFailureReason,
  fetchEngineStderr,
  enableExecution,
  applyGoal,
  fetchConversations,
  createConversation,
  fetchConversationTurns,
  renameConversation,
  archiveConversation,
  attachGoalToConversation,
  closeBrowserWebview,
  closeTerminal,
  navigateBrowserWebview,
  openBrowserWebview,
  openBrowserDevtools,
  closeBrowserDevtools,
  browserDevtoolsAvailable,
  focusBrowserWebview,
  resizeBrowserWebviews,
  type BrowserBounds,
  openTerminal,
  resizeTerminal,
  listShellTabs,
  upsertShellTab,
  deleteShellTab,
  listWorkspaceFiles,
  nextSurfaceRequest,
  answerSurface,
} from "./api";
import {
  runGoalAction,
  canStopGoal,
  isGoalActive,
  isMessageBusy,
} from "./goalActions";
import {
  mergeThreadMessages,
  shouldHydrateThread,
  turnMessageIds,
  turnMessages,
  turnsNeedingHydration,
} from "./threadHydration";
import { Badge } from "./components/ui/Badge";
import { IconButton } from "./components/ui/IconButton";
import { Logo } from "./components/ui/Logo";
import { Toggle } from "./components/ui/Toggle";
import { statusTone } from "./statusTone";
import { BottomCommandBar, ExecutionMode } from "./components/BottomCommandBar";
import { StaleAuthBanner } from "./components/StaleAuthBanner";
import { Sidebar } from "./components/Sidebar";
import { TabBar } from "./components/TabBar";
import { NewTabButton } from "./components/NewTabButton";
import {
  acknowledge,
  activeKeyOf,
  ensureKeys,
  layoutFrom,
  layoutStorage,
  markPending,
  planChanges,
  queueRemoval,
  readLayoutMirror,
  reconcile,
  writeLayoutMirror,
  type Mirror,
} from "./layoutSync";
import { restoreTabs } from "./tabPersistence";
import {
  activeTab,
  closeTab,
  emptyTabs,
  focusTab,
  markTerminalExited,
  openBrowserTab,
  openConversation,
  openBlankTab,
  openEditorTab,
  openTab,
  openTerminalTab,
  renameBrowserTab,
  renameTab,
  setBrowserHistory,
  setBrowserPageUrl,
  setBrowserUrl,
  tabForConversation,
  tabForFile,
  closeConversation,
  tabId,
  type Tab,
  type TabState,
} from "./tabs";
import { PAIR_REFUSALS, closeInSplit, splitPartner, startSplit, type PaneSide } from "./panes";
import { useSplit } from "./useSplit";
import { useSplitFits } from "./useSplitFits";
import { readSplitRatio, writeSplitRatio } from "./splitPref";
import { SplitPanes } from "./components/SplitPanes";
import { EditorPane } from "./components/EditorPane";
import { editorBuffers } from "./editorStore";
import { createEditorSurface, type EditorHost } from "./editorSurface";
import { abortableSleep, createSurfaceRegistry, runSurfaceLoop } from "./surfaceLoop";
import { TabMenu } from "./components/TabMenu";
import { tabMenuItems, type TabMenuItemId } from "./tabMenu";
import { threadLabel } from "./threadTitle";
import {
  goBack,
  goForward,
  pageNavigation,
  visit,
  hostOf,
  type BrowserHistory,
  type InFlightNavigations,
} from "./browserHistory";
import { BrowserPane } from "./components/BrowserPane";
import { classifyBrowserAddress } from "./browserDispatch";
import { TerminalPane } from "./components/TerminalPane";
import { DEFAULT_GRID, type Grid } from "./terminalModel";
import { StatsPanel } from "./components/StatsPanel";
import { ChatTimeline } from "./components/ChatTimeline";
import { looksLikeAudit } from "./components/AuditReport";
import { SettingsModal } from "./components/SettingsModal";
import { CommandPalette } from "./components/CommandPalette";
import { buildPaletteItems, type PaletteItem } from "./commandPalette";
import { resolveShortcut } from "./shortcuts";
import { currentUiScale, DEFAULT_UI_SCALE, stepUiScale, writeUiScale } from "./uiScale";
import { readSidebarOpen, writeSidebarOpen } from "./sidebarPref";
import { closeDrawer, nextDrawer, type Drawer } from "./drawers";
import { useSidebarYield } from "./useSidebarYield";
import { useEngineNotices, useNotifications } from "./useNotifications";
import { catalogNotification, goalNotification, pausedNotification, planNotification, type AppNotification } from "./notifications";
import { NotificationsDrawer } from "./components/NotificationsDrawer";
import { ClipboardDrawer } from "./components/ClipboardDrawer";
import { useClipboardHistory } from "./useClipboardHistory";
import { ClipboardRecorderContext } from "./clipboardContext";
import { pasteIntoTerminal, PASTE_MESSAGES } from "./terminalPaste";
import { copySchemeText } from "./scheme";
import type { Clip } from "./clipboardHistory";
import {
  BROWSER_PAGE_LOADED,
  BROWSER_PAGE_LOADING,
  BROWSER_PAGE_TITLED,
  BROWSER_POPUP_REQUESTED,
  listenShellEvent,
  readBrowserPopupRequested,
  readTerminalExit,
  readTerminalOutput,
  TERMINAL_EXIT,
  TERMINAL_OUTPUT,
} from "./shellEvents";
import {
  applyShellStarvation,
  clearShellStarvation,
  readMotion,
  rearmVerdictForBoot,
  writeSetting,
  MOTION_EVENT,
  type MotionState,
} from "./motionPreference";
import {
  LOADING_TIMEOUT_MS,
  pageTitlePayload,
  readBrowserPageState,
} from "./browserPageState";
import { openGoalStream, GoalStreamHandle } from "./goalStream";
import {
  ownsTerminal,
  recordExit,
  recordOutput,
  retireTerminal,
} from "./terminalBuffer";
import { readRejection } from "./rejection.ts";
import { threadTitleFromPrompt } from "./threadTitle";
import {
  AlertCircle,
  History,
  BarChart3,
  X,
  RefreshCw,
  ScrollText,
  PanelLeftClose,
  PanelLeftOpen,
  Bell,
  ClipboardList,
} from "lucide-react";
import { notableStderrLines } from "./engineLog";
import { RainBackdrop } from "./components/ui/RainBackdrop";
import { WeatherBackdrop } from "./components/ui/WeatherBackdrop";
import {
  ENGINE_STATE_CLASSES,
  ENGINE_STATE_COPY,
  engineState,
} from "./statusTone";

/**
 * How much of the engine's stderr to ask the shell for when it stops answering.
 *
 * More than the panel shows on purpose: the filter in `engineLog.ts` has to see
 * the noise to find the line worth keeping, and the shell's buffer is 200 lines
 * deep, so this costs nothing but a slightly larger IPC payload.
 */
const ENGINE_STDERR_TAIL = 60;

export const App: React.FC = () => {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selectedWs, setSelectedWs] = useState<Workspace | undefined>();
  // Model catalog: discovered from every configured provider by the engine.
  // Nothing here is hardcoded — an empty list means nothing is configured yet.
  /**
   * Bumped whenever the engine reports it has a current catalogue. The settings
   * panel owns its own fetch, so this is how it learns there is something to
   * re-read — a counter rather than the catalogue itself, because handing the
   * panel a second copy of the list is how two screens end up disagreeing about
   * what a provider serves.
   */
  const [catalogTick, setCatalogTick] = useState(0);
  /**
   * Is the engine pushing? While it is, nothing in this app owns a timer: the
   * watcher sweeps on the catalogue's own cache period and one sweep serves every
   * client. While it is *not*, each screen that shows a model has to ask for
   * itself — so the settings panel falls back to its own refresh, and the cost of
   * a dead socket becomes one extra discovery per open panel rather than a stale
   * list.
   */
  const [catalogLive, setCatalogLive] = useState(false);
  const [modelCatalog, setModelCatalog] = useState<ModelCatalog>({
    models: [],
    providers: [],
    fetched_at: 0,
    cached: false,
  });
  const [selectedModel, setSelectedModel] = useState<ModelOption | undefined>();
  const [modelsLoading, setModelsLoading] = useState(false);
  // Signals the model menu orders by. Both are things the engine already records:
  // which model each role is configured with, and which models actually answered.
  const [agentConfigs, setAgentConfigs] = useState<AgentConfig[]>([]);
  const [recentRuns, setRecentRuns] = useState<RecentRunModel[]>([]);
  const [mode, setMode] = useState<ExecutionMode>("direct");
  // The stream callbacks below outlive a render, so what they ask about the composer's mode is read here.
  const modeRef = useRef(mode);
  modeRef.current = mode;
  // What the goal is for, orthogonal to how it executes: a design deliverable
  // drafts or revises the workspace's own brand contract.
  const [goalMode, setGoalMode] = useState<GoalMode>("normal");
  // Opt-in parallelism: independent (path-disjoint) steps of a goal run concurrently.
  const [parallel, setParallel] = useState(false);
  // Opt-in recording: keep every model call this next goal makes so the run can
  // be replayed without a provider. Off by default and disarmed once sent — a
  // recording is a copy of the model's output about the user's code, and a
  // toggle that stayed on would quietly record every prompt afterwards.
  const [record, setRecord] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  // The shell: what is open, and which one is showing. A pure module holds the
  // arithmetic (`ui/src/tabs.ts`) because "where do I land when a tab closes"
  // is the kind of thing that is easy to get subtly wrong and impossible to see
  // in markup.
  //
  // Restored from the shell's own storage, synchronously, before the first
  // paint: the pages are native webviews of *this* process, so waiting for the
  // engine would only delay a strip that does not need it. Ids are minted
  // fresh on the way in (`tabPersistence.ts` says why) and the tab order and
  // the focused tab are the ones that were left. A store that is empty,
  // unreadable or has never held a layout all restore to `emptyTabs`, which
  // is exactly what a first run is.
  //
  // The mirror first, with no round trip: the pages are native webviews of
  // *this* process, so a window that waits for the engine to draw its tabs is a
  // window that shows nothing when the engine is down — even though the pages
  // would have painted. The engine's copy of the strip is folded in underneath
  // this one by the effect below, which is also where a tab that only the
  // engine knows about arrives.
  const [tabState, setTabState] = useState<TabState>(() =>
    ensureKeys(
      restoreTabs(
        emptyTabs,
        readLayoutMirror(layoutStorage()).layout,
      ),
    ).state,
  );
  // The mirror, and what the engine has and has not been told. A ref, not state:
  // it is bookkeeping for the sync effect, and putting it in state would mean a
  // strip write re-rendering the window that made it.
  const layoutMirror = useRef<Mirror>(readLayoutMirror(layoutStorage()));
  // Keys the engine has confirmed it holds, so an unchanged strip costs nothing
  // on a poll. Seeded from the mirror's pending writes: a tab this window pushed
  // but never had acknowledged is still "not known" until the engine says so.
  const engineKnown = useRef<Set<string>>(new Set());
  // What the engine last said each row holds, so a tab that navigated after its
  // first push is seen as changed and sent again (`planChanges`).
  const engineRows = useRef<readonly ShellTabRow[]>([]);
  // Terminal tabs whose shell spoke while no pane was displaying: the tab
  // strip badges them, the way a mail client badges a background folder. The
  // facts arrive one chunk at a time from the recorder below — `recordOutput`
  // returns whether it *kept* the chunk, which is exactly "nobody was
  // displaying this terminal" — and they are cleared the moment the user
  // shows the tab. Kept in App, not in the store: which tabs the user has
  // looked at is a view concern, and the store's answer (the backlog) is
  // already the durable half.
  const [unreadTerminalIds, setUnreadTerminalIds] = useState<Set<string>>(
    () => new Set()
  );

  // ── the terminal recorder: what no mounted pane is there to catch ──────
  //
  // A pane subscribes only while it is mounted, and it is mounted only while
  // its tab is active — so a background terminal's output used to be emitted
  // to nobody and dropped, gone from the live pane and from the workspace
  // scrollback alike. This subscription lives at the app, for as long as the
  // app does, and the store (`terminalBuffer.ts`) decides per chunk who
  // records it: an unowned terminal's chunk is held as backlog, an owned
  // terminal's chunk is left to its pane, which displays and files it.
  //
  // Read through a ref rather than state so the subscription is keyed to the
  // mount alone — the same discipline `TerminalPane` uses. `setTabState` in
  // the updater would otherwise be a dependency that re-subscribes every time
  // a tab moves, and the point of this listener is that it never does.
  const tabStateRef = useRef(tabState);
  useEffect(() => {
    tabStateRef.current = tabState;
  }, [tabState]);
  // A tab on screen has been seen. A shell's first prompt is nearly always
  // printed before its tab exists (the PTY starts talking as `open` returns), so
  // the recorder keeps that chunk for a tab that is not active yet, badges it,
  // and the tab then opens active with the pane replaying the very bytes the
  // badge announces. Nothing else clears a badge but focusing another tab, so a
  // fresh terminal sat on a dot that said "unread" about the screen in front of
  // the user.
  useEffect(() => {
    const active = tabState.activeId;
    if (!active) return;
    setUnreadTerminalIds((prev) => {
      if (!prev.has(active)) return prev;
      const next = new Set(prev);
      next.delete(active);
      return next;
    });
  }, [tabState.activeId, unreadTerminalIds]);
  useEffect(() => {
    let disposed = false;
    const offs: Array<() => void> = [];
    const track = (pending: Promise<() => void>): void => {
      void pending.then((off) => {
        if (disposed) off();
        else offs.push(off);
      });
    };    track(
      listenShellEvent<unknown>(TERMINAL_OUTPUT, (payload) => {
        const chunk = readTerminalOutput(payload);
        if (!chunk) return;
        const kept = recordOutput(chunk.id, chunk.data);
        if (!kept) return;
        // The recorder kept the chunk, so no pane is displaying this terminal
        // — but the *active tab* check still matters: in the window between a
        // pane mounting and its claim landing, chunks are kept by the store
        // even though the user is looking at the tab. Badging the tab the
        // user is reading would make the badge a lie about attention, which
        // is the one thing a badge must not be.
        const active = tabStateRef.current.activeId;
        if (active === chunk.id) return;
        setUnreadTerminalIds((prev) => {
          if (prev.has(chunk.id)) return prev;
          const next = new Set(prev);
          next.add(chunk.id);
          return next;
        });
      })
    );
    track(
      listenShellEvent<unknown>(TERMINAL_EXIT, (payload) => {
        const ptyId = readTerminalExit(payload);
        if (!ptyId) return;
        recordExit(ptyId);
        // An unowned shell finished unseen: its record is complete as of
        // now, so file it while the store still holds it. An owned one's
        // pane does this itself — that bargain lives in the store's docs.
        if (!ownsTerminal(ptyId)) {
          const ws = tabStateRef.current.tabs.find(
            (t) => t.id === ptyId && t.kind === "terminal"
          )?.workspaceId;
          retireTerminal(ptyId, ws);
        }
        // The badge outlives the shell: a background exit is precisely the
        // moment the user most needs pointing at the tab — the build finished
        // (or failed) while they were elsewhere, and the output is still
        // unseen. What *is* seen is an exit on a pane the user is watching,
        // which is the owned case: clear it there.
        if (ownsTerminal(ptyId)) {
          setUnreadTerminalIds((prev) => {
            if (!prev.has(ptyId)) return prev;
            const next = new Set(prev);
            next.delete(ptyId);
            return next;
          });
        }
      })
    );
    return () => {
      disposed = true;
      for (const off of offs) off();
    };
  }, []);
  // Threads are cached by workspace so switching projects changes only the
  // side-panel list, never the global set of open tabs. A late response for one
  // project cannot replace the list belonging to another.
  const [conversationsByWorkspace, setConversationsByWorkspace] = useState<
    Record<string, Conversation[]>
  >({});
  const conversations = selectedWs
    ? conversationsByWorkspace[selectedWs.id] ?? []
    : [];
  const [conversationsLoadingByWorkspace, setConversationsLoadingByWorkspace] =
    useState<Record<string, boolean>>({});
  const conversationsLoading = selectedWs
    ? Boolean(conversationsLoadingByWorkspace[selectedWs.id])
    : false;
  const conversationRequestsByWorkspace = useRef<Record<string, number>>({});
  const conversationRevisionsByWorkspace = useRef<Record<string, number>>({});
  const selectedWorkspaceIdRef = useRef<string | undefined>(selectedWs?.id);
  useEffect(() => {
    selectedWorkspaceIdRef.current = selectedWs?.id;
  }, [selectedWs?.id]);
  const updateWorkspaceConversations = useCallback(
    (
      workspaceId: string,
      update: (current: Conversation[]) => Conversation[],
    ) => {
      conversationRevisionsByWorkspace.current[workspaceId] =
        (conversationRevisionsByWorkspace.current[workspaceId] ?? 0) + 1;
      setConversationsByWorkspace((previous) => ({
        ...previous,
        [workspaceId]: update(previous[workspaceId] ?? []),
      }));
    },
    [],
  );
  // Threads belong to the engine and are listed by their project, so they
  // outlive the window that opened them.
  const [isLoading, setIsLoading] = useState(false);
  // The goal currently in flight, so the command bar can offer to stop it. This is
  // deliberately not `isLoading`: that flag is set while a goal is being *dispatched*
  // and cleared in the same `finally` block, so it describes a network round trip and
  // not a run. Nothing here was tracking a live goal at all, which is why the only
  // control in reach was a Send button that was idle almost all the time.
  const [activeGoalId, setActiveGoalId] = useState<string | null>(null);
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  // Which settings tab to land on. A failure diagnosis sends the user straight
  // to the screen that holds the fix instead of making them find it.
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("keys");
  const openSettings = (tab: SettingsTab = "keys") => {
    setSettingsTab(tab);
    setIsSettingsOpen(true);
  };
  const [error, setError] = useState<string | null>(null);
  // What happened while the person was looking elsewhere: goals this window watched ending, plans waiting
  // for approval, engine connection changes and model list changes. Client-side and in-app only
  // (`notifications.ts` says what is in it and what is not).
  const inbox = useNotifications();
  const notify = inbox.notify;
  // What was copied, cut or pasted in this window, kept in this window (`clipboardHistory.ts`, `docs/09` §11).
  const clipboard = useClipboardHistory();
  const recordClip = clipboard.record;
  const [engineUp, setEngineUp] = useState<boolean | null>(null); // null = checking
  // The motion store, as React state so a change re-renders the tree and every
  // backdrop's effect re-reads it on the way past (the loops read the store at
  // effect time, not per frame — a decision that belongs to the mount, not the
  // clock). The shell's starvation verdict arrives through this: the one party
  // that pays for a software-rasterised backdrop is the process that announces
  // it, and a frozen window cannot reach a settings pane to ask for relief.
  const [motion, setMotion] = useState<MotionState>(() => {
    rearmVerdictForBoot();
    return readMotion();
  });
  useEffect(() => {
    let off: (() => void) | undefined;
    let cancelled = false;
    void listenShellEvent<number>(MOTION_EVENT, (measuredMs) => {
      if (cancelled) return;
      setMotion(applyShellStarvation({ at: Date.now(), measuredMs }));
    }).then((unlisten) => {
      if (cancelled) unlisten();
      else off = unlisten;
    });
    return () => {
      cancelled = true;
      off?.();
    };
  }, []);
  /**
   * What the engine said on its way out, read from the shell that tailed its
   * stderr. Shown only while the engine is down, and fetched once per outage:
   * the interesting moment is the engine stopping, and by the time the 3s health
   * probe notices, the tail already holds everything it said. Empty again as
   * soon as the engine answers, so a resolved outage leaves nothing behind.
   */
  const [engineStderr, setEngineStderr] = useState<string[]>([]);
  const [authOk, setAuthOk] = useState<boolean | null>(null);
  /** The connection, as one word: the pill's label, hint and colours all key off it. */
  const engineConnection = engineState(engineUp, authOk);
  // The outage's own words, for the notice: the last line the engine said on its way out. A ref, because
  // the notice fires on a timer and must read what is known *then*, not when the effect was set up.
  const engineStderrRef = useRef<string[]>([]);
  engineStderrRef.current = engineStderr;
  useEngineNotices(engineConnection, notify, () => engineStderrRef.current[engineStderrRef.current.length - 1]);

  // Live goal streams (one per goal, with reconnect backoff).
  const goalStreams = useRef<Record<string, GoalStreamHandle>>({});
  // Set below; refs let the connection effects call the latest versions
  // without re-subscribing on every render.
  const loadWorkspacesRef = useRef<(() => void) | null>(null);
  const loadModelsRef = useRef<((refresh?: boolean) => void) | null>(null);
  const loadModelSignalsRef = useRef<(() => void) | null>(null);
  // The health probe's latest body, for the auth-stale banner's retry button.
  const forceHealthProbeRef = useRef<(() => void) | null>(null);

  // Live engine liveness probe — replaces the old hardcoded green pill.
  // Polls /health every 3s; backs off while the tab is hidden.
  //
  // Self-healing: when the engine answers but rejects our token (auth-stale —
  // usually the engine restarted and rotated its boot token), re-fetch engine
  // info from Tauri IPC, which tracks the live process's handshake. If that
  // yields different connection info, re-probe immediately instead of waiting
  // for the next tick and warning the user about something we can fix.
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const probe = async () => {
      if (cancelled) return;
      // **Ask the shell before believing the address we cached.** `api.ts`
      // seeds `currentEngine` from `localStorage`, and that is a fact about a
      // *previous* process — an address that can name a live engine belonging
      // to somebody else: another build, another `CODIFY_HOME`, or a test run
      // whose window shares this WebKit profile (a dev server is one origin,
      // so `localStorage` is one origin's). A health check cannot tell "our
      // engine" from "an engine": the cached token is a real one and the other
      // engine answers as authenticated. So the shell — the only party that
      // watched *this* window's handshake — is asked first, and its answer is
      // applied before anything reads or writes the strip. The cost of not
      // asking is measured, and it landed in the developer's real strip: a
      // `make smoke-tabs` run wrote its own tab into whichever engine the cache
      // named, once per run, 129 rows of `example.com/tabs-smoke` deep.
      //
      // Outside the shell there is nothing to ask and the cached pair is the
      // only address there is; `refreshEngineInfoFromIpc` returns null there
      // (see its doc), so the standalone browser build is unchanged.
      const fromShell = await refreshEngineInfoFromIpc();
      if (cancelled) return;
      if (fromShell) {
        // A different engine is a different set of configured providers, so
        // the two loads the recovery path below already makes are made here
        // too — once, because this only answers when the pair *changed*.
        loadWorkspacesRef.current?.();
        loadModelsRef.current?.(true);
      }
      let health = await checkEngineHealth();
      if (cancelled) return;
      // Ask the shell for fresh connection info on **either** half of a stale
      // connection, not just the authenticated one. `ok && !authenticated` is
      // the case this recovery was built for — some engine *is* there, it just
      // does not know us. But `!ok` has a second reading the old guard
      // silently dropped: nothing answered at the port we hold, and the reason
      // may be that the engine is alive somewhere else. The shell tracks the
      // live process's handshake, so it is the one party that can say; a
      // changed answer is applied and the probe re-runs immediately. Without
      // this, an engine that moved ports after a restart (or a desktop app
      // whose localStorage names a port another session chose) leaves the
      // window red until a restart of the whole app — `!ok` was read as "the
      // engine is down", and "down somewhere else" is a different fact.
      if ((health.ok && !health.authenticated) || !health.ok) {
        const fresh = await refreshEngineInfoFromIpc();
        if (cancelled) return;
        if (fresh) {
          // `refreshEngineInfoFromIpc` has already applied the new port and token
          // to the API client; nothing else kept a copy of them to update.
          loadWorkspacesRef.current?.();
          // A fresh token may point at a different engine (and therefore a
          // different set of configured providers) — re-discover its models.
          loadModelsRef.current?.(true);
          // Connection info changed — re-probe right away with the new token.
          health = await checkEngineHealth();
          if (cancelled) return;
        }
      }
      setEngineUp(health.ok);
      setAuthOk(health.authenticated);
      const next = document.hidden ? 15000 : 3000;
      timer = setTimeout(probe, next);
    };
    // The auth-stale banner's "Retry now" runs the same probe — one code path
    // for the automatic check and the user's, so a retry cannot disagree with
    // what the poll would have concluded.
    forceHealthProbeRef.current = probe;
    probe();
    return () => {
      cancelled = true;
      forceHealthProbeRef.current = null;
      if (timer) clearTimeout(timer);
    };
  }, []);

  // Close every stream when the app unmounts — otherwise sockets leak forever.
  useEffect(() => {
    const streams = goalStreams;
    return () => {
      Object.values(streams.current).forEach((s) => s.close());
      streams.current = {};
    };
  }, []);

  // Fetch real engine info from Tauri IPC on mount with retry. The engine can
  // come up after the UI (desktop shell spawn order), so workspaces *and* the
  // model catalog are re-read here rather than only on mount.
  useEffect(() => {
    let attempts = 0;
    const fetchInfo = () => {
      tauriInvoke<EngineInfo>("codify_get_engine_info")
        .then((info) => {
          setEngineInfo(info);
          // Engine just became reachable — re-read workspaces now that the
          // real token/port are set (clears the startup failure banner).
          loadWorkspacesRef.current?.();
          loadModelsRef.current?.(true);
        })
        .catch(async () => {
          attempts++;
          if (attempts < 10) {
            setTimeout(fetchInfo, 500);
            return;
          }
          // Out of retries. The pill can only say "offline", but the shell knows
          // *why* — started outside the checkout, no `python3`, or a process that
          // died before its handshake — so ask it once and let the banner carry the
          // reason. This is the difference between a permanent red dot and a fix.
          const reason = await engineFailureReason();
          if (reason) setError(reason);
        });
    };
    fetchInfo();
  }, []);

  // The engine's own words, the moment it stops answering. `engineFailureReason`
  // can only report what the *launcher* knows (no checkout, no handshake, a
  // port it never reported), which says nothing about an engine that ran fine
  // for an hour and then went: a provider that refused, a traceback, or the
  // bounded-shutdown backstop giving up on a hung websocket. The shell has been
  // reading the engine's stderr the whole time; this is the one moment the
  // window has use for it.
  useEffect(() => {
    if (engineUp !== false) {
      // Not an outage, or an outage already explained and resolved. Either way
      // the previous run's last words are not this one's.
      setEngineStderr([]);
      return;
    }
    let cancelled = false;
    fetchEngineStderr(ENGINE_STDERR_TAIL)
      .then((lines) => {
        if (cancelled) return;
        // The filter is where "worth reading" is decided, and it is the reason
        // the backstop's line is visible rather than the four hundredth line of
        // a session's chatter. See `engineLog.ts`.
        setEngineStderr(notableStderrLines(lines));
      })
      .catch(() => {
        // No tail to show is not a second failure to report: the red pill and
        // the launcher's own reason are already on screen.
      });
    return () => {
      cancelled = true;
    };
  }, [engineUp]);

  // Load workspaces; never fabricate a hardcoded one — if the engine has no
  // workspaces yet, the user picks a folder via the command bar first.
  const loadWorkspaces = useCallback(async () => {
    try {
      const wsList = await listWorkspaces();
      setWorkspaces(wsList);
      setSelectedWs((prev) => {
        if (wsList.length === 0) return undefined;
        // The selected workspace may have been deleted or the list re-read
        // after an engine restart — re-validate against the fresh list.
        if (prev && wsList.some((w) => w.id === prev.id)) return prev;
        return wsList[0];
      });
      setError(null);
    } catch (err: any) {
      // Backend might still be starting — App also retries engine info above.
      setError(
        `Cannot reach the Codify engine at 127.0.0.1:${getEngineInfo().port}. ` +
          "Start it with `make run-engine` (or relaunch the desktop app).",
      );
    }
  }, []);

  // Discover the models every configured provider serves. Called with
  // refresh=true whenever the app opens or the settings screen changes
  // something, so newly released models appear without a restart.
  const loadModels = useCallback(async (refresh = false) => {
    setModelsLoading(true);
    try {
      const catalog = await fetchModelCatalog(refresh);
      setModelCatalog(catalog);
      setSelectedModel((prev) => {
        // Keep the user's pick while it still exists. Ids repeat across
        // providers (e.g. a model served both locally and via an API), so the
        // pair is what identifies a choice.
        if (
          prev &&
          catalog.models.some(
            (m) => m.id === prev.id && m.provider === prev.provider,
          )
        ) {
          return prev;
        }
        // Otherwise prefer a local model: no tokens, no latency, no surprise.
        return (
          catalog.models.find((m) => m.protocol === "ollama") ??
          catalog.models[0]
        );
      });
    } catch (err: any) {
      // Discovery failure must not wedge the picker: keep the last catalog
      // and say why the list may be stale.
      setError(readRejection(err, "Failed to discover models from the engine."));
    } finally {
      setModelsLoading(false);
    }
  }, []);

  useEffect(() => {
    loadWorkspacesRef.current = loadWorkspaces;
    loadModelsRef.current = loadModels;
  }, [loadWorkspaces, loadModels]);

  // Roles and recent runs, for the model menu's ordering. Failures are silent on
  // purpose: a hint about ordering must never be the reason the app shows an
  // error, and the menu works without it (provider order, as before).
  const loadModelSignals = useCallback(async () => {
    const [configs, runs] = await Promise.all([
      fetchAgentConfigs().catch(() => [] as AgentConfig[]),
      fetchRecentRunModels(5).catch(() => [] as RecentRunModel[]),
    ]);
    setAgentConfigs(configs);
    setRecentRuns(runs);
  }, []);

  // Derived, not stored: the same configs and runs already drive the settings
  // screen, so a second copy would only be a way for the two pickers to disagree.
  const modelSignals = useMemo(
    () => buildModelSignals(agentConfigs, recentRuns),
    [agentConfigs, recentRuns],
  );

  // A goal's stream closes on its own (see `onTerminal`), so the refresh there
  // reaches this through a ref rather than re-subscribing every goal to a
  // changing callback.
  useEffect(() => {
    loadModelSignalsRef.current = loadModelSignals;
  }, [loadModelSignals]);

  useEffect(() => {
    loadWorkspacesRef.current?.();
    // refresh=true on open: "what does each provider serve right now" is not a
    // question worth caching across app launches.
    loadModels(true);
    loadModelSignals();
  }, [loadModels, loadModelSignals]);

  /**
   * The engine tells us when a provider's model list moves, so every screen with
   * a model in it updates the moment a release lands — the settings provider
   * rows, the role cards, the command bar — instead of each one owning a timer
   * that happens to fire.
   *
   * The frame carries the *diff*, not the catalogue, so this re-reads `GET /models`
   * with `refresh: false`. That is a cache hit by construction: the engine's
   * watcher is what discovered the change, and it only announces after the answer
   * is in the cache. Asking for a refresh here would ask all eight providers
   * again for something we were just told.
   */
  useEffect(() => {
    const everConnected = { current: false };
    const handle = openEngineStream({
      onFrame: (frame) => {
        // The frame carries the diff, and until the notifications existed it was thrown away. Counted
        // here, per provider, so the one place that says a model went *away* is the inbox: the "new"
        // badge on a provider row never shows removals.
        if (frame.type === "model_catalog_changed") notify(catalogNotification(frame.payload, Date.now()));
        setCatalogTick((n) => n + 1);
        void loadModels(false);
      },
      onConnected: () => {
        setCatalogLive(true);
        if (everConnected.current) {
          // A change that landed while the socket was down was never announced to
          // anybody: the engine only reports to current subscribers, and there is
          // no replay. So a reconnect re-reads, or the one release nobody hears
          // about is the one that arrived while you were not looking.
          setCatalogTick((n) => n + 1);
          void loadModels(false);
        }
        everConnected.current = true;
      },
      // Backoff starts the moment the socket drops, which is exactly when a screen
      // showing a model list needs to start asking for itself again.
      onReconnecting: () => setCatalogLive(false),
    });
    return () => {
      handle.close();
      setCatalogLive(false);
    };
  }, [loadModels, notify]);

  // Whether the "enter a folder path" dialog is open. Owned here rather than by the command bar so a
  // picker that cannot open can open it: on a desktop with no dialog helper the folder button never
  // works, and a banner that says "type the path instead" is a dead end unless the form is already there.
  const [manualWorkspaceOpen, setManualWorkspaceOpen] = useState(false);

  // Native OS File Manager browser handler (opens Nautilus / portal)
  const handleBrowseWorkspace = async () => {
    try {
      const ws = await browseWorkspace();
      if (ws) {
        setWorkspaces((prev) => {
          const exists = prev.some((w) => w.id === ws.id);
          return exists ? prev : [...prev, ws];
        });
        setSelectedWs(ws);
      }
    } catch (err: any) {
      setError(readRejection(err, "Failed to open the folder browser."));
      if (err?.code === "picker_unavailable") setManualWorkspaceOpen(true);
    }
  };

  const handleCreateWorkspace = async (name: string, root_path: string) => {
    setError(null);
    try {
      const ws = await createWorkspace(name, root_path);
      setWorkspaces((prev) => [...prev, ws]);
      setSelectedWs(ws);
    } catch (err: any) {
      setError(readRejection(err, "Failed to create workspace"));
      throw err;
    }
  };

  /**
   * Pin (or, with "", unpin) the workspace's brand contract.
   *
   * The selected workspace is held as its own copy, so both lists have to be
   * refreshed — otherwise the picker keeps showing the pin that was just cleared.
   * Errors are rethrown for the dialog to show: the engine's refusal names the
   * file it could not use, and that message is the whole point of validating here.
   */
  // Both the settings pin and a design goal's transcript card land here: the pin
  // is one engine call and one piece of state either way, and the engine's own
  // validation is what decides whether the file can govern.
  const handleSetDesignContract = async (workspaceId: string, path: string) => {
    const updated = await setWorkspaceDesignContract(workspaceId, path);
    setWorkspaces((prev) =>
      prev.map((w) => (w.id === updated.id ? updated : w)),
    );
    setSelectedWs((current) =>
      current?.id === updated.id ? updated : current,
    );
  };

  // Which brand contract each workspace currently obeys, for the deliverable
  // card. Derived from the list rather than tracked as its own state, so the two
  // cannot disagree: pinning from the card, from the workspace picker, or in
  // another tab all land in the same place, and a reloaded tab reads the pin
  // instead of forgetting it.
  const pinnedContracts = useMemo(() => {
    const byWorkspace: Record<string, string> = {};
    for (const ws of workspaces) {
      if (ws.design_contract_path) byWorkspace[ws.id] = ws.design_contract_path;
    }
    return byWorkspace;
  }, [workspaces]);

  // The thread the visible tab is showing, when it is a chat tab.
  // The active tab, resolved once. Two derived questions are asked of it below —
  // which thread is showing, and whether what is showing is a page — and the
  // second is asked on the *kind*, not on the presence of a `url`, so a terminal
  // tab can never be mistaken for a browser tab by sharing a field.
  const activeTabNow = activeTab(tabState);
  const activeTabKind = activeTabNow?.kind;
  // Whether the left panel is showing. View state, remembered across restarts (`sidebarPref.ts`), and
  // the panel is *unmounted* when hidden rather than collapsed: it keeps no state of its own worth
  // keeping (its right-click menu is transient), and it must leave the layout entirely so the centre
  // column, and a browser pane's native webview inside it, really get the room.
  const [sidebarOpen, setSidebarOpen] = useState(readSidebarOpen);
  useEffect(() => {
    writeSidebarOpen(sidebarOpen);
  }, [sidebarOpen]);
  // The right-hand drawer that is open, if any. One state, not a boolean each: they cannot both be open,
  // and "which one" is what the layout rule below needs to know (`drawers.ts`).
  const [drawer, setDrawer] = useState<Drawer | null>(null);
  const statsOpen = drawer === "stats";
  const historyOpen = drawer === "history";
  const notificationsOpen = drawer === "notifications";
  const clipboardOpen = drawer === "clipboard";
  // The left panel gives way while a drawer is open and the window cannot hold both. Derived, never
  // stored: `codify.sidebar` is written only by the person's own press of the toggle, so closing the
  // drawer brings the panel back as it was (`docs/09` §8.1).
  const mainRef = useRef<HTMLElement>(null);
  // A split puts two tabs side by side (`panes.ts`, `docs/09` §12). `shownSplit` is what it is showing right now.
  const { shown: shownSplit, split, setSplit } = useSplit(tabState);
  const sidebarYielded = useSidebarYield(mainRef, drawer, Boolean(shownSplit));
  const sidebarShown = sidebarOpen && !sidebarYielded;
  // Pressing the toggle while the panel is out of the way because of a drawer or a split means "show it", and the
  // two do not fit, so what took the room is what closes: the drawer first, then the split. Otherwise it flips the
  // person's own choice.
  const toggleSidebar = useCallback(() => {
    if (sidebarOpen && sidebarYielded) {
      if (drawer !== null) setDrawer(null);
      else setSplit(null);
      return;
    }
    setSidebarOpen((open) => !open);
  }, [sidebarOpen, sidebarYielded, drawer, setSplit]);
  // `drawnSplit` is what the split is showing when the centre column can hold both panes; when it cannot, only the
  // focused pane is drawn and the split is kept (`drawers.splitFits`), so widening the window brings the other back.
  const splitFitsRow = useSplitFits(mainRef, sidebarShown, drawer);
  const drawnSplit = shownSplit && splitFitsRow ? shownSplit : null;
  const [splitRatio, setSplitRatio] = useState(readSplitRatio);
  // The thread in view follows the *chat pane*, not the focus: with the terminal beside it focused, the transcript is
  // still the one on screen, and the sidebar still highlights it. Outside a split it is the active tab's, as before.
  const chatInView = shownSplit
    ? [shownSplit.left, shownSplit.right].find((t) => t.kind === "chat")
    : activeTabNow?.kind === "chat"
      ? activeTabNow
      : undefined;
  const activeConversationId = chatInView?.conversationId;
  const activeBrowserTab =
    activeTabNow?.kind === "browser" ? activeTabNow : undefined;
  const activeTerminalTab =
    activeTabNow?.kind === "terminal" ? activeTabNow : undefined;
  const activeEditorTab =
    activeTabNow?.kind === "editor" ? activeTabNow : undefined;
  // What the strip says about the editors: text nobody has saved, and text the assistant changed. Read from the store, which
  // lives above the panes, so a tab that is not on screen still says it.
  const bufferSnapshot = useSyncExternalStore(editorBuffers.subscribe, editorBuffers.getSnapshot);
  const unsavedEditorIds = useMemo(() => editorBuffers.unsavedIds(bufferSnapshot), [bufferSnapshot]);
  const assistantEditedIds = useMemo(() => editorBuffers.assistantEditedIds(bufferSnapshot), [bufferSnapshot]);
  // The terminal a paste would reach: the focused one, else the one beside the chat that has it.
  const terminalInView =
    activeTerminalTab ??
    (drawnSplit ? [drawnSplit.left, drawnSplit.right].find((t) => t.kind === "terminal") : undefined);

  // The clipboard drawer's buttons. Which of Insert and Paste can work follows from which tab is in view: the
  // message box exists only in a chat view (a browser or terminal tab replaces it), and a terminal only in a
  // terminal tab whose shell is still running.
  const canInsertClip = drawnSplit
    ? [drawnSplit.left, drawnSplit.right].some((t) => t.kind === "chat")
    : !activeBrowserTab && !activeTerminalTab && !activeEditorTab;
  const canPasteClip = Boolean(terminalInView) && !terminalInView?.exited;
  const [insertRequest, setInsertRequest] = useState<{ seq: number; text: string } | null>(null);
  const [clipNotice, setClipNotice] = useState<string | null>(null);
  useEffect(() => {
    // A message about a press belongs to the look at the drawer that was open when it was made.
    if (!clipboardOpen) setClipNotice(null);
  }, [clipboardOpen]);
  const handleCopyClip = useCallback(
    async (clip: Clip): Promise<void> => {
      const write =
        typeof navigator !== "undefined" && navigator.clipboard
          ? navigator.clipboard.writeText.bind(navigator.clipboard)
          : undefined;
      const outcome = await copySchemeText(clip.text, {
        writeText: write,
        document: typeof document !== "undefined" ? document : undefined,
      });
      if (outcome === "failed") {
        setClipNotice("Could not copy: the system clipboard refused it.");
        return;
      }
      setClipNotice(null);
      // `writeText` fires no `copy` event, so the history is told: the clip goes back to the top.
      recordClip(clip.text, clip.source);
    },
    [recordClip],
  );
  // No guard here for "there is no message box": a request made with none is never replayed, because a
  // message box ignores the one already pending when it mounts (`BottomCommandBar`), and the drawer
  // disables the button besides.
  const handleInsertClip = useCallback((clip: Clip) => {
    setClipNotice(null);
    setInsertRequest((prev) => ({ seq: (prev?.seq ?? 0) + 1, text: clip.text }));
  }, []);
  const handlePasteClip = useCallback(
    (clip: Clip) => {
      const outcome = terminalInView ? pasteIntoTerminal(terminalInView.id, clip.text) : "no-terminal";
      setClipNotice(outcome === "pasted" ? null : PASTE_MESSAGES[outcome]);
    },
    [terminalInView],
  );

  /**
   * The transcript for the visible thread.
   *
   * `messages` stays one flat list on purpose, and this is the whole of the
   * multi-conversation change on the UI side. Threading it by message id is what
   * keeps the live-run path correct: a goal streams into the thread it was asked
   * in even while the user is looking at another tab, and the store keyed by
   * "the conversation currently on screen" would have written a running goal's
   * events into whichever thread happened to be visible. A derived filter cannot
   * make that mistake.
   */
  const visibleMessages = useMemo(() => {
    if (activeConversationId) {
      return messages.filter((m) => m.conversationId === activeConversationId);
    }
    // Nothing open: only what has not been assigned a thread. A message from
    // another conversation must never leak into this column.
    return messages.filter((m) => m.conversationId == null);
  }, [messages, activeConversationId]);

  /**
   * The threads with a run still in flight.
   *
   * The tab strip's busy dot reads this, which is the only way a user can see a
   * run is live while looking at another tab. Read from the messages rather than
   * tracked separately, so a goal that ends is never reported as busy by a
   * counter nobody decremented — and derived from the goal's own status rather
   * than from the dispatch flag, which nothing clears when the engine finishes
   * (`isMessageBusy`). A transcript that pulses forever says the assistant never
   * stopped, which is the report this fixes.
   */
  const activeGoalIds = useMemo(() => {
    const ids = new Set<string>();
    for (const m of messages) {
      if (isMessageBusy(m) && m.conversationId) ids.add(m.conversationId);
    }
    return ids;
  }, [messages]);

  // ── the side panel ────────────────────────────────────────────────

  const loadConversations = useCallback(async () => {
    const workspaceId = selectedWs?.id;
    if (!workspaceId) return;

    const requestId =
      (conversationRequestsByWorkspace.current[workspaceId] ?? 0) + 1;
    conversationRequestsByWorkspace.current[workspaceId] = requestId;
    let revision = conversationRevisionsByWorkspace.current[workspaceId] ?? 0;
    setConversationsLoadingByWorkspace((previous) => ({
      ...previous,
      [workspaceId]: true,
    }));
    try {
      while (
        conversationRequestsByWorkspace.current[workspaceId] === requestId
      ) {
        let loaded: Conversation[];
        try {
          loaded = await fetchConversations(workspaceId);
        } catch (err) {
          if (
            conversationRequestsByWorkspace.current[workspaceId] !== requestId
          ) {
            return;
          }
          const currentRevision =
            conversationRevisionsByWorkspace.current[workspaceId] ?? 0;
          // A create, rename or archive may have completed while the request
          // was in flight. Retry so the selected project's list is complete.
          if (currentRevision !== revision) {
            revision = currentRevision;
            continue;
          }
          if (selectedWorkspaceIdRef.current === workspaceId) {
            setError(readRejection(err, "Failed to load conversations"));
          }
          return;
        }

        if (
          conversationRequestsByWorkspace.current[workspaceId] !== requestId
        ) {
          return;
        }
        const currentRevision =
          conversationRevisionsByWorkspace.current[workspaceId] ?? 0;
        if (currentRevision !== revision) {
          revision = currentRevision;
          continue;
        }
        // The engine query is already project-scoped; verify the response too
        // so a malformed or stale payload can never cross the panel boundary.
        setConversationsByWorkspace((previous) => ({
          ...previous,
          [workspaceId]: loaded.filter(
            (conversation) => conversation.workspace_id === workspaceId,
          ),
        }));
        return;
      }
    } finally {
      if (conversationRequestsByWorkspace.current[workspaceId] === requestId) {
        setConversationsLoadingByWorkspace((previous) => ({
          ...previous,
          [workspaceId]: false,
        }));
      }
    }
  }, [selectedWs?.id]);

  useEffect(() => {
    void loadConversations();
  }, [loadConversations]);

  /**
   * Open a clean slate in the selected project.
   *
   * ## No conversation is created, and that is the change
   *
   * This used to call the engine before it opened anything, so a tab *was* a
   * conversation from the moment it appeared \u2014 pressing the button to look
   * around left an empty conversation behind, named from a prompt nobody wrote,
   * and the side panel grew a row for it. A tab is now a project's window and
   * nothing else: the thread is created by the first prompt, in the send path,
   * which is the only place that has the prompt to name it with. So the button
   * is now synchronous, cannot fail, and leaves no trace if the tab is closed
   * again unused.
   *
   * It is also the *only* new-tab control. The side panel lost its New Thread
   * button, because that button created a thread and opened a tab for it \u2014
   * which is this control wearing a different name, in the wrong place, with
   * two steps.
   */
  const handleNewTab = useCallback(() => {
    const workspaceId = selectedWs?.id;
    if (!workspaceId) return;
    setTabState((prev) => openBlankTab(prev, workspaceId));
  }, [selectedWs?.id]);

  /**
   * Start a thread *on* the thread the gesture was made in, and show it.
   *
   * The menu's "New thread", and the reason it is a separate handler from
   * `handleNewTab` rather than a second call to it. A new thread with no
   * `parent_id` is a brand-new chat: a different object that happened to be
   * triggered by a button with the same name, and the reader has no way to tell
   * them apart until an unrelated empty tab is already open. `parentConversationId`
   * undefined — nothing is showing — is a top-level thread, which is the honest
   * answer rather than a failure: there was no chat to branch off.
   *
   * The thread is *created* here, unlike a blank tab, because a menu item that
   * opens a branch must be about something to branch from. A clean slate is not
   * about a thread at all, so it waits for the first prompt to make one.
   */
  const handleNewThread = useCallback(
    async (parentConversationId?: string) => {
      const workspaceId = selectedWs?.id;
      if (!workspaceId) return;
      try {
        const convo = await createConversation(
          workspaceId,
          "",
          parentConversationId,
        );
        updateWorkspaceConversations(convo.workspace_id, (previous) => [
          convo,
          ...previous.filter((existing) => existing.id !== convo.id),
        ]);
        setTabState((prev) =>
          openConversation(prev, convo.id, convo.title, convo.workspace_id),
        );
      } catch (err: any) {
        setError(readRejection(err, "Failed to start a thread"));
      }
    },
    [selectedWs?.id, updateWorkspaceConversations],
  );

  /**
   * Show a thread, in its project's tab.
   *
   * `openConversation` is where the whole rule lives: a thread already on screen
   * is focused, a thread in the project of the tab you are looking at replaces
   * what that tab was showing, and anything else gets a tab of its own. This
   * handler only has to name the thread \u2014 which is the side panel's entire job
   * now, and why the panel has no new-tab control left to get wrong.
   */
  const handleSelectConversation = useCallback(
    (conversationId: string) => {
      const convo = conversations.find((c) => c.id === conversationId);
      setTabState((prev) =>
        openConversation(
          prev,
          conversationId,
          convo?.title ?? "",
          convo?.workspace_id,
        ),
      );
    },
    [conversations],
  );

  // ── The keyboard layer ───────────────────────────────────────────────────
  // Ctrl+T new tab, +W close, +1..9 jump, +K palette. The mapping is pure
  // and tested in `shortcuts.ts`; this is only dispatch. Registered in the
  // *capture* phase on `window`, so a keystroke that belongs to the shell
  // wins over whatever the focused control would otherwise do with it — these
  // are global shortcuts, and capture is the first stop. Escape is
  // deliberately NOT handled here: the palette owns it while open and stops
  // its propagation, so one keystroke can never close two surfaces.

  const [paletteOpen, setPaletteOpen] = useState(false);

  // "Open file": the selected workspace's files, asked for when the palette opens and searched (never listed) once there
  // is a query. A refusal is an empty list and not an error: the palette is for jumping, and a request it made for
  // itself that failed is not the person's problem (an older engine has no such route, and says 404).
  const [paletteFiles, setPaletteFiles] = useState<readonly string[]>([]);
  useEffect(() => {
    if (!paletteOpen || !selectedWs || engineUp !== true) return;
    let cancelled = false;
    listWorkspaceFiles(selectedWs.id)
      .then((listed) => {
        if (!cancelled) setPaletteFiles(listed.files);
      })
      .catch(() => {
        if (!cancelled) setPaletteFiles([]);
      });
    return () => {
      cancelled = true;
    };
  }, [paletteOpen, selectedWs, engineUp]);

  const paletteItems = useMemo(
    () =>
      buildPaletteItems({
        tabs: tabState.tabs,
        activeId: tabState.activeId,
        conversations,
        split: { showing: Boolean(shownSplit) },
        files: paletteFiles,
      }),
    [tabState, conversations, shownSplit, paletteFiles],
  );

  // Every close in the shell goes through here — the strip's close button, the
  // Ctrl+W shortcut, and the browser pane — because a browser tab's page is a
  // child webview of this window that has to be told to go. Two seams would be
  // two ways to orphan a running page: close the tab one way and the webview
  // survives, still painting itself over whatever the user switched to.
  //
  // The kind is read from this render's tab list rather than from inside the
  // state updater: an updater must stay pure, and React runs it twice under
  // StrictMode — a shell call inside it would fire twice per click.
  //
  // `tab.url` is the test for "does this tab own a page". A browser tab that
  // was opened but never given an address has no webview, and `close` refuses
  // a tab that has none — so calling it would put the shell's "no browser tab
  // is open" on screen as an error the user caused by closing an empty tab.
  const closeTabNow = useCallback(
    (id: string) => {
      // Closing a showing pane's tab ends the split and goes to the other pane's tab (`panes.closeInSplit`).
      const inSplit = closeInSplit(tabState, split, id);
      setTabState((prev) => {
        const next = closeTab(prev, id);
        return inSplit?.activate ? focusTab(next, inSplit.activate) : next;
      });
      // (A split whose tab has gone ends by itself: `resolveSplit`. What closing adds is where to land.)
      const tab = tabState.tabs.find((t) => t.id === id);
      // A close is owed to the engine until it confirms the delete. Without this
      // the row outlives the tab and the next pull, or the next boot, adopts it
      // back. Written through at once: the sync effect stands down while the
      // engine is down, and an offline close is exactly the one that has to
      // survive a restart.
      if (tab) {
        layoutMirror.current = queueRemoval(layoutMirror.current, tab.key);
        writeLayoutMirror(layoutStorage(), layoutMirror.current);
      }
      // An editor's text is held by the store, not by its pane, so the pane going does not free it: the buffer goes
      // with the tab, here and nowhere else.
      if (tab?.kind === "editor") editorBuffers.close(id);
      if (tab?.kind === "terminal") {
        // A pane that is not mounted never claimed its terminal, so the store
        // still holds what the shell said while this tab sat in the
        // background — and nothing mounted will ever claim it now. File the
        // backlog into the workspace's scrollback before the PTY goes: the
        // guarantee is that what a terminal said is what the next pane in
        // this workspace restores, including one the user has since closed.
        // (An owned terminal's release happens in the pane's own cleanup.)
        if (!ownsTerminal(id)) {
          retireTerminal(id, tab.workspaceId);
        }
        // And the badge goes with the tab: there is nothing left to point at.
        setUnreadTerminalIds((prev) => {
          if (!prev.has(id)) return prev;
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
        // No `ptyId` guard, unlike a browser tab's `url`. `terminal::close`
        // returns `Ok(())` for an id it does not hold, so calling it for a tab
        // whose shell has already exited is the documented quiet path rather
        // than an error to swallow.
        void closeTerminal(id).catch((err: any) =>
          setError(readRejection(err, "Could not close that terminal")),
        );
      }
      // A page exists only for a tab the shell was asked to open (`attemptedSeats`
      // holds exactly those ids). A restored tab in the background has an
      // address and no page yet, and telling the shell to close it produced the
      // shell's own "no browser tab is open" as an error the user caused by
      // closing a tab.
      if (tab?.kind === "browser" && tab.url && attemptedSeats.current.delete(id)) {
        void closeBrowserWebview(id).catch((err: any) =>
          setError(
            readRejection(err, "Could not close that tab's browser page"),
          ),
        );
      }
    },
    [tabState, split],
  );
  // The person closing a tab (the strip's button, Ctrl+W): an editor with text nobody has saved asks first, because
  // closing it is the one close in this window that throws work away. The assistant closing a tab it opened to no purpose
  // (`closeFile` below) goes straight to `closeTabNow`: it can only ever close an editor it made and has not changed.
  const handleCloseTab = useCallback(
    (id: string) => {
      const tab = tabState.tabs.find((t) => t.id === id);
      if (tab?.kind === "editor" && editorBuffers.get(id)?.dirty) {
        if (!window.confirm(`${tab.title} has changes that are not saved. Close it and lose them?`)) return;
      }
      closeTabNow(id);
    },
    [tabState, closeTabNow],
  );

  // ── the browser pane's four moves ──────────────────────────────────────
  //
  // All four are "send this address to this tab's webview", and they differ only
  // in where the address comes from and whether the window exists yet. Keeping
  // them together is the point: a navigation the pane has to know the shape of
  // is a place for the tab's state and the page to disagree.
  //
  // `pending` is the tab being navigated, and it is what the pane's error line
  // belongs to. One browser tab at a time is deliberate: a refusal is a sentence
  // about one address, and attaching it to whichever tab happened to be focused
  // when it arrived would be a lie. Navigating a second tab while one is in
  // flight is not lost either — the first error is cleared by the second
  // `setPending`, and the second tab keeps its own history.
  // The content rectangle the browser pages are seated on, in logical pixels,
  // as the active pane last reported it. A ref, not state: the pages all share
  // one rectangle by construction, and a layout report must not re-render the
  // tree it is measuring. Read by `handleOpenBrowser` — the only call that
  // creates a page, and one that can only follow a mount-time report.
  const browserBoundsRef = useRef<BrowserBounds | null>(null);
  // Tabs whose page the shell has already been asked for, this process. One set
  // answering one question for *both* callers — the address bar and the restore
  // seat below — because the question is the same and the answer has to be
  // shared: if only the seat path recorded, it would re-open a page the address
  // bar had just opened, and an in-flight seat would then land *after* a
  // navigation the user had already asked for and drag the page back to where it
  // was. There is no "is this webview open?" command to ask instead: the pages
  // live in the shell process, and the UI is told about them, not able to query
  // them.
  const attemptedSeats = useRef<Set<string>>(new Set());
  // The same rectangle as state, for the one caller that must *react* to it:
  // the effect that seats a restored page. The ref above is deliberate for
  // every other reader — a layout report must not re-render the tree it is
  // measuring — and a restored tab is exactly the case where waiting for a
  // render is the point, since the page cannot be seated before the pane has
  // been measured (the shell refuses a zero-sized pane by design, §7.2).
  const [browserBounds, setBrowserBounds] = useState<BrowserBounds | null>(null);
  const browserResizeTimerRef = useRef<number | null>(null);
  const handleBrowserBounds = useCallback((bounds: BrowserBounds) => {
    browserBoundsRef.current = bounds;
    setBrowserBounds((prev) =>
      prev &&
      prev.x === bounds.x &&
      prev.y === bounds.y &&
      prev.width === bounds.width &&
      prev.height === bounds.height
        ? prev
        : bounds
    );
    // The pane's observer fires on every layout change; the shell's resize is
    // cheap but not free, and a drag across the window edge would otherwise
    // queue dozens. Coalesced, the last rectangle wins — the one the user
    // ended on.
    if (browserResizeTimerRef.current !== null) {
      window.clearTimeout(browserResizeTimerRef.current);
    }
    browserResizeTimerRef.current = window.setTimeout(() => {
      browserResizeTimerRef.current = null;
      const current = browserBoundsRef.current;
      if (current) void resizeBrowserWebviews(current).catch(() => {});
    }, 120);
  }, []);

  // The DevTools inspector's state, as the shell last reported it. One tab at
  // a time holds it — the inspector belongs to one page — and the toggle is
  // optimistic-free: the state changes when the shell confirms, so the
  // control's `aria-pressed` is the inspector's fact, not the press's.
  const [devtoolsTabId, setDevtoolsTabId] = useState<string | null>(null);
  // Whether this build has an inspector at all, as the shell answers. Asked
  // once per mount rather than hardcoded: a build without the feature hides
  // the control instead of offering a command the shell does not have. In the
  // standalone (no-shell) build the answer is a refusal, and refusing to show
  // an inspector is the correct reading of it.
  const [devtoolsAvailable, setDevtoolsAvailable] = useState(false);
  useEffect(() => {
    let cancelled = false;
    void browserDevtoolsAvailable()
      .then((available) => {
        if (!cancelled) setDevtoolsAvailable(available);
      })
      .catch(() => {
        if (!cancelled) setDevtoolsAvailable(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);
  const handleToggleDevtools = useCallback(
    (id: string) => {
      if (devtoolsTabId === id) {
        void closeBrowserDevtools(id)
          .then(() => setDevtoolsTabId(null))
          .catch((err: any) =>
            setPendingBrowser({ tabId: id, error: readRejection(err, "Could not close the inspector") }),
          );
        return;
      }
      // Opening over another tab's inspector closes that one in the shell's
      // own accounting only if the shell says so; here the state simply moves.
      void openBrowserDevtools(id)
        .then(() => setDevtoolsTabId(id))
        .catch((err: any) =>
          setPendingBrowser({ tabId: id, error: readRejection(err, "Could not open the inspector") }),
        );
    },
    [devtoolsTabId],
  );

  // ── the page's life, as it reports itself ─────────────────────────────
  //
  // Three facts arrive from the shell: a load started, a load finished
  // (carrying the address it actually reached — redirects included), and
  // the document titled itself. They land on the tab strip: a loading
  // marker with a bounded lifetime, and a tab titled by the page.
  //
  // Bounded is load-bearing. The runtime has no failed-load event, so
  // "loading" means "Started, no Finished yet" — and a page that dies into
  // WebKit's TLS interstitial never sends Finished. The marker expires on
  // a timer (LOADING_TIMEOUT_MS) and the interstitial is the story, not a
  // spinner that lies about progress forever.
  const [loadingBrowserIds, setLoadingBrowserIds] = useState<Set<string>>(
    () => new Set()
  );
  const loadingTimersRef = useRef<Map<string, number>>(new Map());
  const clearLoadingTimer = useCallback((id: string) => {
    const timer = loadingTimersRef.current.get(id);
    if (timer !== undefined) {
      window.clearTimeout(timer);
      loadingTimersRef.current.delete(id);
    }
  }, []);
  const startLoading = useCallback(
    (id: string) => {
      clearLoadingTimer(id);
      setLoadingBrowserIds((prev) => {
        if (prev.has(id)) return prev;
        const next = new Set(prev);
        next.add(id);
        return next;
      });
      // The expiry, not a guess of success: a load that finished clears its
      // own marker before this fires, and one that never finishes stops
      // spinning here.
      loadingTimersRef.current.set(
        id,
        window.setTimeout(() => {
          loadingTimersRef.current.delete(id);
          setLoadingBrowserIds((prev) => {
            if (!prev.has(id)) return prev;
            const next = new Set(prev);
            next.delete(id);
            return next;
          });
        }, LOADING_TIMEOUT_MS)
      );
    },
    [clearLoadingTimer]
  );
  const stopLoading = useCallback(
    (id: string) => {
      clearLoadingTimer(id);
      setLoadingBrowserIds((prev) => {
        if (!prev.has(id)) return prev;
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    },
    [clearLoadingTimer]
  );

  const [pendingBrowser, setPendingBrowser] = useState<{
    tabId: string;
    error: string | null;
  } | null>(null);

  // Every address the shell is loading right now, per tab.
  //
  // This is the whole of what tells a navigation the user asked for from one a
  // page decided on its own — the shell announces both through the same
  // `browser-page-loading` event, and the two are indistinguishable once they
  // arrive. Recorded *before* the invoke rather than after it, because the
  // load event for this very navigation can beat the invoke's own answer back.
  const inFlightBrowserCommands = useRef<InFlightNavigations>({});

  const sendToBrowser = useCallback(
    async (tabId: string, url: string, history: BrowserHistory) => {
      setPendingBrowser({ tabId, error: null });
      inFlightBrowserCommands.current[tabId] = url;
      try {
        await navigateBrowserWebview(tabId, url);
        setTabState((prev) => setBrowserUrl(prev, tabId, url, history));
      } catch (err: any) {
        // The tab is left exactly where it was. Recording the address before
        // the shell accepted it would have the address bar claim a page that
        // never loaded, and Back would walk into a place the user never was.
        // The command goes with it: a refused address loads nothing, so no
        // load event will ever answer it, and a stale command would make the
        // next page-initiated load look like an answer to this one.
        delete inFlightBrowserCommands.current[tabId];
        setPendingBrowser({
          tabId,
          error: readRejection(err, "Could not open that address"),
        });
      }
    },
    [],
  );

  // A page announced an address: is it somewhere new the user has been?
  //
  // The decision is `browserHistory.pageNavigation`'s and the ref is written
  // outside the state updater, because an updater may run more than once in a
  // render and a ref that "forgets a spent command" must forget it once. When
  // the answer is "the shell asked for this", `pageNavigation` hands the very
  // same history object back, and that identity is what says no state changed.
  const notePageNavigation = useCallback(
    (tabId: string, url: string) => {
      const tab = tabState.tabs.find((t) => t.id === tabId && t.kind === "browser");
      if (!tab?.history) return;
      const outcome = pageNavigation(
        inFlightBrowserCommands.current,
        tabId,
        url,
        tab.history
      );
      inFlightBrowserCommands.current = outcome.commands;
      if (outcome.history !== tab.history) {
        setTabState((prev) => setBrowserHistory(prev, tabId, outcome.history));
      }
    },
    [tabState.tabs]
  );

  // The address bar's first move, which is the only one that creates a window.
  //
  // `tabId` is the tab already on screen — the one whose address bar the user
  // just typed in — and it is passed to the shell as well as to the tab state,
  // because `browser::open` names the window after it and `browser::navigate`
  // looks the same name up. It used to mint a second id here, which left the
  // window and the tab disagreeing about what page they were showing.
  //
  // A refusal is therefore reported *in that tab*, which is the tab the user is
  // looking at and can type a different address into. Keying the message to an
  // id no tab had is how typing an address came to do nothing at all.
  const handleOpenBrowser = useCallback(async (id: string, url: string) => {
    setPendingBrowser({ tabId: id, error: null });
    // The mirror's pre-flight: an address the shell will refuse is refused
    // here, before the round trip, so no page is ever seated for one. The
    // shell stays the enforcement point; this is the same answer, earlier.
    const classified = classifyBrowserAddress(url, id);
    if (classified.kind === "refuse") {
      setPendingBrowser({ tabId: id, error: classified.reason });
      return;
    }
    try {
      await openBrowserWebview(
        id,
        url,
        browserBoundsRef.current ?? { x: 0, y: 0, width: 0, height: 0 },
      );
      // The shell has this page now, so the restore seat must not open it a
      // second time (see `attemptedSeats`).
      attemptedSeats.current.add(id);
      // The folder is recorded for the same reason a chat tab records one: the
      // strip says which folder a tab is in, and a browser tab that never did
      // was the odd one out.
      setTabState((prev) => openBrowserTab(prev, id, url, selectedWs?.id));
    } catch (err: any) {
      // The tab keeps its empty address bar, so the next attempt is a retype
      // rather than a hunt for the button that made this happen. The shell's
      // wording is the whole of the explanation.
      setPendingBrowser({
        tabId: id,
        error: readRejection(err, "Could not open that address"),
      });
    }
  }, [selectedWs?.id]);

  const handleBackBrowser = useCallback(
    (id: string) => {
      const tab = tabState.tabs.find((t) => t.id === id);
      const step = tab?.history ? goBack(tab.history) : null;
      if (tab && step) void sendToBrowser(id, step.url, step.history);
    },
    [tabState.tabs, sendToBrowser],
  );

  // Seat a page the shell has no webview for, because the tab that wants it
  // was restored from storage and every webview died with the last process.
  //
  // Deliberately *not* `handleOpenBrowser`: that one creates a tab from an
  // address bar, and creating the tab is what resets its stack
  // (`openBrowserTab` starts a fresh one-entry history). A restored tab's stack
  // is the thing this feature exists to keep, so this opens the page and
  // touches no tab state at all — the tab already says which address it is on
  // and where it has been.
  //
  // The address is not re-classified here because it was classified on the way
  // in (`tabPersistence.ts` runs every restored address through the same
  // `classifyBrowserAddress` a typed one goes through) and the shell's
  // `navigation_allowed` is the enforcement point regardless.
  const seatBrowserTab = useCallback(
    async (id: string, url: string, bounds: BrowserBounds) => {
      setPendingBrowser({ tabId: id, error: null });
      try {
        await openBrowserWebview(id, url, bounds);
      } catch (err: any) {
        // The tab keeps its address, so the next attempt is a retype rather
        // than a hunt. The shell's wording is the whole explanation — and the
        // one refusal a restored tab can hit is the zero-sized pane, which
        // cannot happen here because the effect only runs once the pane has
        // reported a real rectangle.
        setPendingBrowser({
          tabId: id,
          error: readRejection(err, "Could not reopen that page"),
        });
      }
    },
    [],
  );

  // Seat restored pages, once the pane has been measured.
  //
  // One tab at a time on purpose: the pane only exists for the active tab, so a
  // background tab's rectangle is not known — and a webview seated at 0×0 is
  // refused by the shell (§7.2's zero-bounds refusal). Focusing a restored tab
  // is what re-runs this effect for it, which is the same moment the pane
  // mounts for it.
  //
  // What selects a tab here is that it has an address, which is true of every
  // page the shell has been asked to open — the address bar records the address
  // *after* the shell accepts it — so "has a url" means "needs a webview" only
  // for a tab that came from storage. `attemptedSeats` is the other half: it
  // already holds every id the address bar has opened, so this only ever fires
  // for a restored tab. Marking before the call keeps a refusal from retrying
  // on every keystroke of geometry; a user who wants that page again types the
  // address, which is a different path with a different error.
  useEffect(() => {
    if (!browserBounds) return;
    const tab = activeTab(tabState);
    if (!tab || tab.kind !== "browser" || !tab.url) return;
    if (attemptedSeats.current.has(tab.id)) return;
    attemptedSeats.current.add(tab.id);
    void seatBrowserTab(tab.id, tab.url, browserBounds);
  }, [browserBounds, tabState, seatBrowserTab]);

  // Keep the strip remembered, and shared.
  //
  // Three things happen here, in this order, and the order is the design (see
  // `layoutSync.ts` for the whole argument):
  //
  // 1. **Mirror, synchronously.** Every tab gets a durable `key` if it has not
  //    got one, and the layout is written to `localStorage`. This is what makes
  //    the *next* boot work, including a boot with no engine at all, and it is
  //    not deferred behind the round trip below.
  // 2. **Push, coalesced.** Only tabs the engine has not been told about, by
  //    `planChanges`. A strip that has not changed costs one `GET`, not a write
  //    per tab; a title change costs one `PUT`.
  // 3. **Adopt the answer.** Both the push and the pull return the merged
  //    strip, so a tab closed in *another* window disappears here in the same
  //    round trip — which is what makes a close shared rather than local.
  //
  // The window's own active tab is excluded from what it pushes, deliberately:
  // which tab a window is *showing* is that window's business (two windows on
  // one strip show different ones, and neither is wrong), and the engine has no
  // use for it.
  useEffect(() => {
    if (engineUp !== true) return;
    const keyed = ensureKeys(tabState);
    if (keyed.state !== tabState) {
      setTabState(keyed.state);
      return;
    }
    // One plan, computed once against the keys the engine has confirmed. The
    // mirror is written from the *keyed* strip, so what a crash leaves behind is
    // a layout whose tabs all have keys — a mirror that could not be pushed is
    // still a mirror the next boot can read.
    const plan = planChanges(
      tabState,
      layoutMirror.current,
      engineKnown.current,
      engineRows.current,
    );
    layoutMirror.current = markPending(
      { ...layoutMirror.current, layout: layoutFrom(tabState) },
      plan,
    );
    writeLayoutMirror(layoutStorage(), layoutMirror.current);

    const active = activeKeyOf(tabState);
    let cancelled = false;
    void (async () => {
      try {
        // Removals first. A tab closed in another window must stop existing
        // before this window's upserts are read back, or the row it deleted is
        // simply written again by the next call.
        let rows: ShellTabRow[] = [];
        for (const key of plan.removals) {
          rows = await deleteShellTab(key);
        }
        for (const tab of plan.upserts) {
          rows = await upsertShellTab(tab);
        }
        // The catch-up read is what makes the *other* window's changes visible
        // here, and it is the only call when this window had nothing to say —
        // which is most runs of this effect, since it fires on every change.
        if (rows.length === 0) {
          rows = await listShellTabs();
        }
        if (cancelled) return;
        // What the engine had confirmed *before* this answer is what makes an
        // absence meaningful: a key that was there and is not now was closed,
        // while a key it has never heard of is simply not pushed yet. Passing
        // the post-response set instead would make a fresh engine's empty strip
        // look like "every tab was closed", and the window would empty itself.
        const confirmedBefore = engineKnown.current;
        const known = new Set(rows.map((row) => row.key));
        engineKnown.current = known;
        engineRows.current = rows;
        layoutMirror.current = acknowledge(
          { ...layoutMirror.current, layout: layoutFrom(tabState) },
          plan,
          known,
        );
        writeLayoutMirror(layoutStorage(), layoutMirror.current);
        setTabState((prev) =>
          reconcile(
            prev,
            rows,
            active,
            layoutMirror.current.pendingRemovals,
            confirmedBefore,
          ),
        );
      } catch {
        // The engine is down, mid-restart, or refusing. The mirror is already
        // written, so this window's strip survives; the next run — the next
        // change, or the poll below — tries again. A layout the user cannot see
        // failing to reach the other window is not worth an error banner.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [tabState, engineUp]);

  // And the other direction, for a window that is *not* changing: a strip the
  // other window closed a tab in has to appear here even if this window is
  // perfectly idle, or "shared" means shared-on-write and nothing else.
  //
  // **Pulled on attention, not on a timer**, and the reason is worth stating
  // because the obvious implementation is an interval:
  //
  // - A timer is a resource nobody can see. A window that is closed, hidden or
  //   — as `tests/terminalEndToEnd.test.ts` does — mounted without unmounting
  //   keeps a timer armed forever, and an armed timer keeps the event loop
  //   alive, which is how a suite ends in "Promise resolution is still pending
  //   but the event loop has already resolved".
  // - The thing a poll is standing in for is a push, and there is no channel
  //   for one: the engine's websocket is scoped to a goal's events (see
  //   `layoutSync`), so a strip change has nowhere to arrive through it. What a
  //   window *can* observe is the moment it was next looked at.
  //
  // So the strip is read when the window is shown, when it regains focus, and
  // once when the engine comes up. The honest cost: a window that is visible and
  // focused *behind* another one shows a strip that is stale until it is touched.
  // Two windows side by side is a thing a person does; two windows side by side
  // where one of them must update invisibly is a thing nobody has asked for, and
  // the day they do, the fix is a new engine stream rather than a shorter timer.
  useEffect(() => {
    if (engineUp !== true) return;
    let cancelled = false;
    const pull = async () => {
      if (cancelled || document.visibilityState === "hidden") return;
      try {
        const rows = await listShellTabs();
        if (cancelled) return;
        // Same distinction as the push: only a key the engine had confirmed can
        // be retired by its absence.
        const confirmedBefore = engineKnown.current;
        engineKnown.current = new Set(rows.map((row) => row.key));
        engineRows.current = rows;
        const mirror = layoutMirror.current;
        setTabState((prev) => {
          const next = reconcile(
            prev,
            rows,
            activeKeyOf(prev),
            mirror.pendingRemovals,
            confirmedBefore,
          );
          return next === prev ? prev : next;
        });
      } catch {
        // Same reasoning as the push: an engine that is not answering is not a
        // reason to interrupt anyone.
      }
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") void pull();
    };
    void pull();
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onVisible);
    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onVisible);
    };
  }, [engineUp]);

  const handleForwardBrowser = useCallback(
    (id: string) => {
      const tab = tabState.tabs.find((t) => t.id === id);
      const step = tab?.history ? goForward(tab.history) : null;
      if (tab && step) void sendToBrowser(id, step.url, step.history);
    },
    [tabState.tabs, sendToBrowser],
  );

  // The page's life, subscribed once: start (marker on), finish (marker
  // off, and the live address — redirects included — into the address bar),
  // title (the tab shows what the page calls itself). Payloads for tabs
  // that are gone are dropped by the reader and the reducers' own kind
  // check, not by luck.
  useEffect(() => {
    let unlisteners: Array<() => void> = [];
    let cancelled = false;
    void (async () => {
      const offLoading = await listenShellEvent<unknown>(
        BROWSER_PAGE_LOADING,
        (payload) => {
          const fact = readBrowserPageState(payload);
          if (!fact) return;
          startLoading(fact.tab_id);
          // A load is the page's own account of where it is going, and a link
          // the user clicked inside a page is a visit the stack would otherwise
          // never hear of — Back would then have nothing to offer but the last
          // address they typed. `notePageNavigation` tells the two apart.
          notePageNavigation(fact.tab_id, fact.url);
        }
      );
      const offLoaded = await listenShellEvent<unknown>(
        BROWSER_PAGE_LOADED,
        (payload) => {
          const fact = readBrowserPageState(payload);
          if (!fact) return;
          stopLoading(fact.tab_id);
          setTabState((prev) => setBrowserPageUrl(prev, fact.tab_id, fact.url));
        }
      );
      const offTitled = await listenShellEvent<unknown>(
        BROWSER_PAGE_TITLED,
        (payload) => {
          const fact = readBrowserPageState(payload);
          if (!fact) return;
          const named = pageTitlePayload(fact);
          if (named) {
            setTabState((prev) =>
              renameBrowserTab(prev, named.tabId, named.title)
            );
          }
        }
      );
      if (cancelled) {
        offLoading();
        offLoaded();
        offTitled();
        return;
      }
      unlisteners = [offLoading, offLoaded, offTitled];
    })();
    return () => {
      cancelled = true;
      unlisteners.forEach((off) => off());
      // And every expiry timer: a listener teardown that left one behind
      // would setState on an unmounted tree.
      loadingTimersRef.current.forEach((timer) => window.clearTimeout(timer));
      loadingTimersRef.current.clear();
    };
  }, [startLoading, stopLoading, notePageNavigation]);

  // ── the terminal's three moves ─────────────────────────────────────────
  //
  // A terminal tab is named by the shell, so the order is: open the PTY, take
  // the id it answers with, then open the tab under that name. Nothing can go
  // wrong between the second and third step, which is what makes leaking a PTY
  // here not a thing to guard against — `openTab` cannot fail.
  //
  // There is no "pending terminal" state here, and there used to be. It held the
  // last PTY opened, and its `error` was rendered only when that id matched the
  // active tab — which a *failed* open never does, because it leaves the id
  // null. So a shell that would not start reported itself to state nothing read,
  // the same silence the browser pane had. The id was then read a second time by
  // `handleTerminalResize`, which is how resizing came to address the last shell
  // opened rather than the one being drawn. Both readers were wrong, so the
  // state is gone rather than trimmed: the tab *is* the record of which PTY is
  // open, and a failure before there is a tab goes to the app-wide banner.

  // Opens a shell in a workspace and returns its id, or null when it could not (and the banner says why). A split
  // wants the id back, to put the new terminal beside the tab that asked.
  const startTerminal = useCallback(
    async (workspaceId: string | undefined): Promise<string | null> => {
      // The shell starts a PTY in a *workspace*, and refuses anything it cannot
      // pin to an existing directory (`pin_cwd`). With no workspace chosen there
      // is nothing to ask for, and the refusal is worth saying before the click
      // rather than after it.
      if (!workspaceId) {
        setError("Pick a workspace before opening a terminal");
        return null;
      }
      try {
        const ptyId = await openTerminal(workspaceId, DEFAULT_GRID.cols, DEFAULT_GRID.rows);
        // The workspace goes on the tab, not just into the open call: the shell's
        // cwd was pinned from it, and it is the key the scrollback is filed under
        // if this tab is closed and reopened.
        setTabState((prev) => openTerminalTab(prev, ptyId, workspaceId));
        return ptyId;
      } catch (err: any) {
        setError(readRejection(err, "Could not start a shell"));
        return null;
      }
    },
    [],
  );
  const handleOpenTerminal = useCallback(async () => {
    await startTerminal(selectedWs?.id);
  }, [startTerminal, selectedWs]);

  // ── the editor (`docs/09` §13) ───────────────────────────────────────────────────────────────────────────────
  //
  // A file is opened by the person (the palette's "Open file", the path on a diff card) or by the assistant
  // (`open_in_editor`, `edit_editor`), and both end the same way: an editor tab, and a buffer in the store
  // (`editorBuffers.ts`). The assistant's handlers run from a poll and not from a render, so they read the tabs and the
  // split through `liveRef`, which says how things are *now*, and not through the closure they were made in.
  const liveRef = useRef({ tabState, shownSplit, drawnSplit, splitFitsRow });
  useEffect(() => {
    liveRef.current = { tabState, shownSplit, drawnSplit, splitFitsRow };
  });

  /** Make the tab for a file, or find it, and say which at once: the assistant needs the answer before the state settles. */
  const placeEditorTab = useCallback(
    (file: { workspaceId: string; path: string }, show: boolean): { tabId: string; opened: boolean } => {
      const existing = tabForFile(liveRef.current.tabState, file.workspaceId, file.path);
      if (existing) {
        if (show) setTabState((prev) => openEditorTab(prev, file, { show: true }));
        return { tabId: existing.id, opened: false };
      }
      const id = tabId("editor");
      setTabState((prev) => openEditorTab(prev, file, { show, id }));
      // The next call in this same breath (an edit right after an open) must find the tab: the ref is a render behind.
      liveRef.current = { ...liveRef.current, tabState: openEditorTab(liveRef.current.tabState, file, { show, id }) };
      return { tabId: id, opened: true };
    },
    [],
  );

  /** The person opening a file: it becomes the tab in front, and its text starts to load. */
  const handleOpenFile = useCallback(
    (path: string, workspaceId?: string) => {
      const workspace = workspaceId ?? selectedWs?.id;
      if (!workspace) {
        setError("Pick a workspace before opening a file");
        return;
      }
      const placed = placeEditorTab({ workspaceId: workspace, path }, true);
      void editorBuffers.open(placed.tabId, { workspaceId: workspace, path });
    },
    [selectedWs, placeEditorTab],
  );

  // Every editor tab has a buffer, however the tab came to be: a tab with none would be a pane that says "Opening…" for ever.
  useEffect(() => {
    for (const tab of tabState.tabs) {
      if (tab.kind === "editor" && tab.workspaceId && tab.path && !editorBuffers.get(tab.id)) {
        void editorBuffers.open(tab.id, { workspaceId: tab.workspaceId, path: tab.path });
      }
    }
  }, [tabState]);

  /**
   * What the assistant's editor tools may do to the window, and nothing else.
   *
   * `show` is the assistant pointing at something. If the person is looking at a conversation and nothing is split, the file
   * goes beside it, made with **the conversation focused**: the assistant does not take the keyboard from the box the
   * person is typing in. Otherwise (a split already showing, a terminal or a page in front, no room) the tab opens and is
   * left, and the strip marks it: the layout the person arranged is not rearranged for them. An edit never moves anything.
   */
  const editorHost = useMemo<EditorHost>(
    () => ({
      view() {
        const live = liveRef.current;
        const active = live.tabState.activeId;
        const shown = live.drawnSplit ? [live.drawnSplit.left.id, live.drawnSplit.right.id] : active ? [active] : [];
        return { shown, focused: active };
      },
      openFile(file, intent) {
        const live = liveRef.current;
        const found = tabForFile(live.tabState, file.workspaceId, file.path);
        if (found) {
          if (live.drawnSplit && [live.drawnSplit.left.id, live.drawnSplit.right.id].includes(found.id)) {
            return { tabId: found.id, opened: false, shown: "beside" };
          }
          if (!live.drawnSplit && live.tabState.activeId === found.id) return { tabId: found.id, opened: false, shown: "front" };
        }
        const chat = live.tabState.tabs.find((t) => t.id === live.tabState.activeId && t.kind === "chat");
        if (intent === "show" && chat && !live.shownSplit && live.splitFitsRow) {
          const placed = placeEditorTab(file, false);
          setSplit({ panes: [chat.id, placed.tabId], focused: 0 });
          return { ...placed, shown: "beside" };
        }
        return { ...placeEditorTab(file, false), shown: "background" };
      },
      closeFile(id) {
        // Only an editor, and only one with nothing in it that is not saved: the assistant undoes its own open, and
        // never throws away a person's text.
        const tab = liveRef.current.tabState.tabs.find((t) => t.id === id);
        if (tab?.kind === "editor" && !editorBuffers.get(id)?.dirty) closeTabNow(id);
      },
    }),
    [placeEditorTab, setSplit, closeTabNow],
  );
  const editorHostRef = useRef(editorHost);
  useEffect(() => {
    editorHostRef.current = editorHost;
  });

  // The window's half of the surface bridge (`engine/surfaces.py`): poll the engine for questions about what is on screen
  // and answer them from the buffers, for as long as the window and the engine are up. It is on from the start and not
  // only while an editor is open, because `open_in_editor` is how one gets opened.
  useEffect(() => {
    if (engineUp !== true) return;
    const registry = createSurfaceRegistry();
    registry.register(
      "editor",
      createEditorSurface(editorBuffers, {
        view: () => editorHostRef.current.view(),
        openFile: (file, intent) => editorHostRef.current.openFile(file, intent),
        closeFile: (id) => editorHostRef.current.closeFile(id),
      }),
    );
    const controller = new AbortController();
    void runSurfaceLoop(
      { next: nextSurfaceRequest, answer: answerSurface, sleep: abortableSleep, now: Date.now },
      registry,
      controller.signal,
    );
    return () => controller.abort();
  }, [engineUp]);

  // ── split panes (`panes.ts` has the rules; `docs/09` §12 says what they are for) ─────────────────────────────
  //
  // A refusal is said, in a thin line above the panes, and goes with the next change of tab: a chord that does
  // nothing in silence reads as a broken key.
  const [paneNotice, setPaneNotice] = useState<string | null>(null);
  useEffect(() => {
    setPaneNotice(null);
  }, [tabState.activeId]);
  const [tabMenu, setTabMenu] = useState<{ tabId: string; x: number; y: number } | null>(null);

  // ── which browser page is on screen ───────────────────────────────────────────────────────────────────────────
  //
  // Visibility is the stacking order, and exactly one page is shown at a time (`browser::focus` shows the one it is
  // named and hides the rest, `docs/09` §7.3). It is the page that is **drawn**: the active tab's when a browser tab
  // fills the column, or the one in a split, which is on screen while a chat beside it has the focus (so the active tab
  // cannot be what says so). And it is hidden while anything is drawn *over* the column, because a native view paints
  // above every DOM overlay and takes the pointer: the command palette, a tab's menu, Settings, and the divider while
  // it is being dragged (a page under the pointer would swallow the moves the drag is made of). Fires on every change
  // of what is drawn, including to and from no page at all; pages that do not exist yet are absent when their turn comes.
  const [splitDragging, setSplitDragging] = useState(false);
  const pageInSplit = drawnSplit ? [drawnSplit.left, drawnSplit.right].find((t) => t.kind === "browser") : undefined;
  const drawnPageId = pageInSplit?.id ?? activeBrowserTab?.id ?? "";
  const pageCovered = splitDragging || paletteOpen || tabMenu !== null || isSettingsOpen;
  const shownPageId = pageCovered ? "" : drawnPageId;
  useEffect(() => {
    void focusBrowserWebview(shownPageId).catch((err: any) =>
      setError(readRejection(err, "Could not switch browser pages")),
    );
  }, [shownPageId]);
  const closeSplit = useCallback(() => setSplit(null), [setSplit]);
  const startSplitWith = useCallback(
    (otherId: string) => {
      const out = startSplit(tabState, otherId);
      if (!out.ok) {
        setPaneNotice(PAIR_REFUSALS[out.why]);
        return;
      }
      setPaneNotice(null);
      setTabState(out.state);
      setSplit(out.split);
    },
    [tabState, setSplit],
  );
  const splitWithNewTerminal = useCallback(async () => {
    const active = activeTab(tabState);
    if (!active) {
      setPaneNotice(PAIR_REFUSALS.missing);
      return;
    }
    const id = await startTerminal(active.workspaceId ?? selectedWs?.id);
    // The new tab is active (`openTerminalTab`), so the pair is shown the moment it exists.
    if (id) setSplit({ panes: [active.id, id], focused: 1 });
  }, [tabState, startTerminal, selectedWs, setSplit]);
  const toggleSplit = useCallback(() => {
    if (shownSplit) {
      closeSplit();
      return;
    }
    const partner = splitPartner(tabState);
    if (partner.kind === "refused") setPaneNotice(PAIR_REFUSALS[partner.why]);
    else if (partner.kind === "tab") startSplitWith(partner.id);
    else void splitWithNewTerminal();
  }, [shownSplit, tabState, closeSplit, startSplitWith, splitWithNewTerminal]);
  const handleFocusPane = useCallback(
    (side: PaneSide) => {
      const tab = side === 0 ? shownSplit?.left : shownSplit?.right;
      if (!tab) return;
      setTabState((prev) => (prev.activeId === tab.id ? prev : focusTab(prev, tab.id)));
    },
    [shownSplit],
  );
  const handleRatioChange = useCallback((ratio: number, commit: boolean) => {
    setSplitRatio(ratio);
    if (commit) writeSplitRatio(ratio);
  }, []);
  const handleTabMenuChoice = useCallback(
    (id: TabMenuItemId) => {
      const target = tabMenu?.tabId;
      if (id === "close-split") closeSplit();
      else if (id === "split-new-terminal") void splitWithNewTerminal();
      else if (target) startSplitWith(target);
    },
    [tabMenu, closeSplit, splitWithNewTerminal, startSplitWith],
  );
  const paneTitle = (tab: Tab): string =>
    threadLabel(tab.title, workspaces.find((w) => w.id === tab.workspaceId)?.name);

  // The shell finished on its own — `exit`, or the user closing a window in a
  // shell that was running something. The tab stays, because a finished
  // command's scrollback is the record of what it printed and throwing it away
  // is not what a user who just pressed Ctrl-D meant.
  const handleTerminalExit = useCallback((ptyId: string) => {
    setTabState((prev) => markTerminalExited(prev, ptyId));
  }, []);

  // Called by the pane once it has a real size. The PTY was opened at
  // DEFAULT_GRID because a pane measures itself after it exists; this is where
  // it finds out the truth. A resize for a terminal that has already exited is a
  // no-op in the shell, so the pane does not have to check.
  //
  // The id arrives with the grid rather than being read out of `pendingTerminal`.
  // That state held the *last* terminal opened, so with two shells up, dragging
  // the window resized the wrong one — the pane is the only party that knows
  // which terminal it is drawing, and it already knows.
  const handleTerminalResize = useCallback(
    (terminalId: string, grid: Grid) => {
      void resizeTerminal(terminalId, grid.cols, grid.rows).catch((err: any) =>
        setError(readRejection(err, "Could not resize that terminal")),
      );
    },
    [],
  );

  // The header's entry point. It opens a tab with no address rather than a
  // default start page: every hardcoded one would be a site this project chose
  // for the user, and a `about:blank` would be refused by the guard. An address
  // bar waiting for an address is the honest version of "new tab".
  const handleNewBrowserTab = useCallback(() => {
    const id = tabId("browser");
    setTabState((prev) => openTab(prev, { id, kind: "browser", title: "New tab" }));
  }, []);

  // Open an address in a new browser tab: the one place a web page is opened from the app's own
  // content. A popup a page asked for and a link the user clicked in an answer are the same request
  // (an address, a new tab, the guarded path), so they share this; `classifyBrowserAddress` and the
  // shell's `navigation_allowed` decide, as they do for a typed address.
  //
  // **Beside the tab it was clicked in** when that tab is alone in the column and the pair fits: a link in an answer is
  // read next to the answer, and the page is the focused pane because it is the one just asked for (`startSplit` says
  // the same). Anything else (a split already showing, no room) is the full-column tab it always was, and the split waits.
  const handleOpenLink = useCallback(
    (url: string) => {
      const id = tabId("browser");
      const live = liveRef.current;
      const from = live.tabState.tabs.find((t) => t.id === live.tabState.activeId);
      const beside = from !== undefined && from.kind !== "browser" && !live.shownSplit && live.splitFitsRow;
      setTabState((prev) =>
        openTab(prev, { id, kind: "browser", title: hostOf(url), workspaceId: selectedWs?.id }),
      );
      if (beside) setSplit({ panes: [from.id, id], focused: 1 });
      void handleOpenBrowser(id, url);
    },
    [handleOpenBrowser, selectedWs?.id, setSplit],
  );

  // A page can still ask the shell for something — a popup window. The shell
  // refuses it (`window.open` returns null to the page, the same answer a
  // popup blocker gives) and announces the target instead, so the *user*
  // decides: one press opens the address as a real tab through the same
  // guarded path as any other. Google sign-in, file pickers and every
  // `target=_blank` link land here rather than nowhere.
  //
  // A malformed payload opens nothing, for the same reason
  // `readBrowserPopupRequested` exists: the event crosses a process boundary
  // and its reader is the thing that decides.
  useEffect(() => {
    let unlisten: (() => void) | undefined;
    let cancelled = false;
    void listenShellEvent<unknown>(BROWSER_POPUP_REQUESTED, (payload) => {
      const popup = readBrowserPopupRequested(payload);
      if (!popup) return;
      handleOpenLink(popup.url);
    })
      .then((off) => {
        // The unlisten can land after unmount — StrictMode mounts, unmounts
        // and remounts in development, and the promise has no idea. Dropping it
        // on the floor would leave a listener calling setState for a tab set
        // that is gone.
        if (cancelled) off();
        else unlisten = off;
      })
      .catch((err: any) => {
        // A shell that cannot deliver events cannot have refused a popup
        // audibly either. Report it rather than leaving a listener that
        // silently never fires.
        if (!cancelled) {
          setError(
            readRejection(err, "Could not listen for browser popup requests"),
          );
        }
      });
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, [handleOpenLink]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const action = resolveShortcut(e);
      if (!action) return;
      switch (action.type) {
        case "new-tab":
          handleNewTab();
          break;
        case "close-active-tab":
          // Through the same seam as the strip's close button, so Ctrl+W on a
          // browser tab closes its webview too. The listener re-binds when the
          // tab set changes: cheap, and it buys the one seam above.
          if (tabState.activeId) handleCloseTab(tabState.activeId);
          break;
        case "focus-tab":
          setTabState((prev) => {
            const tab = prev.tabs[action.index];
            return tab ? focusTab(prev, tab.id) : prev;
          });
          break;
        case "focus-last-tab":
          setTabState((prev) => {
            const tab = prev.tabs[prev.tabs.length - 1];
            return tab ? focusTab(prev, tab.id) : prev;
          });
          break;
        case "toggle-palette":
          setPaletteOpen((v) => !v);
          break;
        // Ctrl+B is also tmux's prefix and readline's back-a-character, so with a terminal in front
        // it is the shell's, not ours: the key goes through untouched (no `preventDefault` below),
        // and the header button still reaches the panel.
        case "toggle-sidebar":
          if (activeTabKind === "terminal") return;
          toggleSidebar();
          break;
        // Ctrl+. is the same from every view, a terminal's included (`shortcuts.ts` says why it is not Ctrl+\).
        case "toggle-split":
          toggleSplit();
          break;
        // The window's size, as a browser's zoom: a step each way and back to the default. The
        // store decides the size and tells every listener (the root, the terminal, Settings).
        case "scale-up":
          writeUiScale(stepUiScale(currentUiScale(), 1));
          break;
        case "scale-down":
          writeUiScale(stepUiScale(currentUiScale(), -1));
          break;
        case "scale-reset":
          writeUiScale(DEFAULT_UI_SCALE);
          break;
      }
      // Claimed keystrokes are consumed: no browser default, no second
      // meaning for whatever held focus.
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [handleNewTab, handleCloseTab, tabState.activeId, activeTabKind, toggleSidebar, toggleSplit]);

  /** Palette pick. The item carries data; this switch is the whole act. */
  const handlePaletteSelect = (item: PaletteItem) => {
    setPaletteOpen(false);
    switch (item.kind) {
      case "tab":
        setTabState((prev) => focusTab(prev, item.tabId));
        break;
      case "conversation":
        handleSelectConversation(item.conversationId);
        break;
      case "settings":
        openSettings(item.settingsTab);
        break;
      case "action":
        if (item.action.type === "close-split") closeSplit();
        else if (item.action.type === "split-new-terminal") void splitWithNewTerminal();
        else startSplitWith(item.action.tabId);
        break;
      case "file":
        handleOpenFile(item.path);
        break;
    }
  };

  /**
   * Name a thread, and move its tab with it.
   *
   * No dialog: the caller has already decided the name, which is the whole
   * difference between this and the handler below it. The strip shows the
   * thread's name, so the tab has to move with the rename — a tab left saying
   * "New chat" over a thread called "Add an index" is the one thing a label
   * must never do. A tab's id is not the thread's id, so the tab is looked up
   * by the thread it points at.
   */
  const nameThread = useCallback(
    async (conversationId: string, title: string) => {
      const updated = await renameConversation(conversationId, title);
      updateWorkspaceConversations(updated.workspace_id, (previous) =>
        previous.map((conversation) =>
          conversation.id === updated.id ? updated : conversation,
        ),
      );
      setTabState((prev) => {
        const tab = tabForConversation(prev, conversationId);
        return tab ? renameTab(prev, tab.id, updated.title) : prev;
      });
    },
    [updateWorkspaceConversations],
  );

  const handleRenameConversation = useCallback(
    async (conversationId: string, title: string) => {
      const next = window.prompt("Name this conversation", title) ?? "";
      if (!next.trim()) return;
      try {
        await nameThread(conversationId, next.trim());
      } catch (err: any) {
        setError(readRejection(err, "Failed to rename the conversation"));
      }
    },
    [nameThread],
  );

  /**
   * Hide a thread from the panel.
   *
   * Archived, never deleted: the engine keeps every run it held either way, so
   * this tidies the tab list and cannot erase a history.
   */
  const handleArchiveConversation = useCallback(
    async (conversationId: string) => {
      try {
        const archived = await archiveConversation(conversationId);
        updateWorkspaceConversations(archived.workspace_id, (previous) =>
          previous.filter((conversation) => conversation.id !== conversationId),
        );
        setTabState((prev) => closeConversation(prev, conversationId));
      } catch (err: any) {
        setError(readRejection(err, "Failed to archive the conversation"));
      }
    },
    [updateWorkspaceConversations],
  );

  // Subscribe to live goal events via WebSocket (reconnects with backoff,
  // closes itself when the goal reaches a terminal status).
  const subscribeToGoal = useCallback(
    (goalId: string, messageId: string, sinceSequence = 0) => {
      if (goalStreams.current[goalId]) return;

      const applyEvent = (ev: Event) => {
        setMessages((prev) =>
          prev.map((msg) => {
            if (msg.id !== messageId) return msg;
            const existingEvents = msg.events || [];
            if (existingEvents.some((item) => item.sequence === ev.sequence)) {
              return msg;
            }
            const updatedEvents = [...existingEvents, ev].sort(
              (a, b) => a.sequence - b.sequence,
            );
            return { ...msg, events: updatedEvents };
          }),
        );

        // The fixer wrote files: an editor holding one of them reloads if the person has not touched it, and says so if they
        // have. Only worth asking when an editor is open at all, and only for a write that happened (not a dry run).
        if (ev.type === "file_change_summary" && editorBuffers.getSnapshot().byTab.size > 0) {
          const payload = ev.payload as { paths?: unknown; dry_run?: unknown } | undefined;
          const paths = Array.isArray(payload?.paths)
            ? payload.paths.filter((p): p is string => typeof p === "string")
            : [];
          if (!payload?.dry_run && paths.length > 0) {
            void getGoal(goalId)
              .then((owner) => editorBuffers.noteDiskChange(owner.workspace_id, paths))
              .catch(() => {});
          }
        }

        if (
          ev.type === "goal_status" ||
          ev.type === "step_status" ||
          ev.type === "plan_updated"
        ) {
          getGoal(goalId)
            .then((refreshed) => {
              setMessages((prev) =>
                prev.map((msg) =>
                  msg.id === messageId ? { ...msg, goal: refreshed } : msg,
                ),
              );
              // A plan that has come back and will *wait* for the person is news; one that starts itself
              // (Direct Apply) is not. Only on a goal_status event: a step or plan edit is the person's own.
              if (ev.type === "goal_status") {
                notify(planNotification(refreshed, modeRef.current, Date.now()));
                // An engine pause (the critic asked for changes, or the conductor could not finish a step)
                // waits for the person too, and says why. Only one that is the goal's current state: the
                // stream replays history on a reconnect.
                notify(pausedNotification(ev, refreshed, Date.now()));
              }
            })
            .catch((err: any) => {
              setError(readRejection(err, "Failed to refresh goal"));
            });
        }
      };

      goalStreams.current[goalId] = openGoalStream({
        goalId,
        onEvent: applyEvent,
        sinceSequence,
        // The engine ended this stream for good (the goal is gone, or the token was refused).
        // Drop the handle for the reason the terminal path below does: a handle left behind makes
        // every later subscribeToGoal() for this goal a silent no-op.
        onGone: () => {
          delete goalStreams.current[goalId];
        },
        // Terminal status: flush one final goal close, then stop streaming.
        onTerminal: () => {
          // This goal is done, so it is no longer the one the command bar offers to
          // stop. Cleared before the refresh below so a new goal dispatched in the
          // same tick is not cleared by this older goal's ending.
          setActiveGoalId((current) => (current === goalId ? null : current));
          // The goal just ran models, so "ran recently" is now stale. Refreshing
          // here is what makes the menu learn from the run you just watched
          // instead of from the run before it.
          loadModelSignalsRef.current?.();
          // Drop the handle: the stream closed itself, and leaving it here made
          // every future subscribeToGoal() for this goal a silent no-op (so a
          // retried or re-applied goal streamed no events).
          delete goalStreams.current[goalId];
          getGoal(goalId)
            .then((refreshed) => {
              // The run is over, so the dispatch flag goes with it. What the
              // busy dot reads is the goal's own status (`isMessageBusy`), but a
              // `true` left on a finished message is a trap for the next reader
              // either way.
              setMessages((prev) =>
                prev.map((msg) =>
                  msg.id === messageId
                    ? { ...msg, goal: refreshed, isStreaming: false }
                    : msg,
                ),
              );
              // This stream ended because the goal did, and a stream exists only for a goal this window
              // watched in flight: opening a thread or restoring History never opens one for a goal that
              // had already finished. That *is* the watched-only rule, and it needs no bookkeeping of its
              // own. A cancel is the person's own act and `goalNotification` stays silent for it.
              notify(goalNotification(refreshed, Date.now()));
            })
            .catch((err: any) => {
              setError(readRejection(err, "Failed to refresh goal"));
            });
        },
      });
    },
    [notify],
  );

  // Goal history: every goal the engine has persisted for the selected
  // workspace, newest first. The engine always kept the records — this is the
  // read that was missing, and the restore path below is what makes them more
  // than rows in a database nobody sees after a restart.
  const [history, setHistory] = useState<Goal[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [restoring, setRestoring] = useState(false);
  // The cross-goal statistics drawer and this one are mutually exclusive, which is what `drawer` (above)
  // is: both sit at the transcript's right edge, and two of them is a squeeze, not a feature.

  const loadHistory = useCallback(async () => {
    setHistoryLoading(true);
    try {
      const goals = await listGoals({
        workspace_id: selectedWs?.id,
        limit: 50,
      });
      setHistory(goals ?? []);
    } catch (err: any) {
      setError(readRejection(err, "Failed to load goal history"));
    } finally {
      setHistoryLoading(false);
    }
  }, [selectedWs?.id]);

  // Fetch when the drawer opens, and again whenever the workspace changes while
  // it stays open — the list follows the selected workspace, never lags behind it.
  useEffect(() => {
    if (historyOpen) loadHistory();
  }, [historyOpen, loadHistory]);

  /** Reopen a past goal in the transcript with its full transcript hydrated
   * from the persisted event log — the same events a live stream delivers, so
   * a restored card renders exactly like one that never left the tab. A still
   * non-terminal goal re-subscribes, so it keeps updating live from here on. */
  const restoreGoal = useCallback(
    async (goalId: string) => {
      if (restoring) return;
      setRestoring(true);
      setError(null);
      try {
        const [goal, events] = await Promise.all([
          getGoal(goalId),
          getGoalEvents(goalId),
        ]);
        // Which thread this goal belongs to, resolved before anything is put in
        // the transcript — because a message stamped with a thread the panel is
        // not showing is a message the user clicks "restore" for and then does
        // not see. A goal that predates conversations gets one here, so history
        // lands in a tab rather than in a bucket nothing renders.
        //
        // The thread is also written back (`PUT /goals/{id}/conversation`), so
        // the link is the store's and not just the panel's — without it the
        // next restart would read this run as its own thread again (docs/09
        // §6). Best effort with the error surfaced: the restore must not be
        // lost because the filing failed, but a filing that failed must not be
        // silent either.
        let threadId = goal.conversation_id ?? null;
        if (!threadId) {
          // A restored legacy goal belongs to its recorded workspace, not
          // whichever project happens to be selected when History is clicked.
          const convo = await createConversation(
            goal.workspace_id,
            (goal.description || goal.title).slice(0, 200),
          );
          threadId = convo.id;
          updateWorkspaceConversations(convo.workspace_id, (previous) => [
            convo,
            ...previous.filter((existing) => existing.id !== convo.id),
          ]);
          try {
            await attachGoalToConversation(goalId, convo.id);
          } catch (err: any) {
            setError(
              readRejection(err, "Goal restored, but its thread link could not be saved to the engine"),
            );
          }
        }
        const thread = threadId ?? null;

        const userMsg: ChatMessage = {
          id: `user-${goalId}`,
          role: "user",
          content: goal.title,
          timestamp: goal.created_at * 1000,
          // A restored goal returns to the thread it was asked in, which is the
          // difference between history that lands in the right tab and history
          // that appears wherever the user happened to be looking.
          conversationId: thread,
        };
        const assistantMsg: ChatMessage = {
          id: `assistant-${goalId}`,
          role: "assistant",
          content: goal.description || goal.title,
          timestamp: goal.created_at * 1000,
          goal,
          events: [...events].sort((a, b) => a.sequence - b.sequence),
          // Terminal goals are done; a live one re-subscribes below instead.
          isStreaming: false,
          conversationId: thread,
        };
        setMessages((prev) => [
          ...prev.filter(
            (m) => m.id !== userMsg.id && m.id !== assistantMsg.id,
          ),
          userMsg,
          assistantMsg,
        ]);
        // And the tab comes forward, so the thing just restored is the thing on
        // screen. A goal with no thread and no workspace has nowhere to go, and
        // the transcript keeps it in the unthreaded bucket instead.
        if (thread) {
          setTabState((prev) =>
            openConversation(prev, thread, goal.title, goal.workspace_id),
          );
        }
        setDrawer((open) => closeDrawer(open, "history"));
        const terminal = new Set(["COMPLETED", "FAILED", "CANCELLED"]);
        if (!terminal.has(goal.status)) {
          subscribeToGoal(goalId, assistantMsg.id);
        }
      } catch (err: any) {
        setError(readRejection(err, "Failed to restore goal"));
      } finally {
        setRestoring(false);
      }
    },
    [restoring, subscribeToGoal, updateWorkspaceConversations],
  );

  /** A notification was pressed: go where the event is. A goal reopens the way History reopens one. */
  const handleOpenNotification = useCallback(
    (item: AppNotification) => {
      const target = item.target;
      if (!target) return;
      if (target.kind === "goal") void restoreGoal(target.goalId);
      else openSettings(target.tab);
    },
    // `openSettings` is a plain function that only sets two pieces of state, so it is stable in effect.
    [restoreGoal],
  );

  // Goals whose execution we have already kicked off in direct mode. Without
  // this, the auto-start had to observe the PLANNING → PENDING transition, so a
  // plan that finished before the first poll tick (the common case with a fast
  // local model) never started at all.
  const autoStartedGoals = useRef<Set<string>>(new Set());
  // Guard against overlapping poll ticks: a slow engine response must not
  // stack a second round of getGoal/startGoal calls on top of the first.
  const polling = useRef(false);
  const TERMINAL_STATUSES = useMemo(
    () => new Set(["COMPLETED", "FAILED", "CANCELLED"]),
    [],
  );

  // ── Reopening a thread ─────────────────────────────────────────────────
  //
  // A thread the user has not sent anything into this session used to open as an
  // empty pane: the store holds what streamed to it, a reload starts it empty, and
  // nothing in the UI ever asked the engine what the thread had already said. The
  // engine has known all along — `GET /conversations/{id}/turns` derives the turns
  // from the goals that answered them, and each turn's events are the same ones a
  // live stream delivers — so opening a thread reads its history back.
  //
  // This is History's restore applied to a whole thread, and the two agree because
  // they build the same message pair (`threadHydration.turnMessages`). The decisions
  // that could go wrong — which turns are still worth fetching, and not writing over
  // a turn that is streaming in front of the user — are pure and tested there.
  //
  // The store is the whole truth about what has been shown, and it is not
  // persisted, so a thread is read once per session; a *failed* read is not
  // recorded, so the next time the thread is opened it is tried again rather than
  // remembered as empty.
  const hydratedThreads = useRef<Set<string>>(new Set());
  const hydratingThreads = useRef<Set<string>>(new Set());
  // The store as of the last render, for the "is this turn already here?" check
  // that has to run before the fetches rather than after them.
  const messagesRef = useRef<ChatMessage[]>([]);
  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  const hydrateThread = useCallback(
    async (conversationId: string | undefined) => {
      if (
        !shouldHydrateThread(
          conversationId,
          hydratedThreads.current,
          hydratingThreads.current,
        )
      ) {
        return;
      }
      const threadId = conversationId as string;
      hydratingThreads.current.add(threadId);
      try {
        const turns = await fetchConversationTurns(threadId);
        // A thread opened a second time in one session already holds most of its
        // turns, and its last one may be live: fetching those again would be a
        // request per turn to learn nothing, and — worse, if one raced the stream —
        // a second copy of the turn the user is watching under a different id.
        const wanted = turnsNeedingHydration(turns, messagesRef.current);
        const fetched = await Promise.all(
          wanted.map(async (turn) => {
            const [goal, events] = await Promise.all([
              getGoal(turn.goal_id),
              getGoalEvents(turn.goal_id),
            ]);
            return { turn, goal, events };
          }),
        );
        setMessages((prev) => {
          // Decided again here, against the store as it is *now*: the check above
          // was made before the requests went out, and a turn dispatched since then
          // is live, not history.
          const still = new Set(
            turnsNeedingHydration(turns, prev).map((t) => t.goal_id),
          );
          return mergeThreadMessages(
            prev,
            fetched
              .filter(({ turn }) => still.has(turn.goal_id))
              .flatMap(({ turn, goal, events }) =>
                turnMessages(turn, goal, events, threadId),
              ),
          );
        });
        hydratedThreads.current.add(threadId);
        // A turn that is not over keeps streaming into the very message it was
        // hydrated from, so a thread whose last turn was in flight when the window
        // closed goes on updating from here rather than freezing mid-answer.
        for (const { turn } of fetched) {
          if (!TERMINAL_STATUSES.has(turn.status)) {
            subscribeToGoal(turn.goal_id, turnMessageIds(turn.goal_id).assistant);
          }
        }
      } catch (err: any) {
        setError(readRejection(err, "Failed to load this thread's history"));
      } finally {
        hydratingThreads.current.delete(threadId);
      }
    },
    [TERMINAL_STATUSES, subscribeToGoal],
  );

  // Whenever the visible thread changes — clicked in the sidebar, restored with the
  // tab, or arrived at by closing another one — read it back if the store cannot
  // already show it. Gated on the engine actually answering, because a restored tab
  // can name a thread before the handshake has set the port this client will use.
  useEffect(() => {
    if (engineUp === true) hydrateThread(activeConversationId);
  }, [activeConversationId, engineUp, hydrateThread]);

  // Poll active goals periodically
  useEffect(() => {
    const interval = setInterval(() => {
      if (polling.current) return;
      polling.current = true;
      (async () => {
        // Prune entries for goals that already reached a terminal status so
        // the set cannot grow without bound across a long session.
        for (const m of messages) {
          if (m.goal && TERMINAL_STATUSES.has(m.goal.status)) {
            autoStartedGoals.current.delete(m.goal.id);
          }
        }
        if (autoStartedGoals.current.size > 200) {
          const ids = [...autoStartedGoals.current].slice(
            0,
            autoStartedGoals.current.size - 200,
          );
          for (const id of ids) autoStartedGoals.current.delete(id);
        }
        for (const msg of messages) {
          if (
            msg.goal &&
            (msg.goal.status === "PLANNING" ||
              msg.goal.status === "RUNNING" ||
              (mode === "direct" && msg.goal.status === "PENDING"))
          ) {
            const goalId = msg.goal.id;
            try {
              const refreshed = await getGoal(goalId);
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === msg.id ? { ...m, goal: refreshed } : m,
                ),
              );

              // Direct mode means direct: start as soon as the plan is ready,
              // whatever we happened to observe first.
              if (
                mode === "direct" &&
                refreshed.status === "PENDING" &&
                !autoStartedGoals.current.has(goalId)
              ) {
                autoStartedGoals.current.add(goalId);
                await startGoal(refreshed.id, refreshed.version);
                const running = await getGoal(refreshed.id);
                setMessages((prev) =>
                  prev.map((m) =>
                    m.id === msg.id ? { ...m, goal: running } : m,
                  ),
                );
              }
            } catch (e) {
              // Ignore polling errors
            }
          }
        }
      })().finally(() => {
        polling.current = false;
      });
    }, 1500);

    return () => clearInterval(interval);
  }, [messages, mode, TERMINAL_STATUSES]);

  /** Import a downloaded audit JSON back into the transcript as a readable
   * report. A closed artifact: it needs no engine connection, so a run can be
   * reviewed long after the goal (or the machine that ran it) is gone. */
  const handleImportAudit = () => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "application/json,.json";
    input.onchange = async () => {
      const file = input.files?.[0];
      if (!file) return;
      try {
        const text = await file.text();
        const doc = JSON.parse(text);
        if (!looksLikeAudit(doc)) {
          setError(
            `"${file.name}" is not a Codify audit export — expected a JSON document with plan_edits / fallbacks / step_outcomes.`,
          );
          return;
        }
        setError(null);
        const imported: ChatMessage = {
          id: `audit-${Date.now()}`,
          role: "assistant",
          content: `${file.name}`,
          timestamp: Date.now(),
          auditDoc: doc,
        };
        setMessages((prev) => [...prev, imported]);
      } catch (err) {
        setError(
          `Could not read "${file.name}": ${err instanceof Error ? err.message : String(err)}`,
        );
      }
    };
    input.click();
  };

  /**
   * Returns false when the prompt was refused *before* anything was dispatched, so
   * the command bar can keep the text. Losing a long prompt because a picker is
   * unset is not a recoverable mistake for the user.
   */
  const handleSendMessage = async (promptText: string): Promise<boolean> => {
    if (!selectedWs) {
      setError(
        "Select a project folder first (folder button in the command bar).",
      );
      return false;
    }
    if (!selectedModel) {
      setError(
        "No model available. Add an API key in Settings → Provider Keys (or start Ollama) " +
          "and the provider's models will load automatically.",
      );
      return false;
    }
    const wsToUse = selectedWs;
    setError(null);

    // Which thread this turn belongs to. A message typed with no tab open starts
    // one, because a turn that had no thread would be invisible to the panel —
    // and a thread that only exists in the transcript is the defect this whole
    // feature replaces.
    let conversationId = activeConversationId;
    if (!conversationId) {
      try {
        const convo = await createConversation(wsToUse.id, threadTitleFromPrompt(promptText));
        conversationId = convo.id;
        // A thread made a moment ago by this very send has no history to read back, and the
        // turn about to be dispatched is *live*, not history. Opening its tab below fires
        // hydration, which would fetch that turn from the engine and draw it a second time
        // beside the optimistic message (which learns its goal id only after the dispatch).
        // Marked read before the tab opens, so the read is never made.
        hydratedThreads.current.add(convo.id);
        updateWorkspaceConversations(convo.workspace_id, (previous) => [
          convo,
          ...previous.filter((existing) => existing.id !== convo.id),
        ]);
        setTabState((prev) =>
          openConversation(prev, convo.id, convo.title, convo.workspace_id),
        );
      } catch (err: any) {
        setError(readRejection(err, "Failed to start a conversation"));
        return false;
      }
    }
    const threadId = conversationId;

    // A thread made by "New chat" has no name, and nothing else was ever going
    // to give it one: the engine stores exactly what it is told. With a tab per
    // thread that stops being cosmetic — three untitled threads are three tabs
    // all labelled "New chat", so opening one looks precisely like nothing
    // happening. The first prompt is the name.
    //
    // Only when the panel knows the thread *and* knows it has no name. A thread
    // missing from this list was created moments ago with this very prompt as
    // its title, or belongs to another workspace; either way, do not guess and
    // overwrite a name this function cannot see.
    const named = conversations.find((c) => c.id === threadId);
    if (named && !named.title.trim()) {
      try {
        await nameThread(threadId, threadTitleFromPrompt(promptText));
      } catch (err: any) {
        // The turn runs either way. Surfaced rather than swallowed because an
        // unnamed thread is a real defect the user meets again in the strip,
        // and this is the only line that can say why.
        setError(
          readRejection(err, "The turn ran, but naming the thread failed"),
        );
      }
    }

    const userMsgId = `user-${Date.now()}`;
    const assistantMsgId = `assistant-${Date.now() + 1}`;

    const userMsg: ChatMessage = {
      id: userMsgId,
      role: "user",
      content: promptText,
      timestamp: Date.now(),
      conversationId: threadId,
    };

    const assistantMsg: ChatMessage = {
      id: assistantMsgId,
      role: "assistant",
      content: "Analyzing workspace and planning atomic steps...",
      timestamp: Date.now() + 1,
      isStreaming: true,
      events: [],
      conversationId: threadId,
    };

    setMessages((prev) => [...prev, userMsg, assistantMsg]);
    setIsLoading(true);

    try {
      // A turn, not a goal. The gate classifies what was said and the engine
      // decides the shape — a question is answered from one model call, and
      // anything else runs the full pipeline. The composer's run flags (dry
      // run, plan-only, parallel, the design/knowledge modes) deliberately do
      // NOT come along: sending them would be asking the client to make the
      // decision, which is the thing that produced a plan for "hi".
      const goal = await createTurn(
        threadId,
        promptText,
        selectedModel.provider,
        selectedModel.id,
        record,
      );
      // Cleared once dispatched: a design deliverable is what THIS goal is for,
      // not a standing preference — leaving it armed would quietly draft a
      // DESIGN.md for the next prompt the user only meant to be code.
      setGoalMode("normal");
      // Same reasoning, with the user's own data at stake: recording arms one
      // run the way it was asked for, and stays off until asked again.
      setRecord(false);

      // The optimistic message claims its goal *now*, from what the dispatch returned, and not
      // after the read below: hydration dedupes by goal id, so until this message carries one, a
      // read of the thread that lands in between sees a turn nothing on screen claims.
      setMessages((prev) =>
        prev.map((m) => (m.id === assistantMsgId ? { ...m, goal } : m)),
      );
      const fullGoal = await getGoal(goal.id);
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantMsgId ? { ...m, goal: fullGoal } : m,
        ),
      );

      // Connect WebSocket telemetry
      subscribeToGoal(goal.id, assistantMsgId);
      // The goal now exists, so there is something to stop. Set after dispatch
      // rather than before: before it, there is no id to cancel.
      setActiveGoalId(goal.id);
    } catch (err: any) {
      const detail = readRejection(err, "Failed to dispatch agent");
      setError(detail);
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantMsgId
            ? {
                ...m,
                content: `Execution failed: ${detail}`,
                isStreaming: false,
              }
            : m,
        ),
      );
    } finally {
      setIsLoading(false);
    }
    return true;
  };

  const handleEnableExecution = async (goalId: string, version: number) => {
    try {
      await enableExecution(goalId, version);
      let refreshed = await getGoal(goalId);
      // "Execute Plan" is one click: lift the guard, then start immediately.
      if (refreshed.status === "PENDING") {
        await startGoal(goalId, refreshed.version);
        refreshed = await getGoal(goalId);
      }
      setMessages((prev) =>
        prev.map((m) =>
          m.goal?.id === goalId ? { ...m, goal: refreshed } : m,
        ),
      );
    } catch (err: any) {
      setError(readRejection(err, "Failed to enable execution"));
    }
  };

  const handleApplyGoal = async (goalId: string) => {
    setError(null);
    try {
      // The apply route is version-guarded: plan edits between viewing the
      // dry run and clicking Apply must 409 (surfaced below) rather than
      // replay a plan the user has since changed.
      // The stream closed when the dry-run goal reached COMPLETED; open a new
      // one for the apply run. Find the assistant message carrying this goal.
      const msg = messages.find((m) => m.goal?.id === goalId);
      // The apply route is version-guarded: plan edits between viewing the
      // dry run and clicking Apply must 409 (surfaced below) rather than
      // replay a plan the user has since changed.
      const version = msg?.goal?.version ?? 0;
      await applyGoal(goalId, version);
      if (msg) {
        delete goalStreams.current[goalId];
        // Floor the replay at the last event already rendered so the server's
        // event replay can't close the stream before live events arrive.
        const lastSeq = Math.max(
          0,
          ...(msg.events || []).map((e) => e.sequence),
        );
        subscribeToGoal(goalId, msg.id, lastSeq);
      }
      const refreshed = await getGoal(goalId);
      setMessages((prev) =>
        prev.map((m) =>
          m.goal?.id === goalId ? { ...m, goal: refreshed } : m,
        ),
      );
    } catch (err: any) {
      setError(readRejection(err, "Failed to apply changes"));
    }
  };

  const handleEditStep = useCallback(
    async (
      goalId: string,
      stepId: string,
      expectedVersion: number,
      patch: {
        title?: string;
        description?: string;
        suggested_paths?: string[];
      },
    ): Promise<boolean> => {
      setError(null);
      try {
        await patchStep(goalId, stepId, expectedVersion, patch);
        const refreshed = await getGoal(goalId);
        setMessages((prev) =>
          prev.map((m) =>
            m.goal?.id === goalId ? { ...m, goal: refreshed } : m,
          ),
        );
        return true;
      } catch (err: any) {
        setError(readRejection(err, "Failed to update plan step"));
        // False leaves the editor open with the user's edits intact: a refused
        // save (conflict, engine down) must not destroy what they typed.
        return false;
      }
    },
    [],
  );

  // These four report through the app's error banner rather than `alert()`: a
  // blocking OS dialog is unstyled, unselectable, and invisible in a screenshot the
  // moment it is dismissed — and every other failure in this file already uses the
  // banner.
  // Start, pause and cancel are version-protected, and the version a card
  // holds can go stale between render and click — the engine legally moves a
  // RUNNING goal under you (a step finished, an event bumped the version).
  // runGoalAction retries exactly those races, treats a state that no longer
  // needs the action as a refresh rather than an error, and surfaces anything
  // else through the banner with the engine's own words. The same policy the
  // wire tests pin, applied where the user feels the difference.
  const handleStartGoal = async (goalId: string, _version: number) => {
    setError(null);
    const outcome = await runGoalAction({
      action: "start",
      attempt: (v) => startGoal(goalId, v),
      observe: () =>
        getGoal(goalId).then((g) => ({ status: g.status, version: g.version })),
    });
    const refreshed = await getGoal(goalId).catch(() => null);
    if (refreshed) {
      setMessages((prev) =>
        prev.map((m) =>
          m.goal?.id === goalId ? { ...m, goal: refreshed } : m,
        ),
      );
    }
    if (outcome.kind === "refused") {
      setError(outcome.message || "Failed to start goal");
    }
  };

  const handlePauseGoal = async (goalId: string, _version: number) => {
    setError(null);
    const outcome = await runGoalAction({
      action: "pause",
      attempt: (v) => pauseGoal(goalId, v),
      observe: () =>
        getGoal(goalId).then((g) => ({ status: g.status, version: g.version })),
    });
    const refreshed = await getGoal(goalId).catch(() => null);
    if (refreshed) {
      setMessages((prev) =>
        prev.map((m) =>
          m.goal?.id === goalId ? { ...m, goal: refreshed } : m,
        ),
      );
    }
    if (outcome.kind === "refused") {
      setError(outcome.message || "Failed to pause goal");
    }
  };

  const handleSetGoalTrace = async (goalId: string, enabled: boolean) => {
    setError(null);
    // The engine owns the rule about when a recording may start, and refuses
    // with `trace_locked` once the run has begun. That refusal is surfaced here
    // rather than swallowed: a control that silently did nothing is worse than
    // one that says why.
    const updated = await setGoalTrace(goalId, enabled);
    setMessages((prev) =>
      prev.map((m) => (m.goal?.id === goalId ? { ...m, goal: updated } : m)),
    );
  };

  const handleCancelGoal = async (goalId: string, _version: number) => {
    setError(null);
    const outcome = await runGoalAction({
      action: "cancel",
      attempt: (v) => cancelGoal(goalId, v),
      observe: () =>
        getGoal(goalId).then((g) => ({ status: g.status, version: g.version })),
    });
    const refreshed = await getGoal(goalId).catch(() => null);
    if (refreshed) {
      setMessages((prev) =>
        prev.map((m) =>
          m.goal?.id === goalId ? { ...m, goal: refreshed } : m,
        ),
      );
    }
    if (outcome.kind === "refused") {
      setError(outcome.message || "Failed to cancel goal");
    }
  };

  /**
   * Stopping the goal the command bar is currently running. Same policy as the goal
   * card's Cancel — `runGoalAction` retries the version race and treats a status
   * that no longer needs the action as *moot* — so a goal that finished a beat
   * before the click refreshes the card instead of reporting a failure. A goal that
   * finished has nothing to fail at, and saying "Failed to stop" would be inventing
   * a problem the user did not have.
   */
  const handleStopGoal = async () => {
    if (!activeGoalId) return;
    setError(null);
    const outcome = await runGoalAction({
      action: "cancel",
      attempt: (v) => cancelGoal(activeGoalId, v),
      observe: () =>
        getGoal(activeGoalId).then((g) => ({
          status: g.status,
          version: g.version,
        })),
    });
    const refreshed = await getGoal(activeGoalId).catch(() => null);
    if (refreshed) {
      setMessages((prev) =>
        prev.map((m) =>
          m.goal?.id === activeGoalId ? { ...m, goal: refreshed } : m,
        ),
      );
      // The stream may not have reached its terminal event yet, so Stop can still be
      // on screen over a goal the engine has already stopped. Hiding it the moment
      // the answer arrives is correct: there is nothing left to stop.
      if (!canStopGoal(refreshed.status)) setActiveGoalId(null);
    }
    if (outcome.kind === "refused") {
      setError(outcome.message || "Failed to stop goal");
    }
  };

  const handleDeleteGoal = async (goalId: string, title: string) => {
    // Confirm names the cascade, because "delete" on a run record is not
    // obviously the same as forgetting a chat entry: the event log, the plan
    // steps, and any dry-run proposals go with it.
    const ok = window.confirm(
      `Delete this goal?\n\n"${title}" and its full record will be removed: ` +
        `its event log, plan steps, and any dry-run proposals.\n\n` +
        `Files your fixer wrote in the workspace are NOT touched.`,
    );
    if (!ok) return;
    setError(null);
    try {
      const removed = await deleteGoal(goalId);
      // The card disappearing is the feedback, matching how the rest of the app
      // reports a successful mutation. The engine's counts are what the confirm
      // dialog promised; if they ever disagree with the dialog's wording, that
      // is a bug in the message, not something to paper over with a toast.
      console.info(
        `deleted goal ${removed.goal_id}: ${removed.events} event(s), ` +
          `${removed.steps} step(s), ${removed.proposed_files} proposal(s); files untouched`,
      );
      // Drop it from the transcript and the history drawer in one move —
      // a card left behind would be a goal the engine has already forgotten.
      setMessages((prev) => prev.filter((m) => m.goal?.id !== goalId));
      setHistory((prev) => prev.filter((g) => g.id !== goalId));
      delete goalStreams.current[goalId];
      autoStartedGoals.current.delete(goalId);
    } catch (err: any) {
      setError(readRejection(err, "Failed to delete goal"));
    }
  };

  const handleDeleteWorkspace = async (workspaceId: string, name: string) => {
    if (
      !window.confirm(
        `Remove "${name}" from Codify?\n\n` +
          `This forgets the folder. Your files stay exactly where they are.`,
      )
    ) {
      return;
    }
    setError(null);
    try {
      await deleteWorkspace(workspaceId);
      // Deleting the selected workspace leaves the picker pointing at a
      // workspace that no longer exists, so clear the selection with it.
      setSelectedWs((current) =>
        current?.id === workspaceId ? undefined : current,
      );
      await loadWorkspacesRef.current?.();
    } catch (err: any) {
      // The engine refuses a workspace that still has goals, and hands back
      // the count. That is a question, not a failure: ask it before deleting
      // anyone's history.
      const code = (err as { code?: string })?.code;
      const goals = Number(
        (err as { extra?: { goals?: number } })?.extra?.goals ?? NaN,
      );
      if (code === "workspace_not_empty" && Number.isFinite(goals)) {
        const go = window.confirm(
          `"${name}" has ${goals} goal${goals === 1 ? "" : "s"} recorded against it.\n\n` +
            `Deleting the workspace deletes all ${goals} of them and their event logs too. ` +
            `Your files are not touched.\n\nContinue?`,
        );
        if (!go) return;
        setError(null);
        try {
          await deleteWorkspace(workspaceId, { deleteGoals: true });
          setSelectedWs((current) =>
            current?.id === workspaceId ? undefined : current,
          );
          // A cascade removed goals the transcript may still be showing.
          setMessages((prev) =>
            prev.filter((m) => !m.goal || m.goal.workspace_id !== workspaceId),
          );
          await loadWorkspacesRef.current?.();
        } catch (retryErr: any) {
          setError(readRejection(retryErr, "Failed to delete workspace"));
        }
        return;
      }
      setError(readRejection(err, "Failed to delete workspace"));
    }
  };

  const handleRetryStep = async (
    goalId: string,
    stepId: string,
    version: number,
  ) => {
    setError(null);
    try {
      await retryStep(goalId, stepId, version);
      // A failed step belongs to a terminal goal, so its stream has already closed
      // itself and been dropped (`onTerminal`). Without re-opening one, the retry
      // runs with no live events in the chat at all — the exact bug that comment
      // warns about. The floor keeps the server's replay from closing the new
      // stream before the retry's own events arrive.
      const msg = messages.find((m) => m.goal?.id === goalId);
      if (msg) {
        delete goalStreams.current[goalId];
        const lastSeq = Math.max(
          0,
          ...(msg.events || []).map((e) => e.sequence),
        );
        subscribeToGoal(goalId, msg.id, lastSeq);
      }
      const refreshed = await getGoal(goalId);
      setMessages((prev) =>
        prev.map((m) =>
          m.goal?.id === goalId ? { ...m, goal: refreshed } : m,
        ),
      );
    } catch (err: any) {
      setError(readRejection(err, "Failed to retry step"));
    }
  };

  // Whether Stop is on screen, and therefore whether the command bar's right-hand
  // slot is a Stop or a Send. Derived from the goal's *status* rather than tracked
  // as a second piece of state, so it cannot disagree with what the card shows: one
  // source, one answer. `PLANNING` counts, because a goal still being planned is the
  // cheapest possible moment to stop it — nothing has been written yet.
  const activeGoal = activeGoalId
    ? messages.find((m) => m.goal?.id === activeGoalId)?.goal
    : undefined;
  const canStop = canStopGoal(activeGoal?.status);

  // What a pane holds. The chat view is built once, here, so a split and a single view draw the same thing and the
  // ~30 props are not written twice; a pane that appears unfocused beside another does not take the keyboard.
  const renderChat = (autoFocus: boolean): React.ReactElement => (
    <>
            <ChatTimeline
              messages={visibleMessages}
              onStartGoal={handleStartGoal}
              onEnableExecution={handleEnableExecution}
              onApplyGoal={handleApplyGoal}
              onEditStep={handleEditStep}
              onPauseGoal={handlePauseGoal}
              onCancelGoal={handleCancelGoal}
              onSetGoalTrace={handleSetGoalTrace}
              onRetryStep={handleRetryStep}
              onDeleteGoal={handleDeleteGoal}
              onOpenSettings={openSettings}
              onImportAudit={handleImportAudit}
              onPinDesignContract={handleSetDesignContract}
              pinnedContracts={pinnedContracts}
              onOpenLink={handleOpenLink}
              onOpenFile={(path) => handleOpenFile(path, chatInView?.workspaceId ?? selectedWs?.id)}
              onAnswerQuestion={(text) => void handleSendMessage(text)}
            />

            <BottomCommandBar
              autoFocus={autoFocus}
              insertRequest={insertRequest}
              workspaces={workspaces}
              selectedWorkspace={selectedWs}
              onSelectWorkspace={setSelectedWs}
              onBrowseWorkspace={handleBrowseWorkspace}
              manualWorkspaceOpen={manualWorkspaceOpen}
              onManualWorkspaceOpenChange={setManualWorkspaceOpen}
              onCreateWorkspace={handleCreateWorkspace}
              onDeleteWorkspace={handleDeleteWorkspace}
              onSetDesignContract={handleSetDesignContract}
              availableModels={modelCatalog.models}
              selectedModel={selectedModel}
              onSelectModel={setSelectedModel}
              modelStatus={modelCatalog.providers}
              modelSignals={modelSignals}
              modelsLoading={modelsLoading}
              onRefreshModels={() => loadModels(true)}
              mode={mode}
              onChangeMode={setMode}
              goalMode={goalMode}
              onChangeGoalMode={setGoalMode}
              parallel={parallel}
              onToggleParallel={setParallel}
              record={record}
              onToggleRecord={setRecord}
              onSubmit={handleSendMessage}
              isLoading={isLoading}
              onStop={handleStopGoal}
              canStop={canStop}
              isRunning={isGoalActive(activeGoal?.status)}
              onOpenSettings={() => openSettings("keys")}
              onOpenAudioSettings={() => openSettings("audio")}
            />
    </>
  );
  const renderTerminal = (tab: Tab, autoFocus: boolean): React.ReactElement => (
    <TerminalPane
      key={tab.id}
      terminalId={tab.id}
      workspaceId={tab.workspaceId}
      exited={tab.exited}
      onResize={handleTerminalResize}
      onExit={handleTerminalExit}
      autoFocus={autoFocus}
    />
  );
  const renderEditor = (tab: Tab, autoFocus: boolean): React.ReactElement => (
    <EditorPane key={tab.id} tabId={tab.id} buffers={editorBuffers} autoFocus={autoFocus} />
  );
  // The page is not in here: `BrowserPane` is its address bar and the rectangle the shell seats the native view over, in the
  // centre column or in half of it (`docs/09` §7.3, §12.8).
  const renderBrowser = (tab: Tab): React.ReactElement => (
    <BrowserPane
      key={tab.id}
      tabId={tab.id}
      onBounds={handleBrowserBounds}
      onToggleDevtools={devtoolsAvailable ? () => handleToggleDevtools(tab.id) : undefined}
      devtoolsOpen={devtoolsTabId === tab.id}
      url={tab.url}
      history={tab.history}
      onOpen={(url) => void handleOpenBrowser(tab.id, url)}
      onNavigate={(url) => {
        if (!tab.history) return;
        void sendToBrowser(tab.id, url, visit(tab.history, url));
      }}
      onBack={() => handleBackBrowser(tab.id)}
      onForward={() => handleForwardBrowser(tab.id)}
      error={pendingBrowser?.tabId === tab.id ? pendingBrowser.error : null}
    />
  );
  const renderPane = (tab: Tab, side: PaneSide): React.ReactElement =>
    tab.kind === "terminal"
      ? renderTerminal(tab, drawnSplit?.focused === side)
      : tab.kind === "editor"
        ? renderEditor(tab, drawnSplit?.focused === side)
        : tab.kind === "browser"
          ? renderBrowser(tab)
          : renderChat(drawnSplit?.focused === side);

  return (
    // select-none REMOVED so text cursor and selection work normally in WebKitGTK
    // `relative` for the rain: the OLED theme's backdrop is a child of this box
    // and needs a positioned ancestor to be the window rather than the viewport's
    // idea of it. See `ui/src/components/ui/RainBackdrop.tsx`.
    <ClipboardRecorderContext.Provider value={recordClip}>
    <div className="relative flex flex-col h-screen bg-codify-bg text-codify-secondary font-sans">
      {/* The window's weather, under everything and mounted once. It used to be
          inside the idle hero, which is why it showed down the middle and left
          when the first message arrived; both were the mount point, and a
          backdrop that lives inside the transcript cannot be behind the sidebar
          or survive a conversation. Gated on the theme publishing a rain
          variable, so the default theme has no canvas at all. `active` is the
          same flag the other themes get, and the rain is what the user reads it
          on — it is the theme most people pick the whole system for. */}
      <RainBackdrop active={canStop} />
      {/* Every other theme's weather, in one mount. Same point and the same
          reasoning as the rain above: a sibling, once, under the chrome, so
          nothing a conversation does can switch it off. The table inside it
          decides whether the active theme has any — a theme that is not the
          one gets no canvas and not even an empty div.
          `active` is the one piece of state any atmosphere is allowed to
          report: while a run is in flight the effect runs faster, and the
          constellation also brightens and sends a ring outward. It comes from
          the goal's own status rather than a second flag, so it cannot disagree
          with the Stop button. DESIGN.md §7 is what admits this — the motion
          carries the run's state, which is the difference between feedback and
          decoration, and the two are not the same thing. */}
      <WeatherBackdrop active={canStop} />
      {/* Top Header Bar */}
      <header className="relative bg-codify-chrome border-b border-codify-border px-4 py-2.5 flex items-center gap-3 z-10 flex-shrink-0">
        <div className="flex items-center gap-3 flex-shrink-0">
          {/* Hide or show the left panel. At the left edge, above the panel it controls, and in the
              header because the header is the one bar that is always there: a toggle inside the panel
              could not bring the panel back. `aria-pressed` is true while it is *hidden*, the state
              the button is currently holding. */}
          <IconButton
            tone="subtle"
            label={sidebarShown ? "Hide left panel" : "Show left panel"}
            title={sidebarShown ? "Hide the left panel (Ctrl+B)" : "Show the left panel (Ctrl+B)"}
            aria-pressed={!sidebarShown}
            onClick={toggleSidebar}
          >
            {sidebarShown ? <PanelLeftClose className="w-4 h-4" /> : <PanelLeftOpen className="w-4 h-4" />}
          </IconButton>
          <div className="flex items-center gap-2 font-bold text-sm tracking-tight text-codify-primary">
            {/* The mark, not a stand-in: `logo.gif` is generated into the brand
                palette by `scripts/make_logo.py`, and the primitive swaps the
                static companion in under `prefers-reduced-motion` — DESIGN.md
                §7's rule, applied for the one asset that cannot read it. */}
            <Logo size={24} />
            <span>CODIFY</span>
          </div>
          {/* New Tab, immediately after the name of the app and before the strip
              that grows. It was at the far end of the strip, which meant its
              distance from the pointer grew with the number of open tabs — the
              one control whose cost went up with the thing it was there to
              shorten. */}
          <NewTabButton onNewTab={handleNewTab} workspaceId={selectedWs?.id} />
        </div>

        {/* What is open. A chat tab is a project; the threads inside it are chosen
            from the side panel, which is why the panel no longer offers a
            new-tab control of its own. */}
        <TabBar
          tabs={tabState.tabs}
          activeId={tabState.activeId}
          onFocus={(id) => {
            setTabState((prev) => focusTab(prev, id));
            // Showing the tab is seeing it: the badge announces output the
            // user has not been shown, and the pane about to mount will claim
            // and replay the backlog that backs it.
            setUnreadTerminalIds((prev) => {
              if (!prev.has(id)) return prev;
              const next = new Set(prev);
              next.delete(id);
              return next;
            });
          }}
          unreadTerminalIds={[...unreadTerminalIds]}
          unsavedIds={unsavedEditorIds}
          assistantEditedIds={assistantEditedIds}
          splitIds={shownSplit ? [shownSplit.left.id, shownSplit.right.id] : []}
          onMenu={(tabId, x, y) => setTabMenu({ tabId, x, y })}
          onClose={handleCloseTab}
          busyTabIds={tabState.tabs
            .filter((t) => t.conversationId && activeGoalIds.has(t.conversationId))
            .map((t) => t.id)}
          loadingTabIds={[...loadingBrowserIds]}
          workspaces={workspaces}
        />

        <div className="flex items-center gap-3 flex-shrink-0">
          {/* The engine connection, as four words: Live, Checking, Auth stale,
              Offline. It used to answer with the port number when healthy, which
              made the number the headline and the state the decoration; the port
              lives in the engine's boot line and the stderr panel instead. The
              wording and the colours both come from `statusTone.ts`, so the pill
              cannot disagree with itself about what a state looks like. */}
          <button
            type="button"
            onClick={() => setIsSettingsOpen(true)}
            title={ENGINE_STATE_COPY[engineConnection].hint}
            className={`flex items-center gap-1.5 text-xs font-mono px-2.5 py-1 rounded-full border transition-colors cursor-pointer ${
              ENGINE_STATE_CLASSES[engineConnection].pill
            }`}
          >
            {/* Not a `<Button>`: this is a *status* pill that happens to be
                clickable, and the four fills above are connection states rather
                than button tones. A tone here would be claiming a button state the
                control does not have.

                The dot is steady, not pulsing. DESIGN.md §7: nothing pulses to look
                alive — and the colour already says healthy, so the loop was carrying
                no information a reduced-motion user could get. */}
            <span className={`w-2 h-2 rounded-full ${ENGINE_STATE_CLASSES[engineConnection].dot}`} />
            <span>{ENGINE_STATE_COPY[engineConnection].label}</span>
          </button>

          {/* Goal history: everything this workspace ever ran, restorable into
              the transcript. Fetched when opened, so a restart can never show a
              stale list.

              `Toggle` because these are exactly what it is for: armed, or at rest.
              The armed classes they hand-typed are `Toggle`'s accent arm byte for
              byte, so the look is unchanged and `aria-pressed` comes free. */}
          <Toggle
            armed={statsOpen}
            tone="accent"
            onClick={() => setDrawer((open) => nextDrawer(open, "stats"))}
            title="Cross-goal statistics — success rate, token spend, daily trend"
          >
            <BarChart3 className="w-3.5 h-3.5" />
            <span>Stats</span>
          </Toggle>

          {/* Notifications: goals that finished or failed, plans waiting for approval, engine connection
              changes and model list changes. Between Stats and History because it is the third of the same
              thing (a drawer on the right, armed while open); the unread count is the whole of the badge, and
              it is absent at zero because a "0" is a control-shaped lie. The title must not start with
              Browser, Terminal, Keys or Settings: those are how the panel's own buttons are found. */}
          <Toggle
            armed={notificationsOpen}
            tone="accent"
            onClick={() => setDrawer((open) => nextDrawer(open, "notifications"))}
            title="Notifications — goals finished or failed, plans waiting for approval, engine and model changes"
            aria-label={inbox.unread > 0 ? `Notifications, ${inbox.unread} unread` : "Notifications"}
          >
            <Bell className="w-3.5 h-3.5" />
            <span>Notifications</span>
            {inbox.unread > 0 && (
              <Badge tone="info" icon={false} className="ml-0.5">
                {inbox.unread > 99 ? "99+" : inbox.unread}
              </Badge>
            )}
          </Toggle>

          {/* Clipboard: what was copied, cut or pasted in this window. Icon-only, and immediately after
              Notifications because it is the same kind of thing: a drawer of what passed, for later. The
              label is the name, since there is no word beside the icon; the title must not start with
              Browser, Terminal, Keys or Settings, which is how the panel's own buttons are found. */}
          <Toggle
            armed={clipboardOpen}
            tone="accent"
            onClick={() => setDrawer((open) => nextDrawer(open, "clipboard"))}
            title="Clipboard — what you copied, cut or pasted in Codify, to copy again, insert or paste into a terminal"
            aria-label="Clipboard history"
          >
            <ClipboardList className="w-3.5 h-3.5" />
          </Toggle>

          <Toggle
            armed={historyOpen}
            tone="accent"
            onClick={() => setDrawer((open) => nextDrawer(open, "history"))}
            title="Goal history — reopen a past goal with its full transcript"
          >
            <History className="w-3.5 h-3.5" />
            <span>History</span>
          </Toggle>

          {/* Browser, Terminal and Keys & Endpoints used to be here, as three
              labelled buttons. They are utilities on the side panel now: the
              header stays focused on the brand, open tabs, and app-wide status,
              rather than a second row of unrelated actions. The panel is already
              "the things you can open". */}
        </div>
      </header>

      {/* Chat column and the drawers are SIBLINGS, not an overlay.

          The drawers used to be `absolute top-0 right-0 bottom-0` on top of the chat
          column, which was wrong twice over and looked it. The command bar is centred
          at `max-w-*`, so a 384px drawer slid underneath its right-hand edge and left
          the send button unreachable with no scroll to reach it; and the transcript
          kept its full width behind an opaque panel, so a user reading a goal had a
          third of their sentence removed with nothing indicating it was removed.

          A flex sibling shrinks the column instead of covering it. The transcript
          reflows, the command bar stays whole, and the boundary is a visible border
          rather than an occlusion. */}
      <main ref={mainRef} className="relative z-10 flex-1 flex overflow-hidden">
        {/* The threads. A flex sibling, not an overlay: the transcript reflows
            rather than being covered, which is the same reasoning as the drawers
            below. */}
        {sidebarShown && (
          <Sidebar
            conversations={conversations}
            selectedWorkspaceId={selectedWs?.id}
            workspaces={workspaces}
            activeConversationId={activeConversationId}
            onNewThread={(parentId) => void handleNewThread(parentId)}
            onNewProject={() => void handleBrowseWorkspace()}
            onSelect={handleSelectConversation}
            onRename={(id, title) => void handleRenameConversation(id, title)}
            onArchive={(id) => void handleArchiveConversation(id)}
            onOpenBrowser={handleNewBrowserTab}
            onOpenTerminal={() => void handleOpenTerminal()}
            onOpenSettings={() => setIsSettingsOpen(true)}
            loading={conversationsLoading}
          />
        )}
        <div className="flex-1 flex flex-col overflow-hidden min-w-0 relative">
          {/* The one place a refusal is shown when there is no pane to show it
              in. It carries a dismiss control because it has no natural
              lifetime: `setError(null)` is called by whichever handler owns the
              next action, so a message from an action that is not repeated
              stayed on screen until the user happened to do something else. A
              refusal the reader has understood and cannot clear is an obstacle,
              not a report.

              `IconButton` rather than a bare `<button>` so the label is a
              required prop — a dismiss icon with no accessible name is a
              control nobody using a screen reader can find. */}
          {error && (
            <div className="mx-auto mt-3 mb-1 w-full max-w-4xl px-4">
              <div
                role="alert"
                className="flex items-start gap-2 p-2.5 rounded-xl bg-codify-danger/40 border border-codify-danger text-xs text-codify-danger-ink"
              >
                <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5 text-codify-danger" />
                <span className="leading-relaxed flex-1">{error}</span>
                <IconButton
                  label="Dismiss error"
                  onClick={() => setError(null)}
                  // Dimming, not a second hue: the banner already says what it
                  // is, and a hover that could not change colour is a hover that
                  // looks broken.
                  className="text-codify-danger hover:opacity-60"
                >
                  <X className="w-3.5 h-3.5" />
                </IconButton>
              </div>
            </div>
          )}
          {/* Auth stale gets a banner of its own, because it is the one failure
              state that has a fix a *browser* tab cannot apply by itself: the
              fresh token lives with the engine's spawner, and no amount of
              retrying in-page can mint it. The banner names the make target and
              shows the two console statements filled in, so the fix is a copy
              and a reload instead of the silent 401 loop every other request
              was stuck in. Desktop never sees this — IPC self-heals first, and
              the probe re-checks before the pill settles. */}
          {/* The shell watched its own main thread and concluded this window is
              being rasterised in software: the animated backdrops cost more than
              the machine can pay, and the cost was landing on input. The verdict
              is one per boot and this banner is its receipt — naming the
              measurement and handing back the choice, because relief that a
              person cannot see and reverse is not a setting, it is a takeover. */}
          {motion.shellStarved && (
            <div className="mx-auto mt-2 mb-1 w-full max-w-4xl px-4">
              <div className="rounded-xl border border-codify-info bg-codify-info/10 p-2.5 text-xs">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-codify-info">
                    Animated backdrops were stopped: the desktop shell measured its main thread
                    spending ~{Math.min(99, motion.shellStarved.measuredMs)}% of a core painting
                    this window, which was starving input. This clears next launch.
                  </span>
                  <button
                    type="button"
                    className="rounded-md border border-codify-border bg-codify-raised px-2 py-0.5 hover:bg-codify-border"
                    onClick={() => {
                      // The choice outranks the verdict, and the verdict is
                      // withdrawn so the banner that announced it goes too.
                      writeSetting("allowed");
                      setMotion(clearShellStarvation());
                    }}
                  >
                    Animate anyway
                  </button>
                </div>
              </div>
            </div>
          )}
          {engineConnection === "auth-stale" && (
            <StaleAuthBanner
              port={getEngineInfo().port}
              onRetry={() => {
                // Re-sync the API client from what was pasted first (standalone
                // preview only; under the desktop shell the token lives in memory
                // and this leaves it alone). `currentEngine` is a module-level
                // copy made at page load, so a retry that skipped this would
                // re-probe with the stale token, fail, and call the banner a
                // liar — the exact loop the first live verification hit.
                resyncEngineInfoFromStorage();
                forceHealthProbeRef.current?.();
              }}
              onDismiss={() => setAuthOk(true)}
            />
          )}
          {/* What the engine said before it stopped answering.
              Its own stderr, not the launcher's diagnosis: the difference between
              "the engine is offline" and "the engine waited 6s for a websocket
              that never closed, then left". Amber rather than red because this is
              a quote of an explanation, not a second error — and it sits under
              the launcher's own message when there is one, so the two read as
              cause and effect. `pre` with wrapping, not a scroll box: a stack
              traceback line cut off at the right edge is the line that mattered. */}
          {engineStderr.length > 0 && (
            <div className="mx-auto mt-2 mb-1 w-full max-w-4xl px-4">
              <div className="rounded-xl border border-codify-warning/40 bg-codify-warning/10 p-2.5 text-xs">
                <div className="mb-1.5 flex items-center gap-1.5 text-codify-warning">
                  <ScrollText className="h-3.5 w-3.5 flex-shrink-0" />
                  <span>What the engine said before it stopped</span>
                </div>
                <pre className="overflow-x-auto whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-codify-warning">
                  {engineStderr.join("\n")}
                </pre>
                <p className="mt-1.5 text-xs text-codify-warning">
                  The shell stops the engine rather than restarting it, so a new
                  engine means relaunching the app.
                </p>
              </div>
            </div>
          )}
          {/* What the active tab is. A browser tab replaces the transcript
              rather than sitting under it: the chat column and the composer are
              a conversation, and a page that arrived in the strip has its own
              controls. Rendering both would leave a user with a goal transcript
              they are not reading sitting under an address bar they are.

              The page is native to this window and inside this app: a
              child webview of the main window, seated over `BrowserPane`'s
              measured content area (`docs/09` §7.3). So the pane is both its
              address bar and the rectangle it paints in, and there is nowhere
              else for the page to be. */}
          {paneNotice && (
            <div
              role="status"
              className="mx-3 mt-2 flex items-start gap-2 rounded-lg border border-codify-warning bg-codify-warning/10 px-2.5 py-1.5 text-xs text-codify-secondary"
            >
              <span className="min-w-0 flex-1">{paneNotice}</span>
              <IconButton label="Dismiss message" onClick={() => setPaneNotice(null)} className="!h-5 !w-5">
                <X className="h-3 w-3" />
              </IconButton>
            </div>
          )}
          {drawnSplit ? (
            <SplitPanes
              left={renderPane(drawnSplit.left, 0)}
              right={renderPane(drawnSplit.right, 1)}
              leftTitle={paneTitle(drawnSplit.left)}
              rightTitle={paneTitle(drawnSplit.right)}
              leftKind={drawnSplit.left.kind}
              rightKind={drawnSplit.right.kind}
              focused={drawnSplit.focused}
              ratio={splitRatio}
              onFocusPane={handleFocusPane}
              onRatioChange={handleRatioChange}
              onDragChange={setSplitDragging}
              onCloseSplit={closeSplit}
            />
          ) : activeBrowserTab ? (
            renderBrowser(activeBrowserTab)
          ) : activeEditorTab ? (
            renderEditor(activeEditorTab, true)
          ) : activeTerminalTab ? (
            renderTerminal(activeTerminalTab, true)
          ) : (
            renderChat(true)
          )}
        </div>

        {/* Cross-goal statistics drawer: the wide-angle lens over the same
              log the per-goal cards render. Independent of workspace on purpose —
              "is this setup working" spans workspaces.

              `28rem`, not the `w-96` (24rem) it shipped with: StatsPanel's stage
              table measured 500px of content inside a 384px drawer, so it scrolled
              sideways and clipped its own last column. */}
        {statsOpen && (
          <aside className="w-[28rem] max-w-[45%] shrink-0 bg-codify-surface border-l border-codify-border flex flex-col">
            <div className="flex items-center justify-between px-4 py-3 border-b border-codify-border">
              <div className="flex items-center gap-2 text-sm font-semibold text-codify-primary">
                <BarChart3 className="w-4 h-4 text-codify-info" />
                Statistics
              </div>
              <IconButton
                label="Close statistics"
                onClick={() => setDrawer((open) => closeDrawer(open, "stats"))}
              >
                <X className="w-4 h-4" />
              </IconButton>
            </div>
            <div className="flex-1 overflow-y-auto overflow-x-hidden p-3">
              <StatsPanel />
            </div>
          </aside>
        )}

        {/* Notifications: the same slot as the other drawers, and a flex sibling for the same reason (the
            native browser webview paints above any overlay). Opening a row goes where the event is: a goal
            reopens through the history restore, an engine or model change opens the Settings tab that
            holds it. */}
        {notificationsOpen && (
          <NotificationsDrawer
            items={inbox.items}
            onMarkAllRead={inbox.markAllRead}
            onClear={inbox.clear}
            onClose={() => setDrawer((open) => closeDrawer(open, "notifications"))}
            onOpenItem={handleOpenNotification}
          />
        )}

        {/* Clipboard: the same slot, a flex sibling for the same reason. A press does what its label says
            (`docs/09` §11); one that cannot work is disabled and says why. */}
        {clipboardOpen && (
          <ClipboardDrawer
            clips={clipboard.clips}
            lastSkip={clipboard.lastSkip}
            notice={clipNotice}
            canInsert={canInsertClip}
            canPasteToTerminal={canPasteClip}
            terminalExited={Boolean(terminalInView?.exited)}
            onCopy={(clip) => void handleCopyClip(clip)}
            onInsert={handleInsertClip}
            onPasteToTerminal={handlePasteClip}
            onPin={clipboard.togglePin}
            onRemove={clipboard.remove}
            onClearUnpinned={clipboard.clearUnpinned}
            onDismissSkip={clipboard.dismissSkip}
            onDismissNotice={() => setClipNotice(null)}
            onClose={() => setDrawer((open) => closeDrawer(open, "clipboard"))}
          />
        )}

        {/* Goal history drawer. Empty only when this workspace never ran a goal. */}
        {historyOpen && (
          <aside className="w-80 max-w-[40%] shrink-0 bg-codify-surface border-l border-codify-border flex flex-col">
            <div className="flex items-center justify-between px-4 py-3 border-b border-codify-border">
              <div className="flex items-center gap-2 text-sm font-semibold text-codify-primary">
                <History className="w-4 h-4 text-codify-info" />
                Goal history
              </div>
              <div className="flex items-center gap-2">
                <IconButton
                  label="Reload goal history"
                  onClick={loadHistory}
                  disabled={historyLoading}
                >
                  {historyLoading ? (
                    <div className="w-3.5 h-3.5 border-2 border-current/30 border-t-current rounded-full animate-spin" />
                  ) : (
                    <RefreshCw className="w-3.5 h-3.5" />
                  )}
                </IconButton>
                <IconButton
                  label="Close goal history"
                  onClick={() => setDrawer((open) => closeDrawer(open, "history"))}
                >
                  <X className="w-4 h-4" />
                </IconButton>
              </div>
            </div>
            <div className="flex-1 overflow-y-auto overflow-x-hidden p-2 space-y-1">
              {history.length === 0 && !historyLoading && (
                <p className="text-xs text-codify-muted px-2 py-4 leading-relaxed">
                  No goals for this workspace yet. Everything you run —
                  including goals from previous sessions — appears here.
                </p>
              )}
              {history.map((g) => (
                <button
                  key={g.id}
                  type="button"
                  onClick={() => restoreGoal(g.id)}
                  className="w-full text-left px-2.5 py-2 rounded-lg hover:bg-codify-raised transition-colors cursor-pointer"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-xs font-medium text-codify-primary truncate">
                      {g.title}
                    </span>
                    <Badge tone={statusTone(g.status)}>{g.status}</Badge>
                  </div>
                  <div className="text-2xs text-codify-muted mt-0.5">
                    {new Date(g.created_at * 1000).toLocaleString()}
                  </div>
                </button>
              ))}
            </div>
          </aside>
        )}
      </main>

      {/* Bottom Pinned Command Center — pickers live on the toolbar's left
            edge; their menus open UP over the chat, never over the textarea. */}

      {/* Global Settings Modal */}
      <SettingsModal
        isOpen={isSettingsOpen}
        initialTab={settingsTab}
        recentRuns={recentRuns}
        catalogTick={catalogTick}
        catalogLive={catalogLive}
        onClose={() => {
          setIsSettingsOpen(false);
          // Keys or endpoints may have changed — re-discover, don't re-read cache.
          loadModels(true);
          // A model may have been reassigned to a role in there, which changes
          // which ids the menus lead with.
          loadModelSignals();
        }}
      />

      {/* Ctrl+K. After SettingsModal in the DOM on purpose: both sit at z-50, and
          a later sibling paints above — the palette must be reachable while
          the dialog is open. */}
      {tabMenu &&
        (() => {
          const target = tabState.tabs.find((t) => t.id === tabMenu.tabId);
          // A tab that went (closed in another window) leaves a menu with nothing to act on, so it is not drawn.
          return target ? (
            <TabMenu
              items={tabMenuItems(target, tabState, Boolean(shownSplit))}
              x={tabMenu.x}
              y={tabMenu.y}
              onChoose={handleTabMenuChoice}
              onClose={() => setTabMenu(null)}
            />
          ) : null;
        })()}

      <CommandPalette
        open={paletteOpen}
        items={paletteItems}
        onClose={() => setPaletteOpen(false)}
        onSelect={handlePaletteSelect}
      />
    </div>
    </ClipboardRecorderContext.Provider>
  );
};
export default App;
