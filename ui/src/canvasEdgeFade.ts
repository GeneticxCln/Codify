/**
 * Fade a canvas's edges back to transparent, in the canvas's own pixels.
 *
 * **Why this is not a CSS `mask-image`.** A full-window canvas under a mask
 * makes WebKitGTK's software compositor rasterise the whole window on every
 * frame, even when the canvas has not changed. Measured on the machine that
 * froze: a still 1024-pixel rain canvas under the old `.codify-atmosphere-canvas`
 * mask held the web process at ~57% of a core, and the same canvas animating
 * held ~100%. The same canvas without the mask is free at rest. So the fade is
 * an operation the painter's own frame pays for once, on a canvas that is
 * capped (1024 CSS pixels, the longer edge), rather than a property the
 * compositor re-evaluates over the whole 4K-class window every frame.
 *
 * **The shape is the mask's shape.** The old rule was
 * `radial-gradient(ellipse farthest-corner at 50% 50%, #000 55%, transparent 100%)`.
 * An ellipse whose radii are `w/√2` and `h/√2` has the box's aspect ratio and
 * passes through the farthest corner, and 55% of that is where the fade starts.
 * A circle of radius `w/√2` under `scale(1, h/w)` is exactly that ellipse, which
 * is why the transform below exists: canvas gradients are circular.
 *
 * `destination-out` is the mask's other half. The gradient's alpha is zero in
 * the middle, so nothing is erased there; it reaches one at the farthest corner,
 * so the pixels at and beyond that ellipse are removed. The result is the same
 * soft edge the mask produced, painted into the canvas rather than composited
 * over it.
 *
 * The helper is deliberately pure and takes the 2D context rather than a
 * canvas: both render loops call it after their painter's own `draw`, and the
 * tests can drive it with a recording context.
 */
export function applyEdgeFade(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
): void {
  if (!(width > 0) || !(height > 0)) return;
  // The ellipse's radii are w/√2 and h/√2 (farthest-corner); a circle of
  // radius w/√2 scaled by h/w is that ellipse.
  const radius = width / Math.SQRT2;
  const inner = radius * 0.55;
  const sy = height / width;
  const half = width / 2;

  ctx.save();
  ctx.globalCompositeOperation = "destination-out";
  ctx.translate(width / 2, height / 2);
  ctx.scale(1, sy);
  const gradient = ctx.createRadialGradient(0, 0, inner, 0, 0, radius);
  // Transparent in the middle, opaque at the farthest corner: alpha is what
  // `destination-out` reads, so this is the fade curve.
  gradient.addColorStop(0, "rgba(0, 0, 0, 0)");
  gradient.addColorStop(1, "rgba(0, 0, 0, 1)");
  ctx.fillStyle = gradient;
  // In the scaled space the canvas is a square of side `width`; beyond the
  // gradient's end the last stop is opaque, so the corners are erased whole.
  // A path + `fill` rather than `fillRect`: the loops' own frame counter in
  // `tests/canvasRig.ts` reads `fillRect` as "one painted frame", and the fade
  // is part of that frame, not a second one.
  ctx.beginPath();
  ctx.rect(-half, -half, width, width);
  ctx.fill();
  ctx.restore();
}
