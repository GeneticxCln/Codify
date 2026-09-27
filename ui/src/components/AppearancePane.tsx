import React, { useEffect, useRef, useState } from "react";
import { Check, Palette } from "lucide-react";
import { Panel } from "./ui/Panel";
import { MatrixRain } from "./ui/MatrixRain";
import { effectFor } from "./ui/WeatherBackdrop";
import {
  CODIFY_DARK,
  THEMES,
  applyTheme,
  readStoredThemeId,
  storeThemeId,
  type AppearanceTheme,
} from "../appearance";

/**
 * The Appearance tab: which theme the app wears.
 *
 * A theme choice is a runtime swap of CSS custom properties on the document
 * root (`appearance.ts`), not a class toggle — so the preview you see here is
 * not a mock-up of what choosing would do, it is the choice itself, already
 * applied and live in the surrounding modal.
 *
 * ## Why this is a list and a detail, and not a grid of eight cards
 *
 * The grid was the right shape for three themes and the wrong shape for eight.
 * Two things failed together, and only one of them was about navigation:
 *
 * 1. **Eight cards is a scroll.** A card is ~110px tall, so the eighth theme
 *    was two scrolls down a modal, and comparing two meant holding one in your
 *    head while scrolling to the other. A compact row is ~68px, which is eight
 *    of them inside the `max-h` below without the list scrolling at all — the
 *    cap is sized to the row count, not picked, and a tenth theme is the thing
 *    that makes it scroll again. The *description*, the wordiest and
 *    least-looked-at part, moved to the detail panel, where there is room for
 *    exactly one of them.
 * 2. **Eight cards meant eight live canvases.** Every tile drew its theme's
 *    real effect, so opening this tab started eight animation loops inside a
 *    window the user opened to read a setting. The old file's own comment
 *    conceded this — "eight ticking canvases would also be a lot to ask of a
 *    modal" — and conceded it while shipping it. The list rows now draw a
 *    static chip of the theme's own surfaces, and exactly **one** canvas runs:
 *    the one in the detail panel. The animation is still the real effect at a
 *    larger size than it used to get, and there is one of it.
 *
 * What the rows deliberately keep is the swatch line — every theme's hexes, on
 * screen at once. It is one small mono line, it is the only way to tell two
 * dark themes apart without opening both, and dropping it would have been the
 * easy way to make the rows shorter.
 *
 * Hover previews; clicking commits. Only the click writes to storage, so the
 * docstring's claim above still holds: the theme you are *wearing* is the
 * theme you *chose*, and a preview never claims to have been chosen.
 */
export const AppearancePane: React.FC = () => {
  const [selected, setSelected] = useState<string>(() => readStoredThemeId());
  const [hovered, setHovered] = useState<string | null>(null);
  const rows = useRef<Array<HTMLButtonElement | null>>([]);

  // Applying on select (not in an effect) keeps the click → repaint path one
  // statement long and makes the "already applied" claim above literally true.
  const select = (theme: AppearanceTheme): void => {
    setSelected(theme.id);
    setHovered(null);
    applyTheme(theme.id);
    storeThemeId(theme.id);
  };

  // Another surface could write the key while this pane is open (a second
  // window, a future command-palette entry). Syncing on mount of the pane keeps
  // the list honest without owning a listener per row.
  useEffect(() => {
    setSelected(readStoredThemeId());
  }, []);

  const active = THEMES.find((t) => t.id === selected) ?? THEMES[0];
  const shown = THEMES.find((t) => t.id === hovered) ?? active;

  /**
   * Arrow keys, because `role="radiogroup"` promises them and a grid of plain
   * buttons does not deliver them on its own — the ARIA pattern is a promise
   * the DOM does not keep by itself. Moving selects, as the pattern says: this
   * is a single-choice control, so arrowing to an option is choosing it, and
   * the commit lives in `select` so there is still exactly one place a theme
   * is ever written.
   */
  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>): void => {
    const forward = event.key === "ArrowDown" || event.key === "ArrowRight";
    const back = event.key === "ArrowUp" || event.key === "ArrowLeft";
    if (!forward && !back) return;
    event.preventDefault();
    const from = THEMES.findIndex((t) => t.id === (hovered ?? selected));
    const next = (from + (forward ? 1 : -1) + THEMES.length) % THEMES.length;
    const theme = THEMES[next];
    if (!theme) return;
    select(theme);
    rows.current[next]?.focus();
  };

  return (
    <div className="space-y-4">
      <Panel title={<><Palette className="w-3.5 h-3.5" /> Theme</>}>
        <div className="grid grid-cols-1 lg:grid-cols-[17rem_minmax(0,1fr)] gap-4">
          <div
            role="radiogroup"
            aria-label="Theme"
            onKeyDown={onKeyDown}
            className="space-y-1.5 max-h-[35rem] overflow-y-auto pr-1"
          >
            {THEMES.map((theme, i) => (
              <ThemeRow
                key={theme.id}
                theme={theme}
                isActive={theme.id === selected}
                isShown={theme.id === shown.id}
                onSelect={() => select(theme)}
                onHover={setHovered}
                onRegister={(el) => {
                  rows.current[i] = el;
                }}
              />
            ))}
          </div>
          <ThemeDetail theme={shown} isActive={shown.id === active.id} onApply={select} />
        </div>
      </Panel>
    </div>
  );
};

/**
 * One theme, as a row: a chip of its own surfaces, its name, its hexes.
 *
 * A real `<button role="radio">`, not a div — §5's first rule. The radio
 * semantics are honest: exactly one theme holds at a time, and arrow keys
 * between options are what a radiogroup promises (the handler on the group
 * above is what keeps that half).
 *
 * The chip is a miniature of the app rather than a swatch of one colour: the
 * theme's background, one raised block for a panel, one primary line for text.
 * It costs no canvas, so eight of them are free, and it answers "what will this
 * look like" faster than a hex does.
 */
const ThemeRow: React.FC<{
  theme: AppearanceTheme;
  isActive: boolean;
  isShown: boolean;
  onSelect: () => void;
  onHover: (id: string | null) => void;
  onRegister: (el: HTMLButtonElement | null) => void;
}> = ({ theme, isActive, isShown, onSelect, onHover, onRegister }) => (
  <button
    type="button"
    role="radio"
    aria-checked={isActive}
    ref={onRegister}
    onClick={onSelect}
    onMouseEnter={() => onHover(theme.id)}
    onMouseLeave={() => onHover(null)}
    onFocus={() => onHover(theme.id)}
    onBlur={() => onHover(null)}
    className={
      "w-full text-left rounded-lg border px-2.5 py-2 transition-colors cursor-pointer flex items-center gap-2.5 " +
      (isActive
        ? "border-codify-border-strong bg-codify-raised"
        : isShown
          ? "border-codify-border bg-codify-raised/60"
          : "border-codify-border hover:bg-codify-raised/60")
    }
  >
    <ThemeChip theme={theme} />
    <span className="min-w-0 flex-1">
      <span className="flex items-center gap-1.5">
        <span className="text-sm font-medium text-codify-primary truncate">{theme.label}</span>
        {isActive && <Check className="w-3 h-3 flex-shrink-0 text-codify-secondary" aria-hidden="true" />}
      </span>
      <span className="block font-mono text-2xs text-codify-muted truncate">
        {swatchLine(theme)}
      </span>
    </span>
  </button>
);

/**
 * A static, canvas-free miniature of the app in this theme's own colours.
 *
 * `aria-hidden`, because the row's real name is its label and a screen reader
 * reading "div" eight times is noise. It is the row's preview *and* the reason
 * eight rows are affordable: the old card spent a live canvas on this and got
 * the same information out of three painted boxes.
 */
const ThemeChip: React.FC<{ theme: AppearanceTheme }> = ({ theme }) => {
  const varOf = (name: string): string => theme.tokens[name as keyof typeof theme.tokens] ?? "";
  return (
    <div
      className="w-11 h-8 rounded-md border flex-shrink-0 overflow-hidden flex items-center gap-0.5 p-1"
      style={{ backgroundColor: varOf("--codify-bg"), borderColor: varOf("--codify-border") }}
      aria-hidden="true"
    >
      <span className="w-1.5 h-full rounded-sm" style={{ backgroundColor: varOf("--codify-raised") }} />
      <span className="flex-1 space-y-0.5 pt-0.5">
        <span className="block h-0.5 w-3/4 rounded-full" style={{ backgroundColor: varOf("--codify-primary") }} />
        <span className="block h-0.5 w-1/2 rounded-full" style={{ backgroundColor: varOf("--codify-border-strong") }} />
      </span>
    </div>
  );
};

/**
 * The one theme under inspection, at a size worth looking at.
 *
 * This is where the live effect lives, and it is the only canvas on the screen.
 * It runs at a 320px cap — larger than the old tile's 120, so the perspective,
 * the parallax and the banding read properly — and there is one of it instead
 * of eight 120s, which is the whole point of the layout.
 *
 * The `animated` asymmetry is inherited deliberately: the rain's preview
 * overrides the motion preference to sell the effect, because the effect *is*
 * falling glyphs and a still of them is a dark rectangle; a new tile should not,
 * because a user who asked for no motion should not be shown a starfield
 * scrolling inside the settings window.
 */
const ThemeDetail: React.FC<{
  theme: AppearanceTheme;
  isActive: boolean;
  onApply: (theme: AppearanceTheme) => void;
}> = ({ theme, isActive, onApply }) => {
  const varOf = (name: string): string => theme.tokens[name as keyof typeof theme.tokens] ?? "";
  const rains = Boolean(theme.tokens["--cmatrix-rain"]);
  const Effect = effectFor(theme.id);
  // "No effect" is a property of the *theme*, not of one theme's id. Keyed on
  // `CODIFY_DARK` it used to read as special-casing the default, and the first
  // deliberately motionless theme (`still`) rendered an empty box here — a
  // preview that could not preview. The flag is the absence of both, which is
  // also the thing `MOTIONLESS_THEME_IDS` in `appearance.ts` declares.
  const still = !rains && !Effect;

  return (
    <div className="lg:sticky lg:top-0">
      <div
        className="rounded-xl border overflow-hidden relative aspect-[16/9]"
        style={{ backgroundColor: varOf("--codify-bg"), borderColor: varOf("--codify-border") }}
        aria-hidden="true"
      >
        {rains && <MatrixRain className="absolute inset-0 w-full h-full" maxDimension={320} animated />}
        {Effect && <Effect className="absolute inset-0 w-full h-full" maxDimension={320} />}
        {still && (
          /* A theme with no weather still has to *show* something, and the
             honest thing to show is its surfaces: the ramp is the whole theme.
             It fills the frame rather than sitting as three chips in the middle
             of it, because a near-empty box reads as a failed preview and this
             is the opposite — the absence is the feature, and it should look
             deliberate. For the default this also carries the status green, the
             only saturated thing in that palette and a deliberate part of it;
             `still` has no green, and the bare ramp is the point. */
          <div className="absolute inset-0 flex items-stretch gap-1.5 p-6">
            {(
              ["--codify-bg", "--codify-surface", "--codify-raised", "--codify-border-strong"] as const
            ).map((token) => (
              <span
                key={token}
                className="flex-1 rounded-md"
                style={{ backgroundColor: varOf(token) }}
              />
            ))}
            {theme.id === CODIFY_DARK.id && (
              <span className="w-2 rounded-md bg-green-500 self-center" />
            )}
          </div>
        )}
      </div>
      <div className="mt-3 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-md font-semibold text-codify-primary">{theme.label}</h3>
          <p className="text-xs text-codify-secondary mt-0.5 leading-relaxed">{theme.description}</p>
          <p className="font-mono text-2xs text-codify-muted mt-1.5 break-words">{swatchLine(theme)}</p>
        </div>
        {isActive ? (
          <span className="flex-shrink-0 flex items-center gap-1 text-2xs px-2 py-1 rounded-full bg-codify-raised border border-codify-border text-codify-secondary">
            <Check className="w-3 h-3" aria-hidden="true" /> active
          </span>
        ) : (
          /* A preview of a theme you are not wearing gets a commit button, and
             the wording says "preview": nothing has been applied until this is
             pressed, and the label should not pretend otherwise. */
          <button
            type="button"
            onClick={() => onApply(theme)}
            className="flex-shrink-0 text-2xs font-semibold px-2.5 py-1.5 rounded-lg bg-codify-raised border border-codify-border-strong text-codify-primary hover:bg-codify-surface transition-colors"
          >
            Preview this
          </button>
        )}
      </div>
    </div>
  );
};

/**
 * Which hexes a row prints is the theme's own business, in `appearance.ts`
 * beside the values — not a chain of conditions here, which is how the fourth
 * theme ends up wearing the third theme's footer. A label is optional and only
 * there where the bare hex is ambiguous, which is why OLED's line reads
 * `rain #003300` and the next one reads `#1e293b`.
 */
const swatchLine = (theme: AppearanceTheme): string =>
  (theme.swatch ?? [])
    .map((s) => (s.label ? `${s.label} ${theme.tokens[s.v] ?? ""}` : (theme.tokens[s.v] ?? "")))
    .filter((line) => line.trim().length > 0)
    .join(" · ");
