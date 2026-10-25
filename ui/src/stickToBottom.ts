import { useEffect, useRef, type RefObject } from "react";

/**
 * Keep a transcript scrolled to its end while the person is at its end, and leave it alone when they are not.
 *
 * It used to scroll on every change to the message list, and that list is a new array on every goal event and every
 * poll tick (about every second and a half while a goal runs), so reading earlier output while a goal ran was
 * impossible: the view was pulled back to the bottom as fast as it was left. A thread running in another tab did it too.
 *
 * The rule: scroll when the last message is new (the person sent something, or an answer started) or when the view
 * was already at the end; otherwise do nothing. "Already at the end" is remembered from the container's scroll events,
 * because by the time the effect runs the content has grown and the distance to the end is no longer the person's.
 */

/** How far from the end still counts as being at it, in pixels. */
export const AT_THE_END_PX = 80;
/** After the code scrolls, scroll events are its own animation and say nothing about where the person is. */
const OWN_SCROLL_MS = 1_000;

/** The nearest ancestor that actually scrolls, or null when nothing above it does. */
export function scrollParent(from: Element | null): HTMLElement | null {
  for (let el = from?.parentElement ?? null; el; el = el.parentElement) {
    const overflowY = el.ownerDocument.defaultView?.getComputedStyle(el).overflowY ?? "";
    if (overflowY === "auto" || overflowY === "scroll") return el;
  }
  return null;
}

export function atTheEnd(el: HTMLElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= AT_THE_END_PX;
}

export function useStickToBottom(sentinel: RefObject<HTMLElement | null>, count: number, changed: unknown): void {
  const stuck = useRef(true);
  const seen = useRef(0);
  const lastOwnScroll = useRef(0);

  useEffect(() => {
    const scroller = scrollParent(sentinel.current);
    if (!scroller) return;
    const onScroll = () => {
      if (atTheEnd(scroller)) stuck.current = true;
      else if (Date.now() - lastOwnScroll.current > OWN_SCROLL_MS) stuck.current = false;
    };
    scroller.addEventListener("scroll", onScroll, { passive: true });
    return () => scroller.removeEventListener("scroll", onScroll);
  }, [sentinel]);

  useEffect(() => {
    const isNew = count > seen.current;
    seen.current = count;
    if (isNew) stuck.current = true;
    if (!stuck.current) return;
    lastOwnScroll.current = Date.now();
    sentinel.current?.scrollIntoView({ behavior: "smooth" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [changed, count]);
}
