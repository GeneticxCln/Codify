/**
 * The About tab's download bar, as arithmetic and words.
 *
 * The thing worth pinning here is the case a bar gets wrong quietly: a download GitHub sends no
 * `Content-Length` for. A bar fed `NaN` looks like a bar at 0%, and a bar that sits at 0% for the
 * whole of a download that is going fine is worse than no bar — it says the download has stopped.
 * So the size-less case is a state of its own, the pane's fallback to the size the *release*
 * reported comes before it, and both are asserted here rather than in a DOM.
 *
 * `readUpdateProgress` is the same file's subject: the pane's numbers arrive across a process
 * boundary, so a payload that is the right shape with the wrong types has to move nothing rather
 * than reach the bar as `NaN`.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import { readUpdateProgress } from "../src/shellEvents.ts";
import { size, updateLine, updatePercent } from "../src/updateProgress.ts";

test("a size is printed the way a release page prints it", () => {
  // Decimal megabytes, and the same rounding the pane's sentences use: 18,000,000 bytes is
  // "18 MB" on the release page and would be "17 MB" in binary mebibytes, and two numbers for one
  // file read as two files.
  assert.equal(size(18_000_000), "18 MB");
  assert.equal(size(90_000_000), "90 MB");
  assert.equal(size(1_500_000), "1.5 MB");
  assert.equal(size(400_000), "0.4 MB");
});

test("a download with a size says how much of it has arrived", () => {
  const progress = { received: 5_400_000, total: 18_000_000 };
  assert.equal(updatePercent(progress, null), 30);
  assert.equal(updateLine(progress, null), "5.4 MB of 18 MB — 30%");
});

test("a percentage is floored, so the bar is never full while bytes are still coming", () => {
  // 99.6% of a 90 MB AppImage: a rounded bar would read 100% and the person would wonder why the
  // file had not landed. The last percent arrives when the file does.
  const almost = { received: 89_640_000, total: 90_000_000 };
  assert.equal(updatePercent(almost, null), 99);
  assert.equal(updatePercent({ received: 90_000_000, total: 90_000_000 }, null), 100);
});

test("a size the server never announced falls back to the size the release reported", () => {
  // GitHub streams an asset through a redirect and does not always send a length. The release
  // itself published one, and the pane was already showing it in the sentence above the bar.
  const progress = { received: 9_000_000, total: null };
  assert.equal(updatePercent(progress, 18_000_000), 50);
  assert.equal(updateLine(progress, 18_000_000), "9.0 MB of 18 MB — 50%");
});

test("with no size from anywhere it says so instead of drawing a bar at zero", () => {
  const progress = { received: 9_000_000, total: null };
  assert.equal(updatePercent(progress, null), null, "0% is a claim about a total nobody has");
  assert.equal(
    updateLine(progress, null),
    "9.0 MB downloaded, and GitHub did not say how big the file is.",
  );
  // And a total of zero, which a broken server could send, is "no size" rather than a division.
  assert.equal(updatePercent({ received: 1, total: 0 }, 0), null);
});

test("nothing announced yet reads as starting, not as finished and not as a percentage", () => {
  // The state between the click and the first byte: `null` is not 0%, and the caption does not
  // claim any bytes have arrived.
  assert.equal(updatePercent(null, 18_000_000), null);
  assert.equal(updateLine(null, 18_000_000), "Starting the download…");
  assert.equal(updateLine(null, null), "Starting the download…");
});

test("more bytes than the announced size is a full bar, never a bar past the end", () => {
  // A lying `Content-Length` — the shell refuses an over-size body, but the bar must not be drawn
  // at 120% in the meantime.
  assert.equal(updatePercent({ received: 20_000_000, total: 18_000_000 }, null), 100);
});

test("a progress payload that is not one moves nothing", () => {
  assert.deepEqual(readUpdateProgress({ received: 1_000, total: 18_000_000 }), {
    received: 1_000,
    total: 18_000_000,
  });
  // A server that announced no size: absent and null are the same answer, and it is "unknown".
  assert.deepEqual(readUpdateProgress({ received: 1_000 }), { received: 1_000, total: null });
  assert.deepEqual(readUpdateProgress({ received: 1_000, total: null }), {
    received: 1_000,
    total: null,
  });
  for (const payload of [
    null,
    "update-progress",
    42,
    {},
    { received: "1,000" },
    { received: -1 },
    { received: 1.5 },
    { received: 1_000, total: "18 MB" },
    { received: 1_000, total: 0 },
    { received: 1_000, total: -5 },
  ]) {
    assert.equal(
      readUpdateProgress(payload),
      null,
      `${JSON.stringify(payload)} was read as a progress report`,
    );
  }
});
