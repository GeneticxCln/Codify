import { useSyncExternalStore } from "react";
import {
  activeThemeId,
  subscribeTheme,
  themeById,
  type AppearanceTheme,
} from "../appearance";

/**
 * The applied theme, as React state.
 *
 * The CSS custom properties carry the *colors* to every corner of the app with
 * no JavaScript; this hook exists for the pieces that cannot be a CSS answer —
 * a component that must **exist only under one theme**. The idle backdrop is
 * that case: `MatrixRain` should mount when the OLED theme is chosen and
 * unmount when it is not, rather than a hidden canvas ticking behind a theme
 * that never reads it.
 *
 * `useSyncExternalStore` over the module's subscribe/snapshot pair: the store
 * is `applyTheme`'s `codify:theme-changed` event plus the `storage` event for
 * cross-window writes, and the snapshot is the persisted id. No local
 * `useState` mirroring it — a second copy of the answer is where two surfaces
 * start to disagree.
 */
export function useTheme(): AppearanceTheme {
  const id = useSyncExternalStore(subscribeTheme, activeThemeId, activeThemeId);
  return themeById(id);
}
