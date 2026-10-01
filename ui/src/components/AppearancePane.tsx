import React, { useEffect, useRef, useState } from "react";
import { Check, ClipboardPaste, Copy, FileDown, FileUp, Palette, RotateCcw, Undo2 } from "lucide-react";
import { Panel } from "./ui/Panel";
import { UiScalePanel } from "./UiScalePanel";
import { MatrixRain } from "./ui/MatrixRain";
import { effectFor } from "./ui/WeatherBackdrop";
import {
  CODIFY_DARK,
  THEMES,
  readStoredThemeId,
  storeThemeId,
  themeById,
  type AppearanceTheme,
} from "../appearance";
import {
  applyTintedTheme,
  isTintable,
  loadTints,
  resolveTint,
  resolveTints,
  saveTints,
  tintableVars,
  TINT_ROLES,
  type TintMap,
  type TintStore,
} from "../tint";
import {
  canUndo,
  record,
  undo as undoHistory,
  type TintHistory,
} from "../tintHistory";
import {
  DECODE_MESSAGES,
  copySchemeText,
  decodeScheme,
  describeImportFailure,
  downloadScheme,
  encodeScheme,
  mergeScheme,
  schemeFilename,
  type ImportReport,
} from "../scheme";

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
 *
 * ## The custom-colour picker, and why it is below the commit
 *
 * `ThemeTints` offers the theme's accent and the weather variables its own
 * painter reads, and it is rendered only for the *selected* theme. Both halves
 * are decisions rather than omissions. Offering a hovered theme would write a
 * palette to disk on a stray mouse-cross, and the colour being dragged would
 * have no visible effect anyway, because the app is wearing a different theme;
 * so customising a theme means choosing it first, which the "Preview this"
 * button already does.
 *
 * What the picker cannot offer is the bigger half of this file's work: the
 * clamp, the surfaces, and the five status tones. `ui/src/tint.ts` owns those,
 * and `ui/tests/tint.test.ts` holds them.
 */
export const AppearancePane: React.FC = () => {
  const [selected, setSelected] = useState<string>(() => readStoredThemeId());
  const [hovered, setHovered] = useState<string | null>(null);
  const [tints, setTints] = useState<TintStore>(() => loadTints());
  const [history, setHistory] = useState<TintHistory>(() => []);
  const rows = useRef<Array<HTMLButtonElement | null>>([]);

  // Applying on select (not in an effect) keeps the click → repaint path one
  // statement long and makes the "already applied" claim above literally true.
  // `applyTintedTheme` rather than `applyTheme`: a theme is stored *with* its
  // custom colours, so switching to a theme you have tinted has to bring them
  // along or the switch is a silent reset.
  const select = (theme: AppearanceTheme): void => {
    setSelected(theme.id);
    setHovered(null);
    applyTintedTheme(theme.id);
    storeThemeId(theme.id);
  };

  /**
   * Write a tint, then repaint. Persist, state and apply are one call so the
   * three cannot disagree: a colour that is on screen but not in storage is
   * lost on restart, and one that is in storage but not on screen looks broken.
   *
   * The new store is built *outside* the state updater on purpose. A
   * `useState` updater must be pure — React runs it twice in StrictMode — and
   * writing to `localStorage` or to the document root inside one is a
   * side effect that happens twice, to two different results.
   */
  const commitTints = (themeId: string, next: TintStore): void => {
    setTints(next);
    saveTints(undefined, next);
    applyTintedTheme(themeId);
  };

  /**
   * The one way a tint changes, and therefore the one place undo is recorded.
   *
   * Every path — a pick, a per-row reset, a whole-theme reset, an imported
   * scheme — comes through here, so a fourth one added later is undoable
   * without anyone remembering to make it so. `burst` is what tells the history
   * a drag is still in progress: consecutive picks of the *same* variable
   * collapse into one entry, so undoing a gesture does not mean pressing the
   * button once per pixel of slider.
   */
  const commit = (
    themeId: string,
    next: TintStore,
    label: string,
    burst?: string,
  ): void => {
    setHistory((history) => record(history, { store: tints, label, burst, at: Date.now() }));
    commitTints(themeId, next);
  };

  const pickTint = (themeId: string, name: string, hex: string): void => {
    const theme = themeById(themeId);
    commit(
      themeId,
      { ...tints, [themeId]: { ...(tints[themeId] ?? {}), [name]: hex } },
      `${TINT_ROLES[name] ?? name} on ${theme.label}`,
      `${themeId}:${name}`,
    );
  };

  const clearTint = (themeId: string, name: string): void => {
    const map = { ...(tints[themeId] ?? {}) };
    delete map[name];
    const next: Record<string, TintMap> = { ...tints };
    // A theme with no tints left is *absent* from the store, not present and
    // empty, so `saveTints` writes `codify.tints` down rather than leaving a
    // key that says nothing.
    if (Object.keys(map).length === 0) delete next[themeId];
    else next[themeId] = map;
    commit(themeId, next, `Reset ${TINT_ROLES[name] ?? name} on ${themeById(themeId).label}`);
  };

  const resetTints = (themeId: string): void => {
    const next: Record<string, TintMap> = { ...tints };
    delete next[themeId];
    commit(themeId, next, `Reset all colours on ${themeById(themeId).label}`);
  };

  /**
   * Step back one change, and put it back on screen.
   *
   * Repainting matters as much as restoring: the store is the answer, and a
   * restored store nothing has read from is a store the window is not wearing.
   */
  const onUndo = (): void => {
    const step = undoHistory(history);
    if (!step) return;
    setHistory(step.history);
    commitTints(selected, step.store);
  };

  /**
   * An imported scheme is a change like any other and is undoable like any
   * other. It is the largest one — it can touch every theme at once — which is
   * the argument for recording it rather than for exempting it: a user who
   * imports the wrong file and cannot get their colours back has to start
   * over by hand.
   */
  const commitImported = (next: TintStore, label: string): void => {
    commit(selected, next, label);
  };

  // Another surface could write either key while this pane is open — a second
  // window, a future command-palette entry, a hand-edit in devtools — so the
  // pane re-reads *and re-applies*, not just re-reads.
  //
  // Re-reading alone was enough when this pane was the only writer, and it is
  // the reason the old line here was a lie: it set `selected`, so the row drew
  // a check beside a theme the window was not wearing, and pressing Reset would
  // have edited a store the document root had never read. Two copies of the
  // answer, one of which nothing repaints from, is the shape this app's
  // `useTheme` docstring warns about by name.
  //
  // The `storage` event is the other half and costs nothing: the browser fires
  // it in every *other* window and not in this one, which is exactly the set of
  // writes this pane does not make itself.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const sync = (): void => {
      setTints(loadTints());
      setSelected(readStoredThemeId());
      // The undo stack is dropped, not kept. Every entry in it holds a store
      // from *before* something, and the something has just been replaced from
      // outside this window — so undoing now would silently discard a change
      // the user did not make and cannot see. A "way back" that no longer
      // leads anywhere is worse than none.
      setHistory([]);
      applyTintedTheme(readStoredThemeId());
    };
    sync();
    window.addEventListener("storage", sync);
    return () => window.removeEventListener("storage", sync);
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
      <UiScalePanel />
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
          <ThemeDetail
            theme={shown}
            isActive={shown.id === active.id}
            onApply={select}
            tints={tints[shown.id] ?? {}}
            onPick={(name, hex) => pickTint(shown.id, name, hex)}
            onClear={(name) => clearTint(shown.id, name)}
            onReset={() => resetTints(shown.id)}
            onUndo={onUndo}
            undoable={canUndo(history)}
            undoLabel={history[history.length - 1]?.label ?? ""}
          />
        </div>
        {/* A scheme can replace tints on any theme, so the repaint target is
            the theme being *worn* — the others will be re-applied by their own
            `select` when they are chosen, which reads the merged store. */}
        <SchemeBar store={tints} onImport={commitImported} />
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
  tints: TintMap;
  onPick: (name: string, hex: string) => void;
  onClear: (name: string) => void;
  onReset: () => void;
  onUndo: () => void;
  undoable: boolean;
  undoLabel: string;
}> = ({
  theme,
  isActive,
  onApply,
  tints,
  onPick,
  onClear,
  onReset,
  onUndo,
  undoable,
  undoLabel,
}) => {
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
              // Tailwind's green, and deliberately not `--codify-success`. This
              // chip says "this is the default", which is a fact about the
              // palette being demonstrated rather than a status; spending a
              // status token on it would also repaint it in every other theme
              // the moment this pane grew a theme-aware accent. It is pinned at
              // exactly one occurrence, gated on the default theme so it cannot
              // reach `still` — `appearancePane.test.ts` is the pin.
              <span className="w-2 rounded-md bg-green-500 self-center" />
            )}
          </div>
        )}
      </div>
      <div className="mt-3 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-md font-semibold text-codify-primary">{theme.label}</h3>
          <p className="text-xs text-codify-secondary mt-0.5 leading-relaxed">{theme.description}</p>
          {/* The *resolved* line, not the theme's own — see `swatchLine`. */}
          <p className="font-mono text-2xs text-codify-muted mt-1.5 break-words">
            {swatchLine(theme, resolveTints(theme, tints))}
          </p>
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
      {/* Only for the theme you are actually wearing. The picker edits a
          *stored* palette, and storing a palette for a theme you are merely
          hovering would mean a stray mouse-cross writes to disk — and, worse,
          that the colour you are dragging has no effect on the app, because
          the app is showing a different theme. Clicking "Preview this" first
          is the honest order. */}
      {isActive && (
        <ThemeTints
          theme={theme}
          tints={tints}
          onPick={onPick}
          onClear={onClear}
          onReset={onReset}
          onUndo={onUndo}
          undoable={undoable}
          undoLabel={undoLabel}
        />
      )}
    </div>
  );
};

/**
 * Custom colours for one theme, and the guard that keeps them readable.
 *
 * ## Why the swatch shows a colour you did not pick
 *
 * `tint.ts` clamps a proposal before it is ever written, and this component
 * renders the *resolved* value, not the one from the colour well. So a drag
 * that lands on a deep crimson can come back as a lighter crimson with a note
 * under it saying so. Showing the proposal instead would mean the swatch
 * disagreed with the screen, and the note would be the only warning — a
 * warning about something the user cannot see.
 *
 * ## Why backgrounds are not on this list
 *
 * A request for this feature asked for the background pinned to `#000000`
 * everywhere. Seven of the nineteen themes are not black, and their
 * backgrounds are load-bearing: `Liquid Mercury`'s chrome is the chrome, and
 * `Abyss` is a deep field the spores are meant to float in. Flattening all of
 * them to black would delete seven palettes rather than customise nineteen, so
 * the backgrounds stay each theme's own and the clamp does the legibility
 * work instead — which is the part that was actually at risk.
 *
 * ## Why the physics cannot drift
 *
 * Nothing here touches a canvas. A tint is a hex in a custom property, the
 * painter resolves it through `useAtmosphereCanvas`'s live read on the frame
 * it draws, and the arithmetic that moves the particles is untouched. That is
 * why there is nothing here to tune per theme: the same drop of a colour is
 * the whole feature.
 */
const ThemeTints: React.FC<{
  theme: AppearanceTheme;
  tints: TintMap;
  onPick: (name: string, hex: string) => void;
  onClear: (name: string) => void;
  onReset: () => void;
  onUndo: () => void;
  undoable: boolean;
  undoLabel: string;
}> = ({ theme, tints, onPick, onClear, onReset, onUndo, undoable, undoLabel }) => {
  const vars = tintableVars(theme);
  const changed = vars.filter((name) => isTintable(tints[name] ?? ""));

  return (
    <section
      className="mt-3 rounded-lg border border-codify-border bg-codify-raised/60 p-2.5"
      aria-label="Custom colours"
    >
      <div className="flex items-center justify-between gap-2 mb-1.5">
        <h4 className="text-2xs font-semibold uppercase tracking-wide text-codify-muted">
          Custom colours
        </h4>
        <div className="flex items-center gap-1.5">
          {/* The button names the change it would take back, rather than being
              a bare "Undo". Which is also the only way a person finds out that
              the thing they are about to reverse was on *another* theme — the
              stack is global, so pressing this while looking at an untinted
              theme would otherwise appear to do nothing at all. */}
          <button
            type="button"
            onClick={onUndo}
            disabled={!undoable}
            title={undoable ? `Undo ${undoLabel}` : "Nothing to undo"}
            aria-label={undoable ? `Undo ${undoLabel}` : "Nothing to undo"}
            className="flex items-center gap-1 text-2xs px-1.5 py-0.5 rounded border border-codify-border text-codify-secondary hover:bg-codify-surface transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <Undo2 className="w-2.5 h-2.5" aria-hidden="true" />
            Undo
          </button>
          {changed.length > 0 && (
            <button
              type="button"
              onClick={onReset}
              className="flex items-center gap-1 text-2xs px-1.5 py-0.5 rounded border border-codify-border text-codify-secondary hover:bg-codify-surface transition-colors"
            >
              <RotateCcw className="w-2.5 h-2.5" aria-hidden="true" />
              Reset all
            </button>
          )}
        </div>
      </div>

      {/* Every tintable variable gets a well, not only the edited ones. The
          first version listed the edited ones and described the rest in a
          sentence — "3 colours you can change: Interface accent, Bubble body,
          Bubble skin" — which names three things the user has no control over.
          A control that is described rather than offered is the hard version
          of a missing feature. */}
      <ul className="space-y-1.5">
        {vars.map((name) => (
          <TintRow
            key={name}
            theme={theme}
            name={name}
            proposed={isTintable(tints[name] ?? "") ? (tints[name] as string) : null}
            onPick={onPick}
            onClear={onClear}
          />
        ))}
      </ul>
    </section>
  );
};

/**
 * One variable: the colour well, what it resolved to, and why.
 *
 * `proposed` is `null` when the user has not touched it, and that is a
 * different row rather than a tinted one: the well shows the theme's own value
 * and there is nothing to undo. Rendering the untinted rows the same way is
 * what makes the group read as *the theme's palette, editable* rather than *a
 * list of overrides*.
 */
const TintRow: React.FC<{
  theme: AppearanceTheme;
  name: string;
  proposed: string | null;
  onPick: (name: string, hex: string) => void;
  onClear: (name: string) => void;
}> = ({ theme, name, proposed, onPick, onClear }) => {
  const own = theme.tokens[name as keyof typeof theme.tokens] as string | undefined;
  const resolved = proposed ? resolveTint(theme, name, proposed) : null;
  const shown = resolved?.hex || own || "#808080";
  const label = TINT_ROLES[name] ?? name.replace(/^--/, "");
  return (
    <li className="flex items-center gap-2">
      <input
        type="color"
        aria-label={`${label} colour`}
        title={`${label} — ${shown}`}
        value={shown}
        onChange={(event) => onPick(name, event.target.value)}
        className="w-7 h-7 rounded border border-codify-border bg-transparent p-0 cursor-pointer flex-shrink-0"
      />
      <span className="min-w-0 flex-1">
        <span className="block text-2xs text-codify-secondary truncate">{label}</span>
        <span className="block font-mono text-2xs text-codify-muted truncate">
          {/* Say which way it moved and by how much. "Adjusted" alone tells a
              user their pick was overridden without telling them whether it is
              now safe, and "4.6:1" tells them nothing unless the number is the
              lowest of the surfaces it has to sit on. */}
          {resolved?.adjusted
            ? `${shown} · was ${resolved.proposed} · ${resolved.ratio.toFixed(1)}:1`
            : resolved
              ? `${shown} · ${resolved.ratio.toFixed(1)}:1`
              : shown}
        </span>
      </span>
      {proposed !== null && (
        <button
          type="button"
          onClick={() => onClear(name)}
          title={`Reset ${label}`}
          aria-label={`Reset ${label}`}
          className="flex-shrink-0 text-2xs px-1.5 py-0.5 rounded border border-codify-border text-codify-muted hover:bg-codify-surface hover:text-codify-secondary transition-colors"
        >
          Reset
        </button>
      )}
    </li>
  );
};

/**
 * Which hexes a row prints is the theme's own business, in `appearance.ts`
 * beside the values — not a chain of conditions here, which is how the fourth
 * theme ends up wearing the third theme's footer. A label is optional and only
 * there where the bare hex is ambiguous, which is why OLED's line reads
 * `rain #003300` and the next one reads `#1e293b`.
 *
 * ## Why the rows and the detail disagree on purpose
 *
 * The **rows** pass no overrides and print the theme's own hexes, because their
 * job is telling two dark themes apart at a glance and the stock values are the
 * stable identity. The **detail** passes the resolved tints, because its job is
 * telling you what you are looking at — and when it printed the stock value,
 * the panel said `--reagent` was `#9dff3c` on one line and `#ce2c43` on the
 * next, with the colour well in between and nothing saying which one the canvas
 * was painting. Two hexes for one variable, two lines apart, is a question the
 * panel is supposed to be answering.
 */
const swatchLine = (theme: AppearanceTheme, overrides: Record<string, string> = {}): string =>
  (theme.swatch ?? [])
    .map((s) => {
      const value = overrides[s.v] ?? theme.tokens[s.v] ?? "";
      return s.label ? `${s.label} ${value}` : value;
    })
    .filter((line) => line.trim().length > 0)
    .join(" · ");

/**
 * Custom colours out, and a scheme back in.
 *
 * ## Why this is below the grid and not inside the theme detail
 *
 * A scheme is every theme you have tinted, not the one you are looking at, so
 * putting it inside the detail panel would have made it scroll with a preview
 * that is about one theme, and would have hidden it whenever you hovered
 * something else. It is the one control here that is about your settings
 * rather than about a theme, and it is placed like that.
 *
 * ## Why the export is one button and not one per theme
 *
 * The obvious second button — "copy this theme only" — was left out on purpose.
 * The file is the same shape either way, and a scheme that happens to contain
 * one theme is a scheme whose owner has not tinted anything else. A second
 * button would also have to be explained, and the explanation is longer than
 * the feature.
 *
 * ## Why the report is shown rather than a toast
 *
 * An import can partly succeed, and the part that did not is a theme name from
 * someone else's install that this build does not have. A toast that said
 * "Imported" would be a lie in exactly the case where a person most needs to
 * know, so the applied and skipped lists stay on screen until the panel closes.
 */
const SchemeBar: React.FC<{
  store: TintStore;
  onImport: (next: TintStore, label: string) => void;
}> = ({ store, onImport }) => {
  const scheme = encodeScheme(store);
  const empty = scheme.themes.length === 0;
  const [open, setOpen] = useState(false);
  const [pasted, setPasted] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [report, setReport] = useState<ImportReport | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);

  // A notice that never goes away is a second permanent line, and the panel
  // already has three. Four seconds is long enough to read and to notice you
  // missed it, and short enough that coming back does not find it waiting.
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), 4000);
    return () => clearTimeout(timer);
  }, [notice]);

  const json = (): string => JSON.stringify(scheme, null, 2);

  const onCopy = async (): Promise<void> => {
    const clipboard =
      typeof navigator !== "undefined" && navigator.clipboard
        ? navigator.clipboard.writeText.bind(navigator.clipboard)
        : undefined;
    const outcome = await copySchemeText(json(), {
      writeText: clipboard,
      document: typeof document !== "undefined" ? document : undefined,
    });
    setNotice(
      outcome === "failed"
        ? "Could not reach the clipboard — use Download instead."
        : "Scheme copied to the clipboard.",
    );
  };

  const onDownload = (): void => {
    downloadScheme(scheme, {
      document,
      createObjectURL: URL.createObjectURL.bind(URL),
      revokeObjectURL: URL.revokeObjectURL.bind(URL),
    });
    setNotice(`Saved as ${schemeFilename(new Date())}.`);
  };

  const accept = (text: string): void => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      setProblem("That is not JSON, so it cannot be a colour scheme.");
      setReport(null);
      return;
    }
    const decoded = decodeScheme(parsed);
    if (!decoded.ok) {
      setProblem(DECODE_MESSAGES[decoded.reason]);
      setReport(null);
      return;
    }
    const { store: next, report: applied } = mergeScheme(store, decoded.scheme);
    setProblem(null);
    setReport(applied);
    setPasted("");
    onImport(
      next,
      `Import ${applied.applied.length} theme${applied.applied.length === 1 ? "" : "s"}`,
    );
  };

  const onFile = async (event: React.ChangeEvent<HTMLInputElement>): Promise<void> => {
    const file = event.target.files?.[0];
    // Cleared either way, so picking the *same* file twice in a row fires a
    // second `change` — otherwise the second attempt silently does nothing and
    // the user concludes the import is broken.
    event.target.value = "";
    if (!file) return;
    try {
      accept(await file.text());
    } catch (err) {
      setProblem(describeImportFailure(err));
      setReport(null);
    }
  };

  return (
    <section
      className="mt-4 rounded-lg border border-codify-border bg-codify-raised/60 p-2.5"
      aria-label="Share custom colours"
    >
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="min-w-0">
          <h4 className="text-2xs font-semibold uppercase tracking-wide text-codify-muted">
            Share custom colours
          </h4>
          <p className="text-2xs text-codify-muted mt-0.5 leading-relaxed">
            {empty
              ? "Nothing customised yet — pick a colour below and it turns up here."
              : `One file with ${scheme.themes.length} tinted theme${
                  scheme.themes.length === 1 ? "" : "s"
                }: ${scheme.themes.map((t) => t.label).join(", ")}.`}
          </p>
        </div>
        <div className="flex items-center gap-1.5 flex-shrink-0">
          <SchemeButton icon={Copy} label="Copy scheme" onClick={() => void onCopy()} disabled={empty} />
          <SchemeButton icon={FileDown} label="Download" onClick={onDownload} disabled={empty} />
          <SchemeButton
            icon={ClipboardPaste}
            label="Import…"
            onClick={() => {
              setOpen(!open);
              setProblem(null);
            }}
            aria-expanded={open}
          />
        </div>
      </div>

      {notice && (
        <p className="text-2xs text-codify-secondary mt-1.5" role="status">
          {notice}
        </p>
      )}

      {open && (
        <div className="mt-2.5 pt-2.5 border-t border-codify-border space-y-2">
          <label className="block">
            <span className="block text-2xs text-codify-secondary mb-1">
              Paste a colour scheme
            </span>
            <textarea
              value={pasted}
              onChange={(event) => setPasted(event.target.value)}
              rows={4}
              spellCheck={false}
              placeholder='{ "kind": "codify-scheme", ... }'
              aria-describedby={problem ? "scheme-problem" : undefined}
              className="w-full rounded border border-codify-border bg-codify-bg px-2 py-1.5 font-mono text-2xs text-codify-primary resize-y"
            />
          </label>
          <div className="flex items-center gap-1.5 flex-wrap">
            <button
              type="button"
              onClick={() => accept(pasted)}
              disabled={pasted.trim().length === 0}
              className="text-2xs font-semibold px-2.5 py-1.5 rounded-lg bg-codify-raised border border-codify-border-strong text-codify-primary hover:bg-codify-surface transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              Apply pasted scheme
            </button>
            <button
              type="button"
              onClick={() => fileInput.current?.click()}
              className="flex items-center gap-1 text-2xs px-2.5 py-1.5 rounded-lg bg-codify-raised border border-codify-border text-codify-secondary hover:bg-codify-surface transition-colors"
            >
              <FileUp className="w-3 h-3" aria-hidden="true" />
              Load a file…
            </button>
            {/* The input is not `display: none` — a file input that cannot be
                focused is not reachable by keyboard, and the button above is
                not the only way a person should be able to open a file. It is
                visually hidden and still in the tab order. */}
            <input
              ref={fileInput}
              type="file"
              accept="application/json,.json"
              onChange={(event) => void onFile(event)}
              aria-label="Load a colour scheme from a file"
              className="sr-only"
            />
          </div>

          {problem && (
            <p id="scheme-problem" role="alert" className="text-2xs text-codify-danger">
              {problem}
            </p>
          )}
          {report && <ImportSummary report={report} />}
        </div>
      )}
    </section>
  );
};

/** One icon button, so the three share a shape instead of drifting apart. */
const SchemeButton: React.FC<{
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  onClick: () => void;
  disabled?: boolean;
  "aria-expanded"?: boolean;
}> = ({ icon: Icon, label, onClick, disabled, ...rest }) => (
  <button
    type="button"
    onClick={onClick}
    disabled={disabled}
    title={label}
    aria-label={label}
    {...rest}
    className="flex items-center gap-1 text-2xs px-2 py-1.5 rounded-lg border border-codify-border text-codify-secondary hover:bg-codify-surface hover:text-codify-primary transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
  >
    <Icon className="w-3 h-3" />
    {label}
  </button>
);

/**
 * What an import actually did, in the order that matters: what it could not
 * do first, because that is the half the user did not ask for and cannot see.
 */
const ImportSummary: React.FC<{ report: ImportReport }> = ({ report }) => {
  if (report.applied.length === 0 && report.skipped.length === 0) return null;
  return (
    <div className="text-2xs leading-relaxed space-y-0.5" role="status">
      {report.applied.length > 0 && (
        <p className="text-codify-secondary">
          Applied to {report.applied.map((a) => `${a.label} (${a.count})`).join(", ")}.
        </p>
      )}
      {report.skipped.length > 0 && (
        <p className="text-codify-warning">
          {report.skipped.length === 1 ? "This build has no" : "This build has none of"}{" "}
          {report.skipped.map((s) => s.label).join(", ")} —{" "}
          {report.skipped.length === 1 ? "that theme" : "those themes"}, so{" "}
          {report.skipped.length === 1 ? "it was" : "they were"} left out.
        </p>
      )}
    </div>
  );
};
