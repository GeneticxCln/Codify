/** @type {import('tailwindcss').Config} */

// The machine-readable half of the brand contract. `DESIGN.md` at the repository
// root is the human half, and it is binding on any goal run against this repo — the
// engine auto-discovers a root DESIGN.md and makes it the design contract, so this
// config and that document are two views of one thing, not two decisions.
//
// `ui/tests/designTokens.test.ts` fails if a value here stops matching the value
// quoted in DESIGN.md, in either direction. A token that is not documented is as
// much a failure as a documented token that is wrong: the first is a decision
// nobody made, and the second is a document that has already lied once.
//
// The palette this replaces was defined in this same file and used **zero** times,
// while 267 hardcoded `[#hex]` class names carried the real brand. That is why the
// values below are deliberately the same hues the UI already rendered — this is a
// tokenisation, not a repaint. Nothing should look different until something uses a
// name instead of a hex.
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      // ── Colour ─────────────────────────────────────────────────────────────
      // A neutral blue-grey base, so the status hues below are the only saturated
      // things on screen: when something is coloured, it means something. Three
      // surfaces and no more — a fourth would leave a component unable to say what
      // it sits on. See DESIGN.md §2.
      colors: {
        codify: {
          // Surfaces
          bg: "#0d1117",             // app background, behind everything
          surface: "#161b22",        // panels, the command bar card, drawers
          raised: "#21262d",         // controls at rest, hover targets, inset rows
          border: "#30363d",         // dividers, resting control borders
          // The scrollbar thumb, and the border a control takes on hover and
          // focus. Lifted to clear WCAG 1.4.11's 3:1 against `raised`: a thumb
          // is the only thing that says where you are in a long list, and it
          // was 1.84:1 there.
          "border-strong": "#66707c", // scrollbar thumbs, borders on hover/focus

          // Text — three weights. A fourth thing needing emphasis is bigger or
          // bolder, not a new grey.
          primary: "#e6edf3",        // headings, the code you asked to read
          secondary: "#c9d1d9",      // body copy, chat prose
          muted: "#979fa8",          // metadata, placeholders, disabled

          // The one interactive hue. Also the "in flight" status, because
          // "the primary action" and "this is happening" are the same sentence.
          // `#418cf8`, not §2's `#2f81f7`: the send button and a selected row
          // both rest this hue on `raised`, and the old value was 4.06:1 there,
          // under AA for the 11px a thread title is set at.
          accent: "#418cf8",

          // Status — five tones, each with exactly one meaning, and never used
          // decoratively. If a control is not reporting a state it is grey, and
          // that is not a lack of colour, it is the default.
          info: "#418cf8",
          success: "#3fb950",
          warning: "#d29922",
          danger: "#f85149",
          neutral: "#8b949e",

          // Reserved mode accents, one hue per deliverable mode. These existed to
          // fix a real collision: Record and Knowledge Deliverable were carrying
          // byte-identical classes, so two unrelated features looked identical.
          // A new feature picks a tone from the tables above — a hue is added here
          // only for a genuinely new *category*, never to tell two merely-armed
          // controls apart. See DESIGN.md §2.
          design: "#db61a2",
          knowledge: "#39c5cf",
        },
      },

      // ── Type ───────────────────────────────────────────────────────────────
      // Roughly a step tighter than Tailwind's defaults, and deliberately so. The
      // audit found 203 declarations at `text-[10px]` / `text-[11px]` against 134 at
      // scale steps: the off-scale values were the majority, so the defaults were
      // the deviation. This ramp is defined at the sizes the app actually used, and
      // the odd ones out are the ones that change.
      //
      // CONSEQUENCE, and it surprises people: `text-sm` is 12px here, not Tailwind's
      // 14px, and `text-xs` is 11px, not 12px. Anything written expecting the stock
      // values comes out two steps too large.
      //
      // In rem, and that is what makes the UI scale work. The sizes are the ones
      // above at a 16px root (`0.625rem` is 10px), but they are written against the
      // root so Settings → Appearance can scale the whole window by changing one
      // number on `<html>` (`ui/src/uiScale.ts`). In px, a scale would have grown
      // the icons and padding and left every word the same size. Line heights are
      // rem for the same reason: text that grew inside a fixed-px line box overlaps.
      fontSize: {
        "2xs": ["0.625rem", { lineHeight: "0.875rem" }],  // 10px: badges, timestamps, counts
        xs: ["0.6875rem", { lineHeight: "1rem" }],        // 11px: dense rows, pickers, toolbars
        sm: ["0.75rem", { lineHeight: "1.125rem" }],      // 12px: chat prose, labels, default body
        base: ["0.8125rem", { lineHeight: "1.25rem" }],   // 13px: reading copy
        md: ["0.875rem", { lineHeight: "1.3125rem" }],    // 14px: card titles
        lg: ["1rem", { lineHeight: "1.5rem" }],           // 16px: panel headings
        xl: ["1.25rem", { lineHeight: "1.75rem" }],       // 20px: the one true hero moment
      },

      // ── Radius ─────────────────────────────────────────────────────────────
      // `lg` is the default for anything a pointer lands on, which is why it is the
      // value that was already dominant. Nesting a `lg` inside a `lg` looks wrong,
      // so a surface sitting inside a surface of the same kind steps down one level.
      // rem, like the type ramp, so a scaled window keeps its proportions.
      borderRadius: {
        sm: "0.25rem",  // 4px: icon-to-label inside a control
        md: "0.375rem", // 6px: inset rows, nested chips
        lg: "0.5rem",   // 8px: the default control radius
        xl: "0.75rem",  // 12px: cards, the command bar, drawers
      },
    },
  },
  plugins: [],
};
