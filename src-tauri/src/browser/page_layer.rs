use super::*;
use gtk::prelude::*;

/// The widget name the layer's `gtk::Overlay` carries.
///
/// Found by **name**, not by position: a child's index in a parent is a
/// fact about the order things happened in, and that is precisely the kind
/// of fact that stops being true the next time somebody adds a widget above
/// it.
const LAYER_NAME: &str = "codify-page-layer";

/// The widget name the `gtk::Fixed` inside that overlay carries.
const FIXED_NAME: &str = "codify-page-fixed";

/// Run widget work on the one thread GTK allows it from.
///
/// GTK asserts this on every call into a widget, and the assert reads
/// "GTK may only be used from the main thread". A user's click reached it:
/// `codify_browser_open` is an `async` command, Tauri runs those on a tokio
/// worker, and the layer was built there — the page never existed. The
/// embed smoke agreed with the broken code because the smoke seats its page
/// from `setup`, on the main thread, and the click a user makes does not.
///
/// The question below is the toolkit's own. `tao` initialises GTK on the
/// main thread, and `gtk::init` **acquires and leaks** the default main
/// context (gtk-rs#186), so for the life of the process that context has an
/// owner and that owner is the only thread whose widget calls GTK accepts.
/// When this already is that thread the work runs inline: hopping from the
/// main thread would queue a closure behind the call that is waiting for it.
///
/// The hop is a blocking `recv` on the calling thread, which is the shape
/// Tauri itself uses for `add_child`: the worker waits, the toolkit's thread
/// does the work, and the answer comes back to the call that asked. Nothing
/// here is reentrant — the work is widget bookkeeping, not a callback.
fn on_main<T>(
    window: &tauri::Window,
    work: impl FnOnce() -> Result<T, String> + Send + 'static,
) -> Result<T, String>
where
    T: Send + 'static,
{
    if gtk::glib::MainContext::default().is_owner() {
        return work();
    }
    let (done, answer) = std::sync::mpsc::channel();
    window
        .run_on_main_thread(move || {
            // A closed receiver means the caller gave up on the answer; the
            // work has already run and this is not the toolkit's problem.
            let _ = done.send(work());
        })
        .map_err(|e| format!("the page layer could not reach the main thread: {e}"))?;
    answer
        .recv()
        .map_err(|_| "the page layer's main-thread step ended without an answer".to_string())?
}

/// The layer, looked up and **never built**.
///
/// Every path that must not restructure a window as a side effect uses
/// this: closing a page, asking where a page is. A close that arrived
/// before any page existed has nothing to do and must not leave an overlay
/// behind as a souvenir.
fn find(vbox: &gtk::Box) -> Option<(gtk::Overlay, gtk::Fixed)> {
    for child in vbox.children() {
        let Ok(overlay) = child.downcast::<gtk::Overlay>() else {
            continue;
        };
        if overlay.widget_name().as_str() != LAYER_NAME {
            continue;
        }
        for inner in overlay.children() {
            if let Ok(fixed) = inner.downcast::<gtk::Fixed>() {
                if fixed.widget_name().as_str() == FIXED_NAME {
                    return Some((overlay, fixed));
                }
            }
        }
    }
    None
}

/// The layer, built on first use.
///
/// Lazy so that a user who never opens a browser tab has a window that was
/// never restructured — the whole of this exists for pages, and revoking it
/// for people who do not use them is the polite version.
fn layer(window: &tauri::Window) -> Result<(gtk::Overlay, gtk::Fixed), String> {
    let vbox = window
        .default_vbox()
        .map_err(|e| format!("the main window has no GTK container: {e}"))?;
    if let Some(found) = find(&vbox) {
        return Ok(found);
    }
    let built = assemble(&vbox);
    // Once, with the layer: the signal belongs to the window, and a window
    // has one layer. The layer works without it — a page that cannot report
    // taking the keyboard is a coloured edge in the wrong place, not a page
    // that cannot be used — so a window that will not hand over its toplevel
    // costs the report and nothing else.
    if let Ok(top) = window.gtk_window() {
        let sink = window.app_handle().clone();
        watch_focus(&top, move |tab_id| {
            // A shell that cannot deliver the event cannot be asked what to do
            // about it, and this runs inside the toolkit's own signal: nothing
            // here may panic or block.
            let _ = sink.emit(PAGE_FOCUSED_EVENT, BrowserPageFocused { tab_id });
        });
    }
    Ok(built)
}

/// The tab whose page holds `widget`, when `widget` is a page or sits inside
/// one.
///
/// Walks **up**, looking for the label [`adopt`] renamed the page's widget to.
/// The toolkit's focus is always a leaf — the webview itself, or something
/// inside it — and which widget `adopt` found for a page is a fact about how
/// Tauri builds its views that this module has been wrong about before, so the
/// question is asked of the whole ancestry instead of being bet on one level.
/// The app's own webview, the tab strip's window and the layer's containers
/// carry the toolkit's class names, which never start with the label prefix.
fn page_of(widget: &gtk::Widget) -> Option<String> {
    let mut at = Some(widget.clone());
    while let Some(here) = at {
        if let Some(tab_id) = here.widget_name().as_str().strip_prefix(LABEL_PREFIX) {
            return Some(tab_id.to_string());
        }
        at = here.parent();
    }
    None
}

/// Tell `announce` which page the toolkit just gave the keyboard to.
///
/// `set-focus` on the **window**, not `focus-in-event` on a page. A press in a
/// native view makes WebKit grab the toolkit's focus for it, and that is the
/// moment this wants — but `focus-in-event` is delivered to the one widget that
/// is the focus, which is a leaf this module does not own, and is also repeated
/// whenever the window is merely re-activated with a page still holding the
/// focus (nothing moved, so there is nothing to say). `set-focus` is the
/// toolkit announcing that *the focus changed*, and it carries the widget it
/// changed to. The signal runs before the class handler, so the argument is the
/// new focus and `window.focus()` would still be the old one; only the argument
/// is read.
///
/// Focus moving **off** a page says nothing: the UI's own DOM events say where
/// the keyboard went when it went into the app, and a second voice about the
/// same fact would be a second opinion.
///
/// Generic over the window and the sink so that a test can drive it with real
/// widgets and no app: [`layer`] is the one production caller.
pub(super) fn watch_focus<W: IsA<gtk::Window>>(top: &W, announce: impl Fn(String) + 'static) {
    top.connect_set_focus(move |_, focused| {
        if let Some(tab_id) = focused.and_then(page_of) {
            announce(tab_id);
        }
    });
}

/// Restructure `vbox` into the layer: what it holds becomes the overlay's
/// main child, and a fixed sits above it for the pages.
///
/// Split from [`layer`] so it can be built and inspected without a window;
/// `the_page_layer_holds_up_on_real_widgets` does exactly that.
pub(super) fn assemble(vbox: &gtk::Box) -> (gtk::Overlay, gtk::Fixed) {
    let overlay = gtk::Overlay::new();
    overlay.set_widget_name(LAYER_NAME);
    let fixed = gtk::Fixed::new();
    fixed.set_widget_name(FIXED_NAME);
    // Adopt what the window already holds — the app's own webview, the
    // whole UI — as the overlay's **main** child. A `gtk::Overlay` has
    // exactly one of those, which is why this has to run *before* the first
    // page is created: a second `add` is dropped with a warning rather than
    // layered, and a page dropped that way has no parent to be placed in.
    for child in vbox.children() {
        vbox.remove(&child);
        overlay.add(&child);
        child.show();
    }
    overlay.add_overlay(&fixed);
    // **Without this the app stops answering the moment a page exists.** An
    // overlay child that is not pass-through gets an input window over its
    // whole allocation, and a `Fixed` is allocated the entire overlay — so
    // the layer's window sat over the app's own webview and took every
    // click, key and scroll that was aimed at the tab strip, the side panel
    // and the composer. The window kept painting (nothing was wrong with
    // rendering), it just did not respond. Pass-through makes the *layer's*
    // window transparent to input; the pages inside it are child windows of
    // it and keep receiving theirs, which is what `gdk_window_set_pass_through`
    // documents ("the child windows of window are unaffected").
    overlay.set_overlay_pass_through(&fixed, true);
    vbox.pack_start(&overlay, true, true, 0);
    overlay.show();
    fixed.show();
    (overlay, fixed)
}

/// The widget a page's label names inside the layer's fixed.
fn widget(fixed: &gtk::Fixed, label: &str) -> Option<gtk::Widget> {
    fixed
        .children()
        .into_iter()
        .find(|child| child.widget_name().as_str() == label)
}

/// Ready the container for a page that is about to exist.
pub fn prepare(window: &tauri::Window) -> Result<(), String> {
    let here = window.clone();
    on_main(window, move || layer(&here).map(|_| ()))
}

/// Move a just-created page into the layer, at its rectangle.
///
/// The page's widget is *the child that appeared in the window's box when
/// this module asked for a page* — identified that way because Tauri gives
/// out no handle to it. It is then renamed to the page's **label**, which
/// is how every later lookup finds it: the same string the commands take,
/// rather than an entry in a side table that could outlive the widget it
/// describes.
///
/// Whether the page is **shown** is decided by [`seat_visibility`], not by
/// this function: a background tab's page is seated into the layer hidden,
/// and the UI's focus effect is what makes it visible when its turn comes.
pub fn adopt(window: &tauri::Window, page: &tauri::Webview, bounds: &Bounds) -> Result<(), String> {
    let here = window.clone();
    // The label and the rectangle travel instead of the handles: the
    // closure has to be `Send` and `'static` to cross threads, and the
    // widget is found by the label anyway.
    let label = page.label().to_string();
    let size = *bounds;
    let visibility = seat_visibility(&label);
    on_main(window, move || {
        let (_overlay, fixed) = layer(&here)?;
        let vbox = here
            .default_vbox()
            .map_err(|e| format!("the main window has no GTK container: {e}"))?;
        let widget = vbox
            .children()
            .into_iter()
            .find(|child| child.widget_name().as_str() != LAYER_NAME)
            .ok_or_else(|| "the new browser page has no widget to place".to_string())?;
        widget.set_widget_name(&label);
        vbox.remove(&widget);
        fixed.put(&widget, size.x.round() as i32, size.y.round() as i32);
        widget.set_size_request(size.width.round() as i32, size.height.round() as i32);
        match visibility {
            SeatVisibility::Shown => widget.show(),
            // `gtk_widget_hide` on an already-hidden child is a no-op, so
            // the call is unconditional: every page reaches the layer
            // through one branch, and no caller has to know which.
            SeatVisibility::Hidden => widget.hide(),
        }
        Ok(())
    })
}

/// Give a page the toolkit's keyboard focus, the way a press inside it does.
///
/// For the smoke, which has no person to click: it is the only way to make
/// the focus report happen under a display with nobody at it. The app never
/// calls this — the UI marks a pane focused and leaves the keyboard where the
/// person put it.
pub fn take_focus(window: &tauri::Window, label: &str) -> Result<(), String> {
    let here = window.clone();
    let label = label.to_string();
    on_main(window, move || {
        let (_overlay, fixed) = layer(&here)?;
        let widget = widget(&fixed, &label)
            .ok_or_else(|| format!("browser page {label:?} is not in the page layer"))?;
        widget.grab_focus();
        Ok(())
    })
}

/// Put one page at `bounds`, in the layer, by hand.
pub fn place(window: &tauri::Window, page: &tauri::Webview, bounds: &Bounds) -> Result<(), String> {
    let here = window.clone();
    let label = page.label().to_string();
    let size = *bounds;
    on_main(window, move || {
        let (_overlay, fixed) = layer(&here)?;
        let widget = widget(&fixed, &label)
            .ok_or_else(|| format!("browser page {label:?} is not in the page layer"))?;
        fixed.move_(&widget, size.x.round() as i32, size.y.round() as i32);
        widget.set_size_request(size.width.round() as i32, size.height.round() as i32);
        Ok(())
    })
}

/// Take a page's widget out of the layer, before the page is closed.
///
/// A label is reusable — reopening a closed tab asks for the same one — and
/// a `gtk::Fixed` still holding the destroyed widget would hand the next
/// page the last page's geometry.
pub fn release(window: &tauri::Window, label: &str) {
    let here = window.clone();
    let label = label.to_string();
    // The outcome is dropped on purpose: a widget that could not be taken
    // out of the layer is one stale entry against a page that is being
    // destroyed, and refusing to close a tab over it is the worse trade.
    let _: Result<(), String> = on_main(window, move || {
        let Ok(vbox) = here.default_vbox() else {
            return Ok(());
        };
        if let Some((_overlay, fixed)) = find(&vbox) {
            if let Some(widget) = widget(&fixed, &label) {
                fixed.remove(&widget);
            }
        }
        Ok(())
    });
}

/// Where a page's widget **actually is**, in the window's own coordinates.
///
/// The measurement this pane was missing. The UI reports the rectangle it
/// measured and the shell reports what it asked the toolkit for; neither is
/// evidence that the toolkit complied, and on this platform for the whole
/// life of the pane it did not. A page that is not in the layer is an
/// `Err` and not a `None`, because those are different answers: `None`
/// means "this platform does not report geometry", and a missing page is a
/// fault worth failing a smoke over.
pub fn geometry(app: &AppHandle, label: &str) -> Result<Option<(i32, i32, i32, i32)>, String> {
    let window = app
        .get_window("main")
        .ok_or_else(|| "the main window does not exist".to_string())?;
    let here = window.clone();
    let label = label.to_string();
    on_main(&window, move || {
        let vbox = here
            .default_vbox()
            .map_err(|e| format!("the main window has no GTK container: {e}"))?;
        let Some((_overlay, fixed)) = find(&vbox) else {
            return Err("no page layer exists, so no page can be in it".to_string());
        };
        let widget = widget(&fixed, &label)
            .ok_or_else(|| format!("browser page {label:?} is not in the page layer"))?;
        // Deprecated in GTK 3 in favour of size plus margins, and the right
        // call anyway: this reads the box the toolkit gave the widget,
        // which is the only thing that answers the question being asked. A
        // `width()` would report our own request back to us — the mistake
        // this whole check exists to stop.
        #[allow(deprecated)]
        let allocation = widget.allocation();
        Ok(Some((
            allocation.x(),
            allocation.y(),
            allocation.width(),
            allocation.height(),
        )))
    })
}
