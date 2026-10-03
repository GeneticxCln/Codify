import React, { useEffect, useState } from "react";
import { Globe, Save } from "lucide-react";

import { getEngineSettings, saveEngineSettings } from "../api";
import { readRejection } from "../rejection.ts";
import { WEB_MODES, refusalSentence, webAccessDirty, webAccessStatus, webMode } from "../webAccess";

/**
 * Whether the assistant may fetch web pages, and from which sites.
 *
 * The one place a person turns on the engine's own access to the web (`web_fetch`, docs/12), so the card is
 * mostly the sentence that says what each choice lets through (`webAccessStatus`). It is a card of its own and
 * not a row of the conductor's: it is a different decision (what leaves the machine, not which model thinks),
 * and it must be findable by someone who has never opened the conductor's budgets.
 *
 * Which entries of the list are valid is the engine's rule (`parse_hosts`). This card sends what is typed and
 * shows the engine's refusal, so the rule has one owner. An engine that predates the setting answers without it,
 * and an absent card beats one whose save is a 400.
 */
export const WebAccessCard: React.FC = () => {
  // What the engine holds, and what is typed. `null` until the engine says it has the setting.
  const [held, setHeld] = useState<{ mode: number; hosts: string; hostsMax: number } | null>(null);
  const [draft, setDraft] = useState<{ mode: number; hosts: string }>({ mode: 0, hosts: "" });
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    let cancelled = false;
    getEngineSettings()
      .then((s) => {
        if (cancelled || !s || !s.web_fetch || !s.web_fetch_hosts) return;
        const now = {
          mode: webMode(s.web_fetch.value),
          hosts: s.web_fetch_hosts.value,
          hostsMax: s.web_fetch_hosts.max,
        };
        setHeld(now);
        setDraft({ mode: now.mode, hosts: now.hosts });
      })
      .catch(() => {
        // Settings that cannot be read are shown by the cards that need them to be there; this one stays absent.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (held === null) return null;

  const save = async () => {
    setSaving(true);
    setMsg(null);
    try {
      const res = await saveEngineSettings({
        web_fetch: draft.mode,
        web_fetch_hosts: draft.hosts.trim(),
      });
      // The echo, not the draft: the engine stores the list in its own spelling and clamps the mode, so what it
      // kept is what the next fetch reads.
      const kept = {
        mode: webMode(Number(res.saved.web_fetch)),
        hosts: String(res.saved.web_fetch_hosts ?? ""),
        hostsMax: held.hostsMax,
      };
      setHeld(kept);
      setDraft({ mode: kept.mode, hosts: kept.hosts });
      setMsg({ ok: true, text: "Saved — the next run uses it." });
    } catch (err: any) {
      // The engine owns what a site name is, and says which entries it refused.
      setMsg({ ok: false, text: refusalSentence(readRejection(err, ""), "Could not save web access.") });
    } finally {
      setSaving(false);
    }
  };

  const status = webAccessStatus(webMode(draft.mode), draft.hosts);

  return (
    <div className="flex flex-col gap-2.5 bg-codify-bg border border-codify-border rounded-xl p-3.5">
      <div className="flex items-center gap-2 text-xs font-semibold text-codify-secondary">
        <Globe className="w-3.5 h-3.5 text-codify-design" />
        Web pages
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor="web-fetch-mode" className="text-xs text-codify-secondary font-medium">
          The assistant may fetch
        </label>
        <select
          id="web-fetch-mode"
          value={draft.mode}
          disabled={saving}
          onChange={(e) => {
            setDraft({ ...draft, mode: Number(e.target.value) });
            setMsg(null);
          }}
          className="bg-codify-surface border border-codify-border rounded-lg px-2.5 py-1.5 text-xs text-codify-secondary focus:outline-none focus:border-codify-accent disabled:opacity-40"
        >
          {WEB_MODES.map((m) => (
            <option key={m.value} value={m.value}>
              {m.label}
            </option>
          ))}
        </select>
      </div>
      {draft.mode === 1 && (
        <div className="flex flex-col gap-1">
          <label htmlFor="web-fetch-hosts" className="text-xs text-codify-secondary font-medium">
            Sites
          </label>
          <input
            id="web-fetch-hosts"
            type="text"
            value={draft.hosts}
            maxLength={held.hostsMax}
            disabled={saving}
            placeholder="docs.python.org, developer.mozilla.org"
            onChange={(e) => {
              setDraft({ ...draft, hosts: e.target.value });
              setMsg(null);
            }}
            className="w-full bg-codify-surface border border-codify-border rounded-lg px-2.5 py-1.5 text-xs text-codify-secondary focus:outline-none focus:border-codify-accent font-mono disabled:opacity-40"
          />
          <p className="text-xs text-codify-muted leading-relaxed">
            Site names separated by commas. Each covers its subdomains. Not addresses, not URLs.
          </p>
        </div>
      )}
      <p
        className={`text-xs leading-relaxed ${
          status.kind === "any" || status.kind === "empty" ? "text-codify-warning" : "text-codify-muted"
        }`}
      >
        {status.text}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={save}
          disabled={saving || !webAccessDirty(held, draft)}
          className="flex items-center gap-1.5 px-3 py-1.5 bg-codify-accent text-codify-bg hover:brightness-110 text-xs font-semibold rounded-lg transition-colors disabled:opacity-40"
        >
          <Save className="w-3.5 h-3.5" />
          {saving ? "Saving..." : "Save web access"}
        </button>
        {msg && (
          <span className={`text-xs ${msg.ok ? "text-codify-success" : "text-codify-danger"}`}>{msg.text}</span>
        )}
      </div>
    </div>
  );
};
