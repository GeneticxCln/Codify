import React from "react";
import {
  useAtmosphereCanvas,
  type AtmosphereCanvasProps,
  type AtmospherePainter,
} from "../../hooks/useAtmosphereCanvas";
import { channelsOf, withAlpha } from "../../canvasColour";

/**
 * The Anon Fluid theme: data running sideways through the gutters.
 *
 * **Sideways, and that is the whole design.** The OLED theme falls glyphs down
 * the window. A third column of falling characters in a different colour is
 * that theme again, so this one runs *along* the edges instead — which is also
 * the truer picture of a terminal reading a stream rather than printing a file,
 * and it leaves the vertical axis free for the transcript.
 *
 * **A stream, not a grid.** Each row is a train of short dashes advancing at
 * its own rate, and two things stop the result reading as graph paper: the
 * rows are phase-offset from each other by a wave, so the column has a surface,
 * and every dash's brightness is hashed from its row and its index rather than
 * being uniform, so the stream has texture. A field of equal dashes in equal
 * rows is a grid; a field where nothing lines up is data.
 *
 * **The shear is the state report.** At rest the dashes stay short. While a
 * turn is in flight the stream *shears*: a band of rows is displaced sideways
 * and its dashes stretch into blocks, so a working window is legibly different
 * from an idle one without anything having to get faster. This is §7's test —
 * motion that carries information the user did not already have — rather than a
 * rate that simply rises, which the hook already does for every effect here.
 *
 * The gutters are the same 20% the other margin effects use, for the same
 * reason: the middle of this window is the conversation.
 */
export type AnonFluidProps = AtmosphereCanvasProps;

interface Row {
  y: number;
  /** Where this row's first dash is along its axis, in canvas pixels. */
  head: number;
  /** px/s. Rows differ so the columns do not move as one sheet. */
  speed: number;
  /** Phase, for the wave that gives the column a surface. */
  phase: number;
  /** Which edge this row runs from. */
  left: boolean;
  /** Hash seed, so brightness is stable per row rather than flickering. */
  seed: number;
}

const ROWS = 30;
const GUTTER = 0.2;
/** Distance between dashes along a row. */
const PITCH = 26;
/** The resting length of a dash, as a fraction of the pitch. */
const DASH = 0.42;
/** How far the shear band displaces its rows, in canvas pixels. */
const SHEAR = 26;

export function createAnonFluidPainter(
  read: (name: string) => string,
  active: boolean,
): AtmospherePainter {

  let rows: Row[] = [];
  const reach = (w: number): number => GUTTER * w + SHEAR;

  // Only the height matters here: a row's position along its axis is derived
  // from `head` at draw time, so seeding against the width would freeze a
  // number that the cap may still change on the first resize.
  const seed = (h: number): void => {
    rows = Array.from({ length: ROWS }, (_, i) => {
      const left = i % 2 === 0;
      return {
        y: (0.04 + (i / ROWS) * 0.92) * h,
        head: Math.random() * PITCH * 4,
        speed: 14 + Math.random() * 40,
        phase: Math.random() * Math.PI * 2,
        left,
        seed: (i * 2654435761) % 1000,
      };
    });
  };

  return {
    step(dt) {
      for (const row of rows) {
        // The hook's 1.8×, applied to the head, so a run in flight is a faster
        // stream as well as a sheared one.
        row.head += row.speed * dt * (active ? 1.8 : 1);
        row.phase += dt * 0.7;
      }
    },
    draw(ctx, size, tick) {
      // Resolved per frame rather than once at creation, so a theme or a
      // custom tint restyles this canvas on the next frame instead of
      // waiting for a remount. See the hook's note on live reads.
        const bit = channelsOf(read("--fluid-bit"));
        const cursor = channelsOf(read("--fluid-cursor"));
      if (rows.length === 0) seed(size.height);
      const { width: w, height: h } = size;
      ctx.clearRect(0, 0, w, h);
      ctx.lineCap = "butt";

      // The shear band: a band of rows, drifting up the window, that is
      // displaced sideways and drawn in blocks while the agent is working. At
      // rest `bandCentre` is below zero and every row takes the ordinary path,
      // so the two states share one code path rather than two.
      const bandCentre = active ? ((tick.seconds * 0.2) % 1.5) - 0.25 : -1;
      const bandHalf = 0.06;

      // How far a row's train marches, and where it starts. `travel` is the
      // gutter plus a dash's worth of slack, so a dash is fully off the edge
      // before it wraps and nothing pops at the boundary.
      const travel = reach(w) + PITCH;

      for (const row of rows) {
        // The wave. Every row is offset from its neighbours, which is what
        // makes the column read as a liquid with a surface rather than as
        // independent rows of dashes.
        const y = row.y + Math.sin(row.phase + row.y * 0.012) * 5;
        const inBand =
          bandCentre >= 0 && Math.abs(row.y / h - bandCentre) < bandHalf;
        const shift = inBand ? (row.left ? SHEAR : -SHEAR) : 0;
        // A row in the band is pulled slightly out of phase, so the shear
        // displaces the *stream* rather than sliding a whole block of dashes
        // as one rigid object.
        const phasePos =
          ((row.head + (inBand ? row.seed % PITCH : 0)) % travel + travel) % travel;

        for (let i = 0; i < 8; i++) {
          const along = (phasePos + i * PITCH) % travel;
          const dx = row.left ? along - PITCH : w - along;
          if (dx < -PITCH || dx > w + PITCH) continue;
          // Brightest at the window's edge, gone by the middle: a bright green
          // line across somebody's code is the failure every one of these
          // effects is shaped around.
          const inner = Math.min(Math.abs(dx), Math.abs(dx - w)) / (GUTTER * w);
          const fade = Math.max(0, 1 - inner) ** 1.4;
          if (fade < 0.05) continue;
          // Stable per (row, index) brightness: a stream that flickered at
          // random every frame would be noise, and this one has to look like it
          // is carrying something.
          const hash = ((row.seed + i * 97) % 11) / 11;
          const len = PITCH * (inBand ? 0.95 : DASH) * (0.6 + hash * 0.7);
          const x = dx + shift;
          if (x < -PITCH || x > w + PITCH) continue;
          ctx.strokeStyle = withAlpha(
            hash > 0.86 ? cursor : bit,
            (0.16 + hash * 0.5) * fade,
          );
          ctx.lineWidth = inBand ? 2.4 : 1.4;
          ctx.beginPath();
          ctx.moveTo(x, y);
          ctx.lineTo(row.left ? x + len : x - len, y);
          ctx.stroke();
        }
      }
    },
  };
}

export const AnonFluid: React.FC<AnonFluidProps> = ({
  className = "",
  maxDimension = 1024,
  fps = 30,
  active = false,
  animated,
}) => {
  const vars = {
    "--fluid-bit": "#00ff9c",
    "--fluid-cursor": "#a8ffd8",
  };

  const canvasRef = useAtmosphereCanvas({
    maxDimension,
    fps,
    animated,
    active,
    vars,
    create: (read) => createAnonFluidPainter(read, active),
  });

  return (
    <canvas
      ref={canvasRef}
      aria-hidden="true"
      className={`pointer-events-none ${className}`}
    />
  );
};
