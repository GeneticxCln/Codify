#!/bin/sh
# Put Codify in this user's applications menu and on their PATH — no installer, no root.
#
#   scripts/install-local.sh            install (after `make build-app`)
#   scripts/install-local.sh uninstall  remove exactly what install wrote
#
# Three files, all under the user's own XDG directories, and nothing is copied out of the checkout
# except the icon: the `codify` command is a symlink to scripts/codify, so a rebuilt app is the app
# the menu entry starts. Deleting the checkout leaves a dangling entry, which `uninstall` removes.
set -eu

action=${1:-install}
here=$(dirname -- "$(readlink -f -- "$0")")
root=$(dirname -- "$here")
bin_dir=${XDG_BIN_HOME:-$HOME/.local/bin}
data_dir=${XDG_DATA_HOME:-$HOME/.local/share}
link="$bin_dir/codify"
entry="$data_dir/applications/codify.desktop"
icon="$data_dir/icons/hicolor/128x128/apps/codify.png"

refresh_caches() {
  # Best effort: a menu that lags by a session is not a failed install.
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$data_dir/applications" >/dev/null 2>&1 || true
  fi
  if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t "$data_dir/icons/hicolor" >/dev/null 2>&1 || true
  fi
}

case $action in
  install)
    if [ ! -x "$root/src-tauri/target/release/codify-desktop" ]; then
      echo "install-local: no release build yet — run 'make build-app' first" >&2
      exit 1
    fi
    # The Exec line is parsed by the desktop-entry spec, not by a shell: a path with any of these
    # in it cannot be quoted safely, and a menu entry that quietly does nothing is worse than a
    # refusal that says why.
    case $link in
      *[\"\`\$\\]*)
        echo "install-local: $link contains a quote, backtick, dollar or backslash, which a" >&2
        echo "install-local: .desktop Exec line cannot carry; set XDG_BIN_HOME to a plainer path" >&2
        exit 1
        ;;
    esac
    exec_path=$link
    case $link in *" "*) exec_path="\"$link\"" ;; esac

    mkdir -p "$bin_dir" "$data_dir/applications" "$(dirname -- "$icon")"
    ln -sf "$root/scripts/codify" "$link"
    cp "$root/src-tauri/icons/128x128.png" "$icon"
    cat > "$entry" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Codify
Comment=Local-first multi-agent coding assistant
Exec=$exec_path
Icon=codify
Terminal=false
Categories=Development;IDE;
DESKTOP
    refresh_caches
    echo "installed: $link -> $root/scripts/codify"
    echo "installed: $entry"
    case ":$PATH:" in
      *":$bin_dir:"*) ;;
      *) echo "note: $bin_dir is not on your PATH; the menu entry works, the 'codify' command needs it" ;;
    esac
    ;;
  uninstall)
    rm -f "$link" "$entry" "$icon"
    refresh_caches
    echo "removed: $link $entry $icon"
    ;;
  *)
    echo "usage: $0 [install|uninstall]" >&2
    exit 2
    ;;
esac
