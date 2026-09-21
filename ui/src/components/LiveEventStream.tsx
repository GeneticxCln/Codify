import React, { useEffect, useState, useRef } from "react";
import { Event } from "../types";
import { getEngineInfo } from "../api";
import { DiffViewer } from "./DiffViewer";
import { Terminal, CheckCircle, AlertOctagon, Bot, FileCheck } from "lucide-react";

interface LiveEventStreamProps {
  goalId: string;
}

export const LiveEventStream: React.FC<LiveEventStreamProps> = ({ goalId }) => {
  const [events, setEvents] = useState<Event[]>([]);
  const [connected, setConnected] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let retryDelay = 1000;
    let destroyed = false;
    const retryTimer = { id: 0 as ReturnType<typeof setTimeout> };

    const connect = () => {
      if (destroyed) return;
      const engine = getEngineInfo();
      const wsUrl = `ws://127.0.0.1:${engine.port}/ws/goals/${goalId}`;
      ws = new WebSocket(wsUrl);

      ws.onopen = () => {
        setConnected(true);
        retryDelay = 1000; // reset backoff on success
        ws!.send(JSON.stringify({ type: "auth", token: engine.token }));
      };

      ws.onmessage = (e) => {
        try {
          const ev: Event = JSON.parse(e.data);
          setEvents((prev) => {
            if (prev.some((item) => item.sequence === ev.sequence)) return prev;
            return [...prev, ev].sort((a, b) => a.sequence - b.sequence);
          });
        } catch (err) {
          console.error("WS Parse error", err);
        }
      };

      ws.onclose = () => {
        setConnected(false);
        if (!destroyed) {
          // Exponential backoff: 1s → 2s → 4s → 8s → capped at 16s
          retryTimer.id = setTimeout(() => {
            retryDelay = Math.min(retryDelay * 2, 16000);
            connect();
          }, retryDelay);
        }
      };

      ws.onerror = () => {
        // onclose will fire after onerror; reconnect handled there
        setConnected(false);
      };
    };

    connect();

    return () => {
      destroyed = true;
      clearTimeout(retryTimer.id);
      ws?.close();
    };
  }, [goalId]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events]);

  return (
    <div className="bg-[#0d1117] border border-[#30363d] rounded-lg flex flex-col h-80 overflow-hidden font-mono text-xs">
      <div className="bg-[#161b22] px-3 py-2 border-b border-[#30363d] flex items-center justify-between">
        <div className="flex items-center gap-2 text-gray-300 font-medium">
          <Terminal className="w-4 h-4 text-gray-400" />
          <span>Execution Event Stream</span>
        </div>
        <div className="flex items-center gap-2">
          <span
            className={`w-2 h-2 rounded-full ${
              connected ? "bg-green-500 animate-pulse" : "bg-red-500"
            }`}
          />
          <span className="text-gray-500">{connected ? "Connected" : "Disconnected"}</span>
        </div>
      </div>

      <div className="flex-1 p-3 overflow-y-auto space-y-2">
        {events.length === 0 ? (
          <div className="text-gray-500 italic py-8 text-center">
            Waiting for execution events...
          </div>
        ) : (
          events.map((ev) => (
            <div key={ev.sequence} className="flex flex-col gap-1">
              <div className="flex items-center gap-2 text-gray-500">
                <span className="text-[10px]">#{ev.sequence}</span>
                <span className="text-[10px] uppercase font-bold text-gray-400 bg-[#21262d] px-1.5 py-0.5 rounded">
                  {ev.type}
                </span>
                <span className="text-[10px]">
                  {new Date(ev.timestamp * 1000).toLocaleTimeString()}
                </span>
              </div>

              {/* Event Content by Type */}
              {ev.type === "agent_assigned" && (
                <div className="text-purple-300 flex items-center gap-1.5 pl-2">
                  <Bot className="w-3.5 h-3.5" />
                  <span>
                    Sub-agent assigned: <strong>{ev.payload.role}</strong> ({ev.payload.provider}/{ev.payload.model})
                  </span>
                </div>
              )}

              {ev.type === "log" && (
                <div
                  className={`pl-2 ${
                    ev.payload.level === "warn"
                      ? "text-amber-400"
                      : ev.payload.level === "error"
                      ? "text-red-400"
                      : "text-gray-300"
                  }`}
                >
                  {ev.payload.message}
                </div>
              )}

              {ev.type === "test_result" && (
                <div className="bg-[#161b22] border border-[#30363d] rounded p-2.5 my-1">
                  <div className="flex items-center gap-2 font-semibold">
                    {ev.payload.verdict === "pass" ? (
                      <CheckCircle className="w-4 h-4 text-green-400" />
                    ) : (
                      <AlertOctagon className="w-4 h-4 text-red-400" />
                    )}
                    <span
                      className={
                        ev.payload.verdict === "pass" ? "text-green-400" : "text-red-400"
                      }
                    >
                      Verdict: {ev.payload.verdict?.toUpperCase()}
                    </span>
                    {ev.payload.argv && (
                      <span className="text-gray-400 text-xs font-mono">
                        ({ev.payload.argv.join(" ")})
                      </span>
                    )}
                  </div>
                  {ev.payload.explanation && (
                    <p className="text-gray-400 mt-1 pl-6">{ev.payload.explanation}</p>
                  )}
                </div>
              )}

              {ev.type === "diff" && (
                <DiffViewer path={ev.payload.path} diffText={ev.payload.unified_diff} />
              )}

              {ev.type === "file_change_summary" && (
                <div className="text-blue-400 pl-2 flex items-center gap-1.5">
                  <FileCheck className="w-3.5 h-3.5" />
                  <span>
                    Files modified: {ev.payload.paths?.join(", ") || "none"}
                    {ev.payload.dry_run ? " (Dry Run)" : ""}
                  </span>
                </div>
              )}

              {ev.type === "error" && (
                <div className="text-red-400 pl-2 font-semibold">
                  Error [{ev.payload.code}]: {ev.payload.message}
                </div>
              )}
            </div>
          ))
        )}
        <div ref={endRef} />
      </div>
    </div>
  );
};
