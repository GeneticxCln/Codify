/**
 * What the web-access setting says to the person who is deciding whether to turn it on.
 *
 * `web_fetch` lets the engine itself send a request to an address a model chose, and the consent for that is the
 * sentence on this card. So what is pinned here is the wording's *claims* rather than its phrasing: that off
 * means off, that a list with nothing in it is said to allow nothing, and that both on-states tell the person the
 * address is sent to the site and can carry what the assistant has read. A card that dropped that second claim
 * would still pass every test of a switch and be the one the setting's consent depends on.
 *
 * The engine owns which entries of the list are valid; nothing here may start to.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { WEB_MODES, listedSites, refusalSentence, webAccessDirty, webAccessStatus, webMode } from "../src/webAccess.ts";

test("off is the first choice and is what anything unrecognised means", () => {
  assert.equal(WEB_MODES[0].value, 0);
  assert.deepEqual(WEB_MODES.map((m) => m.value), [0, 1, 2]);
  for (const stored of [undefined, null, 0, 3, -1, 99]) {
    assert.equal(webMode(stored as number | null | undefined), 0, String(stored));
  }
  assert.equal(webMode(1), 1);
  assert.equal(webMode(2), 2);
});

test("off says the assistant cannot fetch, and that the open tab can still be read", () => {
  const status = webAccessStatus(0, "docs.python.org");
  assert.equal(status.kind, "off");
  assert.match(status.text, /cannot fetch web pages/);
  assert.match(status.text, /page you have open/);
});

test("a list mode with nothing listed says it allows nothing, and does not claim to be on", () => {
  for (const blank of ["", "   ", " , ; "]) {
    const status = webAccessStatus(1, blank);
    assert.equal(status.kind, "empty", JSON.stringify(blank));
    assert.match(status.text, /list is empty/);
    assert.match(status.text, /nothing can be fetched/);
  }
});

test("a list names how many sites it allows, in the singular and the plural", () => {
  assert.match(webAccessStatus(1, "docs.python.org").text, /1 site you listed/);
  assert.match(webAccessStatus(1, "docs.python.org, example.com").text, /2 sites you listed/);
  assert.equal(webAccessStatus(1, "docs.python.org").kind, "listed");
});

test("both ways of being on say the address is sent to the site and can carry what was read", () => {
  for (const status of [webAccessStatus(1, "docs.python.org"), webAccessStatus(2, "")]) {
    assert.match(status.text, /sent to that site/, status.kind);
    assert.match(status.text, /anything the assistant has read in your files/, status.kind);
    assert.match(status.text, /shown in the transcript before it is made/, status.kind);
  }
});

test("any public site is the widest choice and says the private network is still refused", () => {
  const status = webAccessStatus(2, "");
  assert.equal(status.kind, "any");
  assert.match(status.text, /nothing here can tell that from an ordinary request/);
  assert.match(status.text, /private network are always refused/);
});

test("the list is split the way the engine splits it", () => {
  assert.deepEqual(listedSites(" a.example, b.example ;c.example   d.example "), [
    "a.example",
    "b.example",
    "c.example",
    "d.example",
  ]);
  assert.deepEqual(listedSites(""), []);
});

test("a change is a change in the mode or in the sites, and spacing is neither", () => {
  const stored = { mode: 1, hosts: "docs.python.org, example.com" };
  assert.equal(webAccessDirty(stored, { mode: 1, hosts: "docs.python.org,example.com" }), false);
  assert.equal(webAccessDirty(stored, { mode: 1, hosts: "docs.python.org" }), true);
  assert.equal(webAccessDirty(stored, { mode: 2, hosts: "docs.python.org, example.com" }), true);
  assert.equal(webAccessDirty({ mode: 0, hosts: "" }, { mode: 7, hosts: "" }), false, "an unrecognised mode is off");
});

test("a refusal is shown as the engine's sentence, not as its JSON", () => {
  const body = JSON.stringify({ code: "invalid_value", message: "web_fetch_hosts takes site names, not URLs: https://x.example/" });
  assert.equal(refusalSentence(body, "fallback"), "web_fetch_hosts takes site names, not URLs: https://x.example/");
  // A body in the older `detail` shape, a transport failure and nothing at all are all still said.
  assert.equal(refusalSentence(JSON.stringify({ detail: "an older engine" }), "fallback"), "an older engine");
  assert.equal(refusalSentence("connection refused", "fallback"), "connection refused");
  assert.equal(refusalSentence("   ", "fallback"), "fallback");
  assert.equal(refusalSentence(JSON.stringify({ code: "x" }), "fallback"), JSON.stringify({ code: "x" }));
});
