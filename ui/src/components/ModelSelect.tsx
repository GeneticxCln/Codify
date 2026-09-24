import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Check, ChevronDown, RefreshCw } from "lucide-react";
import { ModelOption, ProviderModelStatus } from "../types";
import { EMPTY_SIGNALS, ModelSignals, modelBadges, orderProviderModels } from "../modelSignals";

interface ModelSelectProps {
  provider: string;
  value: string;
  onChange: (val: string) => void;
  /** Models the engine discovered for this provider, if any. */
  options?: ModelOption[];
  /** What discovery actually returned for this provider, including failures. */
  discovery?: ProviderModelStatus;
  /** Ask every provider again — the list is cached for a minute at most. */
  onRefresh?: () => void;
  refreshing?: boolean;
  /**
   * Which other roles already run each model, and which one ran last. Same
   * signals as the chat's menu, so an id carries the same badges in both places.
   */
  signals?: ModelSignals;
}

interface MenuPlacement {
  left: number;
  top: number;
  width: number;
  /** null = open below the field. */
  maxHeight: number;
  above: boolean;
}

/**
 * Model identifier field.
 *
 * Free text on purpose — a provider can serve a model its own `/models` list
 * does not mention yet (a brand-new release, a private fine-tune, an alias), and
 * a dropdown-only field would make those unreachable.
 *
 * The discovered models are browsable, though: click the field (or the caret)
 * and the provider's whole list opens, scrollable, newest first. This used to be
 * an HTML `<datalist>`, which shows nothing at all until you type a matching
 * prefix — with ids like `hf.co/zaakirio/…-GGUF:Q8_0` nobody guesses the prefix,
 * so every role looked like it had no model list.
 */
export const ModelSelect: React.FC<ModelSelectProps> = ({
  provider,
  value,
  onChange,
  options = [],
  discovery,
  onRefresh,
  refreshing = false,
  signals = EMPTY_SIGNALS,
}) => {
  const [open, setOpen] = useState(false);
  // Typing narrows the list; opening the menu on purpose shows all of it.
  const [browsing, setBrowsing] = useState(false);
  const [placement, setPlacement] = useState<MenuPlacement | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const fieldRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const close = () => {
    setOpen(false);
    setBrowsing(false);
    setPlacement(null);
  };

  const measure = () => {
    const field = fieldRef.current;
    if (!field) return;
    const rect = field.getBoundingClientRect();
    const below = window.innerHeight - rect.bottom - 12;
    const above = rect.top - 12;
    const openAbove = below < 200 && above > below;
    const space = openAbove ? above : below;
    setPlacement({
      left: rect.left,
      top: openAbove ? rect.top : rect.bottom,
      width: rect.width,
      maxHeight: Math.max(96, Math.min(260, space)),
      above: openAbove,
    });
  };

  useLayoutEffect(() => {
    if (open) measure();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, options.length]);

  // A menu drawn over a scrolling panel has to follow the field it points at.
  useEffect(() => {
    if (!open) return;
    const onMove = () => measure();
    window.addEventListener("resize", onMove);
    window.addEventListener("scroll", onMove, true);
    return () => {
      window.removeEventListener("resize", onMove);
      window.removeEventListener("scroll", onMove, true);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (wrapRef.current?.contains(target) || listRef.current?.contains(target)) return;
      close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const typed = value.trim().toLowerCase();
  const isExact = options.some((m) => m.id.toLowerCase() === typed);
  const query = browsing || isExact ? "" : typed;
  // Ordered by the shared rules: the stored model leads, then chat-capable ids
  // newest-first, then anything the provider marks as non-chat.
  const matches = orderProviderModels(options, {
    selected: { provider, id: value },
    filter: query,
  });

  // Say what actually happened. "No models yet — add its API key" is wrong for a
  // local provider, which needs no key at all, and it sends the user to a
  // setting that cannot fix anything.
  const note = (() => {
    if (options.length > 0) {
      const chosen = options.find((m) => m.id === value);
      const chat = chosen?.supports_chat === false ? " · this one is not a chat model" : "";
      return `${options.length} models discovered for ${provider}${chat}`;
    }
    if (discovery && !discovery.ok) return `No models discovered for ${provider} — ${discovery.error}`;
    if (discovery?.ok) return `${provider} answered, but reports no models.`;
    return `No model list for ${provider} yet — refresh the model picker to ask it again.`;
  })();

  const choose = (id: string) => {
    onChange(id);
    close();
  };

  const menu = placement && (
    <div
      ref={listRef}
      style={{
        position: "fixed",
        left: Math.max(8, Math.min(placement.left, window.innerWidth - placement.width - 8)),
        top: placement.above ? undefined : placement.top + 4,
        bottom: placement.above ? window.innerHeight - placement.top + 4 : undefined,
        width: placement.width,
        maxHeight: placement.maxHeight,
      }}
      className="z-[60] bg-[#161b22] border border-[#30363d] rounded-xl shadow-2xl p-1.5 overflow-y-auto"
    >
      {options.length === 0 ? (
        <div className="px-2 py-2 text-[11px] text-gray-400 leading-relaxed">
          {discovery && !discovery.ok
            ? `Nothing to pick from: ${discovery.error}`
            : "This provider reports no models. Type the id and press Enter if you know it."}
        </div>
      ) : matches.length === 0 ? (
        <div className="px-2 py-2 text-[11px] text-gray-400 leading-relaxed">
          Nothing matches “{value.trim()}”. Every discovered model is listed — clear the field to
          see them, or press Enter to use this id as written.
        </div>
      ) : (
        <>
          <div className="flex items-center justify-between px-2 py-1">
            <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-500">
              {matches.length === options.length
                ? `${options.length} discovered`
                : `${matches.length} of ${options.length}`}
            </span>
            {onRefresh && (
              <button
                type="button"
                onClick={onRefresh}
                disabled={refreshing}
                title="Ask this provider what it serves right now"
                className="flex items-center gap-1 text-[10px] text-gray-400 hover:text-gray-200 disabled:opacity-50 cursor-pointer"
              >
                <RefreshCw className={refreshing ? "w-2.5 h-2.5 animate-spin" : "w-2.5 h-2.5"} />
                Refresh
              </button>
            )}
          </div>
          {matches.map((m) => {
            const badges = modelBadges(m, signals);
            return (
              <button
                key={`${m.provider}:${m.id}`}
                type="button"
                title={
                  `${m.id}` +
                  (badges.rolesTitle ? `\n${badges.rolesTitle}` : "") +
                  (badges.lastRunTitle ? `\n${badges.lastRunTitle}` : "") +
                  (m.description ? `\n${m.description}` : "")
                }
                onClick={() => choose(m.id)}
                className={
                  "w-full text-left px-2 py-1 rounded-lg flex items-center gap-2 text-xs transition-colors cursor-pointer " +
                  (m.id === value
                    ? "bg-blue-600/20 text-blue-200 font-medium"
                    : "text-gray-300 hover:bg-[#21262d]")
                }
              >
                <span className="font-mono truncate flex-1 min-w-0">{m.id}</span>
                {badges.roles && (
                  <span
                    className="text-[10px] text-teal-300/90 flex-shrink-0 max-w-[9rem] truncate"
                    title={badges.rolesTitle}
                  >
                    {badges.roles}
                  </span>
                )}
                {badges.lastRun && (
                  <span className="text-[10px] text-blue-300/90 flex-shrink-0">last run</span>
                )}
                {badges.notChat ? (
                  <span className="text-[10px] text-amber-400/80 flex-shrink-0">not a chat model</span>
                ) : (
                  !badges.roles &&
                  m.description && (
                    <span className="text-[10px] text-gray-500 truncate max-w-[110px] flex-shrink-0">
                      {m.description}
                    </span>
                  )
                )}
                {m.id === value && <Check className="w-3 h-3 text-blue-400 flex-shrink-0" />}
              </button>
            );
          })}
        </>
      )}
    </div>
  );

  return (
    <div className="flex flex-col gap-1.5">
      <label className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
        Model Identifier
      </label>
      <div ref={wrapRef} className="relative">
        <div ref={fieldRef} className="flex items-center gap-1.5">
          <input
            type="text"
            placeholder="Start typing, or click the arrow to browse this provider's models"
            value={value}
            onFocus={() => {
              if (options.length > 0) {
                setBrowsing(true);
                setOpen(true);
              }
            }}
            onChange={(e) => {
              setBrowsing(false);
              onChange(e.target.value);
              setOpen(options.length > 0);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                close();
              }
            }}
            className="flex-1 bg-[#0d1117] border border-[#30363d] rounded px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-blue-500 font-mono"
          />
          <button
            type="button"
            aria-label="Browse discovered models"
            disabled={options.length === 0}
            onClick={() => {
              if (open) close();
              else {
                setBrowsing(true);
                setOpen(true);
              }
            }}
            title={
              options.length === 0
                ? "This provider reported no models"
                : `Browse ${options.length} discovered models`
            }
            className="flex items-center gap-1 px-2 py-2 bg-[#21262d] border border-[#30363d] rounded text-gray-300 hover:bg-[#30363d] transition-colors disabled:opacity-40 cursor-pointer disabled:cursor-not-allowed"
          >
            <ChevronDown className={open ? "w-3.5 h-3.5 rotate-180" : "w-3.5 h-3.5"} />
          </button>
        </div>
        {menu}
      </div>
      <span
        className={`text-[10px] ${discovery && !discovery.ok ? "text-amber-400/90" : "text-gray-500"}`}
      >
        {note}
      </span>
    </div>
  );
};
