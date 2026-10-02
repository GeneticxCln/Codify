import type { ProviderProtocol } from "./types.ts";

/**
 * How a provider and a protocol are named on screen. Display only.
 *
 * A provider's *slug* (`openai`, `nvidia`, a custom `my-gateway`) is an identifier: it is what the
 * engine stores, what a role's `provider` field holds and what every request carries, and none of
 * that changes here. What this module owns is the words a person reads. The row used to print the
 * slug through CSS `capitalize`, which made `openai` read "Openai" and `nvidia` "Nvidia", and printed
 * the protocol as the raw tag `openai_compat`; the provider select printed every slug in capitals.
 *
 * **This is not a catalogue.** The brand spellings below are a table of how eight names are written,
 * so that a provider is called what it calls itself. It never decides what a provider is, whether it
 * exists, or what it serves: the providers in Settings, and every model, still come from the engine
 * and from live discovery (`docs/06`), and a slug this table has never heard of is shown as written,
 * title-cased. A provider missing here is shown correctly enough; a provider listed here that the
 * engine does not have is simply never drawn.
 */
const BRANDS: Readonly<Record<string, string>> = {
  openai: "OpenAI",
  openrouter: "OpenRouter",
  deepseek: "DeepSeek",
  nvidia: "NVIDIA",
  groq: "Groq",
  google: "Google",
  anthropic: "Anthropic",
  ollama: "Ollama",
};

/** `my-gateway` → "My Gateway": a slug nobody told this module about, made readable and no more. */
function titleCase(slug: string): string {
  return slug
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

/** The name a person reads for a provider slug. Empty in, empty out: an unset field is not a name. */
export function providerLabel(slug: string): string {
  const key = slug.trim().toLowerCase();
  if (!key) return "";
  return BRANDS[key] ?? titleCase(slug.trim());
}

/** A protocol's two spellings: a word for a row, and the word plus its endpoint for the choice list. */
export const PROTOCOL_LABELS: Readonly<Record<ProviderProtocol, { short: string; long: string }>> = {
  openai_compat: { short: "OpenAI-compatible", long: "OpenAI Compatible (chat/completions)" },
  anthropic: { short: "Anthropic", long: "Anthropic (v1/messages)" },
  ollama: { short: "Ollama", long: "Ollama (api/generate)" },
  google: { short: "Google Gemini", long: "Google Gemini (v1beta)" },
};

/** The words for a protocol tag. A tag this module does not know is shown as the engine sent it. */
export function protocolLabel(protocol: string, form: "short" | "long" = "short"): string {
  const known = (PROTOCOL_LABELS as Readonly<Record<string, { short: string; long: string }>>)[protocol];
  return known ? known[form] : protocol;
}
