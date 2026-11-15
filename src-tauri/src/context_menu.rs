//! The main window's right-click menu, minus the browser chrome.
//!
//! WebKitGTK builds a context menu for every webview it owns. On this window
//! that menu is mostly wrong: the app is a single page, so Back, Forward, Stop
//! and Reload describe navigation the person cannot take, Inspect Element is a
//! developer action in a shipping AppImage, and the Open Link/Frame entries
//! would navigate the app itself or open a browser window it has no chrome for.
//! The useful entries — Cut, Copy, Paste, Select All, spelling, Copy Link — are
//! already in the same menu, underneath those browser actions.
//!
//! ## Why filter instead of drawing a menu
//!
//! Because the editing entries are not decoration. WebKit's Cut and Paste
//! act on the focused field, honour the system clipboard and undo stack, and
//! do the right thing in a textarea, a contenteditable editor and a terminal
//! alike. A React menu would have to reimplement that through `navigator.clipboard`
//! and `execCommand`, and would be wrong exactly where it matters — pasting
//! into a field. This removes the browser-only items and leaves everything else
//! WebKit's.
//!
//! The filter runs on the **main** webview only. A browser tab is a separate,
//! untrusted webview with its own history; its Back/Forward entries are real
//! navigation, and its inspector is the explicit DevTools control the browser
//! pane already owns (`browser::open_devtools`). Filtering that page's menu
//! would take away capabilities from a surface whose menu is supposed to be a
//! browser's.
//!
//! ## Why an empty menu is suppressed
//!
//! On ordinary chrome with no selection, those five were the whole menu. Once
//! they are gone there is nothing to show, and an empty popup looks like a
//! rendering fault. Returning `true` from the signal stops WebKit from drawing
//! it; text fields and selections still have their editing entries, so they
//! keep a menu.

/// Remove the browser-only entries from the main window's context menu.
///
/// The signal is connected once, at setup, and the closure returns whether
/// WebKit should suppress the menu entirely. Separators left at the edges or in
/// a run are removed too, because a leading line with no item above it reads as
/// a missing entry rather than a group boundary.
#[cfg(target_os = "linux")]
pub fn install(app: &tauri::AppHandle) {
    use tauri::Manager;
    use webkit2gtk::{ContextMenuExt, ContextMenuItemExt, WebViewExt};

    let Some(window) = app.get_webview_window("main") else {
        eprintln!("[Codify] context menu: no main window to filter");
        return;
    };

    let result = window.with_webview(|webview| {
        webview
            .inner()
            .connect_context_menu(|_view, menu, _event, _hit_test| {
                for item in menu.items() {
                    if hides(item.stock_action()) {
                        menu.remove(&item);
                    }
                }
                trim_separators(menu);
                // No entries left: ordinary chrome, not a text field. Suppress
                // the empty popup rather than draw a border with nothing in it.
                menu.n_items() == 0
            });
    });

    if let Err(error) = result {
        // The app still works without the filter; the menu is just the browser's
        // again. Say so on the launch log rather than failing the boot.
        eprintln!("[Codify] context menu: could not install the filter: {error}");
    }
}

#[cfg(not(target_os = "linux"))]
pub fn install(_app: &tauri::AppHandle) {}

/// Whether an entry belongs to browser navigation or the developer inspector.
///
/// This is deliberately a small allowlist of things to remove, not a list of
/// things to keep: WebKit adds editing actions, spelling guesses, emoji and
/// media controls depending on what is under the pointer, and all of those are
/// legitimate. The removed entries are the ones that would navigate the main
/// page, open a window this app does not have, or inspect the app itself.
/// Copy Link and the editing actions stay: they act on the thing under the
/// pointer without leaving the page.
#[cfg(target_os = "linux")]
fn hides(action: webkit2gtk::ContextMenuAction) -> bool {
    use webkit2gtk::ContextMenuAction::*;
    matches!(
        action,
        GoBack
            | GoForward
            | Stop
            | Reload
            | InspectElement
            | OpenLink
            | OpenLinkInNewWindow
            | OpenFrameInNewWindow
    )
}

/// Drop separators that no longer separate anything.
#[cfg(target_os = "linux")]
fn trim_separators(menu: &webkit2gtk::ContextMenu) {
    use webkit2gtk::{ContextMenuExt, ContextMenuItemExt};

    loop {
        let leading = menu.items().first().is_some_and(|item| item.is_separator());
        if !leading {
            break;
        }
        let Some(item) = menu.items().into_iter().next() else {
            break;
        };
        menu.remove(&item);
    }

    loop {
        let trailing = menu.items().last().is_some_and(|item| item.is_separator());
        if !trailing {
            break;
        }
        let Some(item) = menu.items().into_iter().last() else {
            break;
        };
        menu.remove(&item);
    }

    let mut previous_was_separator = false;
    for item in menu.items() {
        if !item.is_separator() {
            previous_was_separator = false;
            continue;
        }
        if previous_was_separator {
            menu.remove(&item);
        }
        previous_was_separator = true;
    }
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::hides;
    use webkit2gtk::ContextMenuAction;

    #[test]
    fn removes_browser_navigation_open_and_inspector_but_keeps_editing() {
        use ContextMenuAction::*;

        for action in [
            GoBack,
            GoForward,
            Stop,
            Reload,
            InspectElement,
            OpenLink,
            OpenLinkInNewWindow,
            OpenFrameInNewWindow,
        ] {
            assert!(hides(action), "{action:?} should not be in the app's menu");
        }
        for action in [
            Copy,
            Cut,
            Paste,
            Delete,
            SelectAll,
            CopyLinkToClipboard,
            CopyImageToClipboard,
            CopyImageUrlToClipboard,
            InputMethods,
            NoAction,
        ] {
            assert!(!hides(action), "{action:?} is a normal app menu action");
        }
    }
}
