/**
 * What a browser address becomes, and who is allowed to answer it.
 *
 * The shell (`src-tauri/src/browser.rs`) is the enforcement point: its
 * `navigation_allowed` sees every navigation a page attempts, redirects
 * included, and no caller routes around it. This module is not a second
 * guard. It exists for the two cases where waiting for the shell is the
 * wrong answer:
 *
 * 1. **An address the shell will refuse is refused before the round trip.**
 *    Asking the shell for a scheme the guard refuses used to come back as an
 *    error after the user had already watched the sheet seat and fail;
 *    classifying first puts the refusal where the typing happened, in the
 *    same frame, and never seats a sheet for an address that cannot load.
 * 2. **The refusal is one sentence in one place.** `refuse` carries the
 *    shell's own wording, byte for byte, so the sentence has exactly one
 *    owner and this file is a mirror of it — pinned in both directions by
 *    `ui/tests/browserDispatch.test.ts` (which feeds this module's test
 *    list to the Rust side through a comment contract) and by the Rust test
 *    `the_ui_mirrors_the_navigation_refusals_this_module_makes`, which
 *    re-runs this file's refused/allowed lists through
 *    `navigation_allowed`.
 *
 * The rules are the shell's, restated: `http`/`https` only; never a loopback
 * or unspecified host; and the same canonicalised spellings count —
 * `http://2130706433/` *is* 127.0.0.1, `http://0177.0.0.1/` too, and a
 * trailing dot on `localhost.` changes nothing. A browser `URL` canonicalises
 * most of those on parse (which is why testing the *hostname* is mostly
 * enough), but the mirror keeps the numeric parser for the forms a URL
 * parser may hand through untouched, so the answer never depends on which
 * parser ran first. Everything else is the shell's to accept or refuse; this
 * module must never refuse an address the shell would take.
 */

/** What a browser address may become. */
export type BrowserDispatch =
  | { kind: "shell"; tabId: string; url: string }
  | { kind: "refuse"; reason: string };

/**
 * The refusal sentence the shell words, mirrored here.
 *
 * `browser.rs::parse_navigation` words the same refusal; the dispatch test
 * asserts the two agree, so editing one without the other fails a test on
 * whichever side runs first rather than shipping two truths about the same
 * rule.
 */
export const NON_WEB_ADDRESS_MESSAGE =
  "refusing to navigate to %ADDRESS%: browser webviews load http(s) on " +
  "non-loopback hosts only (docs/03 §1.5)";

function refuse(raw: string): BrowserDispatch {
  return {
    kind: "refuse",
    reason: NON_WEB_ADDRESS_MESSAGE.replace("%ADDRESS%", JSON.stringify(raw)),
  };
}

function notNavigable(raw: string): BrowserDispatch {
  return {
    kind: "refuse",
    reason: `not a navigable URL: ${JSON.stringify(raw)}`,
  };
}

/**
 * The value of a hostname read as a WHATWG IPv4 address, or `null` when the
 * host is a domain name.
 *
 * This is the spec's "IPv4 parser", minimised: numbers in decimal, octal
 * (`0177`) or hex (`0x7f`), one to four parts, every part but the last below
 * 256, the last below 256 to the power of the remaining room. A part that is
 * none of those makes the host a domain, and a domain is never an address —
 * that is what keeps `127.0.0.1.nip.io` allowed while `0177.0.0.1` is not.
 */
function ipv4Value(host: string): number | null {
  let parts = host.split(".");
  if (parts.length > 1 && parts[parts.length - 1] === "") {
    // A trailing dot is the same address (`127.0.0.1.`), but a lone dot is
    // not a number at all.
    parts = parts.slice(0, -1);
    if (parts.length === 0) return null;
  }
  if (parts.length > 4) return null;
  const nums: number[] = [];
  for (const part of parts) {
    let n: number;
    if (/^0x[0-9a-f]+$/i.test(part)) {
      n = parseInt(part, 16);
    } else if (/^0[0-7]*$/.test(part)) {
      n = parseInt(part, 8);
    } else if (/^\d+$/.test(part)) {
      n = parseInt(part, 10);
    } else {
      return null;
    }
    if (!Number.isFinite(n) || n > 4294967295) return null;
    nums.push(n);
  }
  for (let i = 0; i < nums.length - 1; i++) {
    if (nums[i] > 255) return null;
  }
  const last = nums[nums.length - 1];
  if (last >= Math.pow(256, 5 - nums.length)) return null;
  let value = 0;
  for (let i = 0; i < nums.length; i++) {
    value += nums[i] * Math.pow(256, nums.length - 1 - i);
  }
  return value;
}

/**
 * Is this IPv6 literal one of the loopback/unspecified forms the shell
 * refuses?
 *
 * `URL.hostname` hands IPv6 through with brackets dropped and lowercased:
 * `::1`, `::`, and the IPv4-mapped shapes that would dodge a naive `::1`
 * check — `[::ffff:127.0.0.1]` arrives as `::ffff:127.0.0.1`, and the same
 * address serialises from some parsers as `::ffff:7f00:1`, so both are read
 * here rather than one.
 */
function ipv6LiteralIsRefused(host: string): boolean {
  if (host === "::1" || host === "::") return true;
  const mapped = host.match(/^::ffff:(.+)$/);
  if (!mapped) return false;
  const tail = mapped[1];
  const dotted = ipv4Value(tail);
  if (dotted !== null) return mappedTailRefused(dotted);
  // Hex-group spelling: one or two 16-bit groups are the last 32 bits.
  const groups = tail.split(":");
  if (groups.length < 1 || groups.length > 2) return false;
  if (!groups.every((g) => /^[0-9a-f]{1,4}$/i.test(g))) return false;
  const hi = groups.length === 2 ? parseInt(groups[0], 16) : 0;
  const lo = parseInt(groups[groups.length - 1], 16);
  return mappedTailRefused(hi * 65536 + lo);
}

function mappedTailRefused(value: number): boolean {
  const top = Math.floor(value / 16777216);
  return value === 0 || top === 127;
}

/**
 * Is this hostname one of the loopback/unspecified forms the shell refuses?
 *
 * Runs after the URL parser has lowercased and bracket-stripped, which is
 * what the shell's `url::Host` sees after parsing too.
 */
function hostIsRefused(hostname: string): boolean {
  // No host at all — `about:blank`-class or opaque — is not a web origin.
  if (!hostname) return true;
  // WHATWG `hostname` keeps the brackets on an IPv6 literal; every check
  // below wants the address, not the brackets.
  const bare =
    hostname.startsWith("[") && hostname.endsWith("]")
      ? hostname.slice(1, -1)
      : hostname;
  // RFC 6761 reserves `localhost` and everything under `.localhost`; the
  // trailing dot is the same host.
  const host = bare.replace(/\.+$/, "").toLowerCase();
  if (!host || host === "localhost" || host.endsWith(".localhost")) return true;
  if (host.includes(":")) return ipv6LiteralIsRefused(host);
  const value = ipv4Value(host);
  if (value !== null) {
    // 127/8 is loopback; exactly 0.0.0.0 is unspecified — the same two
    // predicates `navigation_allowed` runs, not the whole of RFC 1918.
    return value === 0 || Math.floor(value / 16777216) === 127;
  }
  return false;
}

/**
 * Classify an address for the browser tab `tabId`.
 *
 * Takes the address **as the user typed it**: a bare host gets the scheme a
 * browser would assume (`https://`), which is the one transformation
 * `browserHistory.normaliseAddress` makes and the reason this module owns it
 * too — the mirror test feeds raw typed addresses, and two normalisers would
 * be two places for them to disagree. The scheme test is the pane's own
 * (`^https?://`), so `localhost:3000` is a host with a port and
 * `javascript:…` is not a scheme the pane ever meant to send.
 *
 * One divergence the mirror test caught and pinned: a typed scheme the pane
 * does not recognise (`tauri://…`, `file://…`) gets `https://` in front like
 * any other non-http input, but Node's URL parser then reads the original
 * scheme's authority as a **host** (`https://tauri://localhost/` has host
 * `tauri`). The shell would refuse the original string; so does this module,
 * by a second look at the *original* text before the prefix is applied.
 *
 * The classification, not the judgement: `shell` means "the shell may be
 * asked" and the shell may still refuse something this module passed — that
 * is the boundary staying where it is. `url` is the parsed address
 * re-serialised, which normalises what `Url::parse` would normalise anyway.
 */
export function classifyBrowserAddress(
  raw: string,
  tabId: string,
): BrowserDispatch {
  const trimmed = raw.trim();
  // The pane's own rule, verbatim: only `http(s)://` counts as a scheme the
  // user typed. Everything else — bare hosts, `localhost:3000`, a scheme —
  // gets `https://` in front and is judged as what the pane would have sent.
  const isHttp = /^https?:\/\//i.test(trimmed);
  // A typed scheme the pane does not recognise must not be rescued by the
  // URL parser into a host: `https://tauri://localhost/` parses with host
  // `tauri`, which would allow a page the shell refuses. The original text
  // had a scheme, and the answer was always going to be no.
  if (!isHttp && /^[a-z][a-z0-9+.-]*:\/\//i.test(trimmed)) {
    return refuse(trimmed);
  }
  const absolute = isHttp ? trimmed : `https://${trimmed}`;
  let parsed: URL;
  try {
    parsed = new URL(absolute);
  } catch {
    return notNavigable(trimmed);
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return refuse(trimmed);
  }
  if (hostIsRefused(parsed.hostname)) {
    return refuse(trimmed);
  }
  return { kind: "shell", tabId, url: parsed.toString() };
}
