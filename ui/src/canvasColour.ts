import { hexChannels } from "./appearance";

/**
 * Colour maths for the canvas atmospheres.
 *
 * Six painters need the same three things: a hex the theme published turned
 * into channels, that colour with an alpha, and a blend between two of them.
 * Carried inline, that is three helpers in six files, and the sixth copy is
 * where a `#rrggbb` regex that forgot the `^` anchor would live.
 *
 * `hexChannels` is the module the themes already use to derive the `-rgb`
 * triplets the utility overrides compose, so this is one parser rather than a
 * second one that agrees with it by coincidence.
 */

const channelCache = new Map<string, string>();
const tupleCache = new Map<string, readonly [number, number, number]>();

function parseTuple(channels: string): readonly [number, number, number] {
  let cached = tupleCache.get(channels);
  if (cached) return cached;
  const parts = channels.split(",");
  cached = [Number(parts[0]), Number(parts[1]), Number(parts[2])];
  if (tupleCache.size > 256) tupleCache.clear();
  tupleCache.set(channels, cached);
  return cached;
}

/** `#00e5ff` → `"0, 229, 255"`. Throws on anything that is not `#rrggbb`. */
export function channelsOf(hex: string): string {
  let cached = channelCache.get(hex);
  if (cached !== undefined) return cached;
  cached = hexChannels(hex).split(" ").join(", ");
  if (channelCache.size > 256) channelCache.clear();
  channelCache.set(hex, cached);
  return cached;
}

/** `"0, 229, 255"` + `0.5` → `"rgba(0, 229, 255, 0.5)"`. */
export function withAlpha(channels: string, alpha: number): string {
  return `rgba(${channels}, ${alpha})`;
}

/** A hex and an alpha in one call, for the common case. */
export function toRgba(hex: string, alpha: number): string {
  return withAlpha(channelsOf(hex), alpha);
}

/**
 * Linear blend of two `"r, g, b"` channel strings. `t` is clamped, because the
 * callers that feed it a distance-derived weight should not have to.
 */
export function mix(from: string, to: string, t: number): string {
  const k = Math.min(1, Math.max(0, t));
  const a = parseTuple(from);
  const b = parseTuple(to);
  return `${Math.round(a[0] + (b[0] - a[0]) * k)}, ${Math.round(a[1] + (b[1] - a[1]) * k)}, ${Math.round(a[2] + (b[2] - a[2]) * k)}`;
}

/** A cheap deterministic wobble in `[-1, 1]`, for drifting without state. */
export function wobble(seed: number, t: number, speed = 1): number {
  return Math.sin(t * speed + seed) * 0.5 + Math.sin(t * speed * 1.7 + seed * 2.3) * 0.5;
}
