import React from "react";

/**
 * The app's name, in its own letters.
 *
 * This is the centre of the empty transcript, and it replaced a heading plus a
 * paragraph explaining what Codify would do. The paragraph was the problem: a
 * window with nothing in it was asking to be read before it had been used, and
 * the three lines of it ("select a project folder… inspect your codebase, plan
 * atomic steps, propose file diffs…") described a pipeline to someone who had
 * not asked for one. The name is what an empty window should be.
 *
 * **Thin by design.** Everything that could be decided without a DOM was
 * decided in `ui/src/index.css`, which is where the gradient, the sweep and the
 * `prefers-reduced-motion` answer live — a CSS animation needs no JavaScript to
 * honour reduced motion, and doing it in CSS means it is correct on the first
 * paint rather than after a hydration tick. What is left here is the one thing
 * CSS cannot say: that the two visual layers are decoration and the word itself
 * is the accessible content.
 *
 * **It is a logotype, so it is announced once.** The glyphs are painted twice
 * (the fill and the sweep) and both copies are `aria-hidden`; the container
 * carries the name. Without that, a screen reader reads "Codify" twice, and the
 * second reading is the part that happens to be a `div` with a gradient in it.
 */
export interface WordmarkProps {
  /**
   * Extra classes on the container. The size ramp lives here rather than in the
   * component so a caller that needs a smaller mark — a settings header, a
   * print stylesheet — does not have to fork the component to get one.
   */
  className?: string;
}

export const Wordmark: React.FC<WordmarkProps> = ({ className = "" }) => (
  <span
    role="img"
    aria-label="Codify"
    className={
      "codify-wordmark text-[clamp(2.75rem,12vw,7.5rem)] " +
      (className || "")
    }
  >
    <span aria-hidden="true" className="codify-wordmark-fill">
      Codify
    </span>
    <span aria-hidden="true" className="codify-wordmark-sweep">
      Codify
    </span>
  </span>
);
