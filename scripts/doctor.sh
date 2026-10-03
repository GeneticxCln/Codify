#!/usr/bin/env bash
# Say what this machine is missing before the gate says it in a worse way.
#
# The README's prerequisites had drifted from what `make ci` actually needs: it listed
# a Node floor that was wrong, no system libraries at all for the desktop shell (the
# first `cargo check` on a fresh machine fails on a missing `webkit2gtk-4.1`), no
# display for the one Rust test that builds real GTK widgets, and installed Python
# dependencies with a command that modern distributions refuse (PEP 668). A list in
# prose is where that drift lives, so this asks the machine instead.
#
# Read-only. It installs nothing and changes nothing; every line is a fact about this
# machine plus, when something is missing, the command that fixes it.
#
# Usage: scripts/doctor.sh        (or: make doctor)
# Exit:  0 everything `make ci` needs is present, 1 something is missing.
#
# Only Debian/Ubuntu package names were verified (they are what a real `cargo check`
# was run against). The dnf and pacman lines follow Tauri's published prerequisites and
# are best effort; the script says so rather than presenting them as tested.
#
# DOCTOR_TOOL_PATH replaces PATH for *finding tools only* — the script's own grep and sed
# keep the real one. That is what lets the tests give it a machine with things missing.

set -uo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tool_path="${DOCTOR_TOOL_PATH:-$PATH}"

missing=0
hints=()

find_tool() { PATH="$tool_path" command -v "$1" 2>/dev/null; }
have() { find_tool "$1" >/dev/null; }
# Run a discovered tool by its resolved path, so the same PATH decides both the
# question "is it there" and the answer to "what does it say".
run() { local bin; bin="$(find_tool "$1")" || return 127; shift; "$bin" "$@"; }

ok() { printf '  ok       %s\n' "$1"; }
# Something the app can use and the gate does not need: reported, never a failure.
note() {
  printf '  note     %s\n' "$1"
  [ -n "${2:-}" ] && printf '           %s\n' "$2"
  return 0
}
bad() {
  printf '  MISSING  %s\n' "$1"
  missing=1
  [ -n "${2:-}" ] && hints+=("$2")
}

echo "Codify doctor — what 'make ci' needs, checked on this machine"
echo

echo "Python (the engine and the gate)"
if have python3; then
  if run python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    ok "python3 $(run python3 -c 'import platform; print(platform.python_version())') (needs 3.10 or newer)"
  else
    bad "python3 is older than 3.10" "Install Python 3.10 or newer; the engine is booted as 'python3 -m engine'."
  fi
  # Debian and Ubuntu ship venv separately, and a bare `python3 -m venv` then fails with
  # a message about ensurepip that does not say which package to install.
  if run python3 -c 'import ensurepip, venv' 2>/dev/null; then
    ok "python3 can create a virtual environment (venv + ensurepip)"
  else
    bad "python3 cannot create a virtual environment" "On Debian/Ubuntu: sudo apt install python3-venv"
  fi
else
  bad "python3 is not installed" "Install Python 3.10 or newer."
fi
if have uv || have python3.10; then
  ok "a Python 3.10 for the declared-minimum leg ($(have uv && echo uv || echo python3.10))"
else
  bad "neither uv nor python3.10 — 'make ci' cannot provision its 3.10 leg" \
    "Install uv (https://docs.astral.sh/uv/) or python3.10; the floor leg never skips."
fi
echo

# The machine being ready and this checkout's environment being current are different
# questions. A `.venv` made before a dev dependency was added keeps working, and keeps
# warning (Starlette's TestClient says to install `httpx2`), and nothing said why. The
# check runs in the checkout's own interpreter, the one the Makefile puts first on PATH,
# and never reads DOCTOR_TOOL_PATH: it is about this checkout, not about what is installed
# elsewhere.
echo "This checkout's virtual environment"
venv_py="$root/.venv/bin/python3"
if [ -e "$root/.venv" ]; then
  gaps="$("$venv_py" "$root/scripts/venv_gaps.py" "$root/pyproject.toml" 2>&1)"
  case "$?" in
    0) ok ".venv has every dependency pyproject.toml declares" ;;
    1) bad ".venv is missing: ${gaps//$'\n'/, }" \
         "make setup   (installs into the existing .venv; one made before a dependency was added does not have it)" ;;
    *) bad ".venv could not be checked: ${gaps:-its python3 did not run}" \
         "make setup   (or delete .venv and run it again if its python3 is gone)" ;;
  esac
else
  ok "no .venv in this checkout (fine if you install into an environment of your own; 'make setup' makes one)"
fi
echo

echo "Node (the UI)"
if have node && have npm; then
  if run node "$root/ui/scripts/check-node.mjs" >/dev/null 2>&1; then
    ok "node $(run node --version) and npm $(run npm --version)"
  else
    bad "node $(run node --version) is outside the range the UI tests support" \
      "Use Node 22.22.2 or newer 22, 24.15 or newer 24, or 26+ (jsdom 30's own range)."
  fi
else
  bad "node and/or npm not installed" "Install Node 22.22.2+, 24.15+ or 26+ (which brings npm)."
fi
echo

echo "Rust and the desktop shell's system libraries"
if have cargo && have rustc; then
  ok "$(run rustc --version)"
  # `make check-tauri` ends in `cargo fmt --check`, and rustup's *minimal* profile (and some distro
  # packages) leave rustfmt out: the build and the tests then pass and the gate fails at its last step,
  # after the long part. Found by running `make ci` as a fresh user from the README alone.
  if run cargo fmt --version >/dev/null 2>&1; then
    ok "rustfmt ($(run cargo fmt --version 2>/dev/null))"
  else
    bad "rustfmt is not installed for this Rust toolchain ('make check-tauri' runs cargo fmt --check)" \
      "Run: rustup component add rustfmt   (rustup's default profile includes it; --profile minimal does not)"
  fi
else
  bad "cargo/rustc not installed" "Install Rust with rustup: https://rustup.rs"
fi
if have pkg-config; then
  ok "pkg-config"
  libs_missing=()
  for lib in webkit2gtk-4.1 gtk+-3.0 libsoup-3.0 librsvg-2.0 openssl; do
    if run pkg-config --exists "$lib"; then ok "$lib"; else libs_missing+=("$lib"); fi
  done
  if [ "${#libs_missing[@]}" -gt 0 ]; then
    bad "system libraries not found by pkg-config: ${libs_missing[*]}" \
"Debian/Ubuntu (verified): sudo apt install build-essential pkg-config libwebkit2gtk-4.1-dev libgtk-3-dev libsoup-3.0-dev librsvg2-dev libssl-dev
  Fedora (per Tauri's docs, not verified here): sudo dnf install gcc pkgconf-pkg-config webkit2gtk4.1-devel gtk3-devel libsoup3-devel librsvg2-devel openssl-devel
  Arch (per Tauri's docs, not verified here): sudo pacman -S base-devel webkit2gtk-4.1 gtk3 libsoup3 librsvg openssl"
  fi
else
  bad "pkg-config is not installed" "Debian/Ubuntu: sudo apt install build-essential pkg-config"
fi
echo

echo "A display (one Rust test builds real GTK widgets and says so loudly without one)"
if [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
  ok "a display is available (${DISPLAY:-$WAYLAND_DISPLAY})"
elif have xvfb-run; then
  ok "no display, but xvfb-run is installed — 'make check-tauri' will use it"
else
  bad "no display and no xvfb-run" "Debian/Ubuntu: sudo apt install xvfb   (Fedora: xorg-x11-server-Xvfb, Arch: xorg-server-xvfb)"
fi
echo

echo "Machine tab (bubblewrap: 'make ci' needs it, because the machine's containment tests build a real jail)"
if have bwrap; then
  ok "bwrap ($(run bwrap --version 2>/dev/null))"
  # Asking is the only honest test: a kernel switch (kernel.unprivileged_userns_clone, user.max_user_namespaces)
  # or an AppArmor policy decides whether a jail can be made, and the sysctls cannot see the second.
  if run bwrap --unshare-all --ro-bind / / --dev /dev --proc /proc true >/dev/null 2>&1; then
    ok "bwrap can build a jail here (user namespaces are available to it)"
  else
    bad "bwrap is installed but cannot build a jail here" \
"User namespaces are refused to it. Check 'sysctl user.max_user_namespaces' (0 disables them), Debian's
  'sysctl kernel.unprivileged_userns_clone' (0 disables them for non-root), and on Ubuntu 24.04+ the AppArmor
  restriction 'sysctl kernel.apparmor_restrict_unprivileged_userns'. Run: bwrap --unshare-all --ro-bind / / true   to see its own message."
  fi
else
  bad "bwrap (bubblewrap) is not installed, and the machine tab cannot be isolated without it" \
    "Arch/CachyOS: sudo pacman -S bubblewrap   Debian/Ubuntu: sudo apt install bubblewrap   Fedora: sudo dnf install bubblewrap  (WebKitGTK's own sandbox uses it too)"
fi
# A machine's own copy of the project needs an unprivileged overlay mount (docs/09 section 14.1a). It is a feature and not a
# requirement: where it cannot be made the machine is read-only and says so, so this is a note and never a failure. The
# asking is the same as the app's: a throwaway project, a tmpfs and an overlay in a user namespace of its own, and a
# directory deleted and made again, which is the part a filesystem without user.* attributes (a tmpfs before Linux 6.6) cannot do.
if have unshare; then
  layer_dir="$(mktemp -d 2>/dev/null)" || layer_dir=""
  layer_ok=""
  if [ -n "$layer_dir" ] && mkdir "$layer_dir/lower" "$layer_dir/stage" && mkdir "$layer_dir/lower/d"; then
    if run unshare --user --map-root-user --mount -- sh -c \
      'mount -t tmpfs tmpfs "$1" && mkdir "$1/u" "$1/w" "$1/m" && mount -t overlay overlay -o "userxattr,lowerdir=$2,upperdir=$1/u,workdir=$1/w" "$1/m" && rm -r "$1/m/d" && mkdir "$1/m/d"' \
      sh "$layer_dir/stage" "$layer_dir/lower" >/dev/null 2>&1; then
      layer_ok=1
    fi
  fi
  [ -n "$layer_dir" ] && { rmdir "$layer_dir/lower/d" "$layer_dir/lower" "$layer_dir/stage" "$layer_dir" 2>/dev/null || true; }
  if [ -n "$layer_ok" ]; then
    ok "an unprivileged overlay mount works here: a machine edits its own copy of the project"
  else
    note "an unprivileged overlay mount does not work here, so a machine's project will be read-only" \
      "Not a failure. It needs a kernel that allows an overlay in a user namespace (Linux 5.11+) and, on a tmpfs, Linux 6.6+ (user.* attributes)."
  fi
else
  note "unshare (util-linux) not found, so a machine's project will be read-only" \
    "Not a failure. Arch/CachyOS: util-linux   Debian/Ubuntu: util-linux   Fedora: util-linux"
fi
echo

echo "Git"
if have git; then ok "$(run git --version)"; else bad "git is not installed" "Install git."; fi
echo

echo "Voice (optional: the app's mic button; 'make ci' does not need it)"
if have pw-record && have pw-dump; then
  ok "PipeWire's pw-record and pw-dump — the mic button can record"
else
  note "pw-record/pw-dump not found: the app runs, but the mic button cannot record" \
    "For dictation install PipeWire's tools — Arch/CachyOS: pipewire, Debian/Ubuntu: pipewire-bin, Fedora: pipewire-utils (package names not verified here)"
fi
echo

if [ "$missing" -eq 0 ]; then
  echo "Everything 'make ci' needs is here. Next: make setup && make ci"
  exit 0
fi

echo "Something is missing. How to fix it:"
for hint in "${hints[@]}"; do
  printf '  - %s\n' "$hint"
done
exit 1
