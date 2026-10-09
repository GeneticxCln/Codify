import React, { useEffect, useState } from "react";
import { MousePointerClick } from "lucide-react";

import { getEngineSettings, saveEngineSettings } from "../api";
import { readRejection } from "../rejection.ts";

/**
 * Whether the assistant may act on the browser tab: open an address, click, and type (`page_actions`).
 *
 * Off on a fresh install, and the card's sentence is the reason: each of the three can carry what the assistant
 * has read in the workspace off the machine. An address it opens is sent to that site, and text it types into a
 * field a page watches is sent as it is typed (docs/03 §1.6). The engine cannot tell either from an ordinary
 * action, so the choice is a person's, made here and nowhere else (docs/00 §6.2). Reading the open page is not
 * covered: it sends nothing.
 *
 * A switch that saves as it is flipped, like the conductor's "drives an approved plan", and shows what the
 * engine stored rather than what was clicked. An engine that predates the setting answers without it, and an
 * absent card beats one whose save is a 400.
 */
export const PageActionsCard: React.FC = () => {
  // What the engine holds. `null` until the engine says it has the setting.
  const [allowed, setAllowed] = useState<boolean | null>(null);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getEngineSettings()
      .then((s) => {
        if (cancelled || !s || !s.page_actions) return;
        setAllowed(s.page_actions.value === 1);
      })
      .catch(() => {
        // Settings that cannot be read are shown by the cards that need them to be there; this one stays absent.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (allowed === null) return null;

  const save = async (next: boolean) => {
    setSaving(true);
    setMsg(null);
    try {
      const res = await saveEngineSettings({ page_actions: next });
      setAllowed(Number(res.saved.page_actions) === 1);
    } catch (err: any) {
      setMsg(readRejection(err, "Could not change whether the assistant may act on the browser tab."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex flex-col gap-2.5 bg-codify-bg border border-codify-border rounded-xl p-3.5">
      <div className="flex items-center gap-2 text-xs font-semibold text-codify-secondary">
        <MousePointerClick className="w-3.5 h-3.5 text-codify-design" />
        Browser tab
      </div>
      <label className="flex items-center gap-2 text-xs text-codify-secondary font-medium">
        <input
          type="checkbox"
          checked={allowed}
          disabled={saving}
          onChange={(e) => save(e.target.checked)}
          className="accent-codify-accent"
        />
        The assistant may open addresses, click and type in the browser tab
      </label>
      <p className={`text-xs leading-relaxed ${allowed ? "text-codify-warning" : "text-codify-muted"}`}>
        {allowed
          ? "An address the assistant opens is sent to that site, and text it types can be sent by the page as it is " +
            "typed. Either can carry anything it has read in your files, and the tab may be signed in to something. " +
            "Every action is shown in the transcript before it is taken."
          : "Off: the assistant can read the page you have open, but cannot open an address, click or type in it."}
      </p>
      {msg && <span className="text-xs text-codify-danger">{msg}</span>}
    </div>
  );
};
