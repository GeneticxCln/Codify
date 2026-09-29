/**
 * Where the engine's boot token may rest.
 *
 * Under the desktop shell the token is fetched over IPC on every health probe,
 * so a copy in `localStorage` is a credential at rest with no job. Outside it
 * (the standalone browser preview) nothing can supply one, and the paste flow
 * needs the stored copy. `healthProbeRecovery.test.ts` proves the shell path
 * through the whole App; these pin both halves of the storage rule directly.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { storedEngineInfo, rememberEngineInfo, resyncEngineInfoFromStorage, getEngineInfo } =
  await import("../src/api.ts");

/** A `localStorage` that lives in a map. The module reads the global, so install one. */
function withStorage(seed: Record<string, string>, body: (map: Map<string, string>) => void): void {
  const map = new Map(Object.entries(seed));
  const fake = {
    getItem: (k: string) => (map.has(k) ? (map.get(k) as string) : null),
    setItem: (k: string, v: string) => void map.set(k, v),
    removeItem: (k: string) => void map.delete(k),
  };
  const g = globalThis as Record<string, unknown>;
  const before = Object.getOwnPropertyDescriptor(g, "localStorage");
  Object.defineProperty(g, "localStorage", { value: fake, configurable: true, writable: true });
  try {
    body(map);
  } finally {
    if (before) Object.defineProperty(g, "localStorage", before);
    else delete g.localStorage;
  }
}

test("under the shell a stored token is purged and never trusted", () => {
  withStorage({ CODIFY_PORT: "7431", CODIFY_TOKEN: "leftover" }, (map) => {
    assert.deepEqual(storedEngineInfo(true), { port: 7431, token: "" });
    assert.equal(map.has("CODIFY_TOKEN"), false, "the old copy is still on disk");
    assert.equal(map.get("CODIFY_PORT"), "7431", "the port is not a secret and stays");
  });
});

test("under the shell the token is remembered nowhere, the port is", () => {
  withStorage({}, (map) => {
    rememberEngineInfo({ port: 7432, token: "secret" }, true);
    assert.equal(map.get("CODIFY_PORT"), "7432");
    assert.equal(map.has("CODIFY_TOKEN"), false);
  });
});

test("outside the shell the pasted token is still read and kept", () => {
  withStorage({ CODIFY_PORT: "7433", CODIFY_TOKEN: "pasted" }, (map) => {
    assert.deepEqual(storedEngineInfo(false), { port: 7433, token: "pasted" });
    rememberEngineInfo({ port: 7434, token: "fresh" }, false);
    assert.equal(map.get("CODIFY_TOKEN"), "fresh");
    assert.equal(map.get("CODIFY_PORT"), "7434");
  });
});

test("paste, then retry: the standalone preview picks up what was pasted", () => {
  // The sequence the banner asks for. The client holds a module-level copy made
  // at page load, so a retry that did not re-read storage would re-probe with
  // the old token and leave the banner up after the user did what it said.
  withStorage({ CODIFY_PORT: "7440", CODIFY_TOKEN: "pasted-after-load" }, () => {
    const applied = resyncEngineInfoFromStorage();
    assert.deepEqual(applied, { port: 7440, token: "pasted-after-load" });
    assert.deepEqual(getEngineInfo(), applied);
  });
});
