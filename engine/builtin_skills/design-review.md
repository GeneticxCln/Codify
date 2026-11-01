---
name: design-review
description: Review a UI for the generic AI look and the states it owes - direction, banned defaults, evidence per score, then the fixes. Use before shipping a screen or when a page feels templated.
moves: design, read_file, read_page, write, verify
---

# Design review

A screen can be correct and still look like every other generated page. This
skill gives the work a direction it could only have here, and checks the states
a person will actually meet.

## What you can see, and what you cannot

- `read_file` the screen, its tokens and the shared components it uses.
- `read_page` the rendered text and controls when a window and a page are
  attached. It is not a picture: it cannot show contrast, spacing, a gradient or
  a font.
- There is no screenshot tool. A claim about what the screen *looks* like needs
  the person's eyes or a rendered image you were actually given. If neither is
  available, mark that area **not checked visually** and score only what the
  source shows. Do not turn a source reading into a visual verdict.
- `read_editor` can show the person's open buffer, unsaved text included, when
  there is an editor attached.

## Direction before components

Write five lines or fewer before judging or building: purpose, audience, tone,
the one memorable thing, and the mode (persuade, operate or read). Take it from
the subject's own world, not from the usual look of its category.

Then the tokens: a display face and a body face that are clearly different; a
scale of five to seven steps; body text at 16px or more and lines under 80
characters; four to six named colours with one dominant field and one accent for
actions; a 4px or 8px spacing base; one or two radii; one shadow style or none.

Ask of each choice: "Would I make this same choice for any similar prompt?" If
yes, it is the generic look, and it needs a reason or a change.

## Banned defaults

Use one only when the brief asks for it:

- purple-blue gradient washes, or a decorative gradient doing no work;
- a default system font as the display face;
- emoji standing in for icons, or more than one icon set;
- a centred hero followed by three identical cards;
- glass or glow on every surface;
- low-contrast grey text on a grey background;
- stock copy such as "Unlock your potential" or "Seamless";
- an all-caps eyebrow above every heading;
- the same radius and soft shadow on every card.

## Required states

Every state the screen can be in must exist, not only the happy one:

- hover and focus-visible;
- disabled;
- empty;
- loading;
- error, with a sentence a person can act on;
- long content, including a long name and a long list.

The accessibility floor is part of the review: contrast at 4.5:1 or better, a
visible focus ring, a label on every field, touch targets of at least 44px, alt
text on images, and reduced motion respected.

## Audit with evidence

Score each area from 1 to 5 and give one line of evidence for the score: a line
of code, a rendered value, or the person's observation.

- **Hierarchy** — the main message and action are clear within three seconds,
  with one focal point.
- **Typography** — the pairing is deliberate, the scale is consistent, and line
  length and leading are comfortable.
- **Colour** — one dominant field and one accent, values come from tokens, and
  no decorative gradient is present.
- **Spacing** — the scale is followed, the rhythm is consistent, and the narrow
  layout aligns.
- **States** — the list above exists.
- **Accessibility** — the floor above is met.
- **Copy** — it is specific to this product, with no stock phrase and no
  placeholder data.

If you did not see an area, write **not checked** and the reason. A score
without evidence is a guess with a number on it.

## Fix the top, not everything

Rank the fixes by what a person meets first: broken accessibility and an unclear
main action before decoration. For each, name the file, the change, and the score
it should raise. Apply the top three to five, and fix the cause in the tokens or
the shared component rather than in each instance. Keep the product's identity,
content and behaviour.

A fix is a `write`, and a turn has no approved plan. Before the plan is approved,
report the ranked fixes with their files and expected scores and let `plan` carry
them; do not describe them as applied. Once a plan is approved, `write` and
`verify` are the moves that make and check the change.

Do not change factual copy, prices, claims or feature behaviour without asking.
If the screen needs a rebuild rather than a repair, recommend the `design` move
and a plan; do not rebuild it inside this review.

## Stop when

- Two passes are done, or every area scores 3 or higher and no banned default
  remains.
- The report shows the before and after scores, the evidence, the fixes applied,
  and the fixes not applied with a reason. Before and after are shown only where
  a rendered view existed; otherwise say what was not seen.
