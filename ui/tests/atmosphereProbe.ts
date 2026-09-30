/**
 * The shared atmosphere clock with a painter a test can read.
 *
 * `useAtmosphereCanvas` takes a painter factory, so the smallest thing that mounts the real hook is
 * a component that calls it with a painter that records what it was asked to do, frame by frame.
 * The painter also draws one `fillRect` per frame into the rig's context, so "a frame was painted"
 * is something the rig can count for this loop exactly as it counts the rain's veil.
 */
import React from "react";

import { useAtmosphereCanvas, type AtmospherePainter } from "../src/hooks/useAtmosphereCanvas.ts";

/** What a painter was asked to do, frame by frame. */
export interface Recorder {
  /** Painters built: the loop builds one per effect run, so a rebuild is a re-armed loop. */
  created: number;
  /** The `dt` of every `step`. */
  steps: number[];
  /** `draw` calls, the static frame included. */
  draws: number;
  /** `tick.frame` at each draw. */
  frames: number[];
  /** `tick.seconds` at each draw. */
  seconds: number[];
  create: () => AtmospherePainter;
}

export function recorder(): Recorder {
  const rec: Recorder = {
    created: 0,
    steps: [],
    draws: 0,
    frames: [],
    seconds: [],
    create: () => {
      rec.created += 1;
      return {
        step: (dt) => {
          rec.steps.push(dt);
        },
        draw: (ctx, _size, tick) => {
          ctx.fillRect(0, 0, 1, 1);
          rec.draws += 1;
          rec.frames.push(tick.frame);
          rec.seconds.push(tick.seconds);
        },
      };
    },
  };
  return rec;
}

export interface ProbeProps {
  rec: Recorder;
  active?: boolean;
  animated?: boolean;
  fps?: number;
  maxDimension?: number;
}

/** The shared clock with a recording painter: the smallest thing that mounts the hook. */
export function Probe(props: ProbeProps): React.ReactElement {
  const ref = useAtmosphereCanvas({
    maxDimension: props.maxDimension ?? 1024,
    fps: props.fps ?? 30,
    animated: props.animated,
    active: props.active,
    create: props.rec.create,
  });
  return React.createElement("canvas", { ref });
}

export const probe = (props: ProbeProps): React.ReactElement => React.createElement(Probe, props);
