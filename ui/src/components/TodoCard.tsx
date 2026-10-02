import React from "react";
import { Check, Circle, CircleDot, ListChecks } from "lucide-react";

import type { TodoItem, TodoStatus } from "../todoList.ts";

const MARK: Record<TodoStatus, React.ReactNode> = {
  pending: <Circle className="w-3 h-3 flex-shrink-0 mt-0.5 text-codify-muted" aria-hidden="true" />,
  doing: <CircleDot className="w-3 h-3 flex-shrink-0 mt-0.5 text-codify-info" aria-hidden="true" />,
  done: <Check className="w-3 h-3 flex-shrink-0 mt-0.5 text-codify-success" aria-hidden="true" />,
  dropped: null,
};

const LABEL: Record<TodoStatus, string> = { pending: "To do", doing: "In progress", done: "Done", dropped: "Dropped" };

/**
 * The conductor's own notes on this goal, read-only.
 *
 * They are the model's note to its next run, not the plan: nobody approves them and nothing in the engine acts
 * on them, which is why they are drawn apart from the plan's steps and say whose they are. Each item is plain
 * text (React escapes it), one row, wrapping rather than clipped.
 */
export const TodoCard: React.FC<{ items: readonly TodoItem[] }> = ({ items }) => (
  <div
    data-todo-card="true"
    role="group"
    aria-label="The conductor's notes"
    className="flex flex-col gap-1.5 text-xs rounded-lg border border-codify-border/60 bg-codify-raised/60 p-3"
  >
    <div className="flex items-center gap-1.5 font-semibold text-codify-muted uppercase tracking-wider">
      <ListChecks className="w-3.5 h-3.5" aria-hidden="true" />
      The conductor's notes
    </div>
    <ul className="flex flex-col gap-1">
      {items.map((item) => (
        <li key={item.id} data-todo-status={item.status} className="flex items-start gap-1.5">
          {MARK[item.status]}
          <span className="sr-only">{LABEL[item.status]}: </span>
          <span className={`min-w-0 break-words leading-relaxed ${item.status === "done" ? "text-codify-muted line-through" : ""}`}>
            {item.text}
          </span>
        </li>
      ))}
    </ul>
  </div>
);
