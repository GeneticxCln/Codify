import React, { useState } from "react";
import { Target, Sparkles } from "lucide-react";

interface GoalCreatorProps {
  workspaceId: string;
  onCreateGoal: (title: string, description: string, dry_run: boolean) => Promise<void>;
}

export const GoalCreator: React.FC<GoalCreatorProps> = ({
  workspaceId: _workspaceId,
  onCreateGoal,
}) => {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [desc, setDesc] = useState("");
  const [dryRun, setDryRun] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title) return;
    setLoading(true);
    setError(null);
    try {
      await onCreateGoal(title, desc, dryRun);
      setTitle("");
      setDesc("");
      setDryRun(false);
      setOpen(false);
    } catch (err: any) {
      setError(err.message || "Failed to create goal");
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="flex items-center gap-1.5 px-3 py-1.5 bg-blue-600 hover:bg-blue-500 text-white text-xs font-semibold rounded shadow-sm transition-colors"
      >
        <Sparkles className="w-3.5 h-3.5" /> New Goal
      </button>

      {open && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="bg-[#161b22] border border-[#30363d] rounded-lg p-6 max-w-lg w-full shadow-2xl">
            <h3 className="text-base font-bold text-gray-100 mb-4 flex items-center gap-2">
              <Target className="w-5 h-5 text-blue-400" /> Define New Goal
            </h3>

            <form onSubmit={handleSubmit} className="flex flex-col gap-4">
              <div className="flex flex-col gap-1.5">
                <label className="text-xs font-semibold text-gray-400">Goal Objective</label>
                <input
                  type="text"
                  placeholder="e.g. Add user authentication with JWT"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
                  required
                />
              </div>

              <div className="flex flex-col gap-1.5">
                <label className="text-xs font-semibold text-gray-400">
                  Detailed Requirements / Guidance
                </label>
                <textarea
                  rows={4}
                  placeholder="Provide context, required packages, constraints, or test expectations..."
                  value={desc}
                  onChange={(e) => setDesc(e.target.value)}
                  className="bg-[#0d1117] border border-[#30363d] rounded px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-blue-500 resize-y"
                />
              </div>

              <label className="flex items-center gap-2 text-xs text-gray-300 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={dryRun}
                  onChange={(e) => setDryRun(e.target.checked)}
                  className="accent-blue-500 rounded cursor-pointer w-4 h-4"
                />
                <span>
                  <strong>Dry Run Mode</strong> — propose file diffs and simulate execution without writing files to disk
                </span>
              </label>

              {error && <p className="text-xs text-red-400 font-medium">{error}</p>}

              <div className="flex justify-end gap-2 pt-2 border-t border-[#21262d]">
                <button
                  type="button"
                  onClick={() => setOpen(false)}
                  className="px-3 py-1.5 bg-[#21262d] hover:bg-[#30363d] text-gray-300 text-xs rounded transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={loading}
                  className="px-4 py-1.5 bg-blue-600 hover:bg-blue-500 text-white text-xs font-semibold rounded transition-colors disabled:opacity-50"
                >
                  {loading ? "Planning..." : "Dispatch Planner"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </>
  );
};
