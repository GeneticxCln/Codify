/**
 * A picker that draws its own list.
 *
 * The app used HTML `<select>`s for providers, protocols, microphones, web
 * access and the bulk model choice. The closed control was themed; the list it
 * opened was not. A `<select>`'s popup is not a DOM element — the desktop's own
 * widget toolkit draws it (GTK under WebKitGTK on Linux, Cocoa on macOS), so it
 * ignores `background`, `color` and every `--codify-*` token, and it comes out
 * in the machine's palette. On a light desktop that is a white panel with the
 * theme's near-white option text on it: a list that cannot be read.
 *
 * `color-scheme: dark` does not fix that on WebKitGTK, and `option { background }`
 * does not either, because neither reaches the GTK popup. The only list that
 * cannot be white is one this app paints, so this is that list: a button and a
 * `role="listbox"` panel built from the theme's own tokens.
 *
 * Behaviour kept from the control it replaces, because a picker that loses it is
 * a regression a screenshot would not show: the arrow keys move through the
 * list, Home/End jump, Escape closes the list and stops there (Settings closes
 * on Escape too, and the list is not the place to discard the form), a click
 * outside closes it, and typing a letter jumps to the next matching option.
 *
 * Geometry comes from `../modelMenu`, the same arithmetic the model menus use,
 * so every popup in a scrolling settings panel clamps and flips the same way.
 */
import React, { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { Check, ChevronDown } from "lucide-react";
import { MENU_EDGE, MenuPlacement, menuPlacement } from "../modelMenu";

export interface SelectOption {
  value: string;
  label: string;
}

export interface SelectMenuProps {
  /** The chosen value. */
  value: string;
  options: readonly SelectOption[];
  onChange: (value: string) => void;
  /** The name a screen reader announces for the field and for its list. */
  label: string;
  /** For a neighbouring `<label htmlFor>`. */
  id?: string;
  /**
   * The field's whole appearance. Omitted, it is `FIELD_CLASS`.
   *
   * It replaces rather than extends, so a caller that wants a different size
   * passes the full class and no two Tailwind utilities end up fighting over
   * the same property — which one won would depend on the stylesheet's order,
   * not on this file.
   */
  className?: string;
  disabled?: boolean;
  /** Render option labels in the mono face, for model ids. */
  mono?: boolean;
  /**
   * Put a search field at the top of the list. For a list long enough that
   * scrolling to an id is the slow way to choose it — the bulk model picker,
   * where a provider may serve hundreds.
   */
  searchable?: boolean;
  /** Placeholder for that field, e.g. "Search discovered models…". */
  searchPlaceholder?: string;
}

/**
 * How long a list has to be before the search field earns its row.
 *
 * A short list is faster to read than to search: a field above three
 * microphones asks the person to type the name of something they can see.
 * Past this many, scanning means scrolling and reading every row, and a
 * substring is the cheaper way in. Callers compare the number of options they
 * hand a picker against this, so a list that grows starts offering search on
 * its own rather than needing someone to remember to turn it on.
 */
export const SEARCHABLE_FROM = 8;

/** A settings field, the shape the provider and protocol pickers keep. */
export const FIELD_CLASS =
  "flex items-center justify-between gap-2 bg-codify-bg border border-codify-border rounded-sm " +
  "px-3 py-2 text-sm text-codify-secondary focus:outline-hidden focus:border-codify-accent " +
  "disabled:opacity-40 disabled:cursor-not-allowed";

/** How long a run of letters keeps counting as one typeahead word. */
const TYPEAHEAD_MS = 600;

export const SelectMenu: React.FC<SelectMenuProps> = ({
  value,
  options,
  onChange,
  label,
  id,
  className,
  disabled = false,
  mono = false,
  searchable = false,
  searchPlaceholder = "Search…",
}) => {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [placement, setPlacement] = useState<MenuPlacement | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const fieldRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const typedRef = useRef<{ text: string; at: number }>({ text: "", at: 0 });
  const tookFocusRef = useRef(false);
  const listId = useId();

  // A value no option names is shown as written: a microphone that was saved
  // and has since been unplugged is still the microphone the engine holds.
  const chosen = options.find((o) => o.value === value);
  const shown = chosen?.label ?? value;

  const close = (refocus: boolean) => {
    setOpen(false);
    setPlacement(null);
    setQuery("");
    if (refocus) fieldRef.current?.focus();
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

  useLayoutEffect(() => {
    if (open) measure();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, options.length]);

  // A menu drawn over a scrolling settings panel has to follow the field it
  // points at, rather than stay where the field used to be.
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
    const onPointerDown = (event: MouseEvent) => {
      if (rootRef.current?.contains(event.target as Node)) return;
      close(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        // This Escape is spent on the list. Settings listens for it too, and
        // would close the whole modal, discarding the form behind the list.
        event.stopPropagation();
        close(true);
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // The list takes focus as it opens, on the chosen row, so the arrow keys work
  // at once and a screen reader announces the selection rather than the button.
  // Once per opening: `placement` is re-measured on every scroll, and following
  // it here would drag focus back to the selected row as the list moved.
  useLayoutEffect(() => {
    if (!open) {
      tookFocusRef.current = false;
      return;
    }
    if (!placement || tookFocusRef.current) return;
    tookFocusRef.current = true;
    if (searchable) {
      searchRef.current?.focus();
      return;
    }
    const list = listRef.current;
    if (!list) return;
    const selected = list.querySelector<HTMLButtonElement>('[aria-selected="true"]');
    (selected ?? list.querySelector<HTMLButtonElement>('[role="option"]'))?.focus();
  }, [open, placement, searchable]);

  const choose = (next: string) => {
    onChange(next);
    close(true);
  };

  const rows = () => Array.from(listRef.current?.querySelectorAll<HTMLButtonElement>('[role="option"]') ?? []);

  // The query narrows the list; the chosen value survives a filter that hides it,
  // so re-opening with a search still says what is stored.
  const needle = query.trim().toLowerCase();
  const visible = needle
    ? options.filter((option) => option.label.toLowerCase().includes(needle))
    : options;

  const move = (step: number) => {
    const all = rows();
    if (all.length === 0) return;
    const at = all.indexOf(document.activeElement as HTMLButtonElement);
    all[(at + step + all.length) % all.length]?.focus();
  };

  const onFieldKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) {
        setOpen(true);
        return;
      }
      move(event.key === "ArrowDown" ? 1 : -1);
      return;
    }
    if (event.key === "Escape" && open) {
      event.stopPropagation();
      close(true);
    }
  };

  const onListKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      move(event.key === "ArrowDown" ? 1 : -1);
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      // Spelled out rather than left to the browser's default activation: a
      // focused option is a `button`, but a synthetic key event (the suite's,
      // and some assistive tools') runs no default action at all.
      const row = document.activeElement as HTMLElement | null;
      if (row?.getAttribute("role") === "option") {
        event.preventDefault();
        row.click();
      }
      return;
    }
    if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      const all = rows();
      (event.key === "Home" ? all[0] : all[all.length - 1])?.focus();
      return;
    }
    if (event.key === "Escape") {
      event.stopPropagation();
      event.preventDefault();
      close(true);
      return;
    }
    if (event.key === "Tab") {
      // Tab leaves the field, so the list belongs to where focus went.
      close(false);
      return;
    }
    // Typeahead is the field's substitute for a search box; with one open it
    // would move the list while the person is typing somewhere else.
    if (!searchable && event.key.length === 1 && /\S/.test(event.key)) {
      const now = performance.now();
      const recent = now - typedRef.current.at < TYPEAHEAD_MS;
      const text = (recent ? typedRef.current.text : "") + event.key.toLowerCase();
      typedRef.current = { text, at: now };
      const match = options.find((o) => o.label.toLowerCase().startsWith(text));
      if (match) {
        const index = options.indexOf(match);
        rows()[index]?.focus();
      }
    }
  };

  const onSearchKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const all = rows();
      (event.key === "ArrowDown" ? all[0] : all[all.length - 1])?.focus();
      return;
    }
    if (event.key === "Enter") {
      // The top match is the answer to what was typed; Enter should not need a
      // second press on a row that is already the best one.
      event.preventDefault();
      const first = rows()[0];
      if (first?.dataset.value !== undefined) choose(first.dataset.value);
      return;
    }
    if (event.key === "Escape") {
      event.stopPropagation();
      event.preventDefault();
      close(true);
    }
  };

  const optionRows = visible.map((option) => {
    const selected = option.value === value;
    return (
      <button
        key={option.value}
        type="button"
        role="option"
        aria-selected={selected}
        title={option.label}
        data-value={option.value}
        tabIndex={-1}
        onClick={() => choose(option.value)}
        className={
          "w-full text-left px-2 py-1 rounded-lg flex items-center gap-2 text-xs transition-colors cursor-pointer " +
          "focus:outline-hidden " +
          (selected
            ? "bg-codify-accent/20 text-codify-accent-ink font-medium"
            : "text-codify-secondary hover:bg-codify-raised focus:bg-codify-raised")
        }
      >
        <span className={"truncate flex-1 min-w-0 " + (mono ? "font-mono" : "")}>{option.label}</span>
        {selected && <Check className="w-3 h-3 text-codify-accent shrink-0" />}
      </button>
    );
  });

  const menu = placement && (() => {
    const style = {
      position: "fixed" as const,
      left: Math.max(
        MENU_EDGE,
        Math.min(placement.left, window.innerWidth - placement.width - MENU_EDGE),
      ),
      top: placement.above ? undefined : placement.top + 4,
      bottom: placement.above ? window.innerHeight - placement.top + 4 : undefined,
      width: placement.width,
      maxHeight: placement.maxHeight,
    };
    const POPUP = "z-[60] bg-codify-surface border border-codify-border rounded-xl shadow-2xl p-1.5 ";

    if (!searchable) {
      return (
        <div
          ref={listRef}
          id={listId}
          role="listbox"
          aria-label={label}
          onKeyDown={onListKeyDown}
          style={style}
          className={POPUP + "overflow-y-auto"}
        >
          {optionRows}
        </div>
      );
    }

    return (
      <div style={style} className={POPUP + "flex flex-col"}>
        <input
          ref={searchRef}
          type="text"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={onSearchKeyDown}
          placeholder={searchPlaceholder}
          aria-label={`Search ${label}`}
          autoComplete="off"
          spellCheck={false}
          className={
            "w-full shrink-0 mb-1.5 bg-codify-bg border border-codify-border rounded-lg px-2 py-1 " +
            "text-xs text-codify-primary placeholder-codify-muted focus:outline-hidden focus:border-codify-accent"
          }
        />
        {visible.length === 0 && (
          <p className="px-2 py-1.5 text-xs text-codify-muted">
            Nothing matches “{query.trim()}”.
          </p>
        )}
        <div
          ref={listRef}
          id={listId}
          role="listbox"
          aria-label={label}
          onKeyDown={onListKeyDown}
          className="min-h-0 overflow-y-auto"
        >
          {optionRows}
        </div>
      </div>
    );
  })();

  return (
    <div ref={rootRef} className="relative">
      <button
        ref={fieldRef}
        id={id}
        type="button"
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-label={label}
        disabled={disabled}
        title={shown}
        onClick={() => (open ? close(true) : setOpen(true))}
        onKeyDown={onFieldKeyDown}
        className={className ?? FIELD_CLASS}
      >
        <span className={"truncate text-left" + (mono ? " font-mono" : "")}>{shown}</span>
        <ChevronDown
          className={
            "w-3.5 h-3.5 shrink-0 text-codify-muted transition-transform " + (open ? "rotate-180" : "")
          }
        />
      </button>
      {menu}
    </div>
  );
};
