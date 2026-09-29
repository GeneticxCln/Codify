// Refuse to run the UI suite on a Node it cannot pass on, in one sentence.
//
// Before this existed the failure was 161 tests reporting `ERR_VM_MODULE_LINK_FAILURE`
// on Node 22.22.2 and a green run on Node 24, and nothing said which Node the suite
// wanted. The README said "22.6+", which was the floor for `--experimental-strip-types`
// and not for the suite: the loader also needs `module.registerHooks` (22.15), and
// jsdom 30 declares the range below. It is plain `.mjs` on purpose — this has to run
// on a Node too old to strip types, which is exactly when it matters.
//
// The range is jsdom's own `engines`, so that "a Node jsdom supports" and "a Node the
// suite supports" cannot drift apart silently. Odd-numbered releases are excluded
// because jsdom excludes them.

/** `[major, minor, patch]` of a `process.versions.node` string; NaN parts fail every test below. */
function parts(version) {
  return String(version)
    .replace(/^v/, "")
    .split(".")
    .slice(0, 3)
    .map((n) => Number.parseInt(n, 10));
}

/** Whether `version` is in `^22.22.2 || ^24.15.0 || >=26.0.0`. */
export function nodeSupported(version) {
  const [major, minor, patch] = parts(version);
  if (![major, minor, patch].every(Number.isInteger)) return false;
  if (major === 22) return minor > 22 || (minor === 22 && patch >= 2);
  if (major === 24) return minor >= 15;
  return major >= 26;
}

export const NODE_RANGE = "22.22.2 or newer 22, 24.15 or newer 24, or 26+";

if (import.meta.url === `file://${process.argv[1]}`) {
  const have = process.versions.node;
  if (!nodeSupported(have)) {
    console.error(
      `The UI tests need Node ${NODE_RANGE}; this is Node ${have}. ` +
        "jsdom 30 declares that range and the suite's loader needs module.registerHooks, " +
        "so an older Node fails with unrelated-looking module errors rather than a clear one.",
    );
    process.exit(1);
  }
}
