/**
 * Colour schemes: the file that leaves the app and the one that comes back.
 *
 * A format is a promise about a file someone else will read, and the promises
 * worth testing are the ones a person would be annoyed to discover were false.
 * That the JSON parses is not one of them — the interesting claims are the
 * others: that a file can be read by a human as well as by the app, that two
 * exports of the same settings are the same bytes, that a scheme from a
 * *different* install still applies to the themes it is about, and that the
 * half that could not be applied says so rather than passing for a full job.
 *
 * The store in `tint.ts` is the other half of the story and is tested there.
 * What is tested here is only what crossing a process boundary adds: a
 * discriminator so a wrong file is refused, a version so a future one is
 * refused, a label beside every id so the file means something to a person, and
 * a report so a partial import is visible.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { THEMES, themeById } = await import("../src/appearance.ts");
const { contrastRatio } = await import("../src/contrast.ts");
const { applyTintedTheme, resolveTints } = await import("../src/tint.ts");
const {
  DECODE_MESSAGES,
  SCHEME_KIND,
  SCHEME_VERSION,
  copySchemeText,
  decodeScheme,
  describeImportFailure,
  downloadScheme,
  encodeScheme,
  mergeScheme,
  schemeFilename,
} = await import("../src/scheme.ts");

const STORAGE_KEY = "codify.tints";

function fakeStorage(initial: Record<string, string> = {}) {
  const items = new Map<string, string>(Object.entries(initial));
  return {
    getItem: (k: string) => items.get(k) ?? null,
    setItem: (k: string, v: string) => void items.set(k, v),
    removeItem: (k: string) => void items.delete(k),
    items,
  };
}

function fakeStyle() {
  const props = new Map<string, string>();
  return {
    props,
    setProperty: (n: string, v: string) => void props.set(n, v),
    removeProperty: (n: string) => void props.delete(n),
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// Out
// ─────────────────────────────────────────────────────────────────────────────

const store = {
  "toxic-lab": { "--reagent": "#8b1e2d", "--codify-accent": "#ff9f43" },
  "vector-wireframe": { "--vector-line": "#00ff88" },
};

test("a scheme carries every tinted theme, and only tinted ones", () => {
  const scheme = encodeScheme(store);
  assert.equal(scheme.kind, SCHEME_KIND);
  assert.equal(scheme.version, SCHEME_VERSION);
  assert.deepEqual(
    scheme.themes.map((t) => t.id),
    ["vector-wireframe", "toxic-lab"],
    "the order is THEMES', not the store key order — see the byte-stability test",
  );
  assert.deepEqual(scheme.themes[1]!.tints, store["toxic-lab"]);
});

test("a theme with an empty or absent tint map is not in the scheme", () => {
  // A theme whose tints were all corruption, or whose map was pruned to nothing
  // by `saveTints`, is not a theme you have customised. Carrying it would make
  // "2 custom themes" say 3.
  const scheme = encodeScheme({ "still": {}, "toxic-lab": { "--reagent": "#8b1e2d" } });
  assert.equal(scheme.themes.length, 1);
  assert.equal(scheme.name, "Toxic Lab", "one theme borrows its own name");
  assert.equal(encodeScheme({ "toxic-lab": { "--reagent": "#8b1e2d" }, "still": { "--reagent": "red" } }).themes.length, 1);
});

test("the scheme names itself after what is in it", () => {
  assert.equal(encodeScheme({}).name, "No custom colours");
  assert.equal(encodeScheme({ "still": { "--codify-accent": "#112233" } }).name, "Still");
  assert.equal(encodeScheme(store).name, "2 custom themes");
});

test("the file is readable by a person, not only by the app", () => {
  // The whole reason a format exists on top of the store. `{"toxic-lab": …}`
  // is what the store looks like, and it says nothing to the person who opened
  // the file or is deciding whether to trust it.
  const json = JSON.stringify(encodeScheme(store), null, 2);
  assert.match(json, /"label": "Toxic Lab"/, "no human name in the file");
  assert.match(json, /"id": "toxic-lab"/, "no id to resolve with");
  assert.match(json, /#8b1e2d/, "no colour in the file");
  assert.match(json, /"version": 1/, "no version to refuse a future file with");
  // And it is pretty-printed, because a scheme is plausibly kept in a repo
  // next to the thing it themes and a minified file is not diffable.
  assert.match(json, /\n {2}"kind"/, "the file is not indented");
});

test("two exports of the same settings are the same bytes", () => {
  // Which is what makes the file diffable, and it is why `encodeScheme` walks
  // `THEMES` rather than the store's keys: object key order follows insertion
  // order, so a store built by importing a file in a different order would
  // otherwise export to something that looks changed and is not.
  const reordered = {
    "vector-wireframe": { "--vector-line": "#00ff88" },
    "toxic-lab": { "--reagent": "#8b1e2d", "--codify-accent": "#ff9f43" },
  };
  assert.equal(
    JSON.stringify(encodeScheme(store)),
    JSON.stringify(encodeScheme(reordered)),
    "key order leaked into the file",
  );
  assert.equal(
    JSON.stringify(encodeScheme(store)),
    JSON.stringify(encodeScheme(store)),
    "and it is not stable against itself",
  );
});

test("a non-hex never reaches the file", () => {
  // The store is trusted; a hand-edited one is not. Encoding is also the place
  // a bad value would otherwise be laundered — written to a file, handed to
  // someone, and read back as if it had been a choice.
  const scheme = encodeScheme({
    "toxic-lab": { "--reagent": "8b1e2d", "--reagent-skin": 42 as unknown as string },
  });
  assert.equal(scheme.themes.length, 0, "a theme whose values are all corruption is not a scheme");
  const mixed = encodeScheme({ "toxic-lab": { "--reagent": "#8b1e2d", "--reagent-skin": "nope" } });
  assert.deepEqual(mixed.themes[0]!.tints, { "--reagent": "#8b1e2d" });
});

test("the filename follows the audit export's shape", () => {
  assert.equal(
    schemeFilename(new Date("2026-09-28T13:45:00Z")),
    "codify-colours-2026-09-28.json",
  );
  // Two exports of the same settings on the same day are the same file, which a
  // full timestamp would prevent and which a colour scheme has no reason to do.
  assert.equal(
    schemeFilename(new Date("2026-09-28T01:00:00Z")),
    schemeFilename(new Date("2026-09-28T23:59:00Z")),
  );
});

// ─────────────────────────────────────────────────────────────────────────────
// Back in
// ─────────────────────────────────────────────────────────────────────────────

/** The JSON a person would actually paste, and the app would actually read. */
const asFile = (scheme: unknown): string => JSON.stringify(scheme, null, 2);

test("a scheme survives the round trip through text", () => {
  // Through *text*, not through the object: a file is bytes, and JSON's
  // round trip through a string is where a `Map` would quietly stop being one.
  const before = encodeScheme(store);
  const decoded = decodeScheme(JSON.parse(asFile(before)));
  assert.ok(decoded.ok, `a scheme this app wrote did not read back: ${JSON.stringify(decoded)}`);
  assert.deepEqual(decoded.scheme.themes, before.themes);
  assert.equal(decoded.scheme.name, before.name);
});

test("hexes are stored lower-case, so two exports of one setting agree", () => {
  const decoded = decodeScheme(
    JSON.parse(asFile({ kind: SCHEME_KIND, version: 1, themes: [{ id: "still", label: "Still", tints: { "--codify-accent": "#AABBCC" } }] })),
  );
  assert.ok(decoded.ok);
  assert.deepEqual(decoded.scheme.themes[0]!.tints, { "--codify-accent": "#aabbcc" });
});

test("a file that is not a scheme is refused as not-a-scheme", () => {
  // Four shapes, because a scheme arrives from a place where anything can be:
  // a settings file, a git config, a screenshot's text, a half-written file
  // from a crash. Each has to be refused rather than half-read.
  for (const wrong of [
    null,
    42,
    "a string",
    [1, 2, 3],
    { version: 1, themes: [] },
    { kind: "something-else", version: 1, themes: [] },
    { kind: SCHEME_KIND, themes: [] },
    { kind: SCHEME_KIND, version: "1", themes: [] },
    { kind: SCHEME_KIND, version: 1 },
    { kind: SCHEME_KIND, version: 1, themes: {} },
  ]) {
    const decoded = decodeScheme(wrong);
    assert.equal(
      decoded.ok,
      false,
      `${JSON.stringify(wrong)} was accepted as a scheme`,
    );
    assert.equal(
      (decoded as { reason: string }).reason,
      "not-a-scheme",
      `${JSON.stringify(wrong)} was refused for the wrong reason`,
    );
  }
});

test("a scheme from a newer build is refused, and says so", () => {
  // Its own sentence, because "that is not a Codify colour scheme" is the wrong
  // thing to say about a perfectly valid file from a newer one. The person who
  // can act on it is the one who needs to update.
  const decoded = decodeScheme({
    kind: SCHEME_KIND,
    version: SCHEME_VERSION + 1,
    themes: [{ id: "still", label: "Still", tints: { "--codify-accent": "#aabbcc" } }],
  });
  assert.equal(decoded.ok, false);
  assert.equal((decoded as { reason: string }).reason, "newer-version");
  assert.notEqual(DECODE_MESSAGES["newer-version"], DECODE_MESSAGES["not-a-scheme"]);
  for (const [reason, message] of Object.entries(DECODE_MESSAGES)) {
    assert.ok(message.trim().length > 10, `the ${reason} refusal has no sentence`);
  }
});

test("a well-formed scheme with nothing in it is refused rather than applied", () => {
  // The most confusing possible outcome of an import is the app looking exactly
  // as it did before. "Applied" for a scheme that carried no colours is a lie
  // with no visible symptom.
  for (const themes of [[], [{ id: "still", label: "Still", tints: {} }], [null, 7, "x"]]) {
    const decoded = decodeScheme({ kind: SCHEME_KIND, version: 1, themes });
    assert.equal(decoded.ok, false, "an empty scheme was accepted");
    assert.equal((decoded as { reason: string }).reason, "empty");
  }
});

test("an entry with a stale id still finds its theme by label", () => {
  // The label fallback, which is the reason a label is in the file at all. A
  // scheme exported from a build that called this theme something else, or an
  // id that was later changed, is still about Toxic Lab and should still apply.
  const decoded = decodeScheme({
    kind: SCHEME_KIND,
    version: 1,
    themes: [{ id: "toxic-lab-v2", label: "Toxic Lab", tints: { "--reagent": "#8b1e2d" } }],
  });
  assert.ok(decoded.ok);
  assert.equal(
    decoded.scheme.themes[0]!.id,
    "toxic-lab",
    "it was not rewritten to the id this build uses",
  );
  const merged = mergeScheme({}, decoded.scheme);
  assert.deepEqual(merged.store, { "toxic-lab": { "--reagent": "#8b1e2d" } });
  assert.equal(merged.report.applied.length, 1);
});

test("labels are unique, because a scheme matches on them", () => {
  // The precondition the fallback rests on. Two themes sharing a label would
  // make `THEMES.find` in `matchTheme` pick the first, and a scheme meant for
  // the second would quietly repaint the first.
  const labels = THEMES.map((t) => t.label.trim().toLowerCase());
  assert.equal(
    new Set(labels).size,
    labels.length,
    "two themes share a label, so a scheme cannot be matched to one of them",
  );
  const ids = THEMES.map((t) => t.id);
  assert.equal(new Set(ids).size, ids.length, "two themes share an id");
});

// ─────────────────────────────────────────────────────────────────────────────
// Applying
// ─────────────────────────────────────────────────────────────────────────────

test("a theme the scheme names but this build does not have is reported, not dropped", () => {
  const decoded = decodeScheme({
    kind: SCHEME_KIND,
    version: 1,
    themes: [
      { id: "toxic-lab", label: "Toxic Lab", tints: { "--reagent": "#8b1e2d" } },
      { id: "holo-deck-2099", label: "Holo Deck 2099", tints: { "--reagent": "#00ffff" } },
    ],
  });
  assert.ok(decoded.ok);
  const { store: next, report } = mergeScheme({}, decoded.scheme);
  assert.deepEqual(Object.keys(next), ["toxic-lab"], "the theme that did match was not applied");
  assert.equal(report.applied.length, 1);
  assert.equal(report.applied[0]!.label, "Toxic Lab");
  assert.equal(report.skipped.length, 1);
  // By *name*. "One entry could not be applied" is a support question with no
  // answer in it; the person needs to know which theme their friend has.
  assert.equal(report.skipped[0]!.label, "Holo Deck 2099");
});

test("an incoming theme replaces its own tints entirely", () => {
  // Replace, not merge: a scheme that carries two colours for a theme and a
  // local store carrying three means the local one had a third colour the
  // sender never chose, and leaving it would make the app disagree with the
  // thing it was just given.
  const { store: next } = mergeScheme(
    { "toxic-lab": { "--reagent": "#111111", "--reagent-skin": "#222222", "--codify-accent": "#333333" } },
    { kind: SCHEME_KIND, version: 1, name: "x", themes: [{ id: "toxic-lab", label: "Toxic Lab", tints: { "--reagent": "#8b1e2d" } }] },
  );
  assert.deepEqual(next, { "toxic-lab": { "--reagent": "#8b1e2d" } });
});

test("themes the scheme does not mention are left alone", () => {
  // The other half of the same rule. A two-theme file must not be able to clear
  // the other seventeen.
  const before = { "vector-wireframe": { "--vector-line": "#00ff88" } };
  const { store: next } = mergeScheme(before, {
    kind: SCHEME_KIND,
    version: 1,
    name: "x",
    themes: [{ id: "toxic-lab", label: "Toxic Lab", tints: { "--reagent": "#8b1e2d" } }],
  });
  assert.deepEqual(next["vector-wireframe"], { "--vector-line": "#00ff88" });
  assert.equal(Object.keys(next).length, 2);
});

test("an imported colour is stored raw and guarded on arrival, not on import", () => {
  // The sender's machine and the receiver's machine have different surfaces, so
  // a value that was clamped before it was written would arrive pre-clamped
  // against a ground it will never be painted on. The local guard has to be the
  // one that decides — which is also the path a locally-picked colour takes, so
  // a shared scheme and a local pick end up in exactly the same place.
  const unreadable = "#3b0a12";
  const { store: next } = mergeScheme({}, {
    kind: SCHEME_KIND,
    version: 1,
    name: "x",
    themes: [{ id: "toxic-lab", label: "Toxic Lab", tints: { "--codify-accent": unreadable } }],
  });
  assert.equal(
    next["toxic-lab"]!["--codify-accent"],
    unreadable,
    "the value was resolved on import, freezing the sender's surfaces into it",
  );

  const storage = fakeStorage({ [STORAGE_KEY]: JSON.stringify(next) });
  const style = fakeStyle();
  applyTintedTheme("toxic-lab", style, storage);
  const theme = themeById("toxic-lab");
  for (const surface of ["--codify-raised", "--codify-surface", "--codify-bg"] as const) {
    const bg = theme.tokens[surface] as string;
    assert.ok(
      contrastRatio(style.props.get("--codify-accent") as string, bg) >= 4.5,
      `an imported accent landed at ${contrastRatio(style.props.get("--codify-accent") as string, bg).toFixed(2)}:1 on ${bg}`,
    );
  }
  // And the resolved value is what `resolveTints` would have said, so the guard
  // is the same one rather than a second implementation.
  assert.equal(
    style.props.get("--codify-accent"),
    resolveTints(theme, { "--codify-accent": unreadable })["--codify-accent"],
  );
});

test("a scheme can be re-imported over its own export without changing anything", () => {
  // Idempotence, which is the property that makes sharing safe to do twice. A
  // merge that grew the store on each pass would make a second import from the
  // same file a different outcome from the first.
  const once = mergeScheme({}, encodeScheme(store));
  const twice = mergeScheme(once.store, encodeScheme(store));
  assert.deepEqual(twice.store, once.store);
  assert.deepEqual(twice.report, once.report);
});

// ─────────────────────────────────────────────────────────────────────────────
// The two ways out of the app
// ─────────────────────────────────────────────────────────────────────────────

test("the clipboard API is used when it works, and only then", async () => {
  let written = "";
  const outcome = await copySchemeText("{}", {
    writeText: async (t) => void (written = t),
  });
  assert.equal(outcome, "copied");
  assert.equal(written, "{}");
});

test("a rejected clipboard falls back rather than reporting a failure", async () => {
  // `navigator.clipboard` fails silently-by-omission in a webview without the
  // permission, and by rejection where it exists and is refused. A feature that
  // only calls it can appear to work and copy nothing.
  let execed = "";
  const field = {
    value: "",
    style: {} as Record<string, string>,
    setAttribute() {},
    select() {},
  };
  const body = { appendChild() {}, removeChild() {} };
  const outcome = await copySchemeText("payload", {
    writeText: async () => {
      throw new Error("not allowed");
    },
    document: {
      createElement: () => field,
      body,
      execCommand: (c: string) => {
        execed = c;
        return true;
      },
    } as never,
  });
  assert.equal(outcome, "copied-by-fallback");
  assert.equal(execed, "copy", "the fallback did not actually copy");
  assert.equal(field.value, "payload", "the fallback copied an empty field");
});

test("with no clipboard at all it says it failed, rather than claiming success", async () => {
  assert.equal(await copySchemeText("{}", {}), "failed");
  // And a fallback that reports false is a failure, not a quiet success.
  const outcome = await copySchemeText("{}", {
    document: {
      createElement: () => ({ value: "", style: {}, setAttribute() {}, select() {} }),
      body: { appendChild() {}, removeChild() {} },
      execCommand: () => false,
    } as never,
  });
  assert.equal(outcome, "failed");
});

test("the fallback takes its field back out of the document", async () => {
  // A textarea left behind in the body is a focus trap and a visible artefact
  // in every screenshot of the settings modal afterwards.
  let appended = 0;
  let removed = 0;
  await copySchemeText("{}", {
    document: {
      createElement: () => ({ value: "", style: {}, setAttribute() {}, select() {} }),
      body: {
        appendChild: () => void appended++,
        removeChild: () => void removed++,
      },
      execCommand: () => true,
    } as never,
  });
  assert.equal(appended, 1);
  assert.equal(removed, 1, "the copy field was left in the document");
});

test("a read error becomes a sentence, not a blank", () => {
  assert.equal(describeImportFailure(new Error("The file could not be read.")), "The file could not be read.");
  assert.equal(describeImportFailure("no such file"), "no such file");
  assert.equal(describeImportFailure({}), "Could not read that colour scheme.");
  assert.equal(describeImportFailure(undefined), "Could not read that colour scheme.");
});

test("download hands the browser a named, clickable anchor and cleans up the URL", () => {
  let revoked = "";
  const clicked: string[] = [];
  const anchor = { href: "", download: "", click: () => clicked.push("click") };
  const created: string[] = [];
  const name = downloadScheme(
    encodeScheme(store),
    {
      document: {
        createElement: () => {
          created.push("a");
          return anchor;
        },
      } as never,
      createObjectURL: () => {
        created.push("url");
        return "blob:scheme";
      },
      revokeObjectURL: (u) => void (revoked = u),
    },
    new Date("2026-09-28T09:00:00Z"),
  );
  assert.deepEqual(created, ["url", "a"], "the object URL must exist before the anchor uses it");
  assert.equal(anchor.href, "blob:scheme");
  assert.equal(anchor.download, "codify-colours-2026-09-28.json");
  assert.equal(name, anchor.download);
  assert.deepEqual(clicked, ["click"], "the anchor was built and never clicked");
  // Revoked immediately, which is what the other two exporters in this app do
  // and what keeps the blob from being pinned for the life of the window.
  assert.equal(revoked, "blob:scheme");
});

// ─────────────────────────────────────────────────────────────────────────────
// The bar
// ─────────────────────────────────────────────────────────────────────────────

const React = (await import("react")).default;
const { renderToStaticMarkup } = await import("react-dom/server");
const { AppearancePane } = await import("../src/components/AppearancePane.tsx");

const pane = (): string => renderToStaticMarkup(React.createElement(AppearancePane));

test("the bar is closed and inert until there is something to share", () => {
  // A "Copy scheme" button that copies `{"kind":…,"themes":[]}` is a control
  // that appears to do something and produces a file nobody can use — and
  // `decodeScheme` refuses that file as `empty`, so the two ends of the feature
  // would disagree about whether it is worth making. Disabled instead.
  localStorage.setItem("codify.theme", "toxic-lab");
  localStorage.removeItem(STORAGE_KEY);
  const out = pane();
  assert.match(out, /Share custom colours/, "the bar is not on screen at all");
  assert.match(out, /Nothing customised yet/, "and it did not say there is nothing to share");
  assert.match(out, /aria-label="Copy scheme"[^>]*disabled|disabled[^>]*aria-label="Copy scheme"/,
    "Copy is enabled with nothing to copy");
  assert.doesNotMatch(out, /<textarea/, "the import panel is open before anyone asked for it");
  assert.doesNotMatch(out, /Apply pasted scheme/, "and it is offering to apply something");
});

test("the bar names what it would actually put in the file", () => {
  // "Copy scheme" on its own says nothing about *what* gets copied. The line
  // under the heading is the whole difference between a feature and a guess.
  localStorage.setItem("codify.theme", "toxic-lab");
  localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({ "toxic-lab": { "--reagent": "#8b1e2d" }, "still": {} }),
  );
  const out = pane();
  assert.match(out, /One file with 1 tinted theme: Toxic Lab\./, `got: ${out.match(/One file[^<]*/)?.[0]}`);
  // `still` had an empty map, so it is not in the scheme and is not named.
  assert.doesNotMatch(out, /One file with 2 tinted themes/);
  localStorage.removeItem(STORAGE_KEY);
});

test("every control in the bar has a name", () => {
  // Only the collapsed state is renderable here — `renderToStaticMarkup` runs
  // no clicks, so the import panel's contents cannot be reached without a real
  // DOM. Its controls are held by the wiring test below instead, which reads
  // the source. Asserting on a panel that has not been opened would be the
  // easy way to be confident about markup nobody has seen.
  localStorage.setItem("codify.theme", "toxic-lab");
  const out = pane();
  for (const label of ["Copy scheme", "Download", "Import…"]) {
    assert.ok(out.includes(`aria-label="${label}"`), `no accessible name for ${label}`);
  }
  // The one that opens a panel says so, rather than being a button whose effect
  // is invisible until it is pressed.
  assert.match(out, /aria-label="Import…"[^>]*aria-expanded="false"|aria-expanded="false"[^>]*aria-label="Import…"/,
    "the import button does not declare the state of what it opens");
});

test("the import path goes through the same decode and merge the tests cover", () => {
  // `renderToStaticMarkup` runs no effects and no clicks, so the interactive
  // half cannot be driven from here — which is the reason the pure half is in
  // this file at all, and the reason this asserts the *wiring* rather than
  // pretending to test the behaviour. Read as contract, like
  // `appearancePane.test.ts` does for the radiogroup's arrow keys.
  const source = readFileSync(
    new URL("../src/components/AppearancePane.tsx", import.meta.url),
    "utf8",
  );
  assert.match(source, /decodeScheme\(/, "the import does not decode through the tested decoder");
  assert.match(source, /mergeScheme\(/, "the import does not merge through the tested merger");
  assert.match(source, /DECODE_MESSAGES\[decoded\.reason\]/, "and does not use the tested sentences");
  // One `JSON.parse`, for the paste path. A second one somewhere in the import
  // flow is a second implementation of "is this a scheme", and the thing that
  // decides that is precisely what the tests above hold.
  const parses = source.match(/JSON\.parse\(/g) ?? [];
  assert.equal(parses.length, 1, "there is more than one place that reads a scheme as JSON");
  // And it reaches the same commit the colour wells do, so an import is not a
  // second way for the store and the screen to disagree.
  assert.match(
    source,
    /const commitImported = \(next: TintStore, label: string\): void => \{\s*commit\(selected, next, label\);/,
    "the import does not reach the pane's one commit path — and through it, undo",
  );
  assert.match(
    source,
    /onImport=\{\s*commitImported\s*\}/,
    "the import bypasses the history-recording commit",
  );
  // The file input — its name, its reachability by keyboard, and the reset that
  // makes a second pick of the *same* file fire again — used to be four regexes
  // against this file. They are now three tests in `appearanceInteraction.test.ts`
  // that open the panel and use the control, which is the whole difference: a
  // regex on `className="sr-only"` passes if the class is spelled right, and
  // `input.focus()` passes only if the element really is reachable.
  //
  // What is left here is the part the DOM cannot reach, which is the part about
  // *shape*: one place that decides whether something is a scheme, and the two
  // roles that announce a refusal and a report.
  assert.match(source, /role="alert"/, "a failed import is not announced");
  assert.match(source, /role="status"/, "and neither is a report or a confirmation");
});

test("importing a scheme a stranger sent does not clear the themes it omits", () => {
  // The end-to-end claim, through the same three functions the UI calls and
  // through `applyTintedTheme`, with a local store that has a theme the scheme
  // says nothing about. This is the version of the rule a person would notice.
  localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({ "vector-wireframe": { "--vector-line": "#00ff88" } }),
  );
  const incoming = {
    kind: SCHEME_KIND,
    version: 1,
    name: "Toxic Lab",
    themes: [
      { id: "toxic-lab", label: "Toxic Lab", tints: { "--reagent": "#8b1e2d" } },
      { id: "holo-deck-2099", label: "Holo Deck 2099", tints: { "--reagent": "#00ffff" } },
    ],
  };
  const decoded = decodeScheme(JSON.parse(asFile(incoming)));
  assert.ok(decoded.ok);
  const { store: next, report } = mergeScheme(
    { "vector-wireframe": { "--vector-line": "#00ff88" } },
    decoded.scheme,
  );
  assert.deepEqual(Object.keys(next).sort(), ["toxic-lab", "vector-wireframe"]);
  assert.deepEqual(report.skipped.map((s) => s.label), ["Holo Deck 2099"]);

  // And the untouched theme still paints its own colour after the merge.
  const style = fakeStyle();
  applyTintedTheme("vector-wireframe", style, fakeStorage({ [STORAGE_KEY]: JSON.stringify(next) }));
  assert.equal(style.props.get("--vector-line"), "#00ff88");
  const toxic = fakeStyle();
  applyTintedTheme("toxic-lab", toxic, fakeStorage({ [STORAGE_KEY]: JSON.stringify(next) }));
  // The *resolved* value, not the raw one — and specifically a red rather than
  // the `#fefbfb` a one-directional sweep hands back when the proposal already
  // sits above every surface. See `clampForContrast`.
  const resolved = toxic.props.get("--reagent") as string;
  assert.notEqual(resolved, "#fefbfb", "the clamp walked the wrong way and returned its last candidate");
  const green = Number.parseInt(resolved.slice(1, 3), 16);
  const blue = Number.parseInt(resolved.slice(5, 7), 16);
  assert.ok(green > blue, `${resolved} is not a red any more — the hue did not survive the clamp`);
  assert.ok(
    contrastRatio(resolved, "#12271a") >= 3,
    `the imported reagent landed at ${contrastRatio(resolved, "#12271a").toFixed(2)}:1`,
  );
  localStorage.removeItem(STORAGE_KEY);
});
