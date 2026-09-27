/**
 * The logo, pinned where a render test cannot reach it.
 *
 * `Logo.tsx` is an `<img>` — rendering it proves only that the src is spelled
 * right. Everything that makes the mark *the mark* lives in the bytes of
 * `ui/public/logo.gif`, and nothing else in either suite would notice those
 * bytes changing: the GIF is a checked-in asset, so a bad regeneration would
 * ship silently. So this file walks the GIF structure directly — signature,
 * colour table, frame count, frame delays, the loop extension — and holds the
 * three facts the brand contract names: it loops, it blinks, it is drawn in
 * exactly the palette DESIGN.md §2 defines. No Pillow, no image library: a
 * GIF's block structure is small enough to read with arithmetic, which also
 * keeps `make test-ui` free of a dependency only one test wants.
 *
 * The colour assertion is the one that stops a silent repaint. The palette of
 * this GIF is not *derived from* the tokens — it is the tokens, four colours,
 * and any sixth shade of green a regeneration introduces lands here as a
 * failure with the offending hex in the message.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { Logo } = await import("../src/components/ui/Logo.tsx");
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const GIF = readFileSync(path.join(HERE, "..", "public", "logo.gif"));
const STATIC_GIF = readFileSync(path.join(HERE, "..", "public", "logo-static.gif"));

/** The brand palette, quoted from DESIGN.md §2 — the same four hexes
 * `scripts/make_logo.py` draws with. Listed as ints so a palette entry can be
 * compared by value rather than by string. */
const BRAND_PALETTE: number[] = [0x0d1117, 0x3fb950, 0x30363d, 0xe6edf3];

/** Big-endian triple of palette bytes → one 0xRRGGBB int. */
const rgb = (b: Uint8Array, o: number): number =>
  (b[o] << 16) | (b[o + 1] << 8) | b[o + 2];

const u16le = (b: Uint8Array, o: number): number => b[o] | (b[o + 1] << 8);

/**
 * Walk the block structure of one GIF far enough to answer for itself.
 *
 * This reads the parts of the format the logo depends on and refuses anything
 * else — an unknown extension or block means the writer changed, and a freeze
 * that silently skips what it does not recognise would not notice.
 */
function walkGif(
  b: Uint8Array,
  // The animated logo needs GIF89a — the looping Application Extension does
  // not exist in 87a. A single-frame still is valid in either flavour, so the
  // static companion is checked with `accept` rather than dictated.
  accept: [string, string] = ["GIF89a", "GIF89a"],
): {
  width: number;
  height: number;
  globalPalette: number[] | null;
  frames: { delayMs: number }[];
  loopCount: number | null;
} {
  const signature = Buffer.from(b.slice(0, 6)).toString("latin1");
  assert.ok(
    signature === accept[0] || signature === accept[1],
    `not a GIF file: ${signature}`,
  );
  const width = u16le(b, 6);
  const height = u16le(b, 8);
  const packed = b[10];
  let offset = 13;
  let globalPalette: number[] | null = null;
  if (packed & 0x80) {
    const entries = 2 ** ((packed & 0x07) + 1);
    globalPalette = [];
    for (let i = 0; i < entries; i++) globalPalette.push(rgb(b, offset + i * 3));
    offset += entries * 3;
  }
  const frames: { delayMs: number }[] = [];
  let loopCount: number | null = null;
  while (offset < b.length) {
    const block = b[offset];
    if (block === 0x3b) break; // trailer
    if (block === 0x21) {
      const label = b[offset + 1];
      if (label === 0xf9) {
        // Graphic Control Extension: 0x21 0xF9 0x04 <flags> <delay*10ms> <transparent> 0x00
        frames.push({ delayMs: u16le(b, offset + 4) * 10 });
        offset += 8;
      } else if (label === 0xff) {
        // Application Extension; NETSCAPE2.0 is the looping one.
        const name = Buffer.from(b.slice(offset + 3, offset + 14)).toString("latin1");
        if (name === "NETSCAPE2.0") {
          assert.equal(b[offset + 15], 0x01, "unexpected NETSCAPE sub-block id");
          loopCount = u16le(b, offset + 16);
        }
        offset += 19; // 2 + 1 + 1 + 11 + 1 + 2 + 1, whatever the app id
      } else {
        assert.fail(`unexpected extension label 0x${label.toString(16)}`);
      }
    } else if (block === 0x2c) {
      // Image Descriptor: 10 header bytes, optional local table, LZW sub-blocks.
      const localPacked = b[offset + 9];
      offset += 10;
      if (localPacked & 0x80) {
        const entries = 2 ** ((localPacked & 0x07) + 1);
        offset += entries * 3;
      }
      offset += 1; // LZW minimum code size
      while (b[offset] !== 0) offset += b[offset] + 1; // data sub-blocks
      offset += 1; // the terminator this loop stopped on
    } else {
      assert.fail(`unexpected block 0x${block.toString(16)} at ${offset}`);
    }
  }
  return { width, height, globalPalette, frames, loopCount };
}

test("logo.gif is the blinking three-frame loop the contract names", () => {
  const { frames, loopCount } = walkGif(GIF);
  assert.equal(frames.length, 3, "the loop is solid → hollow → bright, so three frames");
  assert.deepEqual(
    frames.map((f) => f.delayMs),
    [160, 160, 550],
    "blink pacing: two quick steps, a long beat on the bright frame",
  );
  assert.equal(loopCount, 0, "loop forever — the sanctioned exception, not a one-shot");
});

test("logo.gif is drawn in exactly the brand palette — no sixth green", () => {
  const { globalPalette } = walkGif(GIF);
  assert.ok(globalPalette, "the GIF must carry a global colour table");
  assert.equal(globalPalette.length, 4);
  assert.deepEqual(
    [...globalPalette].sort((a, b) => a - b),
    [...BRAND_PALETTE].sort((a, b) => a - b),
    "the palette is not the contract's. Only #0d1117, #3fb950, #30363d, #e6edf3 may appear.",
  );
});

test("logo.gif is square at its render size", () => {
  const { width, height } = walkGif(GIF);
  assert.equal(width, 256);
  assert.equal(height, 256);
});

test("logo-static.gif is the one-frame resting pose reduced motion gets", () => {
  const { frames, loopCount, globalPalette } = walkGif(STATIC_GIF, [
    "GIF87a",
    "GIF89a",
  ]);
  assert.ok(frames.length <= 1, "the static companion must not animate");
  assert.equal(loopCount, null, "a still does not loop");
  assert.ok(globalPalette, "the static companion must carry the brand palette too");
  assert.deepEqual(
    [...globalPalette].sort((a, b) => a - b),
    [...BRAND_PALETTE].sort((a, b) => a - b),
  );
});

test("the Tauri icon set carries the same mark, not the default squares", () => {
  // The desktop icon is the same mark; if the bundle regresses to the default
  // Tauri squares it does so as a different pixel size than this mark renders.
  const png = readFileSync(path.join(HERE, "..", "..", "src-tauri", "icons", "32x32.png"));
  // PNG signature, then IHDR: width/height are big-endian at bytes 16..24.
  assert.equal(
    Buffer.from(png.slice(0, 8)).toString("latin1"),
    "\u0089PNG\r\n\u001a\n",
    "icons/32x32.png is not a PNG",
  );
  assert.equal(png.readUInt32BE(16), 32);
  assert.equal(png.readUInt32BE(20), 32);
});

// ── the component half ──────────────────────────────────────────────────────

const markup = (props: Record<string, unknown>): string =>
  renderToStaticMarkup(React.createElement(Logo, props));

test("Logo renders the animated asset by default", () => {
  const out = markup({ size: 24 });
  assert.match(out, /src="\/logo\.gif"/);
  // Decorative by position: the wordmark beside it says the name already.
  assert.match(out, /alt=""/);
  assert.match(out, /width="24"/);
  assert.match(out, /height="24"/);
});

test("Logo serves the static companion when motion is reduced", () => {
  // Explicit, because the test process owns no media query to ask.
  const out = markup({ size: 24, animated: false });
  assert.match(out, /src="\/logo-static\.gif"/);
  assert.doesNotMatch(out, /logo\.gif/, "the animated asset must not be reachable");
});

test("an explicit animated prop overrides the default in both directions", () => {
  assert.match(markup({ animated: true }), /src="\/logo\.gif"/);
});
