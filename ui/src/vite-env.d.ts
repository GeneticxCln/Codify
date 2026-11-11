// Vite handles stylesheets itself; `tsc` still has to know the modules exist.
// TypeScript 7 checks side-effect imports (`noUncheckedSideEffectImports`), so
// without this a bare `import "./index.css"` is TS2882 in both the app build and
// the test typecheck.
declare module "*.css";
