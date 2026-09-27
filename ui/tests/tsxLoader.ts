/**
 * Teach `node --test` to import `.tsx`, to resolve `src/`'s imports, and to
 * have the one browser global a module reads while it loads.
 *
 * Every other test here exercises a plain `.ts` module, which
 * `--experimental-strip-types` handles on its own. A *component* is `.tsx`, and
 * node refuses the extension during format detection — before any `load` hook
 * gets a chance to answer — so a render test would have to assert on a
 * component's return value instead of its markup. That is the layer worth
 * testing: whether the sentence about a file lands on screen is a fact about
 * the JSX, not about the function that returns it.
 *
 * So the hook reads the file itself rather than delegating, transforms it with
 * esbuild, and hands node a module. esbuild is the transform vite already uses,
 * which is why nothing new is downloaded for this. A `resolve` hook fills in the
 * extensions node wants, so `src/` keeps the import style the bundler expects.
 *
 * Import this for its side effect **before** importing any component, and reach
 * for the component through a dynamic `import()` — a static one is hoisted and
 * would be resolved before the hook is registered.
 *
 * The `resolve` half is needed by more than the renderers: a test that imports
 * a `.ts` module which itself imports a sibling extensionlessly cannot resolve
 * it either. `tabs.test.ts` needed this for exactly that reason once `tabs.ts`
 * stopped being self-contained, with no `.tsx` anywhere in the file.
 *
 * No test may skip itself out of a guarantee, so a missing esbuild fails here
 * loudly and says what to do rather than quietly rendering nothing.
 */
import module from "node:module";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

type Esbuild = {
  transformSync: (
    source: string,
    options: { loader: "tsx"; jsx: "automatic"; format: "esm" }
  ) => { code: string };
};

let esbuild: Esbuild;
try {
  esbuild = (await import("esbuild")) as unknown as Esbuild;
} catch {
  throw new Error(
    "ui/tests/tsxLoader.ts needs esbuild to render .tsx components, and it is " +
      "not installed. It is declared in package.json under devDependencies: run " +
      "`npm install` in ui/ and try again. The render tests must not be skipped " +
      "silently — a card that is never rendered is a card whose copy nobody reads."
  );
}

let registered = false;

/** What a relative specifier may turn out to be, most specific first. */
const CANDIDATES = [".ts", ".tsx", "/index.ts", "/index.tsx"] as const;

/** A relative specifier that already names a file (or a package) is left alone. */
const HAS_EXTENSION = /\.[cm]?[jt]sx?$|\.json$/i;

/**
 * A `Storage` that works, for the one read that happens while a module loads.
 *
 * `api.ts` reads the engine's port and boot token at module scope — which is
 * the right shape for the app, where the shell writes them before the bundle
 * evaluates, and the wrong one for a test. Without this, importing anything that
 * reaches `api.ts` dies with `Cannot read properties of undefined (reading
 * 'getItem')`, and the reach is wide: `ChatTimeline` and `App` both import it,
 * so *every* card those two draw was unrenderable here. That is what limited the
 * render tests to the two components that had been extracted out of them.
 *
 * Working rather than throwing, because a module that loads and then fails on
 * its first read is a worse failure to diagnose than one that never loaded.
 * Nothing here reaches the network: `api.ts` only *builds* a base URL at import
 * time, and `renderToStaticMarkup` never runs an effect, so a component that
 * would fetch on mount does not.
 *
 * Installed by replacing the property outright rather than by checking whether
 * one is there first, for two reasons. Node ships `localStorage` as a lazy
 * accessor that returns `undefined` and warns when read, so "is it defined"
 * both trips an `ExperimentalWarning` on every run and answers `no` anyway.
 * And a store that deferred to the host would inherit whatever
 * `--localstorage-file` pointed at, which is the opposite of hermetic: a
 * developer's real token would leak into a render test.
 *
 * Only `localStorage`. `window`, `document` and `navigator` are read inside
 * effects and event handlers throughout `src/`, and `renderToStaticMarkup` runs
 * neither — so stubbing them would be a guess about a blocker that does not
 * exist. A new module-scope read will say so loudly, which is the right way to
 * find out.
 */
function installStorage(): void {
  const items = new Map<string, string>();
  Object.defineProperty(globalThis, "localStorage", {
    value: {
      get length(): number {
        return items.size;
      },
      key: (i: number): string | null => [...items.keys()][i] ?? null,
      getItem: (k: string): string | null =>
        items.has(k) ? (items.get(k) as string) : null,
      setItem: (k: string, v: string): void => {
        items.set(k, String(v));
      },
      removeItem: (k: string): void => {
        items.delete(k);
      },
      clear: (): void => {
        items.clear();
      },
    },
    configurable: true,
    writable: true,
    enumerable: true,
  });
}

/**
 * Register the transform once per process.
 *
 * `registerHooks` stacks rather than replaces, and several test files import
 * this module, so the guard is about a single test file being loaded twice — not
 * about the others.
 */
export function registerTsx(): void {
  if (registered) return;
  registered = true;
  installStorage();
  module.registerHooks({
    // Every import in `src/` is extensionless, because vite and tsc both resolve
    // it that way and rewriting the source to suit a test runner would be the
    // tail wagging the dog. So the extension is supplied here instead, and only
    // for a relative specifier that has none — a bare package name is not this
    // hook's business.
    resolve(specifier, context, nextResolve) {
      if (!specifier.startsWith(".") || HAS_EXTENSION.test(specifier)) {
        return nextResolve(specifier, context);
      }
      for (const candidate of CANDIDATES) {
        try {
          return nextResolve(specifier + candidate, context);
        } catch {
          // Not that one; the next candidate may be it.
        }
      }
      return nextResolve(specifier, context);
    },
    load(url, context, nextLoad) {
      if (!url.endsWith(".tsx")) return nextLoad(url, context);
      const source = readFileSync(fileURLToPath(url), "utf8");
      const { code } = esbuild.transformSync(source, {
        loader: "tsx",
        jsx: "automatic",
        format: "esm",
      });
      return { format: "module", source: code, shortCircuit: true };
    },
  });
}
