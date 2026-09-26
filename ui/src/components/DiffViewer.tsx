import React from "react";
import { FileCode } from "lucide-react";

interface DiffViewerProps {
  path: string;
  diffText: string;
}

export const DiffViewer: React.FC<DiffViewerProps> = ({ path, diffText }) => {
  const lines = diffText.split("\n");

  return (
    <div className="bg-codify-bg border border-codify-border rounded-lg overflow-hidden my-2 font-mono text-xs">
      <div className="bg-codify-surface px-3 py-2 border-b border-codify-border flex items-center gap-2 text-gray-300 font-medium">
        <FileCode className="w-4 h-4 text-blue-400" />
        <span>{path}</span>
      </div>
      <div className="p-2 overflow-x-auto max-h-96">
        {lines.map((line, idx) => {
          let color = "text-gray-400";
          let bg = "transparent";
          if (line.startsWith("+") && !line.startsWith("+++")) {
            color = "text-green-400";
            bg = "bg-green-950/30";
          } else if (line.startsWith("-") && !line.startsWith("---")) {
            color = "text-red-400";
            bg = "bg-red-950/30";
          } else if (line.startsWith("@@")) {
            color = "text-purple-400";
            bg = "bg-purple-950/20";
          }

          return (
            <div key={idx} className={`px-2 py-0.5 rounded ${color} ${bg} whitespace-pre`}>
              {line || " "}
            </div>
          );
        })}
      </div>
    </div>
  );
};
