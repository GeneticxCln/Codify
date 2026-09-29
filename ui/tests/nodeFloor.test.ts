/**
 * The Node the suite says it needs is the Node it actually passes on.
 *
 * The README once said "22.6+", which was the floor for `--experimental-strip-types` and
 * not for the suite: on 22.22.2 a loader `load` hook broke jsdom, and on anything older
 * than 22.15 `module.registerHooks` does not exist. The version logic is a pure function
 * so the boundaries can be pinned; the preflight that calls it is `npm test`'s `pretest`.
 */
import { strict as assert } from "node:assert";
import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { NODE_RANGE, nodeSupported } from "../scripts/check-node.mjs";

test("the supported ranges are exactly jsdom's, at their boundaries", () => {
  // ^22.22.2
  assert.equal(nodeSupported("22.22.1"), false);
  assert.equal(nodeSupported("22.22.2"), true);
  assert.equal(nodeSupported("22.23.0"), true);
  assert.equal(nodeSupported("22.9.0"), false);
  // ^24.15.0
  assert.equal(nodeSupported("24.14.9"), false);
  assert.equal(nodeSupported("24.15.0"), true);
  assert.equal(nodeSupported("24.21.0"), true);
  // >=26
  assert.equal(nodeSupported("26.0.0"), true);
  assert.equal(nodeSupported("27.1.0"), true);
});

test("releases jsdom excludes, and versions too old to strip types, are refused", () => {
  for (const v of ["18.20.0", "20.19.0", "21.7.0", "23.11.0", "25.2.0", "22.5.0"]) {
    assert.equal(nodeSupported(v), false, v);
  }
});

test("a version string that is not one is refused rather than let through", () => {
  for (const v of ["", "v", "banana", "24", "24.x.1"]) {
    assert.equal(nodeSupported(v), false, JSON.stringify(v));
  }
  assert.equal(nodeSupported("v24.21.0"), true, "a leading v is what `node --version` prints");
});

test("package.json declares the same range the preflight enforces", () => {
  const pkg = JSON.parse(
    readFileSync(fileURLToPath(new URL("../package.json", import.meta.url)), "utf8"),
  ) as { engines?: { node?: string }; scripts?: Record<string, string> };
  assert.equal(pkg.engines?.node, "^22.22.2 || ^24.15.0 || >=26.0.0");
  assert.match(pkg.scripts?.pretest ?? "", /check-node\.mjs/, "npm test must run the preflight");
  assert.ok(NODE_RANGE.length > 0);
});

test("the preflight passes on this Node, since this suite is running on it", () => {
  const script = fileURLToPath(new URL("../scripts/check-node.mjs", import.meta.url));
  const done = spawnSync(process.execPath, [script], { encoding: "utf8" });
  assert.equal(done.status, 0, done.stderr);
});
