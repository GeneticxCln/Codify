import type { ProviderModelStatus } from "./types.ts";

export type ProviderHealthState =
  | "ok"
  | "auth_error"
  | "unreachable"
  | "error"
  | "offline"
  | "unconfigured"
  | "unknown";

export interface ProviderHealthView {
  state: ProviderHealthState;
  /** Semantic Tailwind dot color class (`bg-codify-*`). */
  dotClass: string;
  /** Short latency badge when measured, e.g. `"42 ms"`. */
  latency: string | null;
  /** Full human-readable tooltip / aria-label describing reachability, auth, and latency. */
  detail: string;
}

function fmtLatency(ms: number | null | undefined): string | null {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 0) return null;
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

/**
 * Derive the Settings status dot, latency label, and tooltip from a provider's live health check.
 * Pure and DOM-free so every state transition can be asserted directly.
 */
export function providerHealthView(
  status: ProviderModelStatus | undefined,
  opts: { hasKey: boolean; needsKey: boolean },
): ProviderHealthView {
  if (!status) {
    if (opts.needsKey && !opts.hasKey) {
      return {
        state: "unconfigured",
        dotClass: "bg-codify-border",
        latency: null,
        detail: "Not configured — add an API key to check health",
      };
    }
    return {
      state: "unknown",
      dotClass: "bg-codify-border",
      latency: null,
      detail: "Health check pending",
    };
  }

  const latency = fmtLatency(status.latency_ms);

  if (status.offline_skipped) {
    return {
      state: "offline",
      dotClass: "bg-codify-border",
      latency: null,
      detail: "Offline mode — remote health check paused",
    };
  }

  if (status.ok) {
    const parts = ["Reachable"];
    if (opts.needsKey) parts.push("auth ok");
    if (latency) parts.push(latency);
    return {
      state: "ok",
      dotClass: "bg-codify-success",
      latency,
      detail: parts.join(" · "),
    };
  }

  if (status.auth_ok === false) {
    const parts = [status.reachable === false ? "Missing key" : "Auth failed"];
    if (latency) parts.push(latency);
    if (status.error) parts.push(status.error);
    return {
      state: "auth_error",
      dotClass: "bg-codify-danger",
      latency,
      detail: parts.join(" · "),
    };
  }

  if (status.reachable === false) {
    const parts = ["Unreachable"];
    if (latency) parts.push(latency);
    if (status.error) parts.push(status.error);
    return {
      state: "unreachable",
      dotClass: "bg-codify-danger",
      latency,
      detail: parts.join(" · "),
    };
  }

  const parts = ["Error"];
  if (latency) parts.push(latency);
  if (status.error) parts.push(status.error);
  return {
    state: "error",
    dotClass: "bg-codify-warning",
    latency,
    detail: parts.join(" · "),
  };
}
