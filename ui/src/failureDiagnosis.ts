import type { AgentConfig, ModelOption, ProviderKeyStatus, ProviderModelStatus } from "./types.ts";

/** What the engine told us about a failure, plus the live state of the role. */
export interface FailureFacts {
  code: string;
  message: string;
  /** The role the engine attributed the failure to, when it knew. */
  role?: string | null;
  /** That role's stored config, if the engine has one. */
  config?: AgentConfig;
  /** Credential state for the role's provider. */
  keys?: ProviderKeyStatus;
  /** What discovery said for the role's provider on this run. */
  discovery?: ProviderModelStatus;
  /** Everything discovery returned, so the model can be checked against it. */
  catalog: ModelOption[];
}

/**
 * Failure codes where a role's configuration can actually be the cause.
 *
 * For anything else — a path escape, a bad JSON reply, a failing test — a missing
 * key or a retired model is a *separate* problem waiting to happen, and leading
 * with it would blame the wrong thing.
 */
const CONFIG_EXPLAINS = new Set([
  "agent_not_configured",
  "provider_http",
  "secrets_unwritable",
  "invalid_base_url",
  "unknown_protocol",
]);

export interface Verdict {
  level: "blocker" | "warning" | "info";
  title: string;
  detail: string;
  /** Where the fix lives, when there is one. */
  fix?: { tab: "keys" | "agents"; label: string };
}

/**
 * What each failure code actually means, in the engine's vocabulary.
 *
 * These are the strings the user sees in the chat, so the panel has to explain
 * them rather than repeat them.
 */
export const CODE_MEANING: Record<string, string> = {
  agent_not_configured:
    "The role could not be called at all — it has no model chosen, or its provider has no credential. Nothing was sent, so the model is not at fault.",
  agent_output_invalid:
    "The provider answered, but not in the JSON shape the role's contract requires. A retry often fixes it; a model that struggles with strict JSON will keep failing.",
  provider_http:
    "The provider returned an HTTP error — a bad key, a rate limit, or an outage on their side.",
  tests_failed:
    "The tester ran a command and it reported failure. The agent behaved correctly; the code under test is what failed.",
  command_not_allowed:
    "The sandbox refused a command — the binary or arguments are not on the allowlist. The tester normally retries with a permitted runner.",
  path_escape:
    "A file path resolved outside the workspace root, so the write was refused. No file was touched.",
  laya_blocked:
    "The System-1 gate decided this request is too risky to run (prompt-injection or destructive intent). No model was called and no file was touched.",
  version_conflict:
    "Something else changed the goal first, so the action was refused to avoid clobbering it.",
  nothing_to_apply:
    "There was no stored dry-run proposal to apply.",
  secrets_unwritable:
    "The key store could not be written — permissions or a read-only filesystem.",
};

/**
 * Turn a failure plus the role's live state into an ordered list of verdicts.
 *
 * Ordering is the point: the first verdict is the one that explains the failure.
 * A missing key makes discovery fail too, and reporting both would bury the
 * cause under its own symptom.
 */
export function diagnoseFailure(facts: FailureFacts): Verdict[] {
  const verdicts: Verdict[] = [];
  const { config, keys, discovery } = facts;

  // Declared up front: the no-config early return below pushes it too.
  const explainer: Verdict = {
    level: "info",
    title: `What "${facts.code}" means`,
    detail: CODE_MEANING[facts.code] || "No explanation recorded for this code yet.",
  };

  if (!facts.role) {
    verdicts.push({
      level: "info",
      title: "The engine did not attribute this failure to a role",
      detail:
        "Nothing to inspect automatically, so check the roles below by hand. Errors raised outside a role phase (for example a sandbox refusal) carry no role.",
    });
  }

  if (facts.role && !config) {
    verdicts.push({
      level: "blocker",
      title: `No configuration is stored for the "${facts.role}" role`,
      detail:
        "The engine has no provider or model for this role, so it cannot run. Recreate the role in Settings → Agent Roles.",
      fix: { tab: "agents", label: "Open Agent Roles" },
    });
    // The blocker is the answer, but the failure code still deserves its
    // explainer — an early return here used to drop it.
    verdicts.push(explainer);
    return verdicts;
  }

  if (config && !(config.model_name || "").trim()) {
    verdicts.push({
      level: "blocker",
      title: `No model is chosen for the "${config.role}" role`,
      detail:
        "Codify ships no model list, so every role starts empty and has to be pointed at a model its provider actually serves. Nothing was sent to a provider.",
      fix: { tab: "agents", label: "Pick a model" },
    });
  }

  const needsKey = keys ? keys.needs_key : config ? config.protocol !== "ollama" : false;
  if (config && needsKey && keys && !keys.has_key) {
    verdicts.push({
      level: "blocker",
      title: `No credential is stored for ${config.provider}`,
      detail: `This role's provider needs a key and none is stored${
        keys.storage === "file"
          ? ` (keys on this machine are kept in ${keys.storage_detail})`
          : " in your OS keychain"
      }. Environment variables for this provider are also checked.`,
      fix: { tab: "keys", label: `Add a key for ${config.provider}` },
    });
  }

  // A discovery failure caused by the missing key above is a symptom, not a
  // second problem — and its error text already says so. Suppress only when a
  // missing key is CONFIRMED: with key state unknown (`keys` unset), treating
  // it as missing would hide a real discovery problem.
  const discoveryIsSymptom = (discovery?.error || "").toLowerCase().includes("no api key");
  const keyConfirmedMissing = keys?.has_key === false;
  if (config && discovery && !discovery.ok && !(discoveryIsSymptom && keyConfirmedMissing)) {
    verdicts.push({
      level: "warning",
      title: `Could not read ${config.provider}'s model list`,
      detail: `Discovery reported: ${discovery.error}. Until that succeeds the picker has nothing to offer, and a model name cannot be verified.`,
      fix: { tab: "keys", label: "Check the provider" },
    });
  }

  if (config && discovery?.ok) {
    const catalogIds = facts.catalog
      .filter((m) => m.provider === config.provider)
      .map((m) => m.id);
    const chosen = (config.model_name || "").trim();
    if (chosen && !catalogIds.includes(chosen)) {
      verdicts.push({
        level: "blocker",
        title: `${config.provider} no longer reports "${chosen}"`,
        detail: `The provider served ${catalogIds.length} model${
          catalogIds.length === 1 ? "" : "s"
        } and that id is not among them — retired or renamed. A call to it fails with a 404 on their side.`,
        fix: { tab: "agents", label: "Choose a current model" },
      });
    } else if (chosen) {
      verdicts.push({
        level: "info",
        title: `${chosen} is currently served by ${config.provider}`,
        detail:
          "The model exists and a call reaches the provider, so this failure came from the reply or from the code under test — not from the configuration.",
      });
    }
  }

  const rank = { blocker: 0, warning: 1, info: 2 } as const;
  const bySeverity = (a: Verdict, b: Verdict) => rank[a.level] - rank[b.level];

  if (CONFIG_EXPLAINS.has(facts.code)) {
    // Configuration can be the cause, so its findings lead and the code explains
    // them underneath.
    return [...verdicts, explainer].sort(bySeverity);
  }

  // Otherwise the code's meaning is the answer. Configuration findings are
  // demoted to second place and labelled as separate, so the panel cannot be
  // read as "your key is missing" when the real failure was a path escape.
  const demoted = verdicts
    .filter((v) => v.level !== "info")
    .map((v) => ({ ...v, level: "warning" as const, detail: `Separate from this failure: ${v.detail}` }));
  return [explainer, ...demoted.sort(bySeverity), ...verdicts.filter((v) => v.level === "info")];
}
