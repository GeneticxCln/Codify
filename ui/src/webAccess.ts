/**
 * What the web-access setting means, as arithmetic rather than a component.
 *
 * `web_fetch` decides whether the conductor's `fetch_page` may read a public web page, and for which sites
 * (engine/web_fetch.py, docs/12). It is the one setting where the engine itself sends a request to an address a
 * model chose, so the sentence a person reads here is the whole of the informed consent: what each choice lets
 * through, and, for the two that let anything through, the thing no rule can prevent. This module is the half
 * worth pinning away from a DOM.
 *
 * What it does not do is decide which entries of the site list are valid. The engine owns that rule
 * (`parse_hosts`), refuses a bad list with a sentence, and the card shows that sentence. A second copy here
 * would be a second rule to get wrong, and the same reason `browser::parse_navigation` has no mirror in the
 * engine (docs/03 §1.6).
 */

import { readErrorBody } from "./errorBody.ts";

/** The three states of `web_fetch`, in the order the picker shows them: narrowest first, and the widest is the default. */
export const WEB_MODES = [
  { value: 0, label: "Off" },
  { value: 1, label: "Only the sites I list" },
  { value: 2, label: "Any public site" },
] as const;

export type WebMode = (typeof WEB_MODES)[number]["value"];

/** What the person has chosen, and whether it can do anything. */
export type WebAccessStatus =
  /** Off: the conductor has no `fetch_page`. */
  | { kind: "off"; text: string }
  /** A list mode with nothing listed: on, and allowing nothing, so the tool is not offered. */
  | { kind: "empty"; text: string }
  /** A list mode with sites: on for exactly those. */
  | { kind: "listed"; text: string }
  /** Any public site: the widest, and said as plainly as the engine says it. */
  | { kind: "any"; text: string };

/** The mode a stored value means. A value outside the three is off, never on: the engine reads it the same way. */
export function webMode(stored: number | null | undefined): WebMode {
  return stored === 1 || stored === 2 ? stored : 0;
}

/** The site names in a list as typed, split the way the engine splits them (commas, semicolons, whitespace). */
export function listedSites(raw: string): string[] {
  return raw
    .split(/[\s,;]+/)
    .map((entry) => entry.trim())
    .filter((entry) => entry.length > 0);
}

/**
 * What a mode and a list mean together, in the words the card shows.
 *
 * Both on-states say that the address is sent to the site and can carry anything the assistant has read, because
 * that is true of every request the tool makes and the engine cannot tell it from an ordinary one
 * (docs/03 §1.6). The list is the narrower choice, and the text says so rather than implying it is a fence.
 */
export function webAccessStatus(mode: WebMode, hosts: string): WebAccessStatus {
  const sites = listedSites(hosts);
  if (mode === 0) {
    return {
      kind: "off",
      text: "The assistant cannot fetch web pages. It can still read the page you have open in the browser tab.",
    };
  }
  if (mode === 1) {
    if (sites.length === 0) {
      return {
        kind: "empty",
        text: "Listed sites only, and the list is empty, so nothing can be fetched and the assistant is not offered the tool. Add a site such as docs.python.org.",
      };
    }
    const noun = sites.length === 1 ? "site" : "sites";
    return {
      kind: "listed",
      text:
        `The assistant may fetch pages from ${sites.length} ${noun} you listed, and their subdomains. ` +
        "Each address it asks for is sent to that site and can carry anything the assistant has read in your files, " +
        "so list only sites you would be comfortable receiving that. Every fetch is shown in the transcript before it is made.",
    };
  }
  return {
    kind: "any",
    text:
      "The assistant may fetch any public web page. Each address it asks for is sent to that site and can carry " +
      "anything the assistant has read in your files, and nothing here can tell that from an ordinary request. " +
      "Pages on this machine or a private network are always refused. Every fetch is shown in the transcript before it is made.",
  };
}

/** Whether the draft differs from what is stored, so Save is not offered for a change that is not one. */
export function webAccessDirty(
  stored: { mode: number; hosts: string },
  draft: { mode: number; hosts: string },
): boolean {
  return (
    webMode(stored.mode) !== webMode(draft.mode) ||
    listedSites(stored.hosts).join(" ") !== listedSites(draft.hosts).join(" ")
  );
}

/**
 * The sentence in a refused save, from what `saveEngineSettings` threw.
 *
 * It throws the response text, which for a refusal is the engine's `{code, message}` body. The message is the
 * useful part (for a bad site list it names the entries that were refused), so it is read out rather than shown
 * as JSON. Text that is not that shape is shown as it is, and an empty one is the caller's own sentence.
 */
export function refusalSentence(thrown: string, fallback: string): string {
  try {
    const sentence = readErrorBody(JSON.parse(thrown)).message;
    if (sentence.trim()) return sentence;
  } catch {
    // Not JSON: a transport failure or an older engine, whose own words are the better ones.
  }
  return thrown.trim() ? thrown : fallback;
}
