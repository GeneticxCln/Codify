import React, { useState } from "react";
import { Workspace } from "../types";
import { Folder, Plus } from "lucide-react";

interface WorkspaceSelectorProps {
  workspaces: Workspace[];
  selectedWorkspace?: Workspace;
  onSelect: (ws: Workspace) => void;
  onCreate: (name: string, root_path: string) => Promise<void>;
}

export const WorkspaceSelector: React.FC<WorkspaceSelectorProps> = ({
  workspaces,
  selectedWorkspace,
  onSelect,
  onCreate,
}) => {
  const [modalOpen, setModalOpen] = useState(false);
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name || !path) return;
    setSubmitting(true);
    setError(null);
    try {
      await onCreate(name, path);
      setName("");
      setPath("");
      setModalOpen(false);
    } catch (err: any) {
      setError(err.message || "Failed to create workspace");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex items-center gap-2">
      <div className="relative">
        <select
          value={selectedWorkspace?.id || ""}
          onChange={(e) => {
            const found = workspaces.find((w) => w.id === e.target.value);
            if (found) onSelect(found);
          }}
          className="bg-[#161b22] border border-[#30363d] rounded px-3 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-blue-500 font-medium cursor-pointer"
        >
          <option value="" disabled>
            Select Workspace...
          </option>
          {workspaces.map((w) => (
            <option key={w.id} value={w.id}>
              {w.name} ({w.root_path})
            </option>
          ))}
        </select>
      </div>

      <button
        onClick={() => setModalOpen(true)}
        className="flex items-center gap-1 px-2.5 py-1.5 bg-[#21262d] hover:bg-[#30363d] text-gray-200 text-xs font-semibold rounded border border-[#30363d] transition-colors"
      >
        <Plus className="w-3.5 h-3.5 text-blue-400" /> New Workspace
      </button>

      {modalOpen && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-6 max-w-md w-full shadow-xl">
            <h3 className="text-base font-bold text-gray-100 mb-4 flex items-center gap-2">
              <Folder className="w-5 h-5 text-blue-400" /> Register Workspace Directory
            </h3>

            <form onSubmit={handleSubmit} className="flex flex-col gap-4">
              <div className="flex flex-col gap-1.5">
                <label className="text-xs font-semibold text-gray-400">Workspace Name</label>
                <input
                  type="text"
                  placeholder="e.g. Backend API"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
                  required
                />
              </div>

              <div className="flex flex-col gap-1.5">
                <label className="text-xs font-semibold text-gray-400">Absolute Root Path</label>
                <input
                  type="text"
                  placeholder="/home/user/projects/my-app"
                  value={path}
                  onChange={(e) => setPath(e.target.value)}
                  className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-blue-500 font-mono"
                  required
                />
              </div>

              {error && <p className="text-xs text-red-400 font-medium">{error}</p>}

              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => setModalOpen(false)}
                  className="px-3 py-1.5 bg-[#21262d] hover:bg-[#30363d] text-gray-300 text-xs rounded transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={submitting}
                  className="px-4 py-1.5 bg-blue-600 hover:bg-blue-500 text-white text-xs font-semibold rounded transition-colors disabled:opacity-50"
                >
                  {submitting ? "Registering..." : "Add Workspace"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
};
