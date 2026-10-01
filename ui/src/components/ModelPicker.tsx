/**
 * A searchable, scrollable list of models. The one dropdown, used in two places.
 *
 * A role card's "Model Identifier" and a provider row's picker want the same
 * control and, until this existed, only one of them had a menu at all — so the
 * provider screen asked people to pick a model from a free-text field while the
 * role screen beside it had a browsable list of the same models. Two fields, one
 * catalogue, and a user who could not tell which one was the real chooser.
 *
 * The differences between the two uses are all props: a role wants to see which
 * other roles run a model, a provider row does not; a provider row's list is
 * this provider's only, so it can afford to be wide.
 *
 * **Search, not a datalist.** `<datalist>` shows nothing until you type a
 * matching prefix, and model ids start `meta-llama/`, `hf.co/zaakirio/`,
 * `accounts/fireworks/models/` — nobody guesses the prefix, so every role looked
 * like it had no models at all. The list opens on the click and searches as you
 * type, over id, name and description (see `searchModels`).
 *
 * **Scroll, and say how much is hidden.** A provider serving 200 models cannot
 * show 200 rows, and a panel that silently cuts off at twelve is a provider
 * that looks like it has twelve. The count is in the header and the rest is
 * reached by scrolling.
 *
 * **New models float to the top.** A provider that released something this
 * morning puts it in this list, and a list of two hundred is not somewhere a
 * release announces itself. `newIds` says which ids the reader had not been
 * shown before; those are badged and listed first.
 */

import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, RefreshCw, Search } from "lucide-react";
import type { ModelOption } from "../types";
import { MENU_EDGE, MenuPlacement, menuPlacement } from "../modelMenu";
import { countLabel, searchModels } from "../providerSetup";
import { newFirst } from "../modelFreshness";

export interface ModelPickerProps {
  provider: string;
  /** The chosen id, shown in the field. Empty means nothing chosen yet. */
  value: string;
  onChange: (id: string) => void;
  /** This provider's discovered models. Never a compiled-in list (docs/06). */
  options: readonly ModelOption[];
  /**
   * Ids this provider did not serve the last time the reader was here.
   *
   * They are badged and floated to the top, because a marker on row 170 of a
   * scroll box is a marker nobody scrolls to. See `modelFreshness`.
   */
  newIds?: readonly string[];
  /** Why discovery returned nothing, when it did. */
  discoveryError?: string | null;
  /** Ask the provider again. */
  onRefresh?: () => void;
  refreshing?: boolean;
  /** Placeholder for the field when nothing is chosen. */
  placeholder?: string;
  disabled?: boolean;
  /** Called with the id the reader chose, for a row that also wants to react. */
  onPick?: (id: string) => void;
}

const DEFAULT_PLACEHOLDER = "Choose a model, or type one";

export const ModelPicker: React.FC<ModelPickerProps> = ({
  provider,
  value,
  onChange,
  options,
  newIds = [],
  discoveryError = null,
  onRefresh,
  refreshing = false,
  placeholder = DEFAULT_PLACEHOLDER,
  disabled = false,
  onPick,
}) => {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [placement, setPlacement] = useState<MenuPlacement | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const fieldRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  const close = () => {
    setOpen(false);
    setQuery("");
    setPlacement(null);
  };

  const measure = () => {
    const field = fieldRef.current;
    if (!field) return;
    setPlacement(
      menuPlacement(field.getBoundingClientRect(), {
        width: window.innerWidth,
        height: window.innerHeight,
      }),
    );
  };

  // Measure on open, and again when the list changes length: a panel measured
  // against an empty list has nothing to clamp to, and the ids only arrive with
  // the discovery response.
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

  // The search box takes the keystrokes as soon as the menu opens, so typing
  // goes to the filter rather than into the id field behind it. Without this,
  // "llama" would rewrite the chosen model id one character at a time and filter
  // nothing.
  useEffect(() => {
    if (open) searchRef.current?.focus();
  }, [open]);

  const matches = useMemo(() => {
    // Search first, then float: a new model that does not match the query is
    // not in the list at all, so the order of those two steps is not a choice.
    const found = searchModels(options, query);
    return newFirst(found, newIds);
  }, [options, query, newIds]);

  const isNew = useMemo(() => new Set(newIds), [newIds]);
  /** How many of the *visible* rows are new, so the header counts what is on screen. */
  const newHere = useMemo(
    () => matches.filter((m) => isNew.has(m.id)).length,
    [matches, isNew],
  );

  const choose = (id: string) => {
    onChange(id);
    onPick?.(id);
    close();
  };

  const emptyMessage = discoveryError
    ? `Could not reach ${provider} — ${discoveryError}`
    : options.length === 0
      ? `${provider} reported no models. Type an id and press Enter if you know it.`
      : `Nothing matches “${query.trim()}”. Every discovered model is listed — clear the search to see them.`;

  const menu = placement && (
    <div
      ref={listRef}
      style={{
        position: "fixed",
        left: Math.max(
          MENU_EDGE,
          Math.min(placement.left, window.innerWidth - placement.width - MENU_EDGE),
        ),
        top: placement.above ? undefined : placement.top,
        bottom: placement.above ? window.innerHeight - placement.top : undefined,
        width: placement.width,
        maxHeight: placement.maxHeight,
      }}
      className="z-[60] flex flex-col bg-codify-surface border border-codify-border rounded-xl shadow-2xl overflow-hidden"
    >
      <div className="flex items-center gap-1.5 px-2 py-1.5 border-b border-codify-border flex-shrink-0">
        <Search className="w-3 h-3 text-codify-muted flex-shrink-0" />
        <input
          ref={searchRef}
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search models"
          aria-label={`Search ${provider} models`}
          className="flex-1 min-w-0 bg-transparent text-xs text-codify-secondary placeholder-codify-muted focus:outline-none"
        />
        {onRefresh && (
          <button
            type="button"
            onClick={onRefresh}
            disabled={refreshing}
            title={`Ask ${provider} what it serves right now`}
            className="flex items-center gap-1 text-2xs text-codify-muted hover:text-codify-secondary disabled:opacity-50 cursor-pointer flex-shrink-0"
          >
            <RefreshCw className={refreshing ? "w-2.5 h-2.5 animate-spin" : "w-2.5 h-2.5"} />
            Refresh
          </button>
        )}
      </div>

      <div className="px-2 py-1 text-2xs font-semibold uppercase tracking-wider text-codify-muted flex-shrink-0 flex items-center gap-2">
        <span>{countLabel(matches.length, options.length)}</span>
        {newHere > 0 && (
          <span className="text-codify-design normal-case tracking-normal font-medium">
            {newHere} new since you last looked
          </span>
        )}
      </div>

      {matches.length === 0 ? (
        <div className="px-2.5 pb-2.5 text-xs text-codify-muted leading-relaxed">{emptyMessage}</div>
      ) : (
        <div className="overflow-y-auto min-h-0">
          {matches.map((m) => (
            <button
              key={`${m.provider}:${m.id}`}
              type="button"
              title={[m.id, m.description, m.supports_chat === false ? "not a chat model" : null]
                .filter(Boolean)
                .join("\n") +
                (isNew.has(m.id) ? "\nnew since you last looked" : "")}
              onClick={() => choose(m.id)}
              className={
                "w-full text-left px-2.5 py-1.5 flex items-center gap-2 text-xs transition-colors cursor-pointer " +
                (m.id === value
                  ? "bg-codify-info/20 text-codify-info-ink font-medium"
                  : "text-codify-secondary hover:bg-codify-raised")
              }
            >
              <span className="font-mono truncate flex-1 min-w-0">{m.id}</span>
              {isNew.has(m.id) && (
                <span
                  title="Not on this provider when you last looked"
                  className="text-2xs font-medium text-codify-design-ink bg-codify-design/20 border border-codify-design/60 px-1.5 py-px rounded-full flex-shrink-0"
                >
                  new
                </span>
              )}
              {m.supports_chat === false && (
                <span className="text-2xs text-codify-warning flex-shrink-0">not chat</span>
              )}
              {m.id === value && <Check className="w-3 h-3 text-codify-info flex-shrink-0" />}
            </button>
          ))}
        </div>
      )}
    </div>
  );

  return (
    <div ref={wrapRef} className="relative min-w-0">
      <div ref={fieldRef} className="flex items-stretch">
        {/* Free text, because a provider can serve a model its own catalogue does
            not list yet — a new release, a private fine-tune, an alias. A
            dropdown-only field would make those unreachable, and "not in the list"
            is not the same claim as "does not exist". */}
        <input
          type="text"
          placeholder={placeholder}
          value={value}
          disabled={disabled}
          onChange={(e) => {
            onChange(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={(e) => {
            if (e.key === "Enter") close();
          }}
          className="flex-1 min-w-0 bg-codify-surface border border-codify-border rounded-l-lg px-3 py-1.5 text-xs text-codify-secondary focus:outline-none focus:border-codify-accent font-mono disabled:opacity-50"
        />
        <button
          type="button"
          aria-label={`Browse ${provider} models`}
          aria-expanded={open}
          disabled={disabled}
          onClick={() => (open ? close() : setOpen(true))}
          title={
            options.length === 0
              ? `${provider} has not reported any models`
              : `Browse ${options.length} ${provider} models`
          }
          className="flex items-center gap-1 px-2.5 py-1.5 bg-codify-raised border border-l-0 border-codify-border rounded-r-lg text-codify-secondary hover:bg-codify-border transition-colors disabled:opacity-40 cursor-pointer disabled:cursor-not-allowed flex-shrink-0"
        >
          <ChevronDown className={open ? "w-3.5 h-3.5 rotate-180" : "w-3.5 h-3.5"} />
        </button>
      </div>
      {menu}
    </div>
  );
};
