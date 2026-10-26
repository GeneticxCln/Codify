/**
 * Settings → About, mounted.
 *
 * The pane says only what the app can truthfully know, so these tests are about the edges of that:
 * what it prints when there is a desktop shell and when there is not, when the shell will not say,
 * when the engine is down or the token is stale, and that the boot token never reaches the page. The
 * shortcut list is checked against the real mapping in `shortcuts.test.ts`.
 *
 * The update section is the one part of the pane that *does* something, and the tests below are about
 * what it will and will not do: nothing is asked of GitHub until a person clicks, a release with a
 * package is offered and a release without one is a sentence, a download that fails leaves the button
 * to try again, and in the browser preview it says the check cannot run rather than offering a control
 * that would fail (docs/18 §4).
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";
import type { UpdateCheck } from "../src/api.ts";

const { withDom } = await import("./dom.ts");
const { SHORTCUT_HELP } = await import("../src/shortcuts.ts");
const React = (await import("react")).default;
const h = React.createElement;

const TOKEN = "boot-token-must-never-be-shown";

const json = (value: unknown, status = 200): Response =>
  new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } });

const RUNTIME = {
  interpreter: {
    executable: "/home/me/Codify/.venv/bin/python",
    version: "3.12.4",
    version_info: [3, 12],
    implementation: "CPython",
    in_virtualenv: true,
    prefix: "/home/me/Codify/.venv",
    base_prefix: "/usr",
  },
  project_root: "/home/me/Codify",
  checkout_interpreter: "/home/me/Codify/.venv/bin/python3",
  laya_sdk: { importable: false, import_error: null, version: null, disabled_by_env: false },
  warnings: [],
};

interface World {
  /**
   * What the desktop shell answers to a command, or `undefined` for no shell at all. A value is
   * returned as it is; a function is called, which is how a test makes one command *fail* the way
   * Tauri does — by rejecting with the sentence a Rust `Err(String)` carried.
   */
  shell?: Record<string, unknown>;
  health?: () => Response;
  runtime?: () => Response;
  /** Every command the pane asked the shell for, in order, filled in as the pane runs. */
  calls?: string[];
}

async function withAbout(world: World, body: (dom: Dom) => Promise<void>): Promise<void> {
  await withDom(async (dom) => {
    const win = dom.window as unknown as Record<string, unknown>;
    if (world.shell) {
      win.__TAURI_INTERNALS__ = {
        invoke: async (cmd: string) => {
          world.calls?.push(cmd);
          if (!(cmd in world.shell!)) return null;
          const answer = world.shell![cmd];
          return typeof answer === "function" ? (answer as () => unknown)() : answer;
        },
        transformCallback: () => 1,
        metadata: { currentWebview: { windowLabel: "main" }, currentWindow: { label: "main" } },
      };
    } else {
      delete win.__TAURI_INTERNALS__;
    }
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const path = new URL(String(input)).pathname;
      if (path === "/health") return (world.health ?? (() => json({ ok: true, authenticated: true })))();
      if (path === "/settings/runtime") return (world.runtime ?? (() => json(RUNTIME)))();
      throw new Error(`no answer for ${path}`);
    }) as typeof fetch;
    const api = await import("../src/api.ts");
    api.setEngineInfo({ port: 7431, token: TOKEN });
    const { AboutPane } = await import("../src/components/AboutPane.tsx");
    await dom.render(h(AboutPane));
    for (let i = 0; i < 4; i++) await dom.settle();
    await body(dom);
  });
}

const SHELL = { "plugin:app|name": "Codify", "plugin:app|version": "0.2.0", "plugin:app|tauri_version": "2.11.6" };

test("under the desktop shell it prints the app's version, the shell, and what the engine is", async () => {
  await withAbout({ shell: SHELL }, async (dom) => {
    const text = dom.text();
    assert.match(text, /Version\s*0\.2\.0/);
    assert.match(text, /Desktop shell\s*Tauri 2\.11\.6/);
    assert.match(text, /Status\s*running/);
    assert.match(text, /127\.0\.0\.1:7431/, "the port the window is connected to is not shown");
    assert.match(text, /3\.12\.4/, "the engine's interpreter did not reach the page");
    assert.match(text, /MIT/);
    assert.match(text, /github\.com\/GeneticxCln\/Codify/);
    assert.match(text, /~\/\.codify/);
  });
});

test("every keyboard shortcut the keyboard layer defines is listed, with what it does", async () => {
  await withAbout({ shell: SHELL }, async (dom) => {
    const text = dom.text();
    for (const shortcut of SHORTCUT_HELP) {
      assert.ok(text.includes(shortcut.keys), `${shortcut.keys} is not listed`);
      assert.ok(text.includes(shortcut.does), `what ${shortcut.keys} does is not listed`);
    }
  });
});

test("the boot token is never on the page", async () => {
  await withAbout({ shell: SHELL }, async (dom) => {
    assert.ok(!dom.text().includes(TOKEN), "the boot token was printed");
    assert.ok(!dom.container.innerHTML.includes(TOKEN), "the boot token is in the markup");
  });
});

test("in a standalone browser there is no shell, and it says so instead of a version", async () => {
  await withAbout({}, async (dom) => {
    const text = dom.text();
    assert.match(text, /standalone browser preview/);
    assert.doesNotMatch(text, /Desktop shell/);
    assert.doesNotMatch(text, /Version\s*\d/);
    // The engine half still works without a shell: it is plain HTTP.
    assert.match(text, /Status\s*running/);
  });
});

test("a shell that will not say its version is not turned into a blank one", async () => {
  for (const answer of [null, "", "   ", 42]) {
    await withAbout({ shell: { ...SHELL, "plugin:app|version": answer } }, async (dom) => {
      const text = dom.text();
      assert.match(text, /unavailable: the desktop shell did not report one/, `${JSON.stringify(answer)} was printed as a version`);
      assert.doesNotMatch(text, /standalone browser preview/, "a shell was claimed not to exist");
    });
  }
});

/** A release that exists, with the package a deb install would take. */
const AVAILABLE: UpdateCheck = {
  current: "1.0.0",
  latest: "1.0.1",
  tag: "v1.0.1",
  published_at: "2026-10-26T09:00:00Z",
  url: "https://github.com/GeneticxCln/Codify/releases/tag/v1.0.1",
  package: "deb",
  asset: "Codify_1.0.1_amd64.deb",
  asset_bytes: 18_000_000,
  available: true,
  message: null,
};

/** The sentence the shell answers a download with, as `next_step`. */
const NEXT_STEP =
  "Install it with:  sudo apt install /home/me/Downloads/Codify_1.0.1_amd64.deb\n" +
  "That is yours to run: this app never escalates, and it does not replace the copy you are running.";

/** Everything with a button, so a test can say that one is *not* there. */
function buttonLabels(dom: Dom): string[] {
  return [...dom.container.querySelectorAll("button")]
    .map((button) => (button.textContent ?? "").trim())
    .filter((label) => label.length > 0);
}

test("nothing is asked of GitHub until the button is pressed", async () => {
  const calls: string[] = [];
  await withAbout({ shell: { ...SHELL, codify_update_check: AVAILABLE }, calls }, async (dom) => {
    // The pane asks the shell for its own version on mount — `plugin:app|*` is Tauri's app API —
    // and asks GitHub nothing. That difference is the whole point of the button.
    assert.ok(
      !calls.includes("codify_update_check"),
      `a check ran on mount, so the app phones home: ${calls.join(", ")}`,
    );
    assert.match(dom.text(), /Check for updates/);
    assert.match(dom.text(), /installs nothing/, "what the button does was not said");

    await dom.click(dom.byButton("Check for updates"));
    await dom.settle();
    assert.equal(
      calls.filter((cmd) => cmd === "codify_update_check").length,
      1,
      `one click asked the shell ${calls.join(", ")}`,
    );
  });
});

test("a release with a package is offered, and downloading says where it went and what to run", async () => {
  await withAbout(
    {
      shell: {
        ...SHELL,
        codify_update_check: AVAILABLE,
        codify_update_download: {
          path: "/home/me/Downloads/Codify_1.0.1_amd64.deb",
          bytes: 18_000_000,
          package: "deb",
          tag: "v1.0.1",
          next_step: NEXT_STEP,
        },
      },
    },
    async (dom) => {
      await dom.click(dom.byButton("Check for updates"));
      await dom.settle();
      const offered = dom.text();
      assert.match(offered, /v1\.0\.1/, "the release was not named");
      assert.match(offered, /Codify_1\.0\.1_amd64\.deb/, "the package for this install was not named");
      assert.match(offered, /2026-10-26/, "when it was published was not shown");
      assert.match(offered, /18 MB/, "how big the download is was not shown");

      await dom.click(dom.byButton("Download Codify_1.0.1_amd64.deb"));
      await dom.settle();
      const done = dom.text();
      assert.match(done, /\/home\/me\/Downloads\/Codify_1\.0\.1_amd64\.deb/, "where it went was not shown");
      assert.match(done, /sudo apt install \/home\/me\/Downloads\/Codify_1\.0\.1_amd64\.deb/, "the command was not shown");
      assert.match(done, /never escalates/, "the app installing nothing was not said");
    },
  );
});

test("being up to date is a sentence, and there is nothing to download", async () => {
  await withAbout(
    {
      shell: {
        ...SHELL,
        codify_update_check: { ...AVAILABLE, available: false, asset: null, asset_bytes: null, message: "1.0.0 is the newest release." },
      },
    },
    async (dom) => {
      await dom.click(dom.byButton("Check for updates"));
      await dom.settle();
      assert.match(dom.text(), /1\.0\.0 is the newest release\./);
      assert.deepEqual(
        buttonLabels(dom).filter((label) => label.startsWith("Download")),
        [],
        "an up-to-date answer offered something to download",
      );
    },
  );
});

test("a check that could not run says so rather than claiming you are current", async () => {
  await withAbout(
    {
      shell: {
        ...SHELL,
        codify_update_check: {
          ...AVAILABLE,
          latest: null,
          tag: null,
          available: false,
          asset: null,
          asset_bytes: null,
          message: "could not reach GitHub (connection refused)",
        },
      },
    },
    async (dom) => {
      await dom.click(dom.byButton("Check for updates"));
      await dom.settle();
      assert.match(dom.text(), /could not reach GitHub/);
      assert.doesNotMatch(dom.text(), /newest release\./);
    },
  );
});

test("a copy running from a checkout is told how to update, not handed a package", async () => {
  await withAbout(
    {
      shell: {
        ...SHELL,
        codify_update_check: {
          ...AVAILABLE,
          package: "source",
          asset: null,
          asset_bytes: null,
          message: "v1.0.1 is newer, and this copy runs from a checkout: `git pull` in it, or take a package from https://github.com/GeneticxCln/Codify/releases",
        },
      },
    },
    async (dom) => {
      await dom.click(dom.byButton("Check for updates"));
      await dom.settle();
      assert.match(dom.text(), /git pull/);
      assert.deepEqual(
        buttonLabels(dom).filter((label) => label.startsWith("Download")),
        [],
        "a checkout was offered a package, which would install a second Codify",
      );
    },
  );
});

test("a download that fails says why and leaves the button there to try again", async () => {
  await withAbout(
    {
      shell: {
        ...SHELL,
        codify_update_check: AVAILABLE,
        // How Tauri hands back a Rust `Err(String)`: a bare string, which is the shape
        // `readRejection` exists to read.
        codify_update_download: () => {
          throw "GitHub answered 502 for Codify_1.0.1_amd64.deb, so nothing was written";
        },
      },
    },
    async (dom) => {
      await dom.click(dom.byButton("Check for updates"));
      await dom.settle();
      await dom.click(dom.byButton("Download Codify_1.0.1_amd64.deb"));
      await dom.settle();
      assert.match(dom.text(), /GitHub answered 502/, "the shell's own sentence was replaced");
      assert.match(dom.text(), /nothing was written/);
      // The offer is still there: a failed download is not a failed check.
      assert.match(dom.text(), /Codify_1\.0\.1_amd64\.deb/);
      assert.ok(
        buttonLabels(dom).includes("Download Codify_1.0.1_amd64.deb"),
        `no way to try again: ${buttonLabels(dom).join(", ")}`,
      );
    },
  );
});

test("in a browser preview it says the check cannot run here instead of offering it", async () => {
  await withAbout({}, async (dom) => {
    assert.match(dom.text(), /needs the desktop shell/, "a control that cannot work was offered");
    assert.deepEqual(
      buttonLabels(dom).filter((label) => label.startsWith("Check") || label.startsWith("Download")),
      [],
      "the browser preview offered an update control",
    );
  });
});

test("an engine that is down, or whose token is stale, is said plainly", async () => {
  await withAbout({ shell: SHELL, health: () => json({ ok: false }, 503), runtime: () => json({}, 503) }, async (dom) => {
    assert.match(dom.text(), /Status\s*not answering/);
    assert.match(dom.text(), /Engine runtime unavailable/, "a missing self-check looked healthy");
  });
  await withAbout({ shell: SHELL, health: () => json({ code: "unauthorized" }, 401) }, async (dom) => {
    assert.match(dom.text(), /running, but this window's token is stale/);
  });
});
