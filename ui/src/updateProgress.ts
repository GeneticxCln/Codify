/**
 * The About tab's download, as arithmetic and words.
 *
 * Kept out of the pane for the reason `engineRuntime.ts` is: what a bar should say at 30% of a size
 * the server never announced is worth deciding once and testing without a DOM, and the pane is then
 * only the layout around it.
 *
 * Every function here takes the progress the shell last sent, which may be `null` — a download that
 * has not reported yet — plus the size the *release* reported, which is the fallback when the
 * download itself carried no `Content-Length`. Two sources of one number, and the wording says
 * which one it had, because "of 18 MB" and "and GitHub did not say how big it is" are different
 * things for a person staring at a bar that is not filling.
 */
import type { UpdateProgress } from "./shellEvents";

/**
 * A size in the units a release page prints — decimal megabytes, as `megabytes` in the pane's own
 * sentences already uses, so the bar and the paragraph above it cannot disagree.
 */
export function size(bytes: number): string {
  const mb = bytes / 1_000_000;
  return mb >= 10 ? `${Math.round(mb)} MB` : `${mb.toFixed(1)} MB`;
}

/**
 * How full the bar is, as a whole percent, or `null` when nothing says how big the file is.
 *
 * `null` is not zero: it is the pane's cue to draw a bar with no fill to claim rather than one that
 * says a download is going nowhere. Floored rather than rounded, so a bar never reads 100% while
 * there are still bytes to come — the last percent arrives when the file does.
 */
export function updatePercent(
  progress: UpdateProgress | null,
  expected: number | null,
): number | null {
  if (progress === null) return null;
  const total = progress.total ?? expected;
  if (total === null || total <= 0) return null;
  const percent = Math.floor((progress.received / total) * 100);
  return Math.max(0, Math.min(100, percent));
}

/** What the pane says under the bar while a download runs. */
export function updateLine(progress: UpdateProgress | null, expected: number | null): string {
  if (progress === null) return "Starting the download…";
  const arrived = size(progress.received);
  const total = progress.total ?? expected;
  if (total === null || total <= 0) {
    return `${arrived} downloaded, and GitHub did not say how big the file is.`;
  }
  const percent = updatePercent(progress, expected);
  return `${arrived} of ${size(total)}${percent === null ? "" : ` — ${percent}%`}`;
}
