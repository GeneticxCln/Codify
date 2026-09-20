import React, { useState } from "react";
import { Play, CheckCircle2, XCircle, Loader2 } from "lucide-react";

interface TestConnectionButtonProps {
  onTest: () => Promise<{ ok: boolean; message: string }>;
}

export const TestConnectionButton: React.FC<TestConnectionButtonProps> = ({ onTest }) => {
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null);

  const handleClick = async () => {
    setTesting(true);
    setResult(null);
    try {
      const res = await onTest();
      setResult(res);
    } catch (err: any) {
      setResult({ ok: false, message: err.message || "Failed to test connection" });
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="flex items-center gap-3">
      <button
        type="button"
        onClick={handleClick}
        disabled={testing}
        className="flex items-center gap-1.5 px-3 py-1.5 bg-[#21262d] hover:bg-[#30363d] text-gray-200 text-xs font-semibold rounded border border-[#30363d] transition-colors disabled:opacity-50"
      >
        {testing ? (
          <Loader2 className="w-3.5 h-3.5 animate-spin" />
        ) : (
          <Play className="w-3.5 h-3.5 text-blue-400" />
        )}
        Test Connection
      </button>

      {result && (
        <div
          className={`flex items-center gap-1.5 text-xs font-medium ${
            result.ok ? "text-green-400" : "text-red-400"
          }`}
        >
          {result.ok ? (
            <CheckCircle2 className="w-4 h-4" />
          ) : (
            <XCircle className="w-4 h-4" />
          )}
          <span>{result.message}</span>
        </div>
      )}
    </div>
  );
};
