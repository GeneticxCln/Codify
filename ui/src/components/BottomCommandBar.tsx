import React, { useState, useRef, useEffect, useLayoutEffect } from "react";
import {
  Workspace,
  ModelOption,
  ProviderModelStatus,
  GoalMode,
} from "../types";
import {
  EMPTY_SIGNALS,
  ModelSection,
  ModelSignals,
  modelBadges,
  orderModelMenu,
} from "../modelSignals";
import {
  Folder,
  FolderOpen,
  ChevronDown,
  Sparkles,
  ArrowUp,
  Shield,
  Layers,
  Check,
  Cpu,
  RefreshCw,
  AlertCircle,
  Workflow,
  Radio,
  Trash2,
  BookOpen,
  Palette,
  Square,
} from "lucide-react";

import { Toggle } from "./ui/Toggle";
import { Button } from "./ui/Button";
import { IconButton } from "./ui/IconButton";

export type ExecutionMode = "direct" | "dry_run" | "plan_only";

/**
 * A hairline between clusters of controls, carrying the cluster's meaning on hover.
 *
 * The toolbar sets seven controls in one row at identical weight, which read as seven
 * independent knobs. They are not peers: there is one orchestrator, and these differ
 * in *kind* — what to work on (workspace, model), what the fixer may do (mode), how
 * work is scheduled (`Parallel`, the only one that changes the executor's topology),
 * and what the run keeps or produces (Record, the two deliverables). A divider is the
 * cheapest thing that says "these are not the same kind of thing", and it costs no
 * width, which a caption in a toolbar this size would.
 */
const Divider: React.FC<{ title: string }> = ({ title }) => (
  <span
    role="separator"
    aria-orientation="vertical"
    title={title}
    className="w-px self-stretch min-h-[1.25rem] my-0.5 bg-codify-border flex-shrink-0"
  />
);

// Ordering and badges come from `modelSignals`, which is pure and shared with the
// Settings field — so an id sits in the same place in both pickers.

interface BottomCommandBarProps {
  workspaces: Workspace[];
  selectedWorkspace?: Workspace;
  onSelectWorkspace: (ws: Workspace) => void;
  onBrowseWorkspace: () => Promise<void>;
  onCreateWorkspace: (name: string, root_path: string) => Promise<void>;
  /** Confirms, then forgets the workspace and (after a second confirm) its goals. */
  onDeleteWorkspace: (workspaceId: string, name: string) => void;
  /**
   * Pin the workspace's brand contract, or clear it with `""`. Rejects with the
   * engine's own message when the path escapes the workspace or is not a
   * readable text file, so the dialog can say which file and why.
   */
  onSetDesignContract: (workspaceId: string, path: string) => Promise<void>;
  availableModels: ModelOption[];
  selectedModel?: ModelOption;
  onSelectModel: (model: ModelOption) => void;
  /** Per-provider discovery outcome, including failures. */
  modelStatus?: ProviderModelStatus[];
  /** What the app already knows: which roles use which model, and what ran last. */
  modelSignals?: ModelSignals;
  modelsLoading?: boolean;
  onRefreshModels: () => void;
  mode: ExecutionMode;
  onChangeMode: (mode: ExecutionMode) => void;
  /** What the goal is *for* — orthogonal to how it executes. */
  goalMode: GoalMode;
  onChangeGoalMode: (mode: GoalMode) => void;
  /** Opt-in: independent (path-disjoint) steps of the goal run concurrently. */
  parallel?: boolean;
  onToggleParallel?: (on: boolean) => void;
  /** Opt-in: record this goal's model calls so the run can be replayed. */
  record?: boolean;
  onToggleRecord?: (on: boolean) => void;
  /**
   * Returns false when the send was refused before anything was dispatched. A
   * promise is awaited before the prompt is cleared.
   */
  onSubmit: (prompt: string) => boolean | void | Promise<boolean | void>;
  isLoading: boolean;
  /**
   * Stop the goal that is running. Absent means this app has no way to stop work,
   * and the button stays a Send — so the prop is optional rather than a required
   * callback that silently does nothing.
   */
  onStop?: () => void;
  /** A goal is in a state that can be stopped, so the right slot offers Stop. */
  canStop?: boolean;
  /** A goal is actively being worked on — drives the "running" affordance. */
  isRunning?: boolean;
  onOpenSettings: () => void;
}

export const BottomCommandBar: React.FC<BottomCommandBarProps> = ({
  workspaces,
  selectedWorkspace,
  onSelectWorkspace,
  onBrowseWorkspace,
  onCreateWorkspace,
  onDeleteWorkspace,
  onSetDesignContract,
  availableModels,
  selectedModel,
  onSelectModel,
  modelStatus = [],
  modelSignals = EMPTY_SIGNALS,
  modelsLoading = false,
  onRefreshModels,
  mode,
  onChangeMode,
  goalMode,
  onChangeGoalMode,
  parallel = false,
  onToggleParallel,
  record = false,
  onToggleRecord,
  onSubmit,
  isLoading,
  onStop,
  canStop = false,
  isRunning = false,
  onOpenSettings,
}) => {
  const [prompt, setPrompt] = useState("");
  const [isFolderOpen, setIsFolderOpen] = useState(false);
  const [isModelOpen, setIsModelOpen] = useState(false);
  const [isModeOpen, setIsModeOpen] = useState(false);
  const [customModelId, setCustomModelId] = useState("");
  const [customModelError, setCustomModelError] = useState<string | null>(null);
  // Typed filter for the model menu. A discovered catalog runs to hundreds of ids
  // on a busy install, so finding one by scrolling is worse than typing a few
  // letters — and the menu stays a short window either way.
  const [modelFilter, setModelFilter] = useState("");
  // Guards the window between "send pressed" and "isLoading is true".
  const submitInFlight = useRef(false);
  // Which workspace's brand contract the dialog is editing, and its draft. Held
  // separately from the workspace list so a refused save leaves the typed path on
  // screen to correct instead of discarding it into an error toast.
  const [contractWs, setContractWs] = useState<Workspace | null>(null);
  const [contractDraft, setContractDraft] = useState("");
  const [contractError, setContractError] = useState<string | null>(null);
  const [contractSaving, setContractSaving] = useState(false);

  useEffect(() => {
    if (!isModelOpen) setModelFilter("");
  }, [isModelOpen]);

  // Roles in use lead, then the models that ran recently (newest first), then
  // every provider — with chat-capable ids first inside each group and non-chat
  // ones badged and last. Every discovered model is still here; the leading
  // sections are a convenience over the same list, not a filter on it.
  const modelSections: ModelSection[] = React.useMemo(
    () =>
      orderModelMenu(availableModels, {
        filter: modelFilter,
        selected: selectedModel,
        signals: modelSignals,
      }),
    [availableModels, modelFilter, selectedModel, modelSignals],
  );

  const shownModelCount = modelSections.reduce(
    (n, s) => n + s.models.length,
    0,
  );

  // Manual workspace path dialog fallback
  const [isManualWsModal, setIsManualWsModal] = useState(false);
  const [wsName, setWsName] = useState("");
  const [wsPath, setWsPath] = useState("");
  const [wsError, setWsError] = useState<string | null>(null);

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const barRef = useRef<HTMLDivElement>(null);

  // Menu placement: every dropdown floats FIXED above the ENTIRE prompt card
  // (textarea included — menus must never cover the chat prompt), aligned to
  // its button's left edge. The scrollable list is capped to the space that
  // actually exists between the window top and the card, so a long model list
  // always scrolls instead of overflowing the screen. Measured from the real
  // DOM before paint, and re-measured on resize/scroll.
  type PickerKey = "folder" | "model" | "mode";
  // Viewport-anchored position for each open menu; null until measured.
  const [menuPos, setMenuPos] = useState<
    Record<PickerKey, { left: number; bottom: number } | null>
  >({
    folder: null,
    model: null,
    mode: null,
  });
  // Cap (px) for each menu's scrollable list; null = Tailwind default max-h.
  const [menuMax, setMenuMax] = useState<Record<PickerKey, number | null>>({
    folder: null,
    model: null,
    mode: null,
  });
  const wrapRefs: Record<PickerKey, React.RefObject<HTMLDivElement | null>> = {
    folder: useRef<HTMLDivElement>(null),
    model: useRef<HTMLDivElement>(null),
    mode: useRef<HTMLDivElement>(null),
  };
  const menuRefs: Record<PickerKey, React.RefObject<HTMLDivElement | null>> = {
    folder: useRef<HTMLDivElement>(null),
    model: useRef<HTMLDivElement>(null),
    mode: useRef<HTMLDivElement>(null),
  };
  const listRefs: Record<PickerKey, React.RefObject<HTMLDivElement | null>> = {
    folder: useRef<HTMLDivElement>(null),
    model: useRef<HTMLDivElement>(null),
    mode: useRef<HTMLDivElement>(null),
  };
  const openState: Record<PickerKey, boolean> = {
    folder: isFolderOpen,
    model: isModelOpen,
    mode: isModeOpen,
  };

  const measure = React.useCallback(() => {
    const card = barRef.current;
    if (!card) return;
    const cardRect = card.getBoundingClientRect();
    (Object.keys(openState) as PickerKey[]).forEach((key) => {
      if (!openState[key]) return;
      const wrap = wrapRefs[key].current;
      const menu = menuRefs[key].current;
      const list = listRefs[key].current;
      if (!wrap || !menu) return;
      const wrapRect = wrap.getBoundingClientRect();
      // Fixed-position anchor: left edge of the button, bottom edge just
      // above the whole card.
      const left = Math.max(8, wrapRect.left);
      const bottom = window.innerHeight - cardRect.top + 8;
      setMenuPos((prev) => {
        const next = { left, bottom };
        const p = prev[key];
        return p && p.left === next.left && p.bottom === next.bottom
          ? prev
          : { ...prev, [key]: next };
      });

      if (list) {
        // Shrink the scrollable list so the whole menu fits between the
        // window top and the card top.
        const chrome = Math.max(0, menu.offsetHeight - list.offsetHeight);
        const cap = Math.floor(cardRect.top - 12 - chrome);
        // Cap at a comfortable window as well as at the available space: a menu
        // that grows to fill the screen buries the chat behind it. Small list,
        // scroll inside it.
        const next = Math.max(120, Math.min(cap, 320));
        setMenuMax((prev) =>
          prev[key] === next ? prev : { ...prev, [key]: next },
        );
      }
    });
    // openState is rebuilt every render; depend on the individual flags.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    isFolderOpen,
    isModelOpen,
    isModeOpen,
    availableModels.length,
    workspaces.length,
  ]);

  useLayoutEffect(() => {
    measure();
  }, [measure]);

  // Re-measure on resize/scroll so an open menu keeps fitting the window.
  useEffect(() => {
    const anyOpen = isFolderOpen || isModelOpen || isModeOpen;
    if (!anyOpen) return;
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, true);
    return () => {
      window.removeEventListener("resize", measure);
      window.removeEventListener("scroll", measure, true);
    };
  }, [isFolderOpen, isModelOpen, isModeOpen, measure]);

  // Dropdowns overlap the chat — close on any click outside the prompt card.
  useEffect(() => {
    if (!isFolderOpen && !isModelOpen && !isModeOpen) return;
    const onPointerDown = (e: MouseEvent | TouchEvent) => {
      if (barRef.current && !barRef.current.contains(e.target as Node)) {
        setIsFolderOpen(false);
        setIsModelOpen(false);
        setIsModeOpen(false);
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("touchstart", onPointerDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("touchstart", onPointerDown);
    };
  }, [isFolderOpen, isModelOpen, isModeOpen]);

  // Auto-resize textarea
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 180)}px`;
    }
  }, [prompt]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // `isComposing` matters for any input method that confirms with Enter (CJK,
    // and the emoji pickers on macOS): sending there truncates the word being
    // composed and dispatches a goal on half of it.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSubmit();
      return;
    }
    // Escape closes an open picker before it will stop anything. The menus open over
    // the chat, so a user reaching for Escape to dismiss one is not reaching for it
    // to kill a run — and stopping a goal is far too expensive a thing to trigger by
    // a key that usually means "dismiss this".
    if (e.key === "Escape") {
      if (isFolderOpen || isModelOpen || isModeOpen) {
        setIsFolderOpen(false);
        setIsModelOpen(false);
        setIsModeOpen(false);
        return;
      }
      if (canStop && onStop && !isLoading) {
        e.preventDefault();
        onStop();
      }
    }
  };

  const handleSubmit = async () => {
    // `isLoading` only arrives on the next render, so two fast Enters could both
    // get past it and dispatch the same goal twice — two full agent runs.
    if (!prompt.trim() || isLoading || submitInFlight.current) return;
    submitInFlight.current = true;
    try {
      const accepted = await onSubmit(prompt.trim());
      // A refused send (no workspace or no model chosen) leaves the text alone: the
      // app shows what is missing in the error banner, and the prompt is still
      // there once it is fixed. Clearing it here threw away work irrecoverably.
      if (accepted === false) return;
      setPrompt("");
      if (textareaRef.current) {
        textareaRef.current.style.height = "auto";
      }
    } catch (err) {
      // handleSendMessage catches its own failures, so this is belt-and-braces —
      // but an escaped rejection must not eat the prompt or wedge the input.
      console.error("onSubmit failed", err);
    } finally {
      submitInFlight.current = false;
    }
  };

  const handleNativeBrowse = async () => {
    setIsFolderOpen(false);
    await onBrowseWorkspace();
  };

  const handleCreateManualWs = async (e: React.FormEvent) => {
    e.preventDefault();
    setWsError(null);
    try {
      await onCreateWorkspace(wsName, wsPath);
      setWsName("");
      setWsPath("");
      setIsManualWsModal(false);
      setIsFolderOpen(false);
    } catch (err: any) {
      setWsError(err.message || "Failed to add workspace");
    }
  };

  const openContractDialog = (ws: Workspace) => {
    setContractWs(ws);
    setContractDraft(ws.design_contract_path || "");
    setContractError(null);
    setIsFolderOpen(false);
  };

  const handleSaveContract = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!contractWs) return;
    setContractSaving(true);
    setContractError(null);
    try {
      await onSetDesignContract(contractWs.id, contractDraft.trim());
      setContractWs(null);
    } catch (err: any) {
      // The engine's refusal names the file and the reason; keep the dialog open
      // with the typed path so it can be corrected rather than re-typed.
      setContractError(err?.message || "Failed to pin the brand contract");
    } finally {
      setContractSaving(false);
    }
  };

  /** Unpinning is its own button, not "clear the field and remember to save": a
   *  half-applied setting is how a workspace keeps obeying a contract the user
   *  thought they had removed. */
  const handleUnpinContract = async () => {
    if (!contractWs) return;
    setContractSaving(true);
    setContractError(null);
    try {
      await onSetDesignContract(contractWs.id, "");
      setContractWs(null);
    } catch (err: any) {
      setContractError(err?.message || "Failed to unpin the brand contract");
    } finally {
      setContractSaving(false);
    }
  };

  const handleCustomModelSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setCustomModelError(null);
    const raw = customModelId.trim();
    if (!raw) return;
    // A custom id must name its provider explicitly ("provider/model"). A bare
    // id used to be accepted as provider "custom", which no provider serves —
    // so the goal failed later, far from the field that caused it.
    const slash = raw.indexOf("/");
    if (slash <= 0 || slash === raw.length - 1) {
      setCustomModelError(
        'Use the form provider/model, e.g. "ollama/qwen2.5-coder:7b".',
      );
      return;
    }
    const provider = raw.slice(0, slash).trim();
    const id = raw.slice(slash + 1).trim();
    if (!provider || !id) {
      setCustomModelError(
        'Use the form provider/model, e.g. "ollama/qwen2.5-coder:7b".',
      );
      return;
    }
    const custom: ModelOption = {
      id,
      name: raw,
      provider,
      description: "User-defined custom model",
    };
    onSelectModel(custom);
    setCustomModelId("");
    setIsModelOpen(false);
  };

  return (
    <div className="w-full max-w-4xl mx-auto p-4 z-20">
      <div
        ref={barRef}
        className="bg-codify-surface border border-codify-border rounded-2xl shadow-2xl overflow-visible focus-within:border-blue-500/80 transition-all duration-200"
      >
        {/* The pickers sit ABOVE the input, and the submit control sits at the END
            of it.

            The card used to be textarea-then-toolbar, which reads as "this toolbar
            belongs to the text above it". It does not: these controls describe the
            *run* — what to work on, with what, under what policy — not the sentence
            being typed. Putting them first says so, and it leaves the input area as
            one uninterrupted block with nothing competing for its right edge.

            The divider between clusters carries that cluster's meaning on hover; a
            caption would not fit at this width without forcing a second row. */}
        <div className="px-3 pt-3 flex flex-wrap items-center gap-2 text-xs">
          {/* Left Pickers */}
          <div className="flex items-center gap-2 flex-wrap">
            {/* Folder Picker */}
            <div className="relative" ref={wrapRefs.folder}>
              <button
                type="button"
                onClick={() => {
                  setIsFolderOpen(!isFolderOpen);
                  setIsModelOpen(false);
                  setIsModeOpen(false);
                }}
                className={`flex items-center gap-1.5 px-2.5 py-1 rounded-lg border transition-colors cursor-pointer ${
                  selectedWorkspace
                    ? "bg-codify-raised border-codify-border text-gray-200 hover:bg-codify-border"
                    : "bg-blue-950/40 border-blue-800 text-blue-300 hover:bg-blue-900/50"
                }`}
                title={selectedWorkspace?.root_path || "Select project folder"}
              >
                <Folder className="w-3.5 h-3.5 text-blue-400" />
                <span className="font-medium max-w-[150px] truncate">
                  {selectedWorkspace
                    ? selectedWorkspace.name
                    : "Select Project Folder"}
                </span>
                <ChevronDown className="w-3 h-3 text-gray-400" />
              </button>

              {/* Folder Dropdown — opens UP over the chat, flips down if no room */}
              {isFolderOpen && (
                <div
                  ref={menuRefs.folder}
                  style={
                    menuPos.folder
                      ? {
                          position: "fixed",
                          left: menuPos.folder.left,
                          bottom: menuPos.folder.bottom,
                        }
                      : undefined
                  }
                  className="absolute left-0 bottom-full mb-2 w-80 bg-codify-surface border border-codify-border rounded-xl shadow-2xl p-2 z-50"
                >
                  <div className="text-xs font-semibold text-gray-400 px-2 py-1 uppercase tracking-wider">
                    Workspaces
                  </div>

                  <div className="p-1 mb-1">
                    <button
                      type="button"
                      onClick={handleNativeBrowse}
                      className="w-full flex items-center justify-center gap-2 px-3 py-2 bg-blue-600 hover:bg-blue-500 text-white rounded-lg text-xs font-semibold transition-colors shadow"
                    >
                      <FolderOpen className="w-4 h-4" /> Open Folder in File
                      Manager...
                    </button>
                  </div>

                  {workspaces.length > 0 && (
                    <div
                      ref={listRefs.folder}
                      style={
                        menuMax.folder != null
                          ? { maxHeight: menuMax.folder }
                          : undefined
                      }
                      className="max-h-40 overflow-y-auto space-y-1 my-1"
                    >
                      {workspaces.map((ws) => (
                        // A row, not a button: the remove control is a sibling
                        // button, and a button inside a button is invalid HTML
                        // that swallows the inner click in practice.
                        <div
                          key={ws.id}
                          className={`w-full flex items-center gap-1 rounded-lg transition-colors ${
                            selectedWorkspace?.id === ws.id
                              ? "bg-blue-600/20 text-blue-400 border border-blue-500/30"
                              : "text-gray-300 hover:bg-codify-raised"
                          }`}
                        >
                          <button
                            type="button"
                            onClick={() => {
                              onSelectWorkspace(ws);
                              setIsFolderOpen(false);
                            }}
                            className="flex-1 min-w-0 text-left px-2.5 py-1.5 text-xs"
                          >
                            <div className="truncate">
                              <div className="font-semibold truncate">
                                {ws.name}
                              </div>
                              <div className="text-2xs text-gray-500 truncate font-mono">
                                {ws.root_path}
                              </div>
                            </div>
                          </button>
                          {selectedWorkspace?.id === ws.id && (
                            <Check className="w-3.5 h-3.5 text-blue-400 flex-shrink-0" />
                          )}
                          <button
                            type="button"
                            onClick={() => openContractDialog(ws)}
                            className={`p-1 rounded-lg transition-colors flex-shrink-0 ${
                              ws.design_contract_path
                                ? "text-pink-400 hover:text-pink-300"
                                : "text-gray-500 hover:text-gray-300"
                            }`}
                            title={
                              ws.design_contract_path
                                ? `${ws.name} obeys ${ws.design_contract_path}`
                                : `Pin a brand contract for ${ws.name} (e.g. DESIGN.md)`
                            }
                            aria-label={`Brand contract for ${ws.name}`}
                          >
                            <Palette className="w-3.5 h-3.5" />
                          </button>
                          <button
                            type="button"
                            onClick={() => onDeleteWorkspace(ws.id, ws.name)}
                            className="p-1 mr-1.5 text-gray-500 hover:text-red-400 rounded-lg transition-colors flex-shrink-0"
                            title={`Remove ${ws.name} from Codify (your files are not deleted)`}
                            aria-label={`Remove workspace ${ws.name}`}
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                        </div>
                      ))}
                    </div>
                  )}

                  <div className="pt-1.5 border-t border-codify-border">
                    <button
                      type="button"
                      onClick={() => {
                        setIsFolderOpen(false);
                        setIsManualWsModal(true);
                      }}
                      className="w-full text-center text-xs text-gray-400 hover:text-gray-200 py-1"
                    >
                      Enter path manually...
                    </button>
                  </div>
                </div>
              )}
            </div>

            {/* Model Picker */}
            <div className="relative" ref={wrapRefs.model}>
              <button
                type="button"
                onClick={() => {
                  setIsModelOpen(!isModelOpen);
                  setIsFolderOpen(false);
                  setIsModeOpen(false);
                }}
                className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-codify-raised border border-codify-border text-gray-200 hover:bg-codify-border transition-colors cursor-pointer"
              >
                <Cpu className="w-3.5 h-3.5 text-purple-400" />
                <span className="font-medium truncate max-w-[150px]">
                  {selectedModel ? selectedModel.name : "No model"}
                </span>
                <ChevronDown className="w-3 h-3 text-gray-400" />
              </button>

              {/* Model Dropdown — opens UP over the chat; list scrolls, capped to fit */}
              {isModelOpen && (
                <div
                  ref={menuRefs.model}
                  style={
                    menuPos.model
                      ? {
                          position: "fixed",
                          left: menuPos.model.left,
                          bottom: menuPos.model.bottom,
                        }
                      : undefined
                  }
                  className="absolute left-0 bottom-full mb-2 w-80 bg-codify-surface border border-codify-border rounded-xl shadow-2xl p-2.5 z-50"
                >
                  <div className="flex items-center justify-between px-2 py-1 mb-1">
                    <span className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
                      Models
                      {availableModels.length > 0
                        ? ` (${availableModels.length})`
                        : ""}
                    </span>
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        onClick={onRefreshModels}
                        disabled={modelsLoading}
                        title="Ask every configured provider what it serves right now"
                        className="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-200 disabled:opacity-50 cursor-pointer"
                      >
                        <RefreshCw
                          className={
                            modelsLoading ? "w-3 h-3 animate-spin" : "w-3 h-3"
                          }
                        />
                        Refresh
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          setIsModelOpen(false);
                          onOpenSettings();
                        }}
                        className="text-xs text-blue-400 hover:underline cursor-pointer"
                      >
                        API Keys
                      </button>
                    </div>
                  </div>

                  {availableModels.length > 0 && (
                    <div className="px-1 pb-1">
                      <input
                        type="text"
                        value={modelFilter}
                        onChange={(e) => setModelFilter(e.target.value)}
                        placeholder={`Filter ${availableModels.length} models…`}
                        className="w-full bg-codify-bg border border-codify-border rounded-lg px-2.5 py-1 text-xs text-gray-200 focus:outline-none focus:border-purple-500"
                      />
                    </div>
                  )}

                  <div
                    ref={listRefs.model}
                    style={
                      menuMax.model != null
                        ? { maxHeight: menuMax.model }
                        : undefined
                    }
                    className="max-h-60 overflow-y-auto space-y-0.5 my-1"
                  >
                    {availableModels.length === 0 && (
                      <div className="px-2.5 py-3 text-xs text-gray-400 leading-relaxed">
                        {modelsLoading
                          ? "Asking each provider what it serves..."
                          : "No models yet. Add an API key under API Keys, or start a local Ollama server — its models appear automatically."}
                      </div>
                    )}

                    {availableModels.length > 0 && shownModelCount === 0 && (
                      <div className="px-2.5 py-3 text-xs text-gray-400">
                        Nothing matches “{modelFilter.trim()}”. Every discovered
                        model is listed — clear the filter to see them, or set
                        the id below.
                      </div>
                    )}

                    {modelSections.map((section) => (
                      <div key={section.id} className="pb-1">
                        <div
                          className={
                            "px-2.5 pt-1.5 pb-0.5 text-2xs font-semibold uppercase tracking-wider " +
                            (section.pinned
                              ? "text-purple-300/80"
                              : "text-gray-500")
                          }
                        >
                          {section.label} ({section.models.length})
                        </div>
                        {section.models.map((m) => {
                          const isCurrent =
                            !!selectedModel &&
                            selectedModel.id === m.id &&
                            selectedModel.provider === m.provider;
                          const badges = modelBadges(m, modelSignals);
                          return (
                            <button
                              key={m.provider + ":" + m.id}
                              type="button"
                              title={
                                `${m.id}` +
                                (badges.rolesTitle
                                  ? `\n${badges.rolesTitle}`
                                  : "") +
                                (badges.lastRunTitle
                                  ? `\n${badges.lastRunTitle}`
                                  : "") +
                                (m.description ? `\n${m.description}` : "")
                              }
                              onClick={() => {
                                onSelectModel(m);
                                setIsModelOpen(false);
                              }}
                              className={
                                "w-full text-left px-2.5 py-1 rounded-lg flex items-center gap-2 text-xs transition-colors cursor-pointer " +
                                (isCurrent
                                  ? "bg-purple-600/20 text-purple-300 border border-purple-500/30 font-medium"
                                  : "text-gray-300 hover:bg-codify-raised")
                              }
                            >
                              <span className="font-semibold truncate flex-1 min-w-0">
                                {m.name}
                              </span>
                              {badges.roles && (
                                <span
                                  className="text-2xs text-teal-300/90 flex-shrink-0 max-w-[9rem] truncate"
                                  title={badges.rolesTitle}
                                >
                                  {badges.roles}
                                </span>
                              )}
                              {badges.lastRun && (
                                <span className="text-2xs text-blue-300/90 flex-shrink-0">
                                  last run
                                </span>
                              )}
                              {badges.notChat ? (
                                <span className="text-2xs text-amber-400/80 flex-shrink-0">
                                  not a chat model
                                </span>
                              ) : (
                                !badges.roles &&
                                m.description && (
                                  <span className="text-2xs text-gray-500 truncate max-w-[110px] flex-shrink-0">
                                    {m.description}
                                  </span>
                                )
                              )}
                              {isCurrent && (
                                <Check className="w-3 h-3 text-purple-400 flex-shrink-0" />
                              )}
                            </button>
                          );
                        })}
                      </div>
                    ))}

                    {/* Providers that answered *nothing* must say why — a silently
                        missing provider looks like a Codify bug. */}
                    {modelStatus
                      .filter((p) => !p.ok)
                      .map((p) => (
                        <div
                          key={"status:" + p.provider}
                          className="flex items-start gap-1.5 px-2.5 py-1 text-2xs text-amber-400/90"
                        >
                          <AlertCircle className="w-3 h-3 flex-shrink-0 mt-0.5" />
                          <span className="truncate">
                            <span className="font-semibold">{p.provider}</span>{" "}
                            — {p.error}
                          </span>
                        </div>
                      ))}
                  </div>

                  {/* Custom Model Input */}
                  <form
                    onSubmit={handleCustomModelSubmit}
                    className="pt-2 border-t border-codify-border mt-1"
                  >
                    <div className="text-2xs text-gray-400 mb-1 px-1">
                      Use Any Custom Model:
                    </div>
                    <div className="flex items-center gap-1.5">
                      <input
                        type="text"
                        placeholder="e.g. ollama/qwen2.5-coder:7b"
                        value={customModelId}
                        onChange={(e) => {
                          setCustomModelId(e.target.value);
                          setCustomModelError(null);
                        }}
                        aria-label="Custom model in provider/model form"
                        className="flex-1 bg-codify-bg border border-codify-border rounded-lg px-2.5 py-1 text-xs text-gray-200 focus:outline-none focus:border-purple-500 font-mono"
                      />
                      <button
                        type="submit"
                        disabled={!customModelId.trim()}
                        className="px-2.5 py-1 bg-purple-600 hover:bg-purple-500 text-white text-xs font-semibold rounded-lg transition-colors disabled:opacity-40"
                      >
                        Set
                      </button>
                    </div>
                    {customModelError && (
                      <p className="text-2xs text-red-400 px-1 mt-1">
                        {customModelError}
                      </p>
                    )}
                  </form>
                </div>
              )}
            </div>

            {/* Execution Mode Selector */}
            <div className="relative" ref={wrapRefs.mode}>
              <button
                type="button"
                onClick={() => {
                  setIsModeOpen(!isModeOpen);
                  setIsFolderOpen(false);
                  setIsModelOpen(false);
                }}
                className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-codify-raised border border-codify-border text-gray-300 hover:bg-codify-border transition-colors cursor-pointer"
              >
                {mode === "direct" && (
                  <Sparkles className="w-3.5 h-3.5 text-green-400" />
                )}
                {mode === "dry_run" && (
                  <Shield className="w-3.5 h-3.5 text-amber-400" />
                )}
                {mode === "plan_only" && (
                  <Layers className="w-3.5 h-3.5 text-blue-400" />
                )}
                <span>
                  {mode === "direct" && "Direct Apply"}
                  {mode === "dry_run" && "Review Diffs"}
                  {mode === "plan_only" && "Plan Only"}
                </span>
                <ChevronDown className="w-3 h-3 text-gray-400" />
              </button>

              {isModeOpen && (
                <div
                  ref={menuRefs.mode}
                  style={
                    menuPos.mode
                      ? {
                          position: "fixed",
                          left: menuPos.mode.left,
                          bottom: menuPos.mode.bottom,
                        }
                      : undefined
                  }
                  className="absolute left-0 bottom-full mb-2 w-56 bg-codify-surface border border-codify-border rounded-xl shadow-2xl p-1.5 z-50"
                >
                  <button
                    type="button"
                    onClick={() => {
                      onChangeMode("direct");
                      setIsModeOpen(false);
                    }}
                    className={`w-full text-left px-2.5 py-1.5 rounded-lg text-xs flex items-center justify-between cursor-pointer ${
                      mode === "direct"
                        ? "bg-green-600/20 text-green-300 font-medium"
                        : "text-gray-300 hover:bg-codify-raised"
                    }`}
                  >
                    <div>
                      <div className="font-semibold">Direct Apply</div>
                      <div className="text-2xs text-gray-500">
                        Edit files & run tests automatically
                      </div>
                    </div>
                    {mode === "direct" && (
                      <Check className="w-3.5 h-3.5 text-green-400" />
                    )}
                  </button>

                  <button
                    type="button"
                    onClick={() => {
                      onChangeMode("dry_run");
                      setIsModeOpen(false);
                    }}
                    className={`w-full text-left px-2.5 py-1.5 rounded-lg text-xs flex items-center justify-between mt-1 cursor-pointer ${
                      mode === "dry_run"
                        ? "bg-amber-600/20 text-amber-300 font-medium"
                        : "text-gray-300 hover:bg-codify-raised"
                    }`}
                  >
                    <div>
                      <div className="font-semibold">
                        Review Diffs (Dry Run)
                      </div>
                      <div className="text-2xs text-gray-500">
                        Simulate changes without writing disk
                      </div>
                    </div>
                    {mode === "dry_run" && (
                      <Check className="w-3.5 h-3.5 text-amber-400" />
                    )}
                  </button>

                  <button
                    type="button"
                    onClick={() => {
                      onChangeMode("plan_only");
                      setIsModeOpen(false);
                    }}
                    className={`w-full text-left px-2.5 py-1.5 rounded-lg text-xs flex items-center justify-between mt-1 cursor-pointer ${
                      mode === "plan_only"
                        ? "bg-blue-600/20 text-blue-300 font-medium"
                        : "text-gray-300 hover:bg-codify-raised"
                    }`}
                  >
                    <div>
                      <div className="font-semibold">Plan Only</div>
                      <div className="text-2xs text-gray-500">
                        Show the plan; you approve execution
                      </div>
                    </div>
                    {mode === "plan_only" && (
                      <Check className="w-3.5 h-3.5 text-blue-400" />
                    )}
                  </button>
                </div>
              )}
            </div>

            <Divider title="Execution policy — whether the fixer may write, and whether steps run concurrently" />

            {/* Parallel toggle: independent (path-disjoint) steps run concurrently.
                Safe by construction — steps that touch the same files still go
                in order, and the sandbox/git stay serialized inside the engine.

                This is the one control here that changes the *orchestration topology*
                rather than a setting of it: the executor switches from a sequential
                walk to `asyncio.gather` over path-disjoint steps, with sandbox
                commands and git commits serialized behind locks. That is why it sits
                on its own rather than in the cluster of flags beside it. */}
            <Toggle
              armed={parallel}
              onClick={() => onToggleParallel?.(!parallel)}
              disabled={!onToggleParallel}
              title="Run independent steps concurrently — steps that touch the same files still go in order"
            >
              <Workflow className="w-3.5 h-3.5" />
              <span>Parallel</span>
            </Toggle>

            <Divider title="What this run keeps, and what it produces" />

            {/* Recording: keep a copy of every model call this run makes, so the
                run can be replayed later with no provider in the loop. Off by
                default and never automatic — a recording is a copy of the
                model's output about the user's code, so it is something they
                ask for while chasing a bad run and can delete at any time. */}
            <Toggle
              armed={record}
              tone="warning"
              onClick={() => onToggleRecord?.(!record)}
              disabled={!onToggleRecord}
              title="Record every model call this run makes, so it can be replayed later without a provider"
            >
              <Radio className="w-3.5 h-3.5" />
              <span>Record</span>
            </Toggle>

            {/* Design and Knowledge are ONE choice, not two switches.

                `goalMode` is a single value — `normal | design | knowledge` — and both
                buttons write to it, so arming one disarms the other. They were drawn
                as two independent toggles, which promised a combination the state
                cannot hold: a user could read two armed pills as "this run produces
                both files", and the engine would only ever write one.

                So they share a container. The border between them is a seam, not a
                gap, so the pair reads as one segmented control with a single armed
                position — which is what it is. `role="radiogroup"` says the same thing
                to a screen reader, and each button keeps `aria-pressed` from `Toggle`
                because that is the honest state: neither is "selected" in a group
                whose value may legitimately be neither. */}
            <div
              role="radiogroup"
              aria-label="Deliverable"
              title="This run produces one deliverable file, or none"
              className="flex items-center rounded-lg border border-codify-border bg-codify-raised overflow-hidden"
            >
              <Toggle
                armed={goalMode === "design"}
                tone="design"
                onClick={() =>
                  onChangeGoalMode(goalMode === "design" ? "normal" : "design")
                }
                title="Design deliverable — draft or revise this workspace's DESIGN.md, reviewed by the critic before you pin it"
                className="rounded-none border-0 border-r border-codify-border"
              >
                <Palette className="w-3.5 h-3.5" />
                <span>Design</span>
              </Toggle>

              <Toggle
                armed={goalMode === "knowledge"}
                tone="knowledge"
                onClick={() =>
                  onChangeGoalMode(
                    goalMode === "knowledge" ? "normal" : "knowledge",
                  )
                }
                title="Knowledge deliverable — draft or revise this workspace's CODIFY.md, which later runs read as a prior"
                className="rounded-none border-0"
              >
                <BookOpen className="w-3.5 h-3.5" />
                <span>Knowledge</span>
              </Toggle>
            </div>

            {/* Knowledge deliverable: the same shape pointed at a different
                file. CODIFY.md is what every later run's librarian reads first
                — as a prior, never as evidence — so the reviewer here is
                approving a description of the repository that will aim every
                future run at files. A goal-mode toggle rather than a workspace
                setting because it is a one-off rewrite, not a standing one.

                Knowledge's own hue, not Record's amber. The two were byte-identical
                here, which meant two unrelated features armed the same colour in the
                same toolbar. See `Toggle`. */}
          </div>
        </div>

        {/* The input area, with submit at its end.

            `items-end` pins the button to the LAST line of the text as the textarea
            grows, rather than letting it float beside line one of a five-line
            prompt — the auto-resize here runs to 180px, so that gap is reachable. */}
        <div className="p-3.5 pt-2 flex items-end gap-2">
          {/* NEVER disabled so the cursor is always responsive. */}
          <textarea
            ref={textareaRef}
            rows={1}
            autoFocus
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={isLoading}
            placeholder="Ask Codify to build, edit files, fix tests, or refactor code..."
            aria-label="Chat prompt"
            className="flex-1 min-w-0 bg-transparent text-gray-100 placeholder-gray-500 text-sm resize-none focus:outline-none leading-relaxed cursor-text"
          />

          {/* Right Action: Send, or Stop when a goal is in flight.

              The same slot for both, deliberately. The right-hand control is where
              the user already looks for "what does this do next", and swapping a
              disabled spinner for an enabled Stop puts the stop exactly where the
              send was — so the muscle memory for pressing it needs no new thought.
              Two buttons would have meant the stop sitting somewhere new, unpressed.

              `danger` because Stop ends a run that may be writing files, and the
              colour is the only warning it gets: there is no confirm dialog, because
              a confirmation on the one control that is already hard to reach is a
              second chance to lose the run, not a safety. The goal card keeps a
              quieter Cancel for when the user is looking at the goal rather than the
              bar. */}
          <div className="flex items-center gap-2 flex-shrink-0">
            {/* The shortcut hint appears only when there is a destructive action to
                shortcut, and it sits BESIDE the button rather than above it.

                Two things were wrong with it stacked on top. "⏎ to send" said nothing
                a user of a chat box does not already know, and as a caption floating
                over a lone blue square it read as a label for something above the
                button rather than a hint about the one below it. "esc to stop" earns
                its place — Stop ends a run that may be writing files, and Esc is
                not a shortcut anyone discovers — so that one stays, inline, where it
                reads as belonging to the control it describes.

                Wording still follows the goal's real state: `canStop` is wider than
                `isRunning` on purpose, because a paused goal can still be ended, and
                a hint reading "esc to stop" over a goal nobody is working on would
                be claiming work that is not happening. */}
            {canStop && (
              <span className="text-2xs text-codify-muted hidden sm:inline">
                {isRunning ? "esc to stop" : "esc to cancel"}
              </span>
            )}
            {canStop ? (
              <Button
                tone="danger"
                size="md"
                onClick={onStop}
                disabled={!onStop || isLoading}
                title="Stop this goal. Work already written to your files is kept."
              >
                <Square className="w-3 h-3 fill-current" />
                <span>Stop</span>
              </Button>
            ) : (
              <IconButton
                label="Send prompt"
                tone="primary"
                size="md"
                onClick={handleSubmit}
                disabled={!prompt.trim() || isLoading}
                className="w-8 h-8 shadow"
              >
                {isLoading ? (
                  <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                ) : (
                  <ArrowUp className="w-4 h-4" />
                )}
              </IconButton>
            )}
          </div>
        </div>
      </div>

      {/* Manual Workspace Path Modal */}
      {isManualWsModal && (
        <div className="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div
            role="dialog"
            aria-modal="true"
            aria-label="Enter workspace path"
            className="bg-codify-surface border border-codify-border rounded-xl p-5 max-w-md w-full shadow-2xl"
          >
            <h3 className="text-sm font-bold text-gray-100 mb-3 flex items-center gap-2">
              <Folder className="w-4 h-4 text-blue-400" /> Enter Workspace Path
            </h3>
            <form onSubmit={handleCreateManualWs} className="space-y-3">
              <div>
                <label className="text-xs text-gray-400 block mb-1">
                  Project Name
                </label>
                <input
                  type="text"
                  placeholder="e.g. My Next.js App"
                  value={wsName}
                  onChange={(e) => setWsName(e.target.value)}
                  className="w-full bg-codify-bg border border-codify-border rounded-lg px-3 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-blue-500"
                  required
                />
              </div>
              <div>
                <label className="text-xs text-gray-400 block mb-1">
                  Absolute Directory Path
                </label>
                <input
                  type="text"
                  placeholder="/home/you/Projects/my-app"
                  value={wsPath}
                  onChange={(e) => setWsPath(e.target.value)}
                  className="w-full bg-codify-bg border border-codify-border rounded-lg px-3 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-blue-500 font-mono"
                  required
                />
              </div>
              {wsError && <p className="text-xs text-red-400">{wsError}</p>}
              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => setIsManualWsModal(false)}
                  className="px-3 py-1 bg-codify-raised text-gray-300 text-xs rounded-lg hover:bg-codify-border"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="px-3 py-1 bg-blue-600 text-white text-xs font-semibold rounded-lg hover:bg-blue-500"
                >
                  Add Directory
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Which file in this workspace the design agent must obey. Blank is a real
          answer — it means "only the conventional DESIGN.md, if the repo has one"
          — so it is offered as its own action rather than a cleared text field. */}
      {contractWs && (
        <div className="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div
            role="dialog"
            aria-modal="true"
            aria-label="Brand contract file"
            className="bg-codify-surface border border-codify-border rounded-xl p-5 max-w-md w-full shadow-2xl"
          >
            <h3 className="text-sm font-bold text-gray-100 mb-1 flex items-center gap-2">
              <Palette className="w-4 h-4 text-pink-400" /> Brand Contract
            </h3>
            <p className="text-xs text-gray-500 mb-3 leading-relaxed">
              The design agent reads this before it decides a direction, and
              treats it as binding. Leave it unpinned to let a{" "}
              <span className="font-mono">DESIGN.md</span> at the workspace root
              be found on its own.
            </p>
            <form onSubmit={handleSaveContract} className="space-y-3">
              <div>
                <label className="text-xs text-gray-400 block mb-1">
                  Path relative to{" "}
                  <span className="font-mono">{contractWs.root_path}</span>
                </label>
                <input
                  type="text"
                  placeholder="docs/DESIGN.md"
                  value={contractDraft}
                  onChange={(e) => setContractDraft(e.target.value)}
                  className="w-full bg-codify-bg border border-codify-border rounded-lg px-3 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-blue-500 font-mono"
                  aria-label="Brand contract path"
                />
              </div>
              {contractError && (
                <p className="text-xs text-red-400">{contractError}</p>
              )}
              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => setContractWs(null)}
                  className="px-3 py-1 bg-codify-raised text-gray-300 text-xs rounded-lg hover:bg-codify-border"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={handleUnpinContract}
                  disabled={contractSaving || !contractWs.design_contract_path}
                  className="px-3 py-1 bg-codify-raised text-gray-300 text-xs rounded-lg hover:bg-codify-border disabled:opacity-40"
                >
                  Unpin
                </button>
                <button
                  type="submit"
                  disabled={contractSaving}
                  className="px-3 py-1 bg-blue-600 text-white text-xs font-semibold rounded-lg hover:bg-blue-500 disabled:opacity-40"
                >
                  {contractSaving ? "Saving..." : "Save"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
};
