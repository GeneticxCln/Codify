/**
 * The auth-stale banner: what a browser tab shows when the engine answers but
 * rejects the token it holds.
 *
 * **Why this exists.** The health probe has always distinguished this state —
 * a 401 from a live engine is "up but rejected", and the header pill has said
 * `Auth stale` in warning colours since it was written. What it never did was
 * help. In the desktop app that gap is nearly invisible, because the shell
 * re-fetches the token over IPC and the pill clears itself within one probe.
 * In a browser tab against a standalone engine (`make run-engine-preview`),
 * there is no IPC and no self-healing, so the state sat there ambers forever
 * while every request the app made died with the same silent 401 — the
 * failure it names, repeated in the console, and not one word about what to
 * do.
 *
 * **Why a paste and not a button.** The fresh token lives with the engine's
 * *spawner* — the Tauri shell, or the `make run-engine-preview` terminal. A
 * browser tab cannot reach either, and a banner that pretended to "refresh
 * the token" would be lying about the one thing it exists to explain. So the
 * banner shows the exact two statements to paste, filled in with the port
 * this tab is already pointed at (the port is not the stale part), plus a
 * copy button, and says where the values come from.
 *
 * The copy is `scheme.ts`'s two-mechanism approach: `navigator.clipboard`
 * where the tab has one, the hidden-textarea fallback where it does not — a
 * file:// preview or an insecure origin is a real place this banner shows.
 * The strings themselves live in `statusTone.ts` (`STALE_AUTH_FIX`), which is
 * the plain module the node --test suite can read without a renderer.
 */
import { Check, ClipboardCopy, RefreshCw } from "lucide-react";
import React, { useEffect, useRef, useState } from "react";

import { STALE_AUTH_FIX } from "../statusTone";

/**
 * The two browser-console statements, as one pasteable block.
 *
 * Exported for the test that pins the shape against `ui/src/api.ts`'s own
 * keys: a key renamed in one place and not the other is a paste instruction
 * that silently does nothing, which is worse than no instruction at all.
 */
export function staleAuthPasteLines(port: number): string[] {
  return [
    `localStorage.setItem("CODIFY_PORT", "${port}");`,
    'localStorage.setItem("CODIFY_TOKEN", "<paste the token from the handshake line>");',
  ];
}

export async function copyText(
  text: string,
  target: {
    writeText?: (value: string) => Promise<void>;
    document?: Document;
  },
): Promise<boolean> {
  if (target.writeText) {
    try {
      await target.writeText(text);
      return true;
    } catch {
      // Fall through to the fallback rather than reporting a failure: the
      // clipboard being unwritable by API is exactly the case it exists for.
    }
  }
  const doc = target.document;
  if (!doc) return false;
  const field = doc.createElement("textarea") as HTMLTextAreaElement;
  field.value = text;
  // Off-screen rather than `display: none`: a hidden element cannot be
  // selected, and the copy would then be of nothing.
  field.setAttribute("readonly", "");
  // Scratch space for the copy, not the person's selection (see `copySchemeText`).
  field.setAttribute("data-clipboard", "off");
  field.style.position = "fixed";
  field.style.opacity = "0";
  doc.body.appendChild(field);
  try {
    field.select();
    return doc.execCommand?.("copy") ?? false;
  } catch {
    return false;
  } finally {
    field.remove();
  }
}

export const StaleAuthBanner: React.FC<{
  port: number;
  onRetry: () => void;
  onDismiss: () => void;
  /** Injected so a test can drive the clipboard without a real one. */
  writeText?: (value: string) => Promise<void>;
}> = ({ port, onRetry, onDismiss, writeText }) => {
  const [copied, setCopied] = useState(false);
  // `window` rather than `document` for the timer so a hot reload cannot
  // leave a timer firing into an unmounted tree; the reset itself is the
  // same pattern AppearancePane's copy notice uses.
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, []);

  const lines = staleAuthPasteLines(port);
  const block = lines.join("\n");

  const onCopy = async (): Promise<void> => {
    const clipboard =
      typeof navigator !== "undefined" && navigator.clipboard
        ? navigator.clipboard.writeText.bind(navigator.clipboard)
        : undefined;
    const ok = await copyText(block, {
      writeText: writeText ?? clipboard,
      document: typeof document !== "undefined" ? document : undefined,
    });
    setCopied(ok);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="mx-auto mt-3 mb-1 w-full max-w-4xl px-4">
      <div
        role="alert"
        className="rounded-xl border border-codify-warning bg-codify-warning/10 p-2.5 text-xs"
      >
        <div className="mb-1 flex items-center gap-1.5 text-codify-warning">
          <ClipboardCopy className="h-3.5 w-3.5 flex-shrink-0" />
          <span className="font-medium">{STALE_AUTH_FIX.heading}</span>
        </div>
        <p className="mb-2 leading-relaxed text-codify-secondary">{STALE_AUTH_FIX.body}</p>
        <div className="mb-2 rounded-lg border border-codify-border bg-codify-bg p-2 font-mono text-xs leading-relaxed">
          {/* `pre` with wrapping, not a scroll box: a token cut off at the right
              edge is the exact character the paste needs. */}
          <pre className="whitespace-pre-wrap break-all text-codify-primary">
            {`$ ${STALE_AUTH_FIX.command}`}
          </pre>
          <pre className="whitespace-pre-wrap break-all text-codify-primary">{block}</pre>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={onCopy}
            className="inline-flex items-center gap-1 rounded-lg border border-codify-border bg-codify-raised px-2 py-1 text-codify-secondary hover:bg-codify-border hover:text-codify-primary"
          >
            {copied ? (
              <Check className="h-3 w-3 text-codify-success" />
            ) : (
              <ClipboardCopy className="h-3 w-3" />
            )}
            <span>{copied ? "Copied" : "Copy commands"}</span>
          </button>
          <button
            type="button"
            onClick={onRetry}
            className="inline-flex items-center gap-1 rounded-lg border border-codify-border bg-codify-raised px-2 py-1 text-codify-secondary hover:bg-codify-border hover:text-codify-primary"
          >
            <RefreshCw className="h-3 w-3" />
            <span>Retry now</span>
          </button>
          <span className="flex-1 leading-relaxed text-codify-muted">
            {STALE_AUTH_FIX.pasteHint}
          </span>
          <button
            type="button"
            onClick={onDismiss}
            className="rounded-lg px-2 py-1 text-codify-muted hover:text-codify-primary"
          >
            Dismiss
          </button>
        </div>
      </div>
    </div>
  );
};
