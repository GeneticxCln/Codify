/**
 * The editor's calls to the engine: list a workspace's files, read one, save one, and the window's two calls of the
 * surface bridge (poll for the next question, post the answer).
 *
 * Plain `fetch` calls against a stub, in the shape every other call in `api.ts` has: the boot token as a bearer, a
 * JSON body only where there is one, and a refusal that is an `Error` carrying the engine's `code`, the status and the
 * extras, so a caller can tell a conflict (409 `file_changed`, with the version it is now) from a missing file without
 * parsing a sentence.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const api = await import("../src/api.ts");

interface Seen {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: string | null;
  signal: AbortSignal | null;
}

async function withEngine<T>(answer: (seen: Seen) => { status?: number; body: unknown }, body: (seen: Seen[]) => Promise<T>): Promise<T> {
  const original = globalThis.fetch;
  const seen: Seen[] = [];
  api.setEngineInfo({ port: 43117, token: "boot-token-under-test" });
  globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
    const record: Seen = {
      url: String(input),
      method: init?.method ?? "GET",
      headers: (init?.headers ?? {}) as Record<string, string>,
      body: typeof init?.body === "string" ? init.body : null,
      signal: (init?.signal as AbortSignal | undefined) ?? null,
    };
    seen.push(record);
    const reply = answer(record);
    return new Response(JSON.stringify(reply.body), { status: reply.status ?? 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  try {
    return await body(seen);
  } finally {
    globalThis.fetch = original;
  }
}

const BASE = "http://127.0.0.1:43117";

test("listing files asks for the workspace's files, with the limit, as the engine's bearer", async () => {
  await withEngine(
    () => ({ body: { files: ["a.py", "src/b.py"], truncated: false, limit: 500 } }),
    async (seen) => {
      const got = await api.listWorkspaceFiles("w 1", 500);

      assert.deepEqual(got, { files: ["a.py", "src/b.py"], truncated: false, limit: 500 });
      assert.equal(seen[0].url, `${BASE}/workspaces/w%201/files?limit=500`);
      assert.equal(seen[0].method, "GET");
      assert.equal(seen[0].headers.Authorization, "Bearer boot-token-under-test");
    },
  );
});

test("listing without a limit leaves it to the engine", async () => {
  await withEngine(
    () => ({ body: { files: [], truncated: false, limit: 5000 } }),
    async (seen) => {
      await api.listWorkspaceFiles("w1");

      assert.equal(seen[0].url, `${BASE}/workspaces/w1/files`);
    },
  );
});

test("reading a file escapes its path, so a name with a space, a hash or an ampersand reaches the engine whole", async () => {
  await withEngine(
    () => ({ body: { path: "a b#c&d.py", content: "x", version: "v", size: 1 } }),
    async (seen) => {
      const got = await api.readWorkspaceFile("w1", "a b#c&d.py");

      assert.equal(got.content, "x");
      assert.equal(seen[0].url, `${BASE}/workspaces/w1/file?path=a%20b%23c%26d.py`);
    },
  );
});

test("a file that is not there is an error with the engine's code and status", async () => {
  await withEngine(
    () => ({ status: 404, body: { code: "file_missing", message: "there is no file at x.py in this workspace" } }),
    async () => {
      await assert.rejects(api.readWorkspaceFile("w1", "x.py"), (err: Error & { code?: string; status?: number }) => {
        assert.equal(err.code, "file_missing");
        assert.equal(err.status, 404);
        assert.match(err.message, /no file at x\.py/);
        return true;
      });
    },
  );
});

test("each way the engine refuses to hold a file keeps its own code", async () => {
  for (const [status, code] of [[422, "file_binary"], [422, "file_not_text"], [422, "file_too_large"], [400, "file_escape"]] as const) {
    await withEngine(
      () => ({ status, body: { code, message: "no" } }),
      async () => {
        await assert.rejects(api.readWorkspaceFile("w1", "x"), (err: Error & { code?: string }) => err.code === code);
      },
    );
  }
});

test("saving sends the path, the text and the version it read, as JSON, by PUT", async () => {
  await withEngine(
    () => ({ body: { path: "a.py", version: "v2", size: 4 } }),
    async (seen) => {
      const got = await api.saveWorkspaceFile("w1", { path: "a.py", content: "new\n", base_version: "v1" });

      assert.deepEqual(got, { path: "a.py", version: "v2", size: 4 });
      assert.equal(seen[0].url, `${BASE}/workspaces/w1/file`);
      assert.equal(seen[0].method, "PUT");
      assert.equal(seen[0].headers["Content-Type"], "application/json");
      assert.deepEqual(JSON.parse(seen[0].body ?? ""), { path: "a.py", content: "new\n", base_version: "v1" });
    },
  );
});

test("a conflict carries the version the file is now, where a caller can read it", async () => {
  await withEngine(
    () => ({ status: 409, body: { code: "file_changed", message: "changed on disk", current_version: "v9" } }),
    async () => {
      await assert.rejects(
        api.saveWorkspaceFile("w1", { path: "a.py", content: "x", base_version: "v1" }),
        (err: Error & { code?: string; status?: number; extra?: { current_version?: string } }) => {
          assert.equal(err.code, "file_changed");
          assert.equal(err.status, 409);
          assert.equal(err.extra?.current_version, "v9");
          return true;
        },
      );
    },
  );
});

test("polling the surface bridge asks for the next question, waits as long as it is told, and can be aborted", async () => {
  const controller = new AbortController();
  await withEngine(
    () => ({ body: { id: "r1", surface: "editor", op: "read", workspace_id: "w1", args: {} } }),
    async (seen) => {
      const got = await api.nextSurfaceRequest(20, controller.signal);

      assert.deepEqual(got, { id: "r1", surface: "editor", op: "read", workspace_id: "w1", args: {} });
      assert.equal(seen[0].url, `${BASE}/surfaces/next?wait=20`);
      assert.equal(seen[0].signal, controller.signal);
    },
  );
});

test("a poll that finds nothing is null, not a question with no id", async () => {
  await withEngine(
    () => ({ body: { id: null } }),
    async () => {
      assert.equal(await api.nextSurfaceRequest(0), null);
    },
  );
});

test("a poll the engine refuses is an error, so the loop backs off instead of spinning", async () => {
  await withEngine(
    () => ({ status: 401, body: { code: "unauthorized", message: "bad token" } }),
    async () => {
      await assert.rejects(api.nextSurfaceRequest(0), (err: Error & { status?: number }) => err.status === 401);
    },
  );
});

test("an answer is posted as exactly an id, a verdict and a payload", async () => {
  await withEngine(
    () => ({ body: { accepted: true } }),
    async (seen) => {
      const ok = await api.answerSurface({ id: "r1", ok: true, result: { open: [], file: null } });
      const no = await api.answerSurface({ id: "r2", ok: false, error: "that file is not open" });

      assert.deepEqual([ok, no], [true, true]);
      assert.equal(seen[0].url, `${BASE}/surfaces/answer`);
      assert.equal(seen[0].method, "POST");
      assert.deepEqual(JSON.parse(seen[0].body ?? ""), { id: "r1", ok: true, result: { open: [], file: null } });
      assert.deepEqual(JSON.parse(seen[1].body ?? ""), { id: "r2", ok: false, error: "that file is not open" });
    },
  );
});

test("an answer the engine did not accept (late, or for a question that is gone) is reported as not accepted", async () => {
  await withEngine(
    () => ({ body: { accepted: false } }),
    async () => {
      assert.equal(await api.answerSurface({ id: "stale", ok: true, result: {} }), false);
    },
  );
});
