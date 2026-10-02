import type { Event } from "./types.ts";

/**
 * The conductor's notes to itself, from the engine's own events.
 *
 * The engine publishes the whole todo list as a `todo_updated` event every time the conductor changes it
 * (`engine/todo.py`; `docs/04` §1.4), so the newest event *is* the list: there is nothing to fold, and a
 * snapshot that cannot be read is an empty list rather than a reason to show an older one. The text is the
 * model's and can echo anything it read, so it is shown as one bounded line of plain text; an item this build
 * cannot read is left out; and an abandoned item is not shown as open work.
 *
 * Pure and DOM-free, so the rules have tests that need no renderer.
 */
export const TODO_STATUSES = ["pending", "doing", "done", "dropped"] as const;
export type TodoStatus = (typeof TODO_STATUSES)[number];

export interface TodoItem {
  id: string;
  text: string;
  status: TodoStatus;
}

/** The engine's limits (`engine/todo.py`), applied again here rather than trusted. */
export const MAX_TODO_ITEMS = 20;
export const MAX_TODO_TEXT = 160;

const isStatus = (value: unknown): value is TodoStatus =>
  typeof value === "string" && (TODO_STATUSES as readonly string[]).includes(value);

const oneLine = (text: string): string => {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length <= MAX_TODO_TEXT ? flat : `${flat.slice(0, MAX_TODO_TEXT - 1).trimEnd()}…`;
};

/** The list as it now stands, without what was dropped, in the order the conductor wrote it. */
export function visibleTodos(events: readonly Event[] | undefined): TodoItem[] {
  let newest: Event | null = null;
  for (const e of events ?? []) {
    if (e.type === "todo_updated" && (!newest || e.sequence > newest.sequence)) newest = e;
  }
  const raw = (newest?.payload as { items?: unknown } | null | undefined)?.items;
  if (!Array.isArray(raw)) return [];
  const items: TodoItem[] = [];
  for (const entry of raw) {
    if (typeof entry !== "object" || entry === null) continue;
    const { id, text, status } = entry as Record<string, unknown>;
    if (typeof id !== "string" || id.length === 0 || typeof text !== "string" || !isStatus(status)) continue;
    const line = oneLine(text);
    if (line.length === 0) continue;
    items.push({ id, text: line, status });
  }
  return items.slice(0, MAX_TODO_ITEMS).filter((i) => i.status !== "dropped");
}
