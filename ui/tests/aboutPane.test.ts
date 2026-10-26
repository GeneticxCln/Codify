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
 *
 * A download is minutes long, so it also has to *report*: the shell's `update-progress` events are
 * delivered here through the same event bus the real shell has (`transformCallback` →
 * `plugin:event|listen` → `emit`), which is what makes "the bar moves, and it stops being drawn when
 * the download ends" a claim about the pane rather than about `updateProgress.ts`.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

import type { Dom } from "./dom.ts";
import type { UpdateCheck } from "../src/api.ts";

const { withDom } = await import("./dom.ts");
const { SHORTCUT_HELP } = await import("../src/shortcuts.ts");
const { UPDATE_PROGRESS } = await import("../src/shellEvents.ts");
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

/** The shell's event bus, as much of it as this pane touches. */
interface ShellBus {
  /** Deliver a shell event, the way the shell's own `emit` reaches the window. */
  emit(event: string, payload: unknown): Promise<void>;
  /** How many handlers are subscribed to an event — the pane's own, and nobody else's. */
  listening(event: string): number;
}

async function withAbout(
  world: World,
  body: (dom: Dom, bus: ShellBus) => Promise<void>,
): Promise<void> {
  await withDom(async (dom) => {
    const win = dom.window as unknown as Record<string, unknown>;
    // The shell's event bus, kept the way the real internals keep it: `transformCallback` hands out
    // an id for each handler, `plugin:event|listen` binds an event name to one, and a test's `emit`
    // calls every handler bound to the name. Without this the pane could subscribe and be told
    // nothing, and every progress test below would pass by drawing no bar at all.
    const callbacks = new Map<number, (payload: unknown) => void>();
    const listeners = new Map<string, Array<{ id: number; handler: (payload: unknown) => void }>>();
    let callbackSeq = 0;
    let eventSeq = 0;
    let delivered = 0;
    const bus: ShellBus = {
      async emit(event, payload) {
        // The handler the SDK registered is handed a whole `Event` — `{ event, id, payload }` — not
        // the payload alone, and inside `act` so the state a handler sets is flushed before the
        // assertion that reads it.
        await dom.act(async () => {
          for (const { handler } of listeners.get(event) ?? []) {
            handler({ event, id: ++delivered, payload });
          }
        });
      },
      listening(event) {
        return (listeners.get(event) ?? []).length;
      },
    };
    if (world.shell) {
      win.__TAURI_INTERNALS__ = {
        invoke: async (cmd: string, a: Record<string, unknown> = {}) => {
          world.calls?.push(cmd);
          if (cmd === "plugin:event|listen") {
            const handler = callbacks.get(Number(a.handler));
            const name = String(a.event ?? "");
            const id = ++eventSeq;
            if (handler && name) listeners.set(name, [...(listeners.get(name) ?? []), { id, handler }]);
            return id;
          }
          if (cmd === "plugin:event|unlisten") {
            const name = String(a.event ?? "");
            listeners.set(
              name,
              (listeners.get(name) ?? []).filter((l) => l.id !== Number(a.eventId)),
            );
            return null;
          }
          if (!(cmd in world.shell!)) return null;
          const answer = world.shell![cmd];
          return typeof answer === "function" ? (answer as () => unknown)() : answer;
        },
        transformCallback(callback: unknown): number {
          const id = ++callbackSeq;
          if (typeof callback === "function") callbacks.set(id, callback as (payload: unknown) => void);
          return id;
        },
        metadata: { currentWebview: { windowLabel: "main" }, currentWindow: { label: "main" } },
      };
      // The SDK's `listen` returns an unsubscribe that calls this before it invokes, so a harness
      // without it fails inside `@tauri-apps/api/event` the moment anything unsubscribes — which
      // reads as a broken pane rather than as an incomplete stand-in.
      win.__TAURI_EVENT_PLUGIN_INTERNALS__ = { unregisterListener() {} };
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
    await body(dom, bus);
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

/** What the shell answers a download with, once it has landed: the file, and the sentence to run. */
const DOWNLOADED = {
  path: "/home/me/Downloads/Codify_1.0.1_amd64.deb",
  bytes: 18_000_000,
  package: "deb",
  tag: "v1.0.1",
  next_step: NEXT_STEP,
};

/** Everything with a button, so a test can say that one is *not* there. */
function buttonLabels(dom: Dom): string[] {
  return [...dom.container.querySelectorAll("button")]
    .map((button) => (button.textContent ?? "").trim())
    .filter((label) => label.length > 0);
}

test("nothing is asked of GitHub until the button is pressed", async () => {
  const calls: string[] = [];
  await withAbout({ shell: { ...SHELL, codify_update_check: AVAILABLE }, calls }, async (dom, bus) => {
    // And nothing is subscribed to either: a pane that sat down on the shell's event bus would be
    // a pane hearing about every download in the app, forever, for a bar nobody asked for.
    assert.equal(bus.listening(UPDATE_PROGRESS), 0, "the pane subscribed to progress on mount");
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
    async (dom, bus) => {
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
      // And the bar is gone with the download: a listener that outlived a failed attempt would
      // paint the next download's first percent over the sentence explaining this one.
      assert.equal(bus.listening(UPDATE_PROGRESS), 0, "a failed download left its listener behind");
    },
  );
});

test("a download says how far it has come, and stops saying it when it lands", async () => {
  let finish!: (answer: unknown) => void;
  const pending = new Promise((resolve) => {
    finish = resolve;
  });
  await withAbout(
    {
      shell: { ...SHELL, codify_update_check: AVAILABLE, codify_update_download: () => pending },
    },
    async (dom, bus) => {
      await dom.click(dom.byButton("Check for updates"));
      await dom.settle();
      await dom.click(dom.byButton("Download Codify_1.0.1_amd64.deb"));
      await dom.settle();
      assert.equal(bus.listening(UPDATE_PROGRESS), 1, "a running download is not listening for its own progress");
      assert.match(dom.text(), /Starting the download…/, "the first state of the wait was not said");

      // What the shell sends while it runs: one message per whole percent.
      await bus.emit(UPDATE_PROGRESS, { received: 5_400_000, total: 18_000_000 });
      await dom.settle();
      assert.match(dom.text(), /5\.4 MB of 18 MB — 30%/);
      let bar = dom.container.querySelector("[role=progressbar]");
      assert.ok(bar, "no bar was drawn while a download was running");
      assert.equal(bar.getAttribute("aria-valuenow"), "30");
      assert.equal(
        (bar.firstElementChild as HTMLElement).style.width,
        "30%",
        "the bar was drawn at a different width than it reported",
      );

      // A second message moves it, so the first number is not simply the last one painted.
      await bus.emit(UPDATE_PROGRESS, { received: 14_400_000, total: 18_000_000 });
      await dom.settle();
      assert.match(dom.text(), /14 MB of 18 MB — 80%/);
      bar = dom.container.querySelector("[role=progressbar]");
      assert.equal(bar?.getAttribute("aria-valuenow"), "80");

      // Where it landed is still the shell's answer, not the last announcement.
      finish(DOWNLOADED);
      await dom.settle();
      assert.match(dom.text(), /sudo apt install \/home\/me\/Downloads\/Codify_1\.0\.1_amd64\.deb/);
      assert.doesNotMatch(dom.text(), /of 18 MB/, "the bar outlived the download it was measuring");
      // `assert.ok(… === null)` rather than `assert.equal(element, null)`: a failed comparison of two
      // DOM elements makes the runner describe both of them, which is what `sourceRules.test.ts`
      // exists to keep out of this suite (it exhausted the machine's memory once already).
      assert.ok(
        dom.container.querySelector("[role=progressbar]") === null,
        "a finished download left a bar on the page",
      );
      assert.equal(bus.listening(UPDATE_PROGRESS), 0, "the pane is still listening after its download");
    },
  );
});

test("a download nobody sized is drawn without a figure to be wrong about", async () => {
  let finish!: (answer: unknown) => void;
  const pending = new Promise((resolve) => {
    finish = resolve;
  });
  await withAbout(
    {
      shell: {
        ...SHELL,
        // A release with no size in its metadata, and a server that sent no `Content-Length`: the
        // two sources of the total are both empty, which is the only case with no percentage to draw.
        codify_update_check: { ...AVAILABLE, asset_bytes: null },
        codify_update_download: () => pending,
      },
    },
    async (dom, bus) => {
      await dom.click(dom.byButton("Check for updates"));
      await dom.settle();
      await dom.click(dom.byButton("Download Codify_1.0.1_amd64.deb"));
      await dom.settle();
      await bus.emit(UPDATE_PROGRESS, { received: 2_000_000, total: null });
      await dom.settle();
      assert.match(dom.text(), /2\.0 MB downloaded, and GitHub did not say how big the file is\./);
      const bar = dom.container.querySelector("[role=progressbar]");
      assert.ok(bar, "no bar at all for a download whose size is unknown");
      assert.equal(
        bar.getAttribute("aria-valuenow"),
        null,
        "a bar with nothing to divide by announced a percentage anyway",
      );
      assert.equal(
        (bar.firstElementChild as HTMLElement).style.width,
        "",
        "a size-less bar was filled to a width nobody knows",
      );
      finish(DOWNLOADED);
      await dom.settle();
      assert.match(dom.text(), /never escalates/);
    },
  );
});

test("a progress message that is not one does not move the bar", async () => {
  let finish!: (answer: unknown) => void;
  const pending = new Promise((resolve) => {
    finish = resolve;
  });
  await withAbout(
    {
      shell: { ...SHELL, codify_update_check: AVAILABLE, codify_update_download: () => pending },
    },
    async (dom, bus) => {
      await dom.click(dom.byButton("Check for updates"));
      await dom.settle();
      await dom.click(dom.byButton("Download Codify_1.0.1_amd64.deb"));
      await dom.settle();
      // Whatever a shell that had gone wrong sent, the pane waits rather than painting it.
      for (const payload of [null, "halfway", { received: "most of it", total: 18_000_000 }, {}]) {
        await bus.emit(UPDATE_PROGRESS, payload);
        await dom.settle();
        assert.match(dom.text(), /Starting the download…/, `${JSON.stringify(payload)} was painted`);
      }
      assert.equal(
        dom.container.querySelector("[role=progressbar]")?.getAttribute("aria-valuenow"),
        null,
        "a payload that is not a progress report moved the bar",
      );
      finish(DOWNLOADED);
      await dom.settle();
      assert.match(dom.text(), /never escalates/);
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
