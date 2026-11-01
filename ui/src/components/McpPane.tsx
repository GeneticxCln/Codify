import React, { useEffect, useState } from "react";
import { AlertCircle, Check, Plus, Plug, RefreshCw, Trash2, X } from "lucide-react";

import {
  createMcpServer,
  deleteMcpServer,
  getMcpServers,
  probeMcpServer,
  setMcpTool,
  updateMcpServer,
} from "../api";
import { readRejection } from "../rejection";
import type { McpServer, McpServerCreate, McpTool } from "../types";
import { SelectMenu } from "./SelectMenu";
import { Checkbox } from "./ui/Checkbox";

/**
 * Settings → MCP.
 *
 * This pane admits a remote HTTPS server and shows what its `tools/list` says.
 * It is deliberately **not** a tool menu: no discovered tool is callable, the
 * conductor has no `call_mcp`, and the copy says so. A server that is offline or
 * refuses the probe stays in the list with the engine's reason, because a saved
 * address that failed once is still a thing a person may want to fix.
 */
type Draft = {
  name: string;
  url: string;
  auth_kind: "none" | "bearer" | "header";
  auth_header: string;
  secret: string;
};

const EMPTY: Draft = { name: "", url: "", auth_kind: "none", auth_header: "", secret: "" };

const stateLabel: Record<string, string> = {
  current: "",
  new: "new",
  changed: "changed",
  gone: "gone",
};

export const McpPane: React.FC = () => {
  const [servers, setServers] = useState<McpServer[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [saving, setSaving] = useState(false);
  const [probing, setProbing] = useState<Record<string, boolean>>({});
  const [savingServer, setSavingServer] = useState<Record<string, boolean>>({});
  const [savingTool, setSavingTool] = useState<Record<string, boolean>>({});
  const [saved, setSaved] = useState<string | null>(null);

  const load = async () => {
    try {
      setServers(await getMcpServers());
      setError(null);
    } catch (err) {
      setError(readRejection(err, "Could not read the MCP servers."));
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const save = async () => {
    setSaving(true);
    setError(null);
    setSaved(null);
    const body: McpServerCreate = {
      name: draft.name,
      url: draft.url,
      auth_kind: draft.auth_kind,
      auth_header: draft.auth_header,
      secret: draft.secret,
    };
    try {
      const stored = await createMcpServer(body);
      setSaved(`${stored.name} saved.`);
      setAdding(false);
      setDraft(EMPTY);
      await load();
    } catch (err) {
      setError(readRejection(err, "Could not save the MCP server."));
      // The engine stores a server before probing it, so a failed probe still
      // leaves a row. Re-read rather than pretending the save vanished.
      await load();
    } finally {
      setSaving(false);
    }
  };

  const probe = async (server: McpServer) => {
    setProbing((current) => ({ ...current, [server.id]: true }));
    setError(null);
    setSaved(null);
    try {
      await probeMcpServer(server.id);
      setSaved(`${server.name} answered tools/list.`);
      await load();
    } catch (err) {
      setError(readRejection(err, "The MCP server did not answer the probe."));
      await load();
    } finally {
      setProbing((current) => ({ ...current, [server.id]: false }));
    }
  };

  const toggleServer = async (server: McpServer, enabled: boolean) => {
    setSavingServer((current) => ({ ...current, [server.id]: true }));
    setError(null);
    setSaved(null);
    try {
      await updateMcpServer(server.id, { enabled });
      setSaved(`${server.name} ${enabled ? "enabled" : "disabled"}.`);
      await load();
    } catch (err) {
      setError(readRejection(err, "Could not change the MCP server."));
      await load();
    } finally {
      setSavingServer((current) => ({ ...current, [server.id]: false }));
    }
  };

  const setTool = async (
    server: McpServer,
    tool: McpTool,
    authority: McpTool["authority"],
    enabled: boolean,
  ) => {
    const key = `${server.id}:${tool.name}`;
    setSavingTool((current) => ({ ...current, [key]: true }));
    setError(null);
    setSaved(null);
    try {
      await setMcpTool(server.id, tool.name, { authority, enabled });
      setSaved(
        authority === "act" && enabled
          ? `${tool.name} admitted as an act tool (only while a goal runs).`
          : authority === "read" && enabled
            ? `${tool.name} admitted as a read tool.`
            : `${tool.name} is not callable.`,
      );
      await load();
    } catch (err) {
      setError(readRejection(err, "Could not change the MCP tool."));
      await load();
    } finally {
      setSavingTool((current) => ({ ...current, [key]: false }));
    }
  };

  const remove = async (server: McpServer) => {
    setError(null);
    setSaved(null);
    try {
      await deleteMcpServer(server.id);
      setSaved(`${server.name} removed.`);
      await load();
    } catch (err) {
      setError(readRejection(err, "Could not remove the MCP server."));
    }
  };

  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-codify-border bg-codify-surface p-4 space-y-2">
        <div className="flex items-start gap-3">
          <Plug className="w-4 h-4 mt-0.5 text-codify-info shrink-0" />
          <div>
            <h3 className="text-sm font-semibold text-codify-primary">Remote MCP servers</h3>
            <p className="text-xs text-codify-muted mt-1 leading-relaxed">
              Codify connects to HTTPS endpoints you add. It never starts a local MCP server and
              never runs its commands. A server is probed for its tool list. A tool you admit as{" "}
              <span className="text-codify-secondary">Read</span> can be called by the conductor on
              any turn; <span className="text-codify-secondary">Act</span> tools only while a goal
              is running.
            </p>
          </div>
        </div>
      </div>

      {error && (
        <div className="flex items-start gap-2 p-3 bg-codify-danger/20 border border-codify-danger/60 rounded-xl text-xs text-codify-danger-ink">
          <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
          <span>{error}</span>
        </div>
      )}
      {saved && (
        <div className="flex items-center gap-2 text-xs text-codify-success">
          <Check className="w-4 h-4" /> {saved}
        </div>
      )}

      <div className="flex items-center justify-between">
        <p className="text-xs text-codify-muted">
          {servers === null ? "Reading servers…" : `${servers.length} of 8 admitted`}
        </p>
        {!adding && (
          <button
            type="button"
            onClick={() => setAdding(true)}
            className="flex items-center gap-1.5 rounded-lg border border-codify-border bg-codify-surface px-3 py-1.5 text-xs font-semibold text-codify-primary hover:bg-codify-raised"
          >
            <Plus className="w-3.5 h-3.5" /> Add server
          </button>
        )}
      </div>

      {adding && (
        <div className="rounded-xl border border-codify-border bg-codify-surface p-4 space-y-3">
          <div className="flex items-center justify-between">
            <h4 className="text-xs font-semibold text-codify-primary">Add a remote server</h4>
            <button
              type="button"
              onClick={() => {
                setAdding(false);
                setDraft(EMPTY);
              }}
              className="text-codify-muted hover:text-codify-secondary"
              aria-label="Cancel adding a server"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
          <label className="block">
            <span className="block text-xs font-semibold text-codify-primary">Server name</span>
            <input
              value={draft.name}
              aria-label="Server name"
              onChange={(event) => setDraft({ ...draft, name: event.target.value })}
              className="mt-1 w-full rounded-lg border border-codify-border bg-codify-bg px-2.5 py-1.5 text-xs text-codify-primary"
              placeholder="Issues"
            />
          </label>
          <label className="block">
            <span className="block text-xs font-semibold text-codify-primary">Server URL</span>
            <input
              value={draft.url}
              aria-label="Server URL"
              onChange={(event) => setDraft({ ...draft, url: event.target.value })}
              className="mt-1 w-full rounded-lg border border-codify-border bg-codify-bg px-2.5 py-1.5 text-xs font-mono text-codify-primary"
              placeholder="https://mcp.example.com/mcp"
            />
          </label>
          <span className="block text-2xs text-codify-muted -mt-1">
            HTTPS, public host, no query string. Localhost and private addresses are refused.
          </span>
          <div>
            <span className="block text-xs font-semibold text-codify-primary">Authentication</span>
            <SelectMenu
              label="Authentication"
              value={draft.auth_kind}
              options={[
                { value: "none", label: "None" },
                { value: "bearer", label: "Bearer token" },
                { value: "header", label: "Custom header" },
              ]}
              onChange={(next) =>
                setDraft({ ...draft, auth_kind: next as Draft["auth_kind"] })
              }
              className="mt-1 w-full rounded-lg border border-codify-border bg-codify-bg px-2.5 py-1.5 text-xs text-codify-primary"
            />
          </div>
          {draft.auth_kind === "header" && (
            <label className="block">
              <span className="block text-xs font-semibold text-codify-primary">Header name</span>
              <input
                value={draft.auth_header}
                aria-label="Header name"
                onChange={(event) => setDraft({ ...draft, auth_header: event.target.value })}
                className="mt-1 w-full rounded-lg border border-codify-border bg-codify-bg px-2.5 py-1.5 text-xs font-mono text-codify-primary"
                placeholder="X-API-Key"
              />
            </label>
          )}
          {draft.auth_kind !== "none" && (
            <label className="block">
              <span className="block text-xs font-semibold text-codify-primary">
                {draft.auth_kind === "bearer" ? "Bearer token" : "Header value"}
              </span>
              <input
                type="password"
                aria-label={draft.auth_kind === "bearer" ? "Bearer token" : "Header value"}
                value={draft.secret}
                onChange={(event) => setDraft({ ...draft, secret: event.target.value })}
                className="mt-1 w-full rounded-lg border border-codify-border bg-codify-bg px-2.5 py-1.5 text-xs font-mono text-codify-primary"
                autoComplete="off"
                data-clipboard="off"
              />
              <span className="mt-1 block text-2xs text-codify-muted">
                Stored by the engine, never shown again.
              </span>
            </label>
          )}
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={saving || !draft.name.trim() || !draft.url.trim()}
              onClick={() => void save()}
              className="rounded-lg bg-codify-accent px-3 py-1.5 text-xs font-semibold text-codify-bg disabled:opacity-40"
            >
              {saving ? "Save and probe…" : "Save and probe"}
            </button>
            <button
              type="button"
              disabled={saving}
              onClick={() => {
                setAdding(false);
                setDraft(EMPTY);
              }}
              className="rounded-lg border border-codify-border px-3 py-1.5 text-xs font-semibold text-codify-secondary"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {servers !== null && servers.length === 0 && !adding && (
        <div className="rounded-xl border border-dashed border-codify-border bg-codify-surface p-6 text-center">
          <p className="text-xs text-codify-muted">No MCP servers yet.</p>
          <p className="text-2xs text-codify-muted mt-1">
            Add a remote HTTPS endpoint to see its tool list. Nothing is called until you admit a tool.
          </p>
        </div>
      )}

      {servers?.map((server) => (
        <div key={server.id} className="rounded-xl border border-codify-border bg-codify-surface p-4 space-y-3">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <h4 className="text-xs font-semibold text-codify-primary">{server.name}</h4>
              <p className="text-2xs font-mono text-codify-muted truncate">{server.url}</p>
              <p className="text-2xs text-codify-muted mt-1">
                {server.protocol_version
                  ? `MCP ${server.protocol_version}${server.server_name ? ` · ${server.server_name}` : ""}`
                  : "Not probed yet"}
                {server.has_secret ? " · credential stored" : ""}
              </p>
              <Checkbox
                checked={server.enabled}
                disabled={savingServer[server.id]}
                onChange={(next) => void toggleServer(server, next)}
                className="mt-1.5"
              >
                Enabled
              </Checkbox>
            </div>
            <div className="flex items-center gap-1 shrink-0">
              <button
                type="button"
                onClick={() => void probe(server)}
                disabled={probing[server.id]}
                className="flex items-center gap-1 rounded-lg border border-codify-border px-2 py-1 text-2xs font-semibold text-codify-secondary hover:bg-codify-raised disabled:opacity-40"
              >
                <RefreshCw className={`w-3 h-3 ${probing[server.id] ? "animate-spin" : ""}`} />
                Probe now
              </button>
              <button
                type="button"
                onClick={() => void remove(server)}
                className="rounded-lg border border-codify-border p-1 text-codify-muted hover:text-codify-danger"
                aria-label={`Remove ${server.name}`}
              >
                <Trash2 className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>

          {server.last_probe_error && (
            <div className="flex items-start gap-2 rounded-lg border border-codify-warning/50 bg-codify-warning/10 px-2.5 py-2 text-2xs text-codify-warning-ink">
              <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
              <span>{server.last_probe_error}</span>
            </div>
          )}

          {server.tools.length === 0 ? (
            <p className="text-2xs text-codify-muted">
              No tools discovered. A successful probe fills this list.
            </p>
          ) : (
            <div className="space-y-1.5">
              <p className="text-2xs text-codify-muted">
                {server.tools.length} tool{server.tools.length === 1 ? "" : "s"} discovered. Read tools
                are callable on any turn; Act tools only while a goal is running.
              </p>
              {server.tools.map((tool) => (
                <div key={tool.name} className="rounded-lg border border-codify-border bg-codify-bg px-2.5 py-2">
                  <div className="flex items-center gap-2">
                    <span className="text-2xs font-mono text-codify-primary">{tool.name}</span>
                    {stateLabel[tool.state] && (
                      <span className="rounded-full border border-codify-border px-1.5 text-2xs text-codify-muted">
                        {stateLabel[tool.state]}
                      </span>
                    )}
                  </div>
                  {tool.description && (
                    <p className="text-2xs text-codify-muted mt-0.5">{tool.description}</p>
                  )}
                  <details className="mt-1.5">
                    <summary className="cursor-pointer text-2xs text-codify-muted hover:text-codify-secondary">
                      Input schema
                    </summary>
                    <pre className="mt-1 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-codify-border bg-codify-surface p-2 text-2xs text-codify-secondary">
                      {JSON.stringify(tool.input_schema, null, 2)}
                    </pre>
                  </details>
                  <div className="mt-2 flex flex-wrap items-center gap-3">
                    <SelectMenu
                      label={`Authority for ${tool.name}`}
                      value={tool.authority ?? ""}
                      options={[
                        { value: "", label: "Not admitted" },
                        { value: "read", label: "Read" },
                        { value: "act", label: "Act" },
                      ]}
                      onChange={(next) =>
                        void setTool(
                          server,
                          tool,
                          next === "read" || next === "act" ? next : null,
                          next === "read" || next === "act",
                        )
                      }
                      disabled={Boolean(savingTool[`${server.id}:${tool.name}`])}
                      className="rounded-lg border border-codify-border bg-codify-surface px-2 py-1 text-2xs text-codify-primary"
                    />
                    <Checkbox
                      checked={tool.enabled}
                      disabled={
                        tool.authority === null || Boolean(savingTool[`${server.id}:${tool.name}`])
                      }
                      onChange={(next) => void setTool(server, tool, tool.authority, next)}
                    >
                      Enabled
                    </Checkbox>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
};
