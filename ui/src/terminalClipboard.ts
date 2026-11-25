import { sanitizeTerminalPaste } from "./terminalPaste.ts";

/** Minimal xterm surface needed for Ctrl+Shift+C / Ctrl+Shift+V handling. */
export interface TerminalClipboardSurface {
  attachCustomKeyEventHandler(handler: (event: KeyboardEvent) => boolean): void;
  hasSelection(): boolean;
  getSelection(): string;
  paste(data: string): void;
}

export interface CopyTarget {
  writeText?: (text: string) => Promise<void>;
  document?: Pick<Document, "createElement" | "body"> & { execCommand?: (c: string) => boolean };
}

export interface TerminalClipboardOptions {
  /** When true, Ctrl+Shift+V is suppressed without writing to a dead PTY. */
  isExited?: () => boolean;
  /** Override clipboard write/fallback targets (for tests). */
  copyTarget?: CopyTarget;
  /** Override clipboard reader (for tests). Defaults to `navigator.clipboard.readText`. */
  readText?: () => Promise<string>;
}

function defaultCopyTarget(): CopyTarget {
  return {
    writeText:
      typeof navigator !== "undefined" && navigator.clipboard?.writeText
        ? navigator.clipboard.writeText.bind(navigator.clipboard)
        : undefined,
    document: typeof document !== "undefined" ? document : undefined,
  };
}

function defaultReadText(): (() => Promise<string>) | undefined {
  return typeof navigator !== "undefined" && navigator.clipboard?.readText
    ? navigator.clipboard.readText.bind(navigator.clipboard)
    : undefined;
}

async function copyText(text: string, target: CopyTarget): Promise<void> {
  if (target.writeText) {
    try {
      await target.writeText(text);
      return;
    } catch {
      // Fall through to execCommand fallback when the Clipboard API is unavailable.
    }
  }
  const doc = target.document;
  if (!doc) return;
  const field = doc.createElement("textarea") as HTMLTextAreaElement;
  field.value = text;
  field.setAttribute("readonly", "");
  field.setAttribute("data-clipboard", "off");
  field.style.position = "fixed";
  field.style.opacity = "0";
  doc.body.appendChild(field);
  try {
    field.select();
    doc.execCommand?.("copy");
  } catch {
    // Best-effort fallback.
  } finally {
    doc.body.removeChild(field);
  }
}

/**
 * Wire standard Linux terminal clipboard chords (`Ctrl+Shift+C` copy, `Ctrl+Shift+V` paste)
 * onto an xterm instance.
 *
 * Without a custom key handler, xterm.js on WebKitGTK treats `Ctrl+Shift+C` as `\x03` (SIGINT)
 * and `Ctrl+Shift+V` as `\x16` (`^V`), interrupting running commands instead of copying and
 * failing to paste. Plain `Ctrl+C` is deliberately untouched so it always sends SIGINT.
 */
export function attachTerminalClipboardKeys(
  term: TerminalClipboardSurface,
  options: TerminalClipboardOptions = {},
): void {
  term.attachCustomKeyEventHandler((event) => {
    if (event.type !== "keydown") return true;
    if (!event.ctrlKey || !event.shiftKey || event.altKey || event.metaKey) return true;

    const lower = event.key.toLowerCase();
    if (lower === "c" || event.code === "KeyC") {
      event.preventDefault();
      if (term.hasSelection()) {
        const selected = term.getSelection();
        if (selected) {
          void copyText(selected, options.copyTarget ?? defaultCopyTarget());
        }
      }
      return false;
    }

    if (lower === "v" || event.code === "KeyV") {
      event.preventDefault();
      if (!options.isExited?.()) {
        const readText = options.readText ?? defaultReadText();
        if (readText) {
          void readText()
            .then((text) => {
              if (options.isExited?.() || !text) return;
              const safe = sanitizeTerminalPaste(text);
              if (safe) term.paste(safe);
            })
            .catch(() => {
              // Clipboard read denied or empty: stay quiet rather than throwing unhandled.
            });
        }
      }
      return false;
    }

    return true;
  });
}

/** Load xterm, FitAddon, and clipboard key bindings in one shared dynamic-import boundary. */
export async function loadTerminalRuntime() {
  const [{ Terminal }, { FitAddon }] = await Promise.all([
    import("@xterm/xterm"),
    import("@xterm/addon-fit"),
  ]);
  return {
    Terminal,
    attachFitAndKeys(term: import("@xterm/xterm").Terminal, isExited: () => boolean) {
      const fit = new FitAddon();
      term.loadAddon(fit);
      attachTerminalClipboardKeys(term, { isExited });
      return fit;
    },
  };
}
