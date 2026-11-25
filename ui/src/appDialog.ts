/**
 * Module-level promise queue for themed in-app confirm & prompt dialogs.
 *
 * Replaces native browser `confirm()` / `prompt()` dialogs, which WebKitGTK renders as
 * unstyled `JavaScript - tauri://localhost` GTK popups that ignore Codify's active theme.
 * One `<AppDialogHost />` in `App.tsx` renders the active request using `--codify-*` tokens.
 */

export type DialogTone = "primary" | "danger";

export interface ConfirmOptions {
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: DialogTone;
}

export interface PromptOptions {
  title: string;
  message?: string;
  initialValue?: string;
  placeholder?: string;
  confirmLabel?: string;
  cancelLabel?: string;
}

export type ActiveDialog =
  | {
      kind: "confirm";
      title: string;
      message: string;
      confirmLabel: string;
      cancelLabel: string;
      tone: DialogTone;
      resolve: (ok: boolean) => void;
    }
  | {
      kind: "prompt";
      title: string;
      message: string;
      initialValue: string;
      placeholder: string;
      confirmLabel: string;
      cancelLabel: string;
      resolve: (value: string | null) => void;
    };

type Listener = (dialog: ActiveDialog | null) => void;

const queue: ActiveDialog[] = [];
const listeners = new Set<Listener>();

function notify(): void {
  const head = queue[0] ?? null;
  for (const listener of listeners) listener(head);
}

/** Current open dialog, or `null` when none is open. */
export function currentDialog(): ActiveDialog | null {
  return queue[0] ?? null;
}

/** Subscribe to dialog open/close transitions. Returns the unsubscribe function. */
export function subscribeDialog(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Open a themed confirmation modal and resolve to `true` (confirmed) or `false` (cancelled). */
export function confirmDialog(options: ConfirmOptions): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    queue.push({
      kind: "confirm",
      title: options.title,
      message: options.message,
      confirmLabel: options.confirmLabel ?? "Confirm",
      cancelLabel: options.cancelLabel ?? "Cancel",
      tone: options.tone ?? "primary",
      resolve,
    });
    if (queue.length === 1) notify();
  });
}

/** Open a themed text-prompt modal and resolve to the entered string, or `null` when cancelled. */
export function promptDialog(options: PromptOptions): Promise<string | null> {
  return new Promise<string | null>((resolve) => {
    queue.push({
      kind: "prompt",
      title: options.title,
      message: options.message ?? "",
      initialValue: options.initialValue ?? "",
      placeholder: options.placeholder ?? "",
      confirmLabel: options.confirmLabel ?? "OK",
      cancelLabel: options.cancelLabel ?? "Cancel",
      resolve,
    });
    if (queue.length === 1) notify();
  });
}

/** Settle the active dialog (called by `<AppDialogHost />`). */
export function settleDialog(result: boolean | string | null): void {
  const head = queue.shift();
  if (!head) return;
  if (head.kind === "confirm") {
    head.resolve(result === true);
  } else {
    head.resolve(typeof result === "string" ? result : null);
  }
  notify();
}

/** Cancel and clear every queued dialog (used on unmount / test cleanup). */
export function resetDialogs(): void {
  while (queue.length > 0) {
    const head = queue.shift()!;
    if (head.kind === "confirm") head.resolve(false);
    else head.resolve(null);
  }
  notify();
}
