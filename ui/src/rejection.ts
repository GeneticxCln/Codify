/**
 * Reading a rejection, whatever shape it arrived in.
 *
 * ## Why this exists
 *
 * `catch (err) { setError(err?.message || "…") }` assumes every rejection is an
 * `Error`. Two things in this app are not, and both are the *interesting* ones:
 *
 * 1. **A Rust command's own refusal.** `terminal::open` and `browser::open`
 *    return `Result<String, String>`, and Tauri rejects with that string. So
 *    `pin_cwd`'s careful sentence — "workspace w1 has no root_path recorded, so
 *    there is nowhere to start a shell" — arrived as a bare string, `err.message`
 *    was `undefined`, and the user was told "Could not start a shell" instead.
 *    The engine's refusal is the most informative sentence in the whole path and
 *    it was being replaced by the least.
 *
 * 2. **An ACL denial.** `RuntimeAuthority::resolve_access` in Tauri 2.11.6
 *    rejects with a formatted `String` ("codify_terminal_open not allowed on
 *    window …"), not an `Error`. A closed grant is a security event and it
 *    arrived wearing the same invisible costume.
 *
 * Both were found the same way: a message that said less than the system knew.
 * The shape of a rejection is not something a caller should have to know, so it
 * is read in one place and every `catch` goes through it.
 *
 * This is the IPC twin of `readErrorBody` in `./errorBody`, which reads an HTTP
 * refusal body. Same idea, different wire: that one is a parsed JSON object,
 * this one is whatever a rejected promise was handed.
 */

/** Anything a `catch` can bind. Deliberately wide — that is the whole problem. */
export type Rejection = unknown;

/**
 * The message a rejection is carrying, or `fallback` when it carries none.
 *
 * The order matters. A string is checked first because it is the shape Tauri
 * uses for both refusals above, and `"".length` on a string is not a reliable
 * test for "has a message" — an engine that refused with an empty string has
 * said nothing, and the caller's own sentence is then the better one.
 *
 * `fallback` defaults to a generic string rather than being required, because a
 * caller with nothing better to say should still not render an empty banner.
 */
export function readRejection(
  rejection: Rejection,
  fallback = "Something went wrong",
): string {
  // A string, or a boxed String.
  if (typeof rejection === "string") {
    return rejection.trim() ? rejection : fallback;
  }
  if (rejection instanceof String) {
    const text = rejection.valueOf().trim();
    return text ? text : fallback;
  }
  // An Error, or anything Error-shaped (Tauri sometimes rejects with a plain
  // object carrying a message).
  if (rejection && typeof rejection === "object") {
    const message = (rejection as { message?: unknown }).message;
    if (typeof message === "string" && message.trim()) return message;
    // `toString` is the last honest attempt: a thrown object with no message
    // still often names itself usefully, and `String({})` is "[object Object]"
    // which is why the bracket form is refused explicitly.
    const text = safeToString(rejection);
    if (text && text !== "[object Object]") return text;
  }
  return fallback;
}

/** `String(x)` without letting a throwing `toString` escape into a render. */
function safeToString(value: object): string | null {
  try {
    const text = String(value).trim();
    return text || null;
  } catch {
    return null;
  }
}
