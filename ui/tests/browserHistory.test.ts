/**
 * The browser tab's back/forward stack, and the one thing the address bar does
 * to what the user typed.
 *
 * These are decisions, not markup. Every one of them is invisible in the
 * rendered pane and load-bearing: whether Reload eats a history entry, whether
 * going back and then typing a new address strands a forward branch, whether
 * `localhost:3000` is read as a scheme. `browserPane.test.ts` proves the pane
 * shows what this module decides; nothing else would catch a wrong answer here.
 */
import test from "node:test";
import assert from "node:assert/strict";

import {
  canGoBack,
  canGoForward,
  currentUrl,
  emptyHistory,
  goBack,
  goForward,
  hostOf,
  normaliseAddress,
  pageNavigation,
  visit,
  type BrowserHistory,
  type InFlightNavigations,
} from "../src/browserHistory.ts";

/** A stack built by visiting `urls` in order, as the pane would. */
const visited = (...urls: string[]): BrowserHistory =>
  urls.reduce((history, url) => visit(history, url), emptyHistory());

test("a tab that has never navigated has nowhere to go", () => {
  const history = emptyHistory();
  assert.equal(currentUrl(history), "");
  assert.equal(canGoBack(history), false);
  assert.equal(canGoForward(history), false);
  // And the bounds refuse rather than wrapping. A back button that wrapped to
  // the newest entry would turn "I went too far" into "I am somewhere else".
  assert.equal(goBack(history), null);
  assert.equal(goForward(history), null);
});

test("the first address is visible and only forward is possible", () => {
  const history = visited("https://example.com");
  assert.equal(currentUrl(history), "https://example.com");
  assert.equal(canGoBack(history), false, "nothing before the first entry");
  assert.equal(canGoForward(history), false, "nothing after the only entry");
});

test("back walks the stack and stops at the first entry", () => {
  const history = visited("https://a.example", "https://b.example");
  const back = goBack(history);
  assert.ok(back);
  assert.equal(back.url, "https://a.example");
  assert.equal(canGoBack(back.history), false, "the first entry has nothing before it");
  assert.equal(goBack(back.history), null);
  // The way back is the way forward, until something new is visited.
  assert.equal(goForward(back.history)?.url, "https://b.example");
});

test("forward walks to the newest entry and stops", () => {
  const back = goBack(visited("https://a.example", "https://b.example"));
  assert.ok(back);
  const forward = goForward(back.history);
  assert.ok(forward);
  assert.equal(forward.url, "https://b.example");
  assert.equal(canGoForward(forward.history), false);
  assert.equal(goForward(forward.history), null);
});

test("visiting after going back drops the abandoned branch", () => {
  // a → b → c, back twice to a, then somewhere new. Forward must be dead: b and
  // c were a change of mind, and a browser that kept them would let forward walk
  // into a page the user had already decided against.
  const atC = visited("https://a.example", "https://b.example", "https://c.example");
  const atB = goBack(atC);
  assert.ok(atB);
  const atA = goBack(atB.history);
  assert.ok(atA);
  const branch = visit(atA.history, "https://d.example");
  assert.equal(currentUrl(branch), "https://d.example");
  assert.equal(canGoForward(branch), false, "the branch ahead was truncated");
  assert.deepEqual(branch.entries, ["https://a.example", "https://d.example"]);
});

test("revisiting the address on screen is not a new entry", () => {
  // This is what makes Reload — a navigate to the current URL, because the
  // shell has no reload command — leave the history alone. Without it, back
  // from a reloaded page reloads the page again.
  const history = visited("https://a.example", "https://b.example");
  const same = visit(history, "https://b.example");
  assert.equal(same, history, "the same object, not merely an equal one");
  assert.equal(same.entries.length, 2);
  assert.equal(canGoBack(same), true, "back still means back");
});

test("a typed address becomes one the shell can parse", () => {
  assert.equal(normaliseAddress("example.com"), "https://example.com");
  assert.equal(normaliseAddress("  example.com/docs  "), "https://example.com/docs");
});

test("an address that already names its scheme is left alone", () => {
  assert.equal(normaliseAddress("http://example.com"), "http://example.com");
  assert.equal(normaliseAddress("HTTPS://example.com"), "HTTPS://example.com");
  // Only these two. A looser test reads "localhost:3000" as a scheme named
  // localhost, and the pane would then send an address with no scheme to a
  // shell that cannot resolve one against anything.
  assert.equal(normaliseAddress("localhost:3000"), "https://localhost:3000");
});

test("a blank address is nothing to send, not an error", () => {
  assert.equal(normaliseAddress(""), "");
  assert.equal(normaliseAddress("   "), "");
});

test("a tab's title is the host, and never throws", () => {
  // Runs during a render, so the failure mode is a tab strip that takes the
  // window with it — a full URL is a worse answer than a host, a thrown one is
  // a broken app.
  assert.equal(hostOf("https://docs.rs/tauri/latest/"), "docs.rs");
  assert.equal(hostOf("https://example.com:8443/x?q=1"), "example.com:8443");
  assert.equal(hostOf(""), "");
  assert.equal(hostOf("not a url"), "not a url");
});

/** The shell has been asked to load `url` for `tabId`, and nothing else. */
const commanded = (tabId: string, url: string): InFlightNavigations => ({
  [tabId]: url,
});

test("a link the user clicked is a place they have been", () => {
  // The whole point: the stack was fed only by what the user typed, so a click
  // through a search result to a site left Back with nothing to offer but the
  // search — which is the complaint this answers.
  const onSearch = visited("https://google.com/search?q=youtube");
  const out = pageNavigation(
    {} as InFlightNavigations,
    "t1",
    "https://www.youtube.com/",
    onSearch
  );
  assert.deepEqual(out.history.entries, [
    "https://google.com/search?q=youtube",
    "https://www.youtube.com/",
  ]);
  assert.equal(currentUrl(out.history), "https://www.youtube.com/");
  const back = goBack(out.history);
  assert.equal(back?.url, "https://google.com/search?q=youtube");
});

test("the address the shell was asked for is not a second visit", () => {
  const history = visited("https://example.com/");
  const out = pageNavigation(
    commanded("t1", "https://example.com/"),
    "t1",
    "https://example.com/",
    history
  );
  assert.equal(out.history, history, "the very same object: nothing changed");
  // And the command is spent, so the *next* load of the same address is
  // reported by a page rather than by this caller.
  assert.deepEqual(out.commands, {});
});

test("Back is not undone by the load event that answered it", () => {
  // The trap. Back moves the cursor and leaves the forward entries alone, and
  // then the page announces the address Back just went to. If that announcement
  // were treated as a new visit, `visit` would truncate the forward branch —
  // Forward would stop working the moment Back had been used once.
  const onVideo = visited(
    "https://google.com/search?q=youtube",
    "https://www.youtube.com/watch?v=1"
  );
  const back = goBack(onVideo);
  assert.ok(back);
  const out = pageNavigation(
    commanded("t1", back.url),
    "t1",
    back.url,
    back.history
  );
  assert.equal(out.history, back.history);
  assert.equal(goForward(out.history)?.url, "https://www.youtube.com/watch?v=1");
});

test("a command for a tab is dropped by whatever the page does next", () => {
  // The page went somewhere else, so the shell's pending load is no longer
  // what is happening. Left behind, a stale command would make this page's own
  // load look like an answer to it — and the visit would be swallowed.
  const history = visited("https://example.com/");
  const out = pageNavigation(
    commanded("t1", "https://example.com/"),
    "t1",
    "https://iana.org/",
    history
  );
  assert.deepEqual(out.commands, {});
  assert.deepEqual(out.history.entries, [
    "https://example.com/",
    "https://iana.org/",
  ]);
  // Two tabs, two commands: forgetting one must not forget the other.
  const both: InFlightNavigations = {
    t1: "https://a.example/",
    t2: "https://b.example/",
  };
  const first = pageNavigation(both, "t1", "https://c.example/", history);
  assert.deepEqual(first.commands, { t2: "https://b.example/" });
  const second = pageNavigation(first.commands, "t2", "https://b.example/", history);
  assert.equal(second.history, history, "t2's own command was still the answer");
});

test("a redirect is a visit, and the module says so", () => {
  // Named cost, not an accident: the target does not match the command, so it
  // becomes an entry of its own and Back steps through it. The alternative —
  // treating *any* in-flight command as the answer — makes Back walk into the
  // place the command is leaving, which the test above pins against.
  const history = visited("https://example.org/");
  const out = pageNavigation(
    commanded("t1", "https://example.org/"),
    "t1",
    "https://www.example.org/",
    history
  );
  assert.deepEqual(out.history.entries, [
    "https://example.org/",
    "https://www.example.org/",
  ]);
});
