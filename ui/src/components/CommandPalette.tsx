import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  filterPalette,
  moveSelection,
  type PaletteItem,
  type PaletteItemKind,
} from "../commandPalette";

/**
 * Ctrl+K: search open tabs, conversations and settings in one input.
 *
 * The model — what the list contains, how a query narrows it, where the
 * selection wraps to — lives in `commandPalette.ts` and is tested there.
 * This component is the thin half: render the matches as a proper
 * combobox/listbox, forward the keys the model answers, and hold focus
 * correctly. Two focus decisions are worth naming because they are invisible:
 *
 * - **Focus never leaves the input while the palette is open.** A mousedown
 *   inside the panel is prevented — clicking a row would otherwise blur the
 *   input to `<body>`, and an Escape typed at `<body>` would bubble to the
 *   window, where `SettingsModal`'s own Escape-close listens: the user would
 *   close both the palette *and* the dialog underneath it with one key.
 *   Preventing the default keeps the input as the event target, so the
 *   input's `stopPropagation` is what every Escape and arrow key meets.
 * - **Focus comes back on close.** The composer (or whichever control) that
 *   had focus when Ctrl+K was pressed is refocused, so closing the palette puts
 *   the cursor back where the typing was instead of on nothing. Best effort:
 *   a detached node's `focus()` is silently ignored, which is the right
 *   behaviour for a control that re-rendered away.
 *
 * Arrow keys and Enter are handled here and stop their propagation; the
 * shell-level shortcuts in `App.tsx` deliberately do not claim them —
 * `resolveShortcut` only fires on modified keys — so the two layers cannot
 * both answer one keystroke.
 */

export interface CommandPaletteProps {
  open: boolean;
  items: PaletteItem[];
  onClose: () => void;
  onSelect: (item: PaletteItem) => void;
}

/** The heading above each group. One place, so the strip order and the palette agree. */
const GROUP_HEADING: Record<PaletteItemKind, string> = {
  tab: "Open tabs",
  conversation: "Conversations",
  settings: "Settings",
};

const optionDomId = (item: PaletteItem): string => `palette-option-${item.id}`;

export const CommandPalette: React.FC<CommandPaletteProps> = ({
  open,
  items,
  onClose,
  onSelect,
}) => {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const restoreRef = useRef<HTMLElement | null>(null);

  const matches = useMemo(() => filterPalette(items, query), [items, query]);

  // A new query or a new world (a tab closed while the palette was up) is a
  // new list: the selection starts at the top rather than pointing at an old
  // offset that now names a different row — or none.
  useEffect(() => {
    setSelected(0);
  }, [query, items]);

  // Open: remember who had focus, clear the last search, take focus.
  // Close: give focus back. One effect, because the two halves are one
  // invariant — the palette borrows focus and returns it.
  useEffect(() => {
    if (!open) return;
    restoreRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setQuery("");
    setSelected(0);
    inputRef.current?.focus();
    return () => {
      restoreRef.current?.focus();
      restoreRef.current = null;
    };
  }, [open]);

  // Keep the selected row visible: ArrowDown past the fold must scroll,
  // and this is the only place that knows which row is selected.
  useEffect(() => {
    if (!open) return;
    const el = listRef.current?.querySelector('[aria-selected="true"]');
    if (el instanceof HTMLElement) el.scrollIntoView({ block: "nearest" });
  }, [selected, open, matches.length]);

  if (!open) return null;

  const onKeyDown = (e: React.KeyboardEvent) => {
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        e.stopPropagation();
        setSelected(moveSelection(selected, 1, matches.length));
        break;
      case "ArrowUp":
        e.preventDefault();
        e.stopPropagation();
        setSelected(moveSelection(selected, -1, matches.length));
        break;
      case "Enter": {
        e.preventDefault();
        e.stopPropagation();
        const item = matches[selected];
        if (item) onSelect(item);
        break;
      }
      case "Escape":
        // Stopped here so it never reaches the window: every other overlay
        // (Settings, the model dropdowns) closes on a window-level Escape,
        // and one keystroke must not close two surfaces.
        e.preventDefault();
        e.stopPropagation();
        onClose();
        break;
      case "Tab":
        // One input, then done — Tab cycles nothing, and Escape is the way
        // out, with focus returned to wherever Ctrl+K was pressed. Letting Tab
        // walk focus behind the overlay would land it on controls the
        // backdrop is covering, from where Escape would miss this component
        // entirely and hit the window's other listeners.
        e.preventDefault();
        e.stopPropagation();
        break;
      default:
        break;
    }
  };

  const current = selected >= 0 ? matches[selected] : undefined;

  return (
    <div
      className="fixed inset-0 z-50 bg-black/75 backdrop-blur-sm flex items-start justify-center pt-[12vh] p-4"
      onMouseDown={onClose}
      role="presentation"
    >
      <div
        className="w-full max-w-xl bg-codify-surface border border-codify-border rounded-xl shadow-2xl overflow-hidden"
        onMouseDown={(e) => {
          // Keep the input focused (see the module docs): a click anywhere
          // in the panel must not move focus to <body>.
          e.preventDefault();
          e.stopPropagation();
        }}
        role="combobox"
        aria-expanded
        aria-controls="command-palette-list"
        aria-haspopup="listbox"
        aria-activedescendant={current ? optionDomId(current) : undefined}
      >
        <input
          ref={inputRef}
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Jump to a tab, thread or setting…"
          aria-label="Command palette"
          aria-autocomplete="list"
          aria-controls="command-palette-list"
          className="w-full bg-transparent px-4 py-3 text-sm text-codify-primary outline-none border-b border-codify-border placeholder:text-codify-muted"
        />
        <ul
          id="command-palette-list"
          ref={listRef}
          role="listbox"
          aria-label="Palette results"
          className="max-h-72 overflow-y-auto p-1.5"
        >
          {matches.length === 0 && (
            <li role="presentation" className="px-3 py-4 text-center text-xs text-codify-muted">
              Nothing matches “{query.trim()}”
            </li>
          )}
          {matches.map((item, i) => {
            const firstOfKind = i === 0 || matches[i - 1].kind !== item.kind;
            const isSelected = i === selected;
            return (
              <React.Fragment key={item.id}>
                {firstOfKind && (
                  <li
                    role="presentation"
                    className="px-3 pt-2.5 pb-1 text-2xs uppercase tracking-wider text-codify-muted"
                  >
                    {GROUP_HEADING[item.kind]}
                  </li>
                )}
                <li
                  id={optionDomId(item)}
                  role="option"
                  aria-selected={isSelected}
                  onMouseEnter={() => setSelected(i)}
                  onClick={() => onSelect(item)}
                  className={
                    "flex items-center justify-between gap-3 px-3 py-2 rounded-lg cursor-pointer text-sm " +
                    (isSelected
                      ? "bg-codify-raised text-codify-primary"
                      : "text-codify-muted hover:bg-codify-raised/60")
                  }
                >
                  <span className="truncate">{item.title}</span>
                  <span className="text-2xs text-codify-muted flex-shrink-0">
                    {item.kind === "tab" && item.active
                      ? "current"
                      : item.label}
                  </span>
                </li>
              </React.Fragment>
            );
          })}
        </ul>
        <div className="px-3.5 py-2 border-t border-codify-border flex items-center justify-between text-2xs text-codify-muted">
          <span>↑↓ move · ↵ open · esc close</span>
          <span>
            {matches.length} of {items.length}
          </span>
        </div>
      </div>
    </div>
  );
};

export default CommandPalette;
