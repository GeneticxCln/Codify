/**
 * The shape of the gate's fine-tuned checkpoint setting.
 *
 * The engine owns the rule (`engine/laya.py` `checkpoint_problem`) and refuses anything else
 * with a 422; this is the same rule for the field, so a save is not a round trip to learn that
 * a URL is not a Hub repo id. Empty means the built-in Router and is always valid.
 *
 * Kept pure and DOM-free, like `conductorSettings.ts`: the panel imports it and the test needs
 * no renderer.
 */
export const CHECKPOINT_MAX = 200;
export const CHECKPOINT_RE =
  /^[A-Za-z0-9][A-Za-z0-9._-]*\/[A-Za-z0-9][A-Za-z0-9._-]*$/;

/** Why `value` is not a Hub repo id, or null when it is (or when it is empty). */
export function checkpointProblem(value: string): string | null {
  const text = value.trim();
  if (text === "") return null;
  if (text.length > CHECKPOINT_MAX) {
    return `A checkpoint id is at most ${CHECKPOINT_MAX} characters.`;
  }
  if (!CHECKPOINT_RE.test(text)) {
    return "Use the Hugging Face repo id the fine-tune was pushed to, like owner/laya-codify.";
  }
  return null;
}
