import React, { useEffect, useId, useRef, useState } from "react";
import { AlertTriangle } from "lucide-react";

import {
  currentDialog,
  settleDialog,
  subscribeDialog,
  type ActiveDialog,
} from "../appDialog";
import { Button } from "./ui/Button";

/**
 * Themed modal host for in-app confirmations and text prompts.
 *
 * Painted strictly from `--codify-*` tokens so every destructive confirmation and rename prompt
 * wears the active theme (including custom colours) instead of WebKitGTK's unthemed native popup.
 */
export const AppDialogHost: React.FC = () => {
  const [dialog, setDialog] = useState<ActiveDialog | null>(() => currentDialog());
  const [draft, setDraft] = useState("");
  const titleId = useId();
  const bodyId = useId();
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    setDialog(currentDialog());
    return subscribeDialog(setDialog);
  }, []);

  useEffect(() => {
    if (!dialog) return;
    if (dialog.kind === "prompt") {
      setDraft(dialog.initialValue);
      queueMicrotask(() => {
        inputRef.current?.focus();
        inputRef.current?.select();
      });
    }
  }, [dialog]);

  useEffect(() => {
    if (!dialog) return;
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        settleDialog(dialog.kind === "confirm" ? false : null);
      }
    };
    document.addEventListener("keydown", onKeyDown, true);
    return () => document.removeEventListener("keydown", onKeyDown, true);
  }, [dialog]);

  if (!dialog) return null;

  const isConfirm = dialog.kind === "confirm";
  const isDanger = isConfirm && dialog.tone === "danger";

  const cancel = (): void => settleDialog(isConfirm ? false : null);
  const submit = (): void => settleDialog(isConfirm ? true : draft);

  return (
    <div
      data-app-dialog-backdrop="true"
      className="fixed inset-0 z-[100] flex items-center justify-center p-4"
      style={{ backgroundColor: "rgb(var(--codify-bg-rgb) / 0.75)" }}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) cancel();
      }}
    >
      <div
        role={isConfirm ? "alertdialog" : "dialog"}
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={dialog.message ? bodyId : undefined}
        className="w-full max-w-md rounded-2xl border border-codify-border-strong bg-codify-surface shadow-2xl p-5 space-y-4"
      >
        <div className="flex items-start gap-3">
          {isDanger && (
            <span
              aria-hidden="true"
              className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-codify-danger/60 bg-codify-danger/20 text-codify-danger-ink"
            >
              <AlertTriangle className="w-4 h-4" />
            </span>
          )}
          <div className="min-w-0 flex-1 space-y-1.5">
            <h2 id={titleId} className="text-sm font-semibold text-codify-primary">
              {dialog.title}
            </h2>
            {dialog.message && (
              <p
                id={bodyId}
                className="whitespace-pre-line text-xs text-codify-secondary leading-relaxed"
              >
                {dialog.message}
              </p>
            )}
          </div>
        </div>

        {dialog.kind === "prompt" && (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              submit();
            }}
          >
            <input
              ref={inputRef}
              type="text"
              aria-label={dialog.title}
              value={draft}
              placeholder={dialog.placeholder}
              onChange={(event) => setDraft(event.target.value)}
              className="w-full bg-codify-bg border border-codify-border rounded-lg px-3 py-2 text-sm text-codify-primary placeholder-codify-muted focus:outline-hidden focus:border-codify-accent"
            />
          </form>
        )}

        <div className="flex items-center justify-end gap-2 pt-1">
          <Button autoFocus={isConfirm} tone="subtle" size="sm" onClick={cancel}>
            {dialog.cancelLabel}
          </Button>
          <Button tone={isDanger ? "danger" : "primary"} size="sm" onClick={submit}>
            {dialog.confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  );
};
