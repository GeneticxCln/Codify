/**
 * The Settings screen and the statistics drawer are not in the entry chunk.
 *
 * The entry chunk was 640 kB and the build warned about it. Both of these open on a click, never on first
 * paint, and `SettingsModal` carries every Settings card with it, so they load on demand. This reads the
 * source because the property is about how the module graph is cut, which no mounted App can show: a
 * static `import` would pass every behavioural test and put the weight back.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const app = readFileSync(path.join(here, "..", "src", "App.tsx"), "utf8");

for (const name of ["SettingsModal", "StatsPanel"]) {
  test(`${name} is loaded on demand, not imported statically`, () => {
    assert.doesNotMatch(
      app,
      new RegExp(`^import\\s*\\{[^}]*\\b${name}\\b[^}]*\\}\\s*from\\s*"\\./components/${name}"`, "m"),
      `App.tsx imports ${name} statically, which puts it back in the entry chunk`,
    );
    assert.match(app, new RegExp(`const ${name} = lazy\\(\\(\\) => import\\("\\./components/${name}"\\)`));
  });
}

test("the lazily loaded Settings screen is mounted under a Suspense boundary", () => {
  assert.match(app, /<Suspense fallback=\{null\}>\s*<SettingsModal/);
  assert.match(app, /<Suspense fallback=\{null\}>\s*<StatsPanel/);
});
