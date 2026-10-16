/**
 * Cutting a string to a length without splitting a character.
 *
 * A JavaScript string is UTF-16, and an emoji (or any character outside the Basic Multilingual Plane) is two code
 * units. `text.slice(0, n)` counts units, so a cut that lands between the two leaves a high surrogate with no low one
 * after it. That string renders as a replacement glyph, and when it is sent to the engine it is refused outright:
 * `JSON.stringify` writes the lone half as `\ud83d`, and the engine's request validation rejects it (422, `string_unicode`),
 * for every attempt, because the same prompt cuts at the same place. A thread named from a prompt that has an emoji at its
 * 60th unit could not be created at all.
 *
 * `trimTail` (`terminalHistory.ts`) is the same rule for the other end of a string; this is for the head.
 */

/** `text.slice(0, limit)`, one unit shorter when the cut would end on the first half of a pair. */
export function clipHead(text: string, limit: number): string {
  if (text.length <= limit) return text;
  const head = text.slice(0, limit);
  const last = head.charCodeAt(head.length - 1);
  // 0xD800-0xDBFF: a high surrogate, which only means something when a low one follows it.
  if (last >= 0xd800 && last <= 0xdbff) return head.slice(0, -1);
  return head;
}
