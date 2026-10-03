import React from "react";
import { FileCode } from "lucide-react";

interface DiffViewerProps {
  path: string;
  diffText: string;
  /**
   * Open the file in an editor tab. Optional: a card drawn without it (a printed view, a test that reads words) has no link
   * rather than one that does nothing.
   */
  onOpen?: (path: string) => void;
}

export const DiffViewer: React.FC<DiffViewerProps> = ({ path, diffText, onOpen }) => {
  const lines = diffText.split("\n");

  return (
    <div className="bg-codify-bg border border-codify-border rounded-lg overflow-hidden my-2 font-mono text-xs">
      <div className="bg-codify-surface px-3 py-2 border-b border-codify-border flex items-center gap-2 text-codify-secondary font-medium">
        <FileCode className="w-4 h-4 text-codify-info" />
        <span className="min-w-0 truncate">{path}</span>
        {onOpen && (
          <button
            type="button"
            onClick={() => onOpen(path)}
            aria-label={`Open ${path} in the editor`}
            title="Open in the editor"
            className="ml-auto shrink-0 rounded-sm px-2 py-0.5 text-2xs text-codify-info-ink hover:bg-codify-raised focus:outline-hidden focus-visible:ring-2 focus-visible:ring-codify-info"
          >
            Open
          </button>
        )}
      </div>
      <div className="p-2 overflow-x-auto max-h-96">
        {lines.map((line, idx) => {
          let color = "text-codify-muted";
          let bg = "transparent";
          if (line.startsWith("+") && !line.startsWith("+++")) {
            color = "text-codify-success";
            bg = "bg-codify-success/15";
          } else if (line.startsWith("-") && !line.startsWith("---")) {
            color = "text-codify-danger";
            bg = "bg-codify-danger/15";
          } else if (line.startsWith("@@")) {
            color = "text-codify-design";
            bg = "bg-codify-design/10";
          }

          return (
            <div key={idx} className={`px-2 py-0.5 rounded-sm ${color} ${bg} whitespace-pre`}>
              {line || " "}
            </div>
          );
        })}
      </div>
    </div>
  );
};
