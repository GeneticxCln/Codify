import React, { useLayoutEffect, useRef } from "react";

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
 * **Thin by design.** The gradient, the band's geometry and its motion answer
 * live in `ui/src/index.css` — a CSS animation needs no JavaScript to honour
 * reduced motion, and doing it in CSS means it is correct on the first paint
 * rather than after a hydration tick.
 *
 * **The band is composited, not repainted.** It used to be a
 * `background-position` animation on text-clipped glyphs, which makes the
 * browser re-rasterize the word every display frame; on a software-composited
 * window that measured ~45% of a core in the renderer, for a highlight. Now the
 * glyphs are rasterized **once** into an alpha mask by [`paintGlyphMask`], and
 * the band underneath is a wide gradient moved by `transform` — a compositor
 * operation over the wordmark's small box, with no glyph painting per frame.
 * The mask is regenerated only when the element's box or the font changes.
 *
 * On a software-composited window even that layer costs a full-window blend,
 * which was measured at a core for the band alone; a verdict stops it there, and
 * the first sweep waits up to ten seconds for one (the boot probe usually has
 * it in under a second — see `frameProbe.ts`). The composited path is what lets
 * a GPU-composited window keep the shimmer without re-rasterizing glyphs.
 *
 * **It is a logotype, so it is announced once.** The fill is `aria-hidden` and
 * the container carries the name. Without that, a screen reader reads "Codify"
 * twice, and the second reading is the part that happens to be a gradient.
 */
export interface WordmarkProps {
  /**
   * Extra classes on the container. The size ramp lives here rather than in the
   * component so a caller that needs a smaller mark — a settings header, a
   * print stylesheet — does not have to fork the component to get one.
   */
  className?: string;
}

/**
 * Rasterize the wordmark's glyphs into an alpha mask, in the element's own
 * coordinate system, and hand it to CSS.
 *
 * The mask is drawn at the element's device-pixel size (capped at 2×, like
 * every other canvas here) and stretched by `mask-size: 100% 100%`, so a
 * resize re-rasterizes rather than smearing. Each character is drawn
 * separately to reproduce CSS `letter-spacing`, which the 2D context has no
 * portable property for; the baseline comes from the font's own ascent and
 * descent, which is the same half-leading CSS uses to place a line box.
 *
 * It says nothing about a colour. White on transparent is the mask's whole
 * vocabulary; the colours stay in the stylesheet, reading the active theme.
 */
function paintGlyphMask(el: HTMLElement): void {
  try {
    paintGlyphMaskUnguarded(el);
  } catch {
    // A DOM without a real 2D context (the test harness) or without a PNG
    // encoder: the fill is still the mark, and the band is left unmasked rather
    // than failing the mount over a decoration.
  }
}

function paintGlyphMaskUnguarded(el: HTMLElement): void {
  const rect = el.getBoundingClientRect();
  if (rect.width < 1 || rect.height < 1) return;
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  const styles = getComputedStyle(el);
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.max(1, Math.round(rect.width * dpr));
  canvas.height = Math.max(1, Math.round(rect.height * dpr));
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, rect.width, rect.height);
  ctx.fillStyle = "#fff";
  ctx.font = `${styles.fontStyle} ${styles.fontWeight} ${styles.fontSize} ${styles.fontFamily}`;
  ctx.textBaseline = "alphabetic";

  const text = styles.textTransform === "uppercase" ? "CODIFY" : "Codify";
  const metrics = ctx.measureText(text) as TextMetrics & {
    fontBoundingBoxAscent?: number;
    fontBoundingBoxDescent?: number;
  };
  // A context that cannot measure text is not a renderer (jsdom's stub reports
  // every string as zero wide), and asking it for a PNG would only print a
  // not-implemented error. The real context measures a wordmark it can paint.
  if (!(metrics.width > 0)) return;
  const ascent = metrics.fontBoundingBoxAscent ?? metrics.actualBoundingBoxAscent;
  const descent = metrics.fontBoundingBoxDescent ?? metrics.actualBoundingBoxDescent;
  const fontHeight = ascent + descent;
  // A font that reports no metrics still has a line box; park the glyphs on its
  // baseline rather than drawing them above the top edge.
  const baseline = fontHeight > 0 ? (rect.height - fontHeight) / 2 + ascent : rect.height * 0.8;

  const spacing = Number.parseFloat(styles.letterSpacing) || 0;
  let x = 0;
  for (const char of text) {
    ctx.fillText(char, x, baseline);
    x += ctx.measureText(char).width + spacing;
  }

  const url = `url(${canvas.toDataURL("image/png")})`;
  el.style.setProperty("-webkit-mask-image", url);
  el.style.setProperty("mask-image", url);
  // The band is hidden until this point: an unmasked band is a bright diagonal
  // rectangle over the word, which is worse than no shimmer at all.
  el.dataset.masked = "true";
}

export const Wordmark: React.FC<WordmarkProps> = ({ className = "" }) => {
  const maskRef = useRef<HTMLSpanElement>(null);

  useLayoutEffect(() => {
    const el = maskRef.current;
    if (!el) return;
    let frame: number | undefined;
    let lastBox = "";
    const paint = (): void => {
      frame = undefined;
      lastBox = `${el.clientWidth}x${el.clientHeight}`;
      paintGlyphMask(el);
    };
    // A resize observer can fire more than once for one layout, and a font's
    // `ready` can land in the same frame; one raster per frame is enough. The
    // old callback rasterized and encoded a PNG every time it was called.
    const paintSoon = (): void => {
      if (frame !== undefined) return;
      if (typeof requestAnimationFrame !== "function") {
        paint();
        return;
      }
      frame = requestAnimationFrame(paint);
    };
    const paintIfResized = (): void => {
      const box = `${el.clientWidth}x${el.clientHeight}`;
      if (box === lastBox) return;
      paintSoon();
    };
    paint();
    let observer: ResizeObserver | undefined;
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(paintIfResized);
      observer.observe(el);
    }
    // A webfont that arrives after the first layout reflows the word; the mask
    // was rasterized in the fallback's metrics until this runs again. It is
    // forced even when the box is unchanged: the metrics are the thing that
    // changed, not the element's size.
    void document.fonts?.ready.then(paintSoon);
    return () => {
      if (frame !== undefined) cancelAnimationFrame(frame);
      observer?.disconnect();
    };
  }, [className]);

  return (
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
      <span aria-hidden="true" ref={maskRef} className="codify-wordmark-sweep-clip">
        <span className="codify-wordmark-sweep" />
      </span>
    </span>
  );
};
