---
name: ui-motion
description: Add or review motion in an interface - purpose and frequency first, short interruptible transitions on transform and opacity, reduced motion - and say plainly that nobody watched it.
moves: read_file, search_code, read_page, plan, write, verify, run_command
---

# UI motion

Motion that explains is worth its bytes. Motion that decorates is noise that
runs on every visit. This recipe is for adding it, or reviewing it, in an
interface this workspace owns.

## Before animating

- Read the project's own motion notes first: a `MOTION.md`, the repository's
  instructions, the design tokens. A project rule overrides everything below.
- Find the motion that already exists (CSS transitions, the animation library in
  the manifest) and match it. A new dependency needs a reason and a look at the
  size budget in `ui/budgets.json`.
- Give each motion one purpose in a phrase: what it shows (where this came from,
  what changed, that it worked). No purpose, no motion.
- **Frequency sets the amount.** A control used dozens of times a day barely
  moves or does not. A rare moment may earn a little more.

## Rules

- **Short.** Responses to a person's action stay under about 300 ms. Leaving is
  quicker than arriving, roughly half.
- **Curve by role.** Arriving and opening: strong ease-out. Moving between two
  states: ease-in-out. Leaving: ease-in. Only an endless loop is linear.
- **Nothing appears from nothing.** Start from a slightly smaller size with
  opacity, not from scale 0.
- **Interruptible.** State-driven motion uses transitions that retarget from the
  current value, not keyframes that restart. An interrupted motion carries on
  from where it is.
- **Things travel, they do not teleport.** If it is the same object in a new
  place, move it; do not unmount one copy and mount another.
- **Transform and opacity only.** Animating width, height, top or left moves the
  layout and janks. The layout stays put; motion happens inside it.
- **Opacity to appear or disappear**, not to dim something halfway.
- **Variety is seeded.** Repeated items get their phase and speed from their
  index, never from a random call on every render. Staggers are short and capped.
- **One main action in a view**, and at most two or three quiet idle loops, none
  on controls or navigation.
- **Press and hover.** A press is a small scale on the control. Hover effects sit
  behind a hover-capable media query; a touch screen has no hover.
- **Nothing runs unseen.** Stop when hidden or off screen, and clean up timers,
  frame callbacks and listeners when the component goes.
- **One clock.** Drive a scene from a single time value so a test can set it. No
  wall-clock reads inside rendering.

## Reduced motion is required

Honour `prefers-reduced-motion`. A loop stops on a frame that is a good drawing;
a one-shot shows its end state. Keep the non-motion cue (a colour or opacity
change) so feedback is not lost. Test the reduced branch.

## What you can check, and what you cannot

- **Can:** which properties are animated, durations and curves, the reduced
  branch, cleanup on unmount, types and lint, the size budget, and unit tests
  that set the clock (`verify`, `run_command`). `read_page` shows the DOM state,
  not the motion.
- **Cannot:** how it feels. There is no screenshot or recording tool. State
  **not watched**, and hand the person a short list: the first frame, the end
  frame, an interruption halfway, reduced motion on, and what to look for in
  each. Do not turn a source reading into a verdict on motion.

## Stop when

- The motion has a purpose, follows the rules above or states why not, the
  reduced branch exists and is tested, and the checks you could run pass.
- The report says what was not watched and who needs to watch it.
- A rule here would need a new dependency or a layout change the person did not
  ask for: stop and ask.
