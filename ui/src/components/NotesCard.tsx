import React, { useEffect, useState } from "react";
import { addNote, deleteNote, getEngineSettings, listNotes, saveEngineSettings } from "../api";
import { readRejection } from "../rejection.ts";
import type { NotesListing } from "../types";
import { Checkbox } from "./ui/Checkbox";

/** The engine's own limit (`engine/notes.py` `MAX_NOTE_CHARS`); it refuses a longer one, this only says so first. */
const MAX_NOTE_CHARS = 500;

/**
 * Standing notes (docs/10 §8): short preferences a person writes once and wants every project to see.
 *
 * The one memory that crosses a workspace, so the card says what it is before it lets anyone turn it on. Two things
 * live here and they are different acts: the switch (`cross_workspace_notes`, off on a fresh install, which decides
 * whether the assistant is shown any note at all) and the notes themselves. Only a person writes them: nothing the
 * assistant reads can add one, which is the line that keeps this from being a hole in the per-project wall.
 *
 * The card is absent on an engine that predates the setting, like the other cards.
 */
export const NotesCard: React.FC = () => {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [listing, setListing] = useState<NotesListing | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([getEngineSettings(), listNotes()])
      .then(([settings, notes]) => {
        if (cancelled || !settings?.cross_workspace_notes) return;
        setEnabled(settings.cross_workspace_notes.value === 1);
        setListing(notes);
      })
      .catch(() => {
        // An engine without notes has nothing to show; the card stays absent.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (enabled === null || listing === null) return null;

  const switchTo = async (next: boolean) => {
    const previous = enabled;
    setEnabled(next);
    setBusy(true);
    setMessage(null);
    try {
      const result = await saveEngineSettings({ cross_workspace_notes: next });
      setEnabled(Number(result.saved.cross_workspace_notes) === 1);
      setMessage({ ok: true, text: "Saved — this applies to the next turn." });
    } catch (error: unknown) {
      setEnabled(previous);
      setMessage({ ok: false, text: readRejection(error, "Could not change the notes setting.") });
    } finally {
      setBusy(false);
    }
  };

  const add = async () => {
    const text = draft.trim();
    if (!text) return;
    setBusy(true);
    setMessage(null);
    try {
      const made = await addNote(text);
      setListing({ ...listing, notes: [...listing.notes, made] });
      setDraft("");
      setMessage({ ok: true, text: "Kept." });
    } catch (error: unknown) {
      // The text stays in the box: a refused note is one to edit, not to type again.
      setMessage({ ok: false, text: readRejection(error, "Could not keep that note.") });
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: string) => {
    setBusy(true);
    setMessage(null);
    try {
      await deleteNote(id);
      setListing({ ...listing, notes: listing.notes.filter((note) => note.id !== id) });
      setMessage({ ok: true, text: "Deleted." });
    } catch (error: unknown) {
      setMessage({ ok: false, text: readRejection(error, "Could not delete that note.") });
    } finally {
      setBusy(false);
    }
  };

  const full = listing.notes.length >= listing.max_notes;
  const tooLong = draft.trim().length > MAX_NOTE_CHARS;

  return (
    <div className="flex flex-col gap-2.5 bg-codify-bg border border-codify-border rounded-xl p-3.5">
      <div className="text-xs font-semibold text-codify-secondary">Standing notes</div>
      <Checkbox checked={enabled} disabled={busy} onChange={(next) => void switchTo(next)}>
        Show my standing notes to the assistant
      </Checkbox>
      <p className="text-xs text-codify-muted leading-relaxed">
        Off by default. Notes are short preferences you want every project to see, such as a style or a habit. They
        are preferences, not commands: your request and Codify&apos;s own rules come first. Only you write them, and
        nothing the assistant reads can add one. With this off, none of them is shown.
      </p>

      {listing.notes.length === 0 ? (
        <p className="text-xs text-codify-muted">No notes yet.</p>
      ) : (
        <ul className="flex flex-col gap-1.5" aria-label="Your standing notes">
          {listing.notes.map((note) => (
            <li
              key={note.id}
              className="flex items-start justify-between gap-2 bg-codify-surface border border-codify-border rounded-lg px-2.5 py-1.5"
            >
              <span className="text-xs text-codify-secondary whitespace-pre-wrap break-words min-w-0">
                {note.text}
                {note.source === "imported" && (
                  <span className="ml-1.5 text-2xs text-codify-muted">(imported from a profile)</span>
                )}
              </span>
              <button
                type="button"
                onClick={() => void remove(note.id)}
                disabled={busy}
                aria-label={`Delete note: ${note.text.slice(0, 40)}`}
                className="text-2xs text-codify-muted hover:text-codify-danger shrink-0 disabled:opacity-40"
              >
                Delete
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="flex flex-col gap-1.5 border-t border-codify-border pt-2.5">
        <label htmlFor="new-standing-note" className="text-xs text-codify-secondary font-medium">
          New note
        </label>
        <textarea
          id="new-standing-note"
          value={draft}
          onChange={(event) => {
            setDraft(event.target.value);
            setMessage(null);
          }}
          rows={2}
          disabled={busy || full}
          className="w-full bg-codify-surface border border-codify-border rounded-lg px-2.5 py-1.5 text-xs text-codify-secondary focus:outline-hidden focus:border-codify-accent disabled:opacity-40"
        />
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => void add()}
            disabled={busy || full || !draft.trim() || tooLong}
            className="px-3 py-1.5 bg-codify-accent text-codify-bg hover:brightness-110 text-xs font-semibold rounded-lg transition-colors disabled:opacity-40"
          >
            Add note
          </button>
          <span className={`text-2xs ${tooLong ? "text-codify-danger" : "text-codify-muted"}`}>
            {draft.trim().length} / {MAX_NOTE_CHARS} · {listing.notes.length} of {listing.max_notes} kept
          </span>
          {full && (
            <span className="text-2xs text-codify-warning">
              The list is full: delete a note to add another.
            </span>
          )}
        </div>
      </div>
      {message && (
        <p className={`text-xs ${message.ok ? "text-codify-muted" : "text-codify-danger"}`} role="status">
          {message.text}
        </p>
      )}
    </div>
  );
};
