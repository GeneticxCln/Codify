import React, { useState, useRef, useEffect } from "react";
import { Workspace, ModelOption } from "../types";
import {
  Folder,
  ChevronDown,
  Sparkles,
  ArrowUp,
  Shield,
  Layers,
  Plus,
  Check,
  Cpu,
} from "lucide-react";

export const AVAILABLE_MODELS: ModelOption[] = [
  { id: "claude-3-7-sonnet-latest", name: "Claude 3.7 Sonnet", provider: "anthropic", description: "Most capable coding & reasoning" },
  { id: "claude-3-5-sonnet-latest", name: "Claude 3.5 Sonnet", provider: "anthropic", description: "High-speed, top-tier coding" },
  { id: "claude-3-5-haiku-latest", name: "Claude 3.5 Haiku", provider: "anthropic", description: "Ultra-fast code execution" },
  { id: "gpt-4o", name: "GPT-4o", provider: "openai", description: "OpenAI flagship multi-modal model" },
  { id: "gpt-4o-mini", name: "GPT-4o Mini", provider: "openai", description: "Fast, cost-effective code generation" },
  { id: "gemini-2.0-flash", name: "Gemini 2.0 Flash", provider: "google", description: "Google next-gen low-latency model" },
  { id: "gemini-1.5-pro", name: "Gemini 1.5 Pro", provider: "google", description: "2M token context, deep reasoning" },
  { id: "deepseek-chat", name: "DeepSeek V3", provider: "deepseek", description: "High performance open-weights API" },
  { id: "deepseek-reasoner", name: "DeepSeek R1", provider: "deepseek", description: "CoT reasoning & advanced coding" },
  { id: "qwen2.5-coder:latest", name: "Ollama (Qwen 2.5 Coder)", provider: "ollama", description: "Local loopback inference" },
  { id: "meta-llama/llama-3.3-70b-instruct", name: "Llama 3.3 70B", provider: "openrouter", description: "OpenRouter hosted inference" },
];

export type ExecutionMode = "direct" | "dry_run" | "plan_only";

interface BottomCommandBarProps {
  workspaces: Workspace[];
  selectedWorkspace?: Workspace;
  onSelectWorkspace: (ws: Workspace) => void;
  onCreateWorkspace: (name: string, root_path: string) => Promise<void>;
  selectedModel: ModelOption;
  onSelectModel: (model: ModelOption) => void;
  mode: ExecutionMode;
  onChangeMode: (mode: ExecutionMode) => void;
  onSubmit: (prompt: string) => void;
  isLoading: boolean;
  onOpenSettings: () => void;
}

export const BottomCommandBar: React.FC<BottomCommandBarProps> = ({
  workspaces,
  selectedWorkspace,
  onSelectWorkspace,
  onCreateWorkspace,
  selectedModel,
  onSelectModel,
  mode,
  onChangeMode,
  onSubmit,
  isLoading,
  onOpenSettings,
}) => {
  const [prompt, setPrompt] = useState("");
  const [isFolderOpen, setIsFolderOpen] = useState(false);
  const [isModelOpen, setIsModelOpen] = useState(false);
  const [isModeOpen, setIsModeOpen] = useState(false);

  // New workspace inline dialog state
  const [isNewWsModal, setIsNewWsModal] = useState(false);
  const [wsName, setWsName] = useState("");
  const [wsPath, setWsPath] = useState("");
  const [wsError, setWsError] = useState<string | null>(null);

  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Auto-resize textarea
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 180)}px`;
    }
  }, [prompt]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const handleSubmit = () => {
    if (!prompt.trim() || isLoading || !selectedWorkspace) return;
    onSubmit(prompt.trim());
    setPrompt("");
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
  };

  const handleCreateWs = async (e: React.FormEvent) => {
    e.preventDefault();
    setWsError(null);
    try {
      await onCreateWorkspace(wsName, wsPath);
      setWsName("");
      setWsPath("");
      setIsNewWsModal(false);
      setIsFolderOpen(false);
    } catch (err: any) {
      setWsError(err.message || "Failed to add workspace");
    }
  };

  return (
    <div className="w-full max-w-4xl mx-auto p-4 z-20">
      <div className="bg-[#161b22] border border-[#30363d] rounded-2xl shadow-2xl overflow-hidden focus-within:border-blue-500/80 transition-all duration-200">
        {/* Main Textarea */}
        <div className="p-3.5 pb-2">
          <textarea
            ref={textareaRef}
            rows={1}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={isLoading || !selectedWorkspace}
            placeholder={
              !selectedWorkspace
                ? "Select a workspace folder below to begin coding..."
                : "Ask Codify to build, edit files, fix tests, or refactor code..."
            }
            className="w-full bg-transparent text-gray-100 placeholder-gray-500 text-sm resize-none focus:outline-none leading-relaxed"
          />
        </div>

        {/* Toolbar Controls */}
        <div className="px-3 py-2 bg-[#0d1117]/60 border-t border-[#30363d]/60 flex flex-wrap items-center justify-between gap-2 text-xs">
          {/* Left Pickers: Folder & Model & Mode */}
          <div className="flex items-center gap-2 relative flex-wrap">
            {/* Folder Picker Button */}
            <div className="relative">
              <button
                type="button"
                onClick={() => {
                  setIsFolderOpen(!isFolderOpen);
                  setIsModelOpen(false);
                  setIsModeOpen(false);
                }}
                className={`flex items-center gap-1.5 px-2.5 py-1 rounded-lg border transition-colors ${
                  selectedWorkspace
                    ? "bg-[#21262d] border-[#30363d] text-gray-200 hover:bg-[#30363d]"
                    : "bg-amber-950/40 border-amber-800 text-amber-300 hover:bg-amber-900/50"
                }`}
                title={selectedWorkspace?.root_path || "Select project folder"}
              >
                <Folder className="w-3.5 h-3.5 text-blue-400" />
                <span className="font-medium max-w-[140px] truncate">
                  {selectedWorkspace ? selectedWorkspace.name : "Select Folder"}
                </span>
                <ChevronDown className="w-3 h-3 text-gray-400" />
              </button>

              {/* Folder Dropdown */}
              {isFolderOpen && (
                <div className="absolute bottom-full left-0 mb-2 w-72 bg-[#161b22] border border-[#30363d] rounded-xl shadow-2xl p-2 z-50">
                  <div className="text-[11px] font-semibold text-gray-400 px-2 py-1 uppercase tracking-wider">
                    Workspaces
                  </div>
                  <div className="max-h-48 overflow-y-auto space-y-1 my-1">
                    {workspaces.map((ws) => (
                      <button
                        key={ws.id}
                        type="button"
                        onClick={() => {
                          onSelectWorkspace(ws);
                          setIsFolderOpen(false);
                        }}
                        className={`w-full text-left px-2.5 py-1.5 rounded-lg flex items-center justify-between text-xs transition-colors ${
                          selectedWorkspace?.id === ws.id
                            ? "bg-blue-600/20 text-blue-400 border border-blue-500/30 font-medium"
                            : "text-gray-300 hover:bg-[#21262d]"
                        }`}
                      >
                        <div className="truncate">
                          <div className="font-semibold truncate">{ws.name}</div>
                          <div className="text-[10px] text-gray-500 truncate font-mono">{ws.root_path}</div>
                        </div>
                        {selectedWorkspace?.id === ws.id && <Check className="w-3.5 h-3.5 text-blue-400 flex-shrink-0" />}
                      </button>
                    ))}
                  </div>
                  <div className="pt-2 border-t border-[#30363d]">
                    <button
                      type="button"
                      onClick={() => setIsNewWsModal(true)}
                      className="w-full flex items-center justify-center gap-1.5 px-2.5 py-1.5 bg-[#21262d] hover:bg-[#30363d] text-gray-200 rounded-lg text-xs font-semibold transition-colors"
                    >
                      <Plus className="w-3.5 h-3.5" /> Add Project Directory
                    </button>
                  </div>
                </div>
              )}
            </div>

            {/* Model Picker Button */}
            <div className="relative">
              <button
                type="button"
                onClick={() => {
                  setIsModelOpen(!isModelOpen);
                  setIsFolderOpen(false);
                  setIsModeOpen(false);
                }}
                className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-[#21262d] border border-[#30363d] text-gray-200 hover:bg-[#30363d] transition-colors"
              >
                <Cpu className="w-3.5 h-3.5 text-purple-400" />
                <span className="font-medium">{selectedModel.name}</span>
                <ChevronDown className="w-3 h-3 text-gray-400" />
              </button>

              {/* Model Dropdown */}
              {isModelOpen && (
                <div className="absolute bottom-full left-0 mb-2 w-80 bg-[#161b22] border border-[#30363d] rounded-xl shadow-2xl p-2 z-50">
                  <div className="flex items-center justify-between px-2 py-1">
                    <span className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">
                      Provider & Model Harness
                    </span>
                    <button
                      type="button"
                      onClick={() => {
                        setIsModelOpen(false);
                        onOpenSettings();
                      }}
                      className="text-[11px] text-blue-400 hover:underline"
                    >
                      Keys & Endpoints
                    </button>
                  </div>
                  <div className="max-h-60 overflow-y-auto space-y-1 my-1">
                    {AVAILABLE_MODELS.map((m) => (
                      <button
                        key={m.id}
                        type="button"
                        onClick={() => {
                          onSelectModel(m);
                          setIsModelOpen(false);
                        }}
                        className={`w-full text-left px-2.5 py-2 rounded-lg flex items-center justify-between text-xs transition-colors ${
                          selectedModel.id === m.id
                            ? "bg-purple-600/20 text-purple-300 border border-purple-500/30 font-medium"
                            : "text-gray-300 hover:bg-[#21262d]"
                        }`}
                      >
                        <div>
                          <div className="font-semibold flex items-center gap-1.5">
                            <span>{m.name}</span>
                            <span className="text-[10px] px-1.5 py-0.2 rounded bg-[#0d1117] text-gray-400 font-normal">
                              {m.provider}
                            </span>
                          </div>
                          <div className="text-[10px] text-gray-500">{m.description}</div>
                        </div>
                        {selectedModel.id === m.id && <Check className="w-3.5 h-3.5 text-purple-400 flex-shrink-0" />}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>

            {/* Execution Mode Selector */}
            <div className="relative">
              <button
                type="button"
                onClick={() => {
                  setIsModeOpen(!isModeOpen);
                  setIsFolderOpen(false);
                  setIsModelOpen(false);
                }}
                className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-[#21262d] border border-[#30363d] text-gray-300 hover:bg-[#30363d] transition-colors"
              >
                {mode === "direct" && <Sparkles className="w-3.5 h-3.5 text-green-400" />}
                {mode === "dry_run" && <Shield className="w-3.5 h-3.5 text-amber-400" />}
                {mode === "plan_only" && <Layers className="w-3.5 h-3.5 text-blue-400" />}
                <span>
                  {mode === "direct" && "Direct Apply"}
                  {mode === "dry_run" && "Review Diffs"}
                  {mode === "plan_only" && "Plan Only"}
                </span>
                <ChevronDown className="w-3 h-3 text-gray-400" />
              </button>

              {isModeOpen && (
                <div className="absolute bottom-full left-0 mb-2 w-56 bg-[#161b22] border border-[#30363d] rounded-xl shadow-2xl p-1.5 z-50">
                  <button
                    type="button"
                    onClick={() => {
                      onChangeMode("direct");
                      setIsModeOpen(false);
                    }}
                    className={`w-full text-left px-2.5 py-1.5 rounded-lg text-xs flex items-center justify-between ${
                      mode === "direct" ? "bg-green-600/20 text-green-300 font-medium" : "text-gray-300 hover:bg-[#21262d]"
                    }`}
                  >
                    <div>
                      <div className="font-semibold">Direct Apply</div>
                      <div className="text-[10px] text-gray-500">Edit workspace files automatically</div>
                    </div>
                    {mode === "direct" && <Check className="w-3.5 h-3.5 text-green-400" />}
                  </button>

                  <button
                    type="button"
                    onClick={() => {
                      onChangeMode("dry_run");
                      setIsModeOpen(false);
                    }}
                    className={`w-full text-left px-2.5 py-1.5 rounded-lg text-xs flex items-center justify-between mt-1 ${
                      mode === "dry_run" ? "bg-amber-600/20 text-amber-300 font-medium" : "text-gray-300 hover:bg-[#21262d]"
                    }`}
                  >
                    <div>
                      <div className="font-semibold">Review Diffs (Dry Run)</div>
                      <div className="text-[10px] text-gray-500">Generate diffs without writing disk</div>
                    </div>
                    {mode === "dry_run" && <Check className="w-3.5 h-3.5 text-amber-400" />}
                  </button>

                  <button
                    type="button"
                    onClick={() => {
                      onChangeMode("plan_only");
                      setIsModeOpen(false);
                    }}
                    className={`w-full text-left px-2.5 py-1.5 rounded-lg text-xs flex items-center justify-between mt-1 ${
                      mode === "plan_only" ? "bg-blue-600/20 text-blue-300 font-medium" : "text-gray-300 hover:bg-[#21262d]"
                    }`}
                  >
                    <div>
                      <div className="font-semibold">Plan Only</div>
                      <div className="text-[10px] text-gray-500">Break goal down into steps first</div>
                    </div>
                    {mode === "plan_only" && <Check className="w-3.5 h-3.5 text-blue-400" />}
                  </button>
                </div>
              )}
            </div>
          </div>

          {/* Right Action: Submit Button & Hint */}
          <div className="flex items-center gap-2">
            <span className="text-[10px] text-gray-500 hidden sm:inline">
              ⏎ to send
            </span>
            <button
              type="button"
              onClick={handleSubmit}
              disabled={!prompt.trim() || isLoading || !selectedWorkspace}
              className="w-8 h-8 rounded-xl bg-blue-600 hover:bg-blue-500 text-white flex items-center justify-center transition-all disabled:opacity-40 disabled:hover:bg-blue-600 shadow-md"
            >
              {isLoading ? (
                <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              ) : (
                <ArrowUp className="w-4 h-4" />
              )}
            </button>
          </div>
        </div>
      </div>

      {/* Inline New Workspace Modal */}
      {isNewWsModal && (
        <div className="fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-4">
          <div className="bg-[#161b22] border border-[#30363d] rounded-xl p-5 max-w-md w-full shadow-2xl">
            <h3 className="text-sm font-bold text-gray-100 mb-3 flex items-center gap-2">
              <Folder className="w-4 h-4 text-blue-400" /> Open Local Project Directory
            </h3>
            <form onSubmit={handleCreateWs} className="space-y-3">
              <div>
                <label className="text-xs text-gray-400 block mb-1">Project Name</label>
                <input
                  type="text"
                  placeholder="e.g. My Next.js App"
                  value={wsName}
                  onChange={(e) => setWsName(e.target.value)}
                  className="w-full bg-[#0d1117] border border-[#30363d] rounded-lg px-3 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-blue-500"
                  required
                />
              </div>
              <div>
                <label className="text-xs text-gray-400 block mb-1">Absolute Directory Path</label>
                <input
                  type="text"
                  placeholder="/home/quinton/Projects/my-app"
                  value={wsPath}
                  onChange={(e) => setWsPath(e.target.value)}
                  className="w-full bg-[#0d1117] border border-[#30363d] rounded-lg px-3 py-1.5 text-xs text-gray-200 focus:outline-none focus:border-blue-500 font-mono"
                  required
                />
              </div>
              {wsError && <p className="text-xs text-red-400">{wsError}</p>}
              <div className="flex justify-end gap-2 pt-2">
                <button
                  type="button"
                  onClick={() => setIsNewWsModal(false)}
                  className="px-3 py-1 bg-[#21262d] text-gray-300 text-xs rounded-lg hover:bg-[#30363d]"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="px-3 py-1 bg-blue-600 text-white text-xs font-semibold rounded-lg hover:bg-blue-500"
                >
                  Add Directory
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
};
