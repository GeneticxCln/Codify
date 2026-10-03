import React from "react";
import { Plus } from "lucide-react";

/**
 * New Tab, in the header, beside the CODIFY badge.
 *
 * ## Why it is here and not at the end of the strip
 *
 * It used to be the last thing in the tab strip, and that put the control for
 * *starting something* at the far end of a list of things already started. With
 * three tabs that is a small distance; with ten it is a walk along the strip,
 * and the strip is the part of the header that grows. This is the one control
 * whose cost went up with the number of tabs, and it is the one that shortens
 * the row — so it sits at the left edge instead, next to the name of the app,
 * where it is the same distance away whatever is open.
 *
 * It is a separate file for the same reason every card in this directory is: a
 * component that renders alone is a component that can be tested alone, and this
 * one has a state worth testing — it refuses when there is no project, because a
 * tab is a project's window and there is no window without one.
 */
export interface NewTabButtonProps {
  /**
   * Open a clean slate in this project.
   *
   * No thread is created. The first prompt creates it, in the send path, which
   * is the only place that has the prompt to name the thread with — so a tab
   * opened to look around leaves nothing behind.
   */
  onNewTab: () => void;
  /** A project id, or nothing when no project is selected. */
  workspaceId?: string;
}

export const NewTabButton: React.FC<NewTabButtonProps> = ({ onNewTab, workspaceId }) => {
  const enabled = Boolean(workspaceId);
  return (
    <button
      type="button"
      aria-label="New tab"
      title={
        enabled
          ? "A new, empty tab in the selected project (Ctrl+T)"
          : "Select a project to open a new tab"
      }
      onClick={onNewTab}
      disabled={!enabled}
      className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-transparent text-codify-muted transition-colors hover:bg-codify-raised hover:text-codify-primary focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-codify-accent disabled:cursor-not-allowed disabled:opacity-40"
    >
      <Plus className="w-3.5 h-3.5" />
    </button>
  );
};

export default NewTabButton;
