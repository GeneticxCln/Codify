import React, { useState, useRef, useEffect, useLayoutEffect } from "react";
import {
  Workspace,
  ModelOption,
  ProviderModelStatus,
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
  ArrowRight,
  Shield,
  Layers,
  Check,
  Cpu,
  RefreshCw,
  AlertCircle,
  Radio,
  Trash2,
  Palette,
  Square,
  type LucideIcon,
} from "lucide-react";
import { COMPOSER_ANCHOR_ID } from "../composerAnchor";

import { Toggle } from "./ui/Toggle";
import { Button } from "./ui/Button";
import { IconButton } from "./ui/IconButton";
import { MicButton } from "./MicButton";
import { ModelBadges } from "./ModelBadges";
import { readRejection } from "../rejection.ts";
import { insertDictation } from "../speech.ts";
import { insertAtCaret } from "../clipboardHistory";
import { clampPickerLeft } from "../threadMenu";
import { CUSTOM_MODEL_DESCRIPTION } from "../modelChoice";
import { uiScaleFactor, useUiScale } from "../uiScale";

export type ExecutionMode = "direct" | "dry_run" | "plan_only";

/**
 * A hairline between clusters of controls, carrying the cluster's meaning on hover.
 *
 * The toolbar sets its controls in one row at identical weight, which reads as independent knobs. They
 * are not peers: these differ in *kind* — what to work on and with (workspace, model), when the plan
 * starts (mode), and what the run keeps (Record). A divider is the cheapest thing that says "these are
 * not the same kind of thing", and it costs no width, which a caption in a toolbar this size would.
 *
 * There used to be three more controls here: Parallel, Design and Knowledge. A turn does not carry them
 * (`TurnCreate` has no run flags, docs/00 §6.8), so each armed a switch that nothing read. They were
 * removed rather than wired: whether steps run concurrently is the engine's call, and a deliverable is
 * something a person asks for in words.
 */
const Divider: React.FC<{ title: string }> = ({ title }) => (
  <span
    role="separator"
    aria-orientation="vertical"
    title={title}
    className="w-px self-stretch min-h-[1.25rem] my-0.5 bg-codify-border shrink-0"
  />
);

/**
 * The three ways a run may treat the files, once. The trigger and the menu both draw from this, so
 * the name on the button is the name in the list (the button said "Review Diffs" and the list said
 * "Review Diffs (Dry Run)"), and so is the icon. The icon's colour says what the mode *does* to your
 * files (green writes, amber only simulates, blue stops at a plan); *which one is current* is the
 * accent tint and the tick, as in every other list in the app (`DESIGN.md` §4, "Armed, selected, and
 * at rest").
 */
const MODES: ReadonlyArray<{
  id: ExecutionMode;
  label: string;
  hint: string;
  Icon: LucideIcon;
  iconClass: string;
}> = [
  { id: "direct", label: "Direct Apply", hint: "Edit files and run tests automatically", Icon: Sparkles, iconClass: "text-codify-success" },
  { id: "dry_run", label: "Review Diffs", hint: "Dry run: shows the changes, writes nothing", Icon: Shield, iconClass: "text-codify-warning" },
  { id: "plan_only", label: "Plan Only", hint: "Show the plan; you approve execution", Icon: Layers, iconClass: "text-codify-info" },
];

// Ordering and badges come from `modelSignals`, which is pure and shared with the
// Settings field — so an id sits in the same place in both pickers.

interface BottomCommandBarProps {
  workspaces: Workspace[];
  selectedWorkspace?: Workspace;
  onSelectWorkspace: (ws: Workspace) => void;
  onBrowseWorkspace: () => Promise<void>;
  onCreateWorkspace: (name: string, root_path: string) => Promise<void>;
  /** Whether the "enter a folder path" dialog is showing. Held by the app, which opens it itself when
   * the native folder dialog cannot. */
  manualWorkspaceOpen: boolean;
  onManualWorkspaceOpenChange: (open: boolean) => void;
  /** Confirms, then forgets the workspace and (after a second confirm) its goals. */
  onDeleteWorkspace: (workspaceId: string, name: string) => void;
  /**
   * Pin the workspace's brand contract, or clear it with `""`. Rejects with the
   * engine's own message when the path escapes the workspace or is not a
   * readable text file, so the dialog can say which file and why.
   */
  onSetDesignContract: (workspaceId: string, path: string) => Promise<void>;
  availableModels: ModelOption[];
  /** The model the conversation will run on, as `resolveModelChoice` decided: a pick, else the configured one. */
  selectedModel?: ModelOption;
  /** The model Settings gives the conversation, marked "default" in the menu. Absent when none is configured. */
  defaultModel?: ModelOption;
  /**
   * The window Codify asks Ollama for on the row that runs the conversation: `null` when none is set,
   * `undefined` when the engine did not say. Only an Ollama model's badge uses it (`contextBadge`).
   */
  conductorNumCtx?: number | null;
  onSelectModel: (model: ModelOption) => void;
  /** Per-provider discovery outcome, including failures. */
  modelStatus?: ProviderModelStatus[];
  /** What the app already knows: which roles use which model, and what ran last. */
  modelSignals?: ModelSignals;
  modelsLoading?: boolean;
  onRefreshModels: () => void;
  mode: ExecutionMode;
  onChangeMode: (mode: ExecutionMode) => void;
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
  /** Settings → Audio, where dictation gets its provider. The mic opens it when dictation has none. */
  onOpenAudioSettings: () => void;
  /**
   * Text the clipboard drawer asked to be put in the box, as it is, at the caret. A new `seq` is a new request:
   * the same text twice is two presses. One that was already there when this box mounted is not replayed.
   */
  insertRequest?: { seq: number; text: string } | null;
  /**
   * Take the keyboard when the box appears. On by default. A box that appears *unfocused* beside another pane in use
   * (the chat half of a split coming back when the window widens) says no, so it does not take the keyboard.
   */
  autoFocus?: boolean;
}

export const BottomCommandBar: React.FC<BottomCommandBarProps> = ({
  workspaces,
  selectedWorkspace,
  onSelectWorkspace,
  onBrowseWorkspace,
  onCreateWorkspace,
  manualWorkspaceOpen: isManualWsModal,
  onManualWorkspaceOpenChange: setIsManualWsModal,
  onDeleteWorkspace,
  onSetDesignContract,
  availableModels,
  selectedModel,
  defaultModel,
  conductorNumCtx,
  onSelectModel,
  modelStatus = [],
  modelSignals = EMPTY_SIGNALS,
  modelsLoading = false,
  onRefreshModels,
  mode,
  onChangeMode,
  record = false,
  onToggleRecord,
  onSubmit,
  isLoading,
  onStop,
  canStop = false,
  isRunning = false,
  onOpenSettings,
  onOpenAudioSettings,
  insertRequest = null,
  autoFocus = true,
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
      const left = clampPickerLeft(wrapRect.left, menu.getBoundingClientRect().width, window.innerWidth);
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
        // Cap the **panel**, not just the list, so the box comes out square.
        //
        // This used to be `Math.min(cap, 320)`, which capped only the list — and
        // `chrome` (header + filter input + the "Use Any Custom Model" footer +
        // padding) measured ~140px, so the panel rendered 320 wide by 460 tall:
        // a portrait slab, stretched because a height cap on *part* of a box is
        // not a cap on the box. Taking the list's allowance from the menu's own
        // width instead makes `chrome + list === width` by construction, which
        // is the only arithmetic here that guarantees squareness rather than
        // hoping the parts happen to add up.
        //
        // `chrome` is independent of the list's height — it is everything in the
        // panel that is not the list — so reading it here is stable across
        // frames rather than self-referential.
        const side = Math.round(menu.getBoundingClientRect().width);
        // Floor, not a guarantee: if the fixed chrome ever grows past the width,
        // a short-but-readable list beats an honest square of zero rows.
        const next = Math.max(120, Math.min(cap, side - chrome));
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

  // Auto-resize textarea. The cap is 180px at 100% and grows with the UI scale: the prompt's text is
  // rem, so a fixed cap would hold fewer lines the bigger the window is. The scale is a dependency
  // so a change re-measures instead of waiting for the next keystroke.
  const uiScale = useUiScale();
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      const cap = Math.round(180 * uiScaleFactor());
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, cap)}px`;
    }
  }, [prompt, uiScale]);

  // Dictated words go where the caret is, read when they *arrive*: the person may have typed on while
  // speaking. The updater form, because the transcript lands after an await and a `prompt` captured at
  // the click would drop whatever was typed since. The caret is put back after the commit, at the end
  // of the inserted words, so typing can carry straight on.
  const dictatedCaret = useRef<number | null>(null);
  const insertTranscript = (spoken: string) => {
    const el = textareaRef.current;
    setPrompt((current) => {
      const start = el?.selectionStart ?? current.length;
      const end = el?.selectionEnd ?? current.length;
      const next = insertDictation(current, start, end, spoken);
      dictatedCaret.current = next.caret;
      return next.value;
    });
  };
  // A clip from the clipboard drawer goes in the same way, but exactly as it is: no space is added where two
  // words would touch, because a clip is code or a path and a space inside one is a different command. The
  // request already pending when this box mounted is marked handled up front, so a box that is rebuilt (a
  // tab away and back) does not replay an insert that was done into the last one.
  const handledInsert = useRef(insertRequest?.seq ?? 0);
  useEffect(() => {
    if (!insertRequest || insertRequest.seq === handledInsert.current) return;
    handledInsert.current = insertRequest.seq;
    const el = textareaRef.current;
    setPrompt((current) => {
      const next = insertAtCaret(
        current,
        el?.selectionStart ?? current.length,
        el?.selectionEnd ?? current.length,
        insertRequest.text,
      );
      dictatedCaret.current = next.caret;
      return next.value;
    });
  }, [insertRequest]);
  useLayoutEffect(() => {
    const caret = dictatedCaret.current;
    if (caret === null || !textareaRef.current) return;
    dictatedCaret.current = null;
    textareaRef.current.focus();
    textareaRef.current.setSelectionRange(caret, caret);
  }, [prompt]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // `isComposing` matters for any input method that confirms with Enter (CJK
    // input methods such as ibus and fcitx): sending there truncates the word
    // being composed and dispatches a goal on half of it.
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
      setContractError(readRejection(err, "Failed to pin the brand contract"));
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
      setContractError(readRejection(err, "Failed to unpin the brand contract"));
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
      description: CUSTOM_MODEL_DESCRIPTION,
    };
    onSelectModel(custom);
    setCustomModelId("");
    setIsModelOpen(false);
  };

  return (
    <div className="w-full max-w-4xl mx-auto p-4 z-20">
      <div
        ref={barRef}
        className="bg-codify-surface border border-codify-border rounded-2xl shadow-2xl overflow-visible focus-within:border-codify-accent transition-all duration-200"
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
                    ? "bg-codify-raised border-codify-border text-codify-secondary hover:bg-codify-border"
                    : "bg-codify-info/40 border-codify-info text-codify-info-ink hover:brightness-110"
                }`}
                title={selectedWorkspace?.root_path || "Select project folder"}
              >
                <Folder className="w-3.5 h-3.5 text-codify-accent" />
                <span className="font-medium max-w-[9.375rem] truncate">
                  {selectedWorkspace
                    ? selectedWorkspace.name
                    : "Select Project Folder"}
                </span>
                <ChevronDown className="w-3 h-3 text-codify-muted" />
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
                  <div className="text-xs font-semibold text-codify-muted px-2 py-1 uppercase tracking-wider">
                    Workspaces
                  </div>

                  <div className="p-1 mb-1">
                    <button
                      type="button"
                      onClick={handleNativeBrowse}
                      className="w-full flex items-center justify-center gap-2 px-3 py-2 bg-codify-accent text-codify-bg hover:brightness-110 rounded-lg text-xs font-semibold transition-colors shadow-sm"
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
                              ? "bg-codify-accent/20 text-codify-accent-ink border border-codify-accent/30"
                              : "text-codify-secondary hover:bg-codify-raised"
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
                              <div className="text-2xs text-codify-muted truncate font-mono">
                                {ws.root_path}
                              </div>
                            </div>
                          </button>
                          {selectedWorkspace?.id === ws.id && (
                            <Check className="w-3.5 h-3.5 text-codify-accent shrink-0" />
                          )}
                          <button
                            type="button"
                            onClick={() => openContractDialog(ws)}
                            className={`p-1 rounded-lg transition-colors shrink-0 ${
                              ws.design_contract_path
                                ? "text-codify-design hover:text-codify-design"
                                : "text-codify-muted hover:text-codify-secondary"
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
                            className="p-1 mr-1.5 text-codify-muted hover:text-codify-danger rounded-lg transition-colors shrink-0"
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
                      className="w-full text-center text-xs text-codify-muted hover:text-codify-secondary py-1"
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
                className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-codify-raised border border-codify-border text-codify-secondary hover:bg-codify-border transition-colors cursor-pointer"
                title={
                  selectedModel
                    ? `${selectedModel.provider}/${selectedModel.id}\nAnswers this conversation and runs its tool loop. The planner, fixer and other roles keep the models set in Settings → Agents.`
                    : "No model"
                }
              >
                <Cpu className="w-3.5 h-3.5 text-codify-accent" />
                <span className="font-medium truncate max-w-[9.375rem]">
                  {selectedModel ? selectedModel.name : "No model"}
                </span>
                <ChevronDown className="w-3 h-3 text-codify-muted" />
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
                    <span className="text-xs font-semibold text-codify-muted uppercase tracking-wider">
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
                        className="flex items-center gap-1 text-xs text-codify-muted hover:text-codify-secondary disabled:opacity-50 cursor-pointer"
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
                        className="text-xs text-codify-info hover:underline cursor-pointer"
                      >
                        API Keys
                      </button>
                    </div>
                  </div>

                  <p className="px-2 pb-1.5 text-2xs leading-snug text-codify-muted">
                    Answers this conversation and runs its tool loop. The planner, fixer and other roles keep the
                    models set in Settings → Agents.
                  </p>

                  {availableModels.length > 0 && (
                    <div className="px-1 pb-1">
                      <input
                        type="text"
                        value={modelFilter}
                        onChange={(e) => setModelFilter(e.target.value)}
                        placeholder={`Filter ${availableModels.length} models…`}
                        className="w-full bg-codify-bg border border-codify-border rounded-lg px-2.5 py-1 text-xs text-codify-secondary focus:outline-hidden focus:border-codify-accent"
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
                      <div className="px-2.5 py-3 text-xs text-codify-muted leading-relaxed">
                        {modelsLoading
                          ? "Asking each provider what it serves..."
                          : "No models yet. Add an API key under API Keys, or start a local Ollama server — its models appear automatically."}
                      </div>
                    )}

                    {availableModels.length > 0 && shownModelCount === 0 && (
                      <div className="px-2.5 py-3 text-xs text-codify-muted">
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
                              ? "text-codify-accent"
                              : "text-codify-muted")
                          }
                        >
                          {section.label} ({section.models.length})
                        </div>
                        {section.models.map((m) => {
                          const isCurrent =
                            !!selectedModel &&
                            selectedModel.id === m.id &&
                            selectedModel.provider === m.provider;
                          const badges = modelBadges(m, modelSignals, 3, {
                            numCtx: conductorNumCtx,
                            isDefault:
                              !!defaultModel && defaultModel.id === m.id && defaultModel.provider === m.provider,
                          });
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
                                "w-full text-left px-2.5 py-1.5 rounded-lg border text-xs transition-colors cursor-pointer " +
                                (isCurrent
                                  ? "bg-codify-accent/20 text-codify-accent-ink border-codify-accent/30 font-medium"
                                  : "border-transparent text-codify-secondary hover:bg-codify-raised")
                              }
                            >
                              {/* Line one is the name and what is true of it, as badges; line two is
                                  the provider's own description. The description used to sit beside
                                  the name and was cut to "Local Ollama model…", and was dropped
                                  altogether whenever a row had a role badge. */}
                              <span className="flex items-center gap-2">
                                <span className="font-semibold truncate flex-1 min-w-0">
                                  {m.name}
                                </span>
                                <ModelBadges badges={badges} />
                                {isCurrent && (
                                  <Check className="w-3 h-3 text-codify-accent shrink-0" />
                                )}
                              </span>
                              {m.description && (
                                <span className="mt-0.5 block truncate text-2xs font-normal text-codify-muted">
                                  {m.description}
                                </span>
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
                          className="flex items-start gap-1.5 px-2.5 py-1 text-2xs text-codify-warning"
                        >
                          <AlertCircle className="w-3 h-3 shrink-0 mt-0.5" />
                          <span className="truncate" title={`${p.provider} — ${p.error}`}>
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
                    <div className="text-2xs text-codify-muted mb-1 px-1">
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
                        className="flex-1 bg-codify-bg border border-codify-border rounded-lg px-2.5 py-1 text-xs text-codify-secondary focus:outline-hidden focus:border-codify-accent font-mono"
                      />
                      <Button
                        type="submit"
                        tone="primary"
                        size="sm"
                        disabled={!customModelId.trim()}
                      >
                        Set
                      </Button>
                    </div>
                    {customModelError && (
                      <p className="text-2xs text-codify-danger px-1 mt-1">
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
                className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-codify-raised border border-codify-border text-codify-secondary hover:bg-codify-border transition-colors cursor-pointer"
              >
                {(() => {
                  const current = MODES.find((m) => m.id === mode) ?? MODES[0]!;
                  return (
                    <>
                      <current.Icon className={`w-3.5 h-3.5 ${current.iconClass}`} />
                      <span>{current.label}</span>
                    </>
                  );
                })()}
                <ChevronDown className="w-3 h-3 text-codify-muted" />
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
                  className="absolute left-0 bottom-full mb-2 w-72 bg-codify-surface border border-codify-border rounded-xl shadow-2xl p-1.5 z-50 space-y-0.5"
                >
                  {MODES.map((m) => {
                    const isCurrent = m.id === mode;
                    return (
                      <button
                        key={m.id}
                        type="button"
                        onClick={() => {
                          onChangeMode(m.id);
                          setIsModeOpen(false);
                        }}
                        className={
                          "w-full text-left px-2.5 py-1.5 rounded-lg border text-xs flex items-start gap-2 cursor-pointer " +
                          (isCurrent
                            ? "bg-codify-accent/20 text-codify-accent-ink border-codify-accent/30"
                            : "border-transparent text-codify-secondary hover:bg-codify-raised")
                        }
                      >
                        <m.Icon className={`mt-0.5 w-3.5 h-3.5 shrink-0 ${m.iconClass}`} />
                        <span className="flex-1 min-w-0">
                          <span className="block font-semibold">{m.label}</span>
                          <span className="block text-2xs font-normal text-codify-muted">{m.hint}</span>
                        </span>
                        {isCurrent && (
                          <Check className="mt-0.5 w-3.5 h-3.5 text-codify-accent shrink-0" />
                        )}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>

            <Divider title="What this run keeps" />

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
            autoFocus={autoFocus}
            // The other end of the empty state's skip link. A fragment link
            // moves focus to the element it names, and a textarea is focusable,
            // so no `tabIndex` is needed here and adding one would be wrong: it
            // would put this control in the tab order a second time. See
            // `composerAnchor.ts` for why the id is a shared constant.
            id={COMPOSER_ANCHOR_ID}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={isLoading}
            // Short, because a placeholder does not wrap: the long sentence was cut mid-word in a narrow
            // column. What the composer can be asked is still said, in full, to a pointer and to a screen
            // reader, which read the title as the field's description.
            placeholder="Ask Codify to build, fix or refactor…"
            title="Ask Codify to build, edit files, fix tests, or refactor code"
            aria-label="Chat prompt"
            className="flex-1 min-w-0 bg-transparent text-codify-primary placeholder-codify-muted text-sm resize-none focus:outline-hidden leading-relaxed cursor-text"
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
          <div className="flex items-center gap-2 shrink-0">
            {/* Dictation, left of the send slot: it fills the prompt, it never sends it. */}
            <MicButton disabled={isLoading} onTranscript={insertTranscript} onNeedsSetup={onOpenAudioSettings} />
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
                className="w-8 h-8 shadow-sm"
              >
                {isLoading ? (
                  /* The spinner sits *on* the accent fill, so it is drawn in the
                     same ink as the label beside it — `border-white` on a themed
                     button was a white ring on a magenta one, and would have
                     been a white ring on a near-white one. */
                  <div className="w-4 h-4 border-2 border-codify-bg/30 border-t-codify-bg rounded-full animate-spin" />
                ) : (
                  /* Right, not up. The prompt is a field you type into and send
                     horizontally, and an up-arrow reads as "grow", which is what
                     it does everywhere else in the OS. */
                  <ArrowRight className="w-4 h-4" />
                )}
              </IconButton>
            )}
          </div>
        </div>
      </div>

      {/* Manual Workspace Path Modal */}
      {isManualWsModal && (
        <div className="fixed inset-0 bg-black/70 backdrop-blur-xs z-50 flex items-center justify-center p-4">
          <div
            role="dialog"
            aria-modal="true"
            aria-label="Enter workspace path"
            className="bg-codify-surface border border-codify-border rounded-xl p-5 max-w-md w-full shadow-2xl"
          >
            <h3 className="text-sm font-bold text-codify-primary mb-3 flex items-center gap-2">
              <Folder className="w-4 h-4 text-codify-info" /> Enter Workspace Path
            </h3>
            <form onSubmit={handleCreateManualWs} className="space-y-3">
              <div>
                <label className="text-xs text-codify-muted block mb-1">
                  Project Name
                </label>
                <input
                  type="text"
                  placeholder="e.g. My Next.js App"
                  value={wsName}
                  onChange={(e) => setWsName(e.target.value)}
                  className="w-full bg-codify-bg border border-codify-border rounded-lg px-3 py-1.5 text-xs text-codify-secondary focus:outline-hidden focus:border-codify-info"
                  required
                />
              </div>
              <div>
                <label className="text-xs text-codify-muted block mb-1">
                  Absolute Directory Path
                </label>
                <input
                  type="text"
                  placeholder="/home/you/Projects/my-app"
                  value={wsPath}
                  onChange={(e) => setWsPath(e.target.value)}
                  className="w-full bg-codify-bg border border-codify-border rounded-lg px-3 py-1.5 text-xs text-codify-secondary focus:outline-hidden focus:border-codify-info font-mono"
                  required
                />
              </div>
              {wsError && <p className="text-xs text-codify-danger">{wsError}</p>}
              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => setIsManualWsModal(false)}
                  className="px-3 py-1 bg-codify-raised text-codify-secondary text-xs rounded-lg hover:bg-codify-border"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="px-3 py-1 bg-codify-accent text-codify-bg text-xs font-semibold rounded-lg hover:brightness-110"
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
        <div className="fixed inset-0 bg-black/70 backdrop-blur-xs z-50 flex items-center justify-center p-4">
          <div
            role="dialog"
            aria-modal="true"
            aria-label="Brand contract file"
            className="bg-codify-surface border border-codify-border rounded-xl p-5 max-w-md w-full shadow-2xl"
          >
            <h3 className="text-sm font-bold text-codify-primary mb-1 flex items-center gap-2">
              <Palette className="w-4 h-4 text-codify-design" /> Brand Contract
            </h3>
            <p className="text-xs text-codify-muted mb-3 leading-relaxed">
              The design agent reads this before it decides a direction, and
              treats it as binding. Leave it unpinned to let a{" "}
              <span className="font-mono">DESIGN.md</span> at the workspace root
              be found on its own.
            </p>
            <form onSubmit={handleSaveContract} className="space-y-3">
              <div>
                <label className="text-xs text-codify-muted block mb-1">
                  Path relative to{" "}
                  <span className="font-mono">{contractWs.root_path}</span>
                </label>
                <input
                  type="text"
                  placeholder="docs/DESIGN.md"
                  value={contractDraft}
                  onChange={(e) => setContractDraft(e.target.value)}
                  className="w-full bg-codify-bg border border-codify-border rounded-lg px-3 py-1.5 text-xs text-codify-secondary focus:outline-hidden focus:border-codify-info font-mono"
                  aria-label="Brand contract path"
                />
              </div>
              {contractError && (
                <p className="text-xs text-codify-danger">{contractError}</p>
              )}
              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => setContractWs(null)}
                  className="px-3 py-1 bg-codify-raised text-codify-secondary text-xs rounded-lg hover:bg-codify-border"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={handleUnpinContract}
                  disabled={contractSaving || !contractWs.design_contract_path}
                  className="px-3 py-1 bg-codify-raised text-codify-secondary text-xs rounded-lg hover:bg-codify-border disabled:opacity-40"
                >
                  Unpin
                </button>
                <button
                  type="submit"
                  disabled={contractSaving}
                  className="px-3 py-1 bg-codify-accent text-codify-bg text-xs font-semibold rounded-lg hover:brightness-110 disabled:opacity-40"
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
