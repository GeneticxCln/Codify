#!/usr/bin/env bash
# Run the local gate and publish its verdict on the commit as a GitHub status.
#
# The GitHub Actions workflow (.github/workflows/check.yml) gives a pull request its own
# check, from a clean runner, and it needs a runner and an account in good standing (it
# was billing-locked once). The gate is `make ci`, and it runs where the code is written.
# This script is the other half: it runs that gate and then says so on the commit, so a PR
# shows a green or a red mark that came from a machine that actually ran the suite. It costs nothing —
# a commit status is a plain API call, not a workflow run, so it needs no runner and
# no billing.
#
# Usage:   scripts/ci-report.sh [command...]        (default command: make ci)
#          make ci-report
#
# Needs the GitHub CLI (`gh`), logged in with permission to write commit statuses
# (a classic token's `repo:status`, or a fine-grained token's "Commit statuses:
# write"). Authentication is entirely `gh`'s: this script never reads, prints or
# stores a token.
#
# Exit status:
#   the command's own   the gate failed; a `failure` status was published
#   0                   the gate passed and a `success` status was published
#   2                   refused, and nothing was run or published (see below)
#   3                   the gate passed but the status could not be published
#
# Two refusals, and both exist because a status is a claim about a *commit*:
#
# - A dirty working tree. The suite runs against the files on disk, the status is
#   attached to what was committed, and the two differ the moment there is an edit
#   not in the commit. A green run of unsaved work must not turn a commit green.
# - A passing gate that changes the tree while it runs. Then the files it tested are
#   no longer the commit's either. `make ci` is meant to leave nothing behind; if it
#   ever does, that is a defect to be told about, not a `success` to be published.
#   (A *failing* gate is published as `failure` regardless: a red is always safe.)
#
# The commit has to be on GitHub already. The status API refuses a SHA it has not
# been sent, and says so; push first.
#
# Environment (for tests and for a different context name):
#   CI_REPORT_CONTEXT  the status name shown on the PR      (default: local/make-ci)
#   CI_REPORT_GH       the GitHub CLI to call               (default: gh)

set -uo pipefail

context="${CI_REPORT_CONTEXT:-local/make-ci}"
gh_bin="${CI_REPORT_GH:-gh}"

if [ "$#" -eq 0 ]; then
  set -- make ci
fi

# The repository the caller is standing in, not the one this file sits in: the
# verdict is about the tree the command will run against.
if ! root="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  echo "ci-report: not inside a git repository, so there is no commit to report on" >&2
  exit 2
fi
cd "$root" || exit 2

# Checked before the gate, not after it: the gate takes minutes, and finding out
# afterwards that the verdict has nowhere to go is the most expensive way to learn it.
if ! command -v "$gh_bin" >/dev/null 2>&1; then
  echo "ci-report: '$gh_bin' (the GitHub CLI) is not installed, so a verdict could not" >&2
  echo "           be published. Install it and run 'gh auth login', then retry." >&2
  echo "           Nothing was run." >&2
  exit 2
fi

dirty() {
  [ -n "$(git status --porcelain --untracked-files=normal)" ]
}

if dirty; then
  echo "ci-report: the working tree has uncommitted changes, so a status would describe" >&2
  echo "           files that are not the commit. Commit or stash them, then retry." >&2
  echo "           Nothing was run." >&2
  exit 2
fi

sha="$(git rev-parse HEAD)"
short="${sha:0:7}"

echo "ci-report: running '$*' on $short"
"$@"
rc=$?

if dirty; then
  if [ "$rc" -eq 0 ]; then
    echo "ci-report: '$*' passed but changed the working tree while it ran, so the result" >&2
    echo "           is not about commit $short. Nothing was published. What it left behind:" >&2
    git status --short >&2
    exit 2
  fi
  # A failure is safe to publish whatever the tree looks like: hiding a red because
  # the run also left files behind would be the worse mistake.
  echo "ci-report: note — '$*' also left changes in the working tree:" >&2
  git status --short >&2
fi

if [ "$rc" -eq 0 ]; then
  state="success"
  verb="passed"
else
  state="failure"
  verb="failed (exit $rc)"
fi

host="${HOSTNAME:-$(uname -n)}"
description="'$*' $verb on $host, $(date -u +%Y-%m-%dT%H:%MZ)"
# GitHub caps a status description at 140 characters and rejects a longer one.
description="${description:0:140}"

# `{owner}` and `{repo}` are placeholders `gh api` fills in from the repository it is
# run in, so this file names no account and works in a fork unchanged.
if ! "$gh_bin" api --method POST "repos/{owner}/{repo}/statuses/$sha" \
    -f "state=$state" \
    -f "context=$context" \
    -f "description=$description" >/dev/null; then
  echo "ci-report: '$*' $verb, but the status could not be published. Is $short pushed," >&2
  echo "           and is 'gh' allowed to write commit statuses on this repository?" >&2
  if [ "$rc" -ne 0 ]; then
    exit "$rc"
  fi
  exit 3
fi

echo "ci-report: published '$state' for $short as $context"
exit "$rc"
