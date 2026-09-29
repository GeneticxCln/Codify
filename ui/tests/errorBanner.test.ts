/**
 * The app-wide error banner is clearable.
 *
 * This is the surface a refusal lands on when there is no pane to put it in —
 * a terminal that would not start, an engine that went away, a settings write
 * the engine refused. It had no dismiss control, and `setError(null)` is only
 * ever called by whichever handler owns the *next* action, so a message from an
 * action the user does not repeat simply stayed on screen. That is what made
 * clicking Terminal in a browser tab feel broken even after the refusal text
 * landed: the one honest sentence the panes can give you was permanent.
 *
 * These mount the whole App against `appHarness.ts` and make a real refusal
 * happen (the shell refusing to start a terminal), then use the banner the way a
 * person does. They replace tests that read `App.tsx` for `onClick={() =>
 * setError(null)}`: that string was there whether or not pressing the control
 * did anything.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withApp } = await import("./appHarness.ts");

const alerts = (root: HTMLElement): HTMLElement[] =>
  [...root.querySelectorAll('[role="alert"]')] as HTMLElement[];

const terminalButton = (root: HTMLElement): HTMLElement => {
  const button = root.querySelector('button[title^="Terminal"]');
  assert.ok(button, "the panel has no Terminal control");
  return button as HTMLElement;
};

/** Make the shell refuse to start a shell, press Terminal, and return the banner. */
async function refuse(
  ctx: { dom: { container: HTMLElement; click(el: Element): Promise<void> }; settle(): Promise<void> },
): Promise<HTMLElement> {
  await ctx.dom.click(terminalButton(ctx.dom.container));
  await ctx.settle();
  const [banner] = alerts(ctx.dom.container);
  assert.ok(banner, "a refused terminal put nothing on screen");
  return banner;
}

const REFUSAL = { codify_terminal_open: "the shell would not start: no such file" };

test("a refusal is announced, and the banner can be dismissed", async () => {
  await withApp({ shellFails: REFUSAL }, async (ctx) => {
    const banner = await refuse(ctx);
    // Announced to assistive technology, not just coloured red, and it says what
    // happened rather than "something went wrong".
    assert.equal(banner.getAttribute("role"), "alert");
    assert.match(banner.textContent ?? "", /no such file/);

    const dismiss = banner.querySelector('button[aria-label="Dismiss error"]');
    assert.ok(
      dismiss,
      "the banner has no control named 'Dismiss error'; a refusal the reader has " +
        "understood and cannot dismiss is an obstacle, not a report",
    );
    await ctx.dom.click(dismiss);
    await ctx.settle();
    assert.equal(alerts(ctx.dom.container).length, 0, "pressing dismiss did not clear the banner");
  });
});

test("the dismiss control has an accessible name (an icon with no label is unreadable)", async () => {
  await withApp({ shellFails: REFUSAL }, async (ctx) => {
    const banner = await refuse(ctx);
    const dismiss = banner.querySelector("button");
    assert.ok(dismiss, "the banner has no button at all");
    const name = dismiss.getAttribute("aria-label") || dismiss.textContent?.trim() || "";
    assert.ok(name.length > 0, "the dismiss control has no accessible name");
  });
});

test("a browser pane's refusal stays in its pane, not in the app banner", async () => {
  // One surface per refusal. The panes have their own channels, and a pane whose
  // message also went to the banner would show the same refusal twice for one
  // mistake — which is how a dismissable banner starts hiding a message that is
  // still being reported.
  await withApp({}, async ({ dom, settle }) => {
    const browser = dom.container.querySelector('button[title^="Browser"]') as HTMLElement;
    await dom.click(browser);
    await settle();
    const address = dom.container.querySelector('input[aria-label="Address"]') as HTMLInputElement | null;
    assert.ok(address, "opening a browser tab showed no address bar");

    // A loopback address is one the shell never lets a page reach.
    await dom.fill(address, "http://127.0.0.1:7430/");
    await dom.press(address, "Enter");
    await settle();

    const shown = alerts(dom.container);
    assert.equal(shown.length, 1, `expected the refusal once, found ${shown.length}`);
    const pane = dom.container.querySelector('[aria-label="Browser"]');
    assert.ok(pane, "there is no browser pane");
    assert.ok(pane.contains(shown[0]), "the refusal is in the app banner instead of the pane the user typed it in");
    assert.ok(shown[0].querySelector('button[aria-label="Dismiss error"]') === null, "the pane's refusal has grown the app banner's dismiss control");
  });
});
