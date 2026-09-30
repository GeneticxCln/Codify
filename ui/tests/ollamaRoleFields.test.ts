/**
 * The two Ollama-only fields on a role card, used rather than read.
 *
 * Both exist because Ollama does something silently. `num_ctx` truncates a
 * prompt that does not fit its 4096 default and returns a degraded answer
 * rather than an error; `keep_alive` unloads a model five minutes after its
 * last request, so a role used every ten minutes pays a full reload each time.
 * Neither failure announces itself, which is why they are per-role fields at
 * all rather than a global default nobody thinks to look for.
 *
 * This file used to match regexes against `AgentConfigCard.tsx`, and it would
 * have kept passing with either field wired to nothing. It mounts the card in a
 * DOM and presses Save instead, so each claim is about what a person sees and
 * what the engine is sent:
 *
 *  - **Both fields are gated on `protocol === "ollama"`.** A window or a
 *    residency control on an OpenAI-compatible provider is not a setting that
 *    does nothing — it is a setting that *looks* like it is doing something,
 *    which is worse, because the user changes it and the behaviour is
 *    identical. The gate is the *protocol*, not the provider's name: a custom
 *    provider that speaks Ollama's protocol needs the fields as much as the
 *    built-in one.
 *
 *  - **An emptied field saves as `null`, not as absent.** The engine treats
 *    these as clearable; sending "no change" instead of "back to the default"
 *    is why a user who deletes the value can see it come straight back.
 *
 *  - **`keep_alive`'s wording says it is about the gap *between* calls.** It
 *    does not make a call faster. A field that implies otherwise gets set
 *    expecting a latency win that never arrives, and then gets blamed.
 *
 *  - **Both are typed on the config and on the patch.** Adding one to only the
 *    config type type-checks the card and then fails at runtime the moment a
 *    save is attempted. `make typecheck-ui-tests` is the leg that says so; the
 *    values below are the declarations it checks.
 */
import test from "node:test";
import assert from "node:assert/strict";

import { registerTsx } from "./tsxLoader.ts";
registerTsx();

const { withDom } = await import("./dom.ts");
import type { Dom } from "./dom.ts";
import type { AgentConfig, AgentConfigPatch, ProviderProtocol } from "../src/types.ts";
import type { AgentConfigStore } from "../src/hooks/useAgentConfigs.ts";

const React = (await import("react")).default;
const h = React.createElement;

const BUILTIN = ["anthropic", "openai", "ollama", "google"];

function role(protocol: ProviderProtocol, extra: Partial<AgentConfig> = {}): AgentConfig {
  return {
    role: "fixer",
    display_name: "Fixer",
    provider: protocol === "ollama" ? "ollama" : "openai",
    protocol,
    model_name: "qwen2.5-coder:3b",
    temperature: 0.2,
    max_tokens: 4096,
    updated_at: 0,
    ...extra,
  };
}

/** A store that answers from memory and remembers every patch it was sent. */
function storeFor(config: AgentConfig): { store: AgentConfigStore; patches: AgentConfigPatch[] } {
  const patches: AgentConfigPatch[] = [];
  const store: AgentConfigStore = {
    configs: [config],
    loading: false,
    error: null,
    update: async (_role, patch) => {
      patches.push(patch);
      return { ...config, ...patch } as AgentConfig;
    },
    testConnection: async () => ({ ok: true, message: "" }),
    refresh: async () => {},
  };
  return { store, patches };
}

/** Mount one role's card inside a DOM. The card is imported inside it, as `dom.ts` explains. */
async function withCard(
  config: AgentConfig,
  body: (dom: Dom, patches: AgentConfigPatch[]) => Promise<void>,
): Promise<void> {
  await withDom(async (dom) => {
    const { AgentConfigCard } = await import("../src/components/AgentConfigCard.tsx");
    const { store, patches } = storeFor(config);
    await dom.render(h(AgentConfigCard, { role: config.role, store, builtinProviders: BUILTIN }));
    await body(dom, patches);
  });
}

/** The tree's text with whitespace collapsed, so a sentence is asserted as a sentence. */
const prose = (dom: Dom): string => dom.text().replace(/\s+/g, " ");

const FIELDS = ["Context Window", "Keep Alive"];

test("both Ollama-only fields are on an Ollama role", async () => {
  await withCard(role("ollama"), async (dom) => {
    for (const label of FIELDS) {
      assert.ok(dom.byField(label), `${label} is missing from an Ollama role`);
    }
  });
});

test("neither field is offered on a provider that has no equivalent", async () => {
  for (const protocol of ["openai_compat", "anthropic", "google"] as const) {
    await withCard(role(protocol), async (dom) => {
      for (const label of FIELDS) {
        assert.throws(
          () => dom.byField(label),
          /no field is labelled/,
          `${label} would be shown on ${protocol}, which has no such setting`,
        );
        assert.ok(
          !prose(dom).includes(label),
          `${label} is on screen for ${protocol} without a control behind it`,
        );
      }
    });
  }
});

test("the gate is the protocol, not the provider's name", async () => {
  await withCard(role("ollama", { provider: "the-box-under-my-desk" }), async (dom) => {
    for (const label of FIELDS) {
      assert.ok(dom.byField(label), `${label} vanished for a custom provider speaking Ollama`);
    }
  });
});

test("each field saves as null when emptied, so a clear is a clear", async () => {
  // Absent means "no change" to the merge in AgentRegistryService; null means
  // "back to Ollama's own default". A user who deletes the value must get the
  // second one, and the difference is invisible until the value comes back.
  await withCard(role("ollama", { num_ctx: 32768, keep_alive: "30m" }), async (dom, patches) => {
    await dom.fill(dom.byField("Context Window") as HTMLInputElement, "");
    await dom.fill(dom.byField("Keep Alive") as HTMLInputElement, "");
    await dom.click(dom.byButton("Save"));
    assert.equal(patches.length, 1, "Save sent nothing, or sent twice");
    assert.ok("num_ctx" in patches[0], "an emptied window was left out, which reads as no change");
    assert.equal(patches[0].num_ctx, null, "an emptied window must arrive as null");
    assert.ok("keep_alive" in patches[0], "an emptied keep-alive was left out");
    assert.equal(patches[0].keep_alive, null, "an emptied keep-alive must arrive as null");
  });
});

test("a blank keep-alive is not a value", async () => {
  await withCard(role("ollama", { keep_alive: "30m" }), async (dom, patches) => {
    await dom.fill(dom.byField("Keep Alive") as HTMLInputElement, "   ");
    await dom.click(dom.byButton("Save"));
    assert.equal(patches[0].keep_alive, null, "whitespace was sent to Ollama as a duration");
  });
});

test("what is typed is what is sent", async () => {
  await withCard(role("ollama"), async (dom, patches) => {
    await dom.fill(dom.byField("Context Window") as HTMLInputElement, "8192");
    await dom.fill(dom.byField("Keep Alive") as HTMLInputElement, "1h30m");
    await dom.click(dom.byButton("Save"));
    assert.equal(patches[0].num_ctx, 8192, "the window must be a number, not the string typed");
    assert.equal(patches[0].keep_alive, "1h30m");
  });
});

test("the keep-alive field says it is about the gap between calls", async () => {
  await withCard(role("ollama"), async (dom) => {
    const text = prose(dom);
    assert.match(
      text,
      /does not make a call faster/i,
      "the field must not imply a per-call speedup, which it does not deliver",
    );
    assert.match(text, /reloaded between goals/i, "or that it addresses the between-goal reload");
  });
});

test("unset fields state what is actually in force rather than reading as blank", async () => {
  await withCard(role("ollama"), async (dom) => {
    const text = prose(dom);
    assert.match(text, /server default \(5m\)/, "keep-alive must name Ollama's own window");
    assert.match(text, /server default \(4096\)/, "the window must name Ollama's own default");
  });
});

test("set fields show their own value in place of the default", async () => {
  await withCard(role("ollama", { num_ctx: 32768, keep_alive: "30m" }), async (dom) => {
    const text = prose(dom);
    assert.ok(text.includes((32768).toLocaleString()), "the window's value is not shown");
    assert.ok(text.includes("30m"), "the keep-alive's value is not shown");
    assert.doesNotMatch(text, /server default \(5m\)/, "a set value is still labelled as the default");
  });
});

test("both fields are typed on the config and on the patch", () => {
  // Compile-time claims, checked by `make typecheck-ui-tests`: `node --test`
  // erases types, so the runtime half of this test only proves the values are
  // constructible. A field removed from either interface fails the typecheck,
  // and a field widened past `string | null` fails the `@ts-expect-error`.
  const config: AgentConfig = role("ollama", { num_ctx: null, keep_alive: "30m" });
  const patch: AgentConfigPatch = { num_ctx: null, keep_alive: null };
  // @ts-expect-error a duration is text; a bare number would be sent as one and rejected late
  const wrong: AgentConfigPatch = { keep_alive: 30 };
  assert.equal(config.keep_alive, "30m");
  assert.equal(patch.keep_alive, null);
  assert.equal(wrong.keep_alive, 30);
});
