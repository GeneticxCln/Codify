/**
 * Every field that can hold a credential is marked as one the clipboard history never reads.
 *
 * A password field is private by its type, but the key fields in this app have an eye button that turns them into
 * plain text fields, and a text field is not private by type. Each one that can ever be `type="password"` has to
 * say `data-clipboard="off"` as well, or showing a key makes its copy and its paste into history. This reads the
 * source, because the failure it guards is a new key field added by someone who did not know the history exists:
 * the mounted tests only cover the fields that were known when they were written.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

const SRC = join(import.meta.dirname, "..", "src");

function sources(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory()
      ? sources(join(dir, entry.name))
      : entry.name.endsWith(".tsx")
        ? [join(dir, entry.name)]
        : [],
  );
}

/** Every `<input ...>` element's opening tag, as written. */
function inputTags(text: string): string[] {
  return [...text.matchAll(/<input\b[\s\S]*?\/>/g)].map((m) => m[0]);
}

test("every input that can be a password field says data-clipboard=\"off\"", () => {
  const unmarked: string[] = [];
  let seen = 0;
  for (const file of sources(SRC)) {
    for (const tag of inputTags(readFileSync(file, "utf8"))) {
      if (!/password/.test(tag)) continue;
      seen += 1;
      if (!/data-clipboard="off"/.test(tag)) unmarked.push(`${file.slice(SRC.length + 1)}: ${tag.split("\n")[1]?.trim() ?? tag.slice(0, 60)}`);
    }
  }
  assert.ok(seen >= 2, `expected to find the key fields (found ${seen}); the scan has stopped seeing them`);
  assert.deepEqual(unmarked, [], "a key field that is a text field once shown is read by the clipboard history");
});

test("the scan reads an input that spans lines and has a nested expression", () => {
  const sample = `<input\n  type={show ? "text" : "password"}\n  onChange={(e) => set(e.target.value)}\n  data-clipboard="off"\n/>`;
  const [tag] = inputTags(sample);
  assert.ok(tag && /password/.test(tag) && /data-clipboard="off"/.test(tag));
  const unmarked = inputTags(`<input type="password" value={v} />`)[0];
  assert.ok(unmarked && /password/.test(unmarked) && !/data-clipboard/.test(unmarked));
});
