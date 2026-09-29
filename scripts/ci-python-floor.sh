#!/usr/bin/env bash
# Build the venv `make ci` runs the declared-minimum Python leg in.
#
# `requires-python = ">=3.10"` in pyproject.toml is a deployment contract, not a
# formality: the desktop shell boots the engine as `python3 -m engine`, and the floor
# is the only interpreter that rejects syntax the newest interpreter parses happily.
# That is not hypothetical — a PEP 701 f-string shipped from a 3.14 laptop and was a
# `SyntaxError` on 3.10, in a module the suite could not even import.
#
# CI covered the floor with a matrix. With that unavailable this leg is the most
# valuable part of the gate, so this script never skips and never silently passes: it
# uses a `python<version>` already on PATH, fetches one with `uv` when the machine has
# none, and fails loudly when it can neither.
#
# Idempotent: an existing venv is reused while it still matches the pins it was built
# from, so provisioning is paid once per dependency change rather than once per run.
# Nothing is written inside the repository — the venv and uv's own caches live under
# the caller's cache directory (CI_CACHE in the Makefile).
#
# Usage: scripts/ci-python-floor.sh <version> <venv-dir>
# The venv path must end in -<version>, and the rebuild below deletes only a
# directory this script owns: one carrying its .ci-python-floor-stamp, or one under
# the default CI cache root the Makefile provisions. The suffix alone once carried
# the whole guarantee, and it does not — a hand-typed
# `ci-python-floor.sh 3.10 ~/proj-backup-3.10` satisfies it on the way to deleting
# the backup. Anything without that provenance is refused with exit 2, never
# deleted.

set -euo pipefail

usage="usage: $(basename "$0") <python-version> <venv-dir>"
version="${1:?$usage}"
venv="${2:?$usage}"

case "$version" in
  [0-9]*.[0-9]*) ;;
  *) echo "ci-python-floor: '$version' is not a version like 3.10" >&2; exit 2 ;;
esac
case "${venv%/}" in
  *-"$version") ;;
  *) echo "ci-python-floor: refusing to manage '${venv%/}': the path must end in -$version" >&2; exit 2 ;;
esac

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
requirements="$root/engine/requirements.txt"
[ -f "$requirements" ] || { echo "ci-python-floor: no $requirements" >&2; exit 2; }

# What `make lint` and `make typecheck` need on PATH. Not engine runtime dependencies:
# the CI job installed them separately, and pyproject.toml's `dev` extra pins the same
# floors.
tools=( "ruff>=0.6" "mypy>=1.11" "httpx2>=2.13,<3" )

python="$venv/bin/python"
stamp_file="$venv/.ci-python-floor-stamp"
# Where the Makefile's default CI_CACHE lives, derived the same way it derives it
# (a set-and-non-empty XDG_CACHE_HOME wins over $HOME/.cache). An explicitly
# overridden CI_CACHE needs no exception here: a venv the script built at an
# overridden location carries the stamp, which is the first check below.
cache_root="${XDG_CACHE_HOME:-$HOME/.cache}/codify"
# A checksum of the pins, not a timestamp: a fresh checkout rewrites mtimes without
# changing what is installed, and a genuinely changed requirements file must reinstall.
want="$( { cat "$requirements"; printf '%s\n' "${tools[@]}"; } | cksum )"

have_version() {
  [ -x "$python" ] || return 1
  [ "$("$python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" = "$version" ]
}

# The rebuild is the one destructive thing this script does, so it asks first
# whether the directory is its to delete. A stamp means this script finished a
# build here (the stamp is written only after the installs succeed); the default
# cache root covers a build interrupted before the stamp was written. A
# directory carrying neither — a backup that merely ends in -$version, say — is
# refused rather than deleted: a gate that guesses wrong here does not get a
# second guess.
owned_by_this_script() {
  [ -f "$stamp_file" ] && return 0
  case "${venv%/}" in
    "$cache_root"/*) return 0 ;;
  esac
  return 1
}

if [ -d "$venv" ] && ! have_version; then
  if owned_by_this_script; then
    echo "==> python $version: replacing $venv (not a working python $version)"
    rm -rf "$venv"
  else
    echo "ci-python-floor: refusing to replace '${venv%/}': no working python $version in it, no stamp of this script's on it, and it is not under $cache_root — remove it by hand if it is really a venv of yours" >&2
    exit 2
  fi
fi

if have_version && [ -f "$stamp_file" ] && [ "$(cat "$stamp_file")" = "$want" ]; then
  echo "==> python $version: reusing $venv ($("$python" -V 2>&1))"
  exit 0
fi

# Prefer uv: it uses a system $version when there is one and downloads it when there
# is not, so both cases take one path.
uv_bin=""
if command -v uv >/dev/null 2>&1; then
  uv_bin="$(command -v uv)"
fi
base=""
if command -v "python$version" >/dev/null 2>&1; then
  base="$(command -v "python$version")"
fi

if [ -z "$uv_bin" ] && [ -z "$base" ]; then
  cat >&2 <<EOF
ci-python-floor: no python$version on PATH and no uv to fetch one.

`make ci` does not drop this leg. Python $version is the declared minimum and the only
leg that rejects syntax the newer interpreters accept. Install either:

  * python$version — e.g. \`apt install python$version\`, or \`pyenv install $version\`, or
  * uv — https://docs.astral.sh/uv/, which downloads the interpreter on demand.

Set CI_CACHE to put the venv somewhere other than the default cache directory.
EOF
  exit 1
fi

cache="$(dirname "$venv")"
mkdir -p "$cache"

if [ -n "$uv_bin" ]; then
  # UV_* are redirected beside the venv so this gate's footprint stays out of the
  # repository and out of the user's global uv state.
  export UV_PYTHON_INSTALL_DIR="$cache/uv-python"
  export UV_CACHE_DIR="$cache/uv-cache"
  "$uv_bin" venv --python "$version" --allow-existing "$venv"
  "$uv_bin" pip install --python "$python" -r "$requirements" "${tools[@]}"
else
  echo "==> python $version: no uv; building $venv from $base"
  "$base" -m venv "$venv"
  "$python" -m pip install --disable-pip-version-check --upgrade pip
  "$python" -m pip install --disable-pip-version-check -r "$requirements" "${tools[@]}"
fi

printf '%s\n' "$want" > "$stamp_file"
echo "==> python $version ready: $("$python" -V 2>&1) at $venv"
