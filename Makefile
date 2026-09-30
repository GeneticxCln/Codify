.PHONY: help test test-engine test-streams smoke-embed test-ui typecheck-ui-tests lint typecheck build-ui dev-ui check-tauri build-tauri build-app install-local uninstall-local run-app dev-app run-engine run-engine-preview run-engine-scratch setup doctor check ci ci-report ci-python-floor check-history hooks clean bench bench-smoke

# A checkout's own virtualenv (`make setup` makes one) wins over whatever `python3` is on
# PATH, so `make setup && make check` works with nothing activated. The floor leg sets
# CODIFY_FLOOR_LEG so its own interpreter, which the recipe below puts first on PATH, is
# not displaced by this: a floor that quietly ran on the host Python would be the gate
# dropping its oldest leg without saying so.
ifneq ($(wildcard .venv/bin/python3),)
ifndef CODIFY_FLOOR_LEG
export PATH := $(CURDIR)/.venv/bin:$(PATH)
PREFER_VENV := 1
endif
endif

# mypy is a dev tool, installed like ruff (`pip install mypy` or `pip install -e ".[dev]"`);
# when it is only in the project venv, fall back to that so `make check` works unactivated.
#
# Looked up with the venv put first *inside* the $(shell): GNU make 4.3 runs $(shell) with the
# environment make started in, not the PATH exported above, so without this a global mypy (a
# `uv tool` or pipx one, which has no pydantic for the plugin) shadowed the venv's and failed
# `make typecheck` on a clean clone.
MYPY ?= $(shell $(if $(PREFER_VENV),PATH="$(CURDIR)/.venv/bin:$$PATH" ,)command -v mypy 2>/dev/null || echo .venv/bin/mypy)

# The declared minimum, read from the one file that declares it. `requires-python` is a
# deployment contract here — the desktop shell boots the engine as `python3 -m engine` —
# and the floor is the only leg that rejects syntax the newest interpreter accepts.
PY_MIN := $(shell sed -n 's/^requires-python *= *">= *\([0-9][0-9.]*\)".*/\1/p' pyproject.toml)
# Outside the repository on purpose: `make ci` must leave no untracked state behind.
CI_CACHE ?= $(if $(XDG_CACHE_HOME),$(XDG_CACHE_HOME),$(HOME)/.cache)/codify
PY_MIN_VENV := $(CI_CACHE)/venv-$(PY_MIN)

# Where a scratch engine keeps its state. Nothing under the real ~/.codify is opened.
SCRATCH_HOME ?= /tmp/codify-scratch

help:
	@echo "Codify Development Commands:"
	@echo "  make setup        - From a fresh clone: create .venv, install the engine and dev tools, npm ci"
	@echo "  make doctor       - Check this machine for everything make ci needs, and say how to install what is missing"
	@echo "  make test         - Run full Python test suite (includes the concurrency/stream tests)"
	@echo "  make test-streams - Run the concurrency/stream-isolation tests explicitly, by name"
	@echo "  make smoke-embed  - Run the embedded-browser first-paint smoke test (needs a display)"
	@echo "  make smoke-tabs   - Run the tab-restoration smoke test (needs a display; never touches ~/.codify)"
	@echo "  make test-ui      - Run the React/TypeScript unit tests (node --test, needs Node 22.22.2+, 24.15+ or 26+)"
	@echo "  make typecheck-ui-tests - Type-check the React/TypeScript test suite (tsc over src+tests)"
	@echo "  make lint         - Lint engine, tests and scripts with ruff (rules pinned in pyproject.toml)"
	@echo "  make typecheck    - Static-type-check engine, tests and scripts with mypy (config in pyproject.toml)"
	@echo "  make build-ui     - Typecheck and build React frontend (Vite)"
	@echo "  make dev-ui       - Start Vite dev server"
	@echo "  make check-tauri  - Cargo check Tauri Rust backend"
	@echo "  make build-tauri  - Build Tauri desktop application"
	@echo "  make run-app      - Build the UI and launch the desktop app with it embedded (the way to open Codify from a checkout)"
	@echo "  make build-app    - Build the release app (UI embedded) that scripts/codify and the menu entry start"
	@echo "  make install-local - build-app, then add a 'codify' command and an applications-menu entry under ~/.local (no root)"
	@echo "  make uninstall-local - Remove exactly what install-local wrote"
	@echo "  make dev-app      - Launch the desktop app against the Vite dev server, with hot reload (needs cargo-tauri)"
	@echo "  make run-engine   - Start Codify Python engine standalone"
	@echo "  make run-engine-preview - Start the engine and print the token/port for the browser preview"
	@echo "  make run-engine-scratch - Start an engine isolated under $(SCRATCH_HOME) (real ~/.codify untouched)"
	@echo "  make bench-smoke  - Run the hermetic benchmark tier (no network, no models, no spend)"
	@echo "  make bench        - Run the repo_scale tier against your configured models (spends tokens)"
	@echo "  make check        - Run all verifications (ruff + UI tests + Python tests + stream tests + UI build + Tauri check)"
	@echo "  make ci           - Run the whole CI gate locally: make check plus the declared $(PY_MIN) leg"
	@echo "  make check-history - Check that every commit in HISTORY_RANGE builds, not just the tip (default: origin/\$$branch..HEAD)"
	@echo "  make ci-python-floor - Run only the $(PY_MIN) leg (uv or a system python$(PY_MIN) covers it)"
	@echo "  make ci-report    - make ci, then publish the verdict on the commit as a GitHub status (needs gh; clean tree; pushed commit)"
	@echo "  make hooks        - Install the git hooks (pre-commit: lint+typecheck, pre-push: 'make ci')"
	@echo "  make clean        - Remove caches and build artifacts"
	@echo ""
	@echo "Prerequisites: engine needs \`pip install -r engine/requirements.txt\` (plus \`pip install ruff mypy\`"
	@echo "for the checks, or one \`pip install -e \".[dev]\"\`); the ui targets need \`npm install\` in ui/,"
	@echo "and \`make test-ui\` needs Node 22.22.2+, 24.15+ or 26+."
	@echo "\`make ci\` additionally needs a python$(PY_MIN) or uv, so its floor leg runs instead of skipping."

# From a fresh clone to a checkout `make check` can run in. A virtualenv rather than the
# system Python, because a modern distribution refuses `pip install` into it (PEP 668) —
# which is the README's old first command failing on stock Ubuntu. The Makefile puts
# ./.venv/bin first on PATH once it exists (above), and the desktop shell looks for the
# same interpreter, so this is the one place a checkout's Python is decided.
setup:
	python3 -m venv .venv
	.venv/bin/python3 -m pip install -e ".[dev]"
	cd ui && npm ci
	@echo ""
	@echo "Set up. Next: make doctor (what is still missing), then make check."

# Read-only: what this machine has and what `make ci` needs, with the fix for each gap.
doctor:
	scripts/doctor.sh

test: test-engine

# Through `scripts/run_tests.py`, not `python3 -m unittest` directly, and the
# wrapper is the point rather than the ceremony: it fails the leg when the test
# process exits 0 without printing unittest's summary. A suite cut short by
# `os._exit` — which is what `engine.app.main` used to do to its own test runner,
# silently, halfway through — exits 0, so the exit code alone called it a pass
# and half the engine suite went unrun while the gate stayed green. See the
# module's docstring. It also fails a run that passed but leaked a database
# connection: Python 3.13+ reports one as a ResourceWarning that scrolls past an OK.
test-engine:
	python3 scripts/run_tests.py discover -s tests -p "test_*.py" -v

# The discovery pattern above already collects these two modules (and the
# stream_isolation.py helper they import). Running them again by name is
# deliberate: it keeps the isolation guarantees in `make check` even if the
# discovery pattern is ever narrowed, a module is renamed, or the helper is
# dropped from the import graph — exactly the slow drifts a pattern-based
# inclusion cannot catch.
test-streams:
	python3 scripts/run_tests.py tests.test_concurrent_streams tests.test_concurrent_streams_ws -v

# The embedded browser's smoke test, and the one claim no unit test can make:
# that a real page paints a real first frame on a real Linux session. It seats
# a page via CODEIFY_EMBED_SMOKE (the shell's own mode — it never starts an
# engine), relays the boot diagnostics that say which first-paint fact holds
# (session bus, WebKit sandbox, DMABUF, display backend), and passes only on
# the paint line the page emits after two composited animation frames.
# Deliberately a local target, not part of `check`: it needs a display, and
# the Actions workflow is manual-only anyway (see check.yml).
# SMOKE_EMBED_ARGS passes the script's own flags through, because a target
# that cannot take `--rebuild` or `--url` is a target people work around:
#   make smoke-embed SMOKE_EMBED_ARGS=--rebuild
#   make smoke-embed SMOKE_EMBED_ARGS="--url https://example.org/ --timeout 60"
smoke-embed:
	python3 scripts/embed_smoke.py $(SMOKE_EMBED_ARGS)

# The tab-restoration smoke: builds the shell, boots it with the engine, seeds
# the strip, and proves the window rehydrated it. Needs a display and is
# therefore not part of check/ci — same reasoning as smoke-embed. Never touches
# the developer's ~/.codify: the whole run is under a throwaway CODIFY_HOME.
#
#   make smoke-tabs SMOKE_TABS_ARGS=--rebuild
#   make smoke-tabs SMOKE_TABS_ARGS="--timeout 150"
smoke-tabs:
	python3 scripts/tabs_smoke.py $(SMOKE_TABS_ARGS)

# `npm run build` typechecks and bundles; it does not *execute* the tests in
# ui/tests/, which assert the client-side rules the chat depends on (goal stream
# framing, model ordering, failure diagnosis, stats-history merging). They run
# directly through `node --test` with type stripping, so they need no build step
# and no bundler — but that flag landed in Node 22.6, and 22.6 is not the suite's floor:
# the loader needs module.registerHooks (22.15) and jsdom 30 declares 22.22.2 / 24.15 / 26.
# `npm test` checks that before it runs (ui/scripts/check-node.mjs), so an older Node is
# told so in one sentence instead of failing 161 tests with module-linking errors.
#
# The suite runs under a hard memory cap when systemd can give one. A leaking test
# once grew to ~25 GB and had systemd-oomd kill the whole desktop session, not just
# the test run; inside a cgroup limit the kernel kills only this run. Swap is off
# for the scope so a runaway is stopped at the cap instead of paging the machine
# to a halt first. Where there is no user systemd (a container, a minimal CI image) the guard is empty
# and the target is exactly `npm test`.
UI_TEST_GUARD := $(shell systemd-run --user --scope --quiet true >/dev/null 2>&1 && echo 'systemd-run --user --scope --quiet -p MemoryMax=6G -p MemorySwapMax=0 --')

test-ui:
	cd ui && $(UI_TEST_GUARD) npm test

# The companion to test-ui, and the one that was missing. `tsconfig.json` has
# "include": ["src"], so every `tsc` in this repository — `npm run build`, and
# therefore `make build-ui` — never read a line of `ui/tests/`. And `node --test`
# cannot close the gap: `--experimental-strip-types` *erases* types, so a suite
# that builds a value the interface no longer accepts, or calls a component with
# the wrong props, still passes. That is not hypothetical drift; it is what had
# happened, and `make ci` said nothing.
#
# This is the mypy leg for the TypeScript half, same argument as the one above
# it: the runtime check and the static check are not substitutes. The config is
# `ui/tsconfig.test.json`, which extends the app's own so `strict` and
# `noUnusedLocals` apply to tests exactly as they do to `src/`.
typecheck-ui-tests:
	cd ui && npm run typecheck:tests

# Ruff's rule selection is pinned in pyproject.toml (target-version = the declared
# minimum Python), so this is the same check on every machine and in CI. It covers
# tests/ and scripts/ too: a dead local in a test is a lost assertion, and this
# project's whole bar is what the suite proves.
lint:
	ruff check engine tests scripts benchmarks

# Same scope as lint (engine, tests, scripts) — ruff sees the syntax and the dead
# names, mypy sees the annotated-but-wrong types neither it nor the tests catch
# (an annotation like `tuple[int, callable]` names a *builtin*, not the type, and
# no runtime check fires on it). Config lives in pyproject.toml: the 3.10 floor,
# the pydantic plugin, and the same engine+tests+scripts file set.
typecheck:
	$(MYPY)

build-ui:
	cd ui && npm run build

dev-ui:
	cd ui && npm run dev

# One Rust test builds real GTK widgets and fails loudly without a display, which is
# right — no test may skip itself out of a guarantee — and which made `make check` fail
# on every headless machine (CI, a container, an SSH session) for a reason the target
# never mentioned. So without a display it runs the tests under Xvfb, when there is one.
# With neither, the test fails and the line below says why, rather than leaving a GTK
# initialisation error to explain itself.
XVFB := $(if $(or $(DISPLAY),$(WAYLAND_DISPLAY)),,$(shell command -v xvfb-run >/dev/null 2>&1 && echo "xvfb-run -a"))

check-tauri:
	cd src-tauri && cargo check
	# The shell's pure pieces — handshake parsing, the project-root guess, role
	# validation — live in src/engine_protocol.rs exactly so this can reach them. The
	# rest of the crate (spawn, read, kill) needs a live process and is not unit-testable.
	@if [ -z "$(DISPLAY)$(WAYLAND_DISPLAY)" ] && [ -z "$(XVFB)" ]; then \
	  echo "check-tauri: no display and no xvfb-run, so the one GTK test will fail — run 'make doctor'" >&2; \
	fi
	cd src-tauri && $(XVFB) cargo test
	cd src-tauri && cargo fmt --check

# NOTE: this only compiles the Rust crate (`cargo build`); Codify ships no installer or
# package (`bundle.active` is false in tauri.conf.json), and there is nothing here to
# produce one. A plain `cargo build` is a *dev* build: the window loads
# http://localhost:5173 and is blank without a Vite server. Fine for the smoke scripts,
# which bring their own page; not something to open by hand. Use `run-app` for that.
build-tauri:
	cd src-tauri && cargo build

# The desktop app with `ui/dist` embedded (the `custom-protocol` feature), so it
# needs no dev server. The UI is rebuilt first, and the Rust build runs under a
# memory cap where systemd can give one, for the reason UI_TEST_GUARD gives: a
# compile that runs away should end that compile, not the desktop session. The app
# itself runs uncapped.
BUILD_GUARD := $(shell systemd-run --user --scope --quiet true >/dev/null 2>&1 && echo 'systemd-run --user --scope --quiet -p MemoryMax=8G -p MemorySwapMax=0 --')

run-app: build-ui
	cd src-tauri && $(BUILD_GUARD) cargo build --features custom-protocol
	cd src-tauri && cargo run --features custom-protocol

# Hot reload: `cargo tauri dev` starts Vite (beforeDevCommand) and points the window at it.
dev-app:
	cd src-tauri && cargo tauri dev

# The build `scripts/codify` starts: release profile, UI embedded (`custom-protocol`),
# under the same memory cap as `run-app`. `bundle.active` is false, so this is a binary
# at src-tauri/target/release/codify-desktop and nothing else.
build-app: build-ui
	cd src-tauri && $(BUILD_GUARD) cargo build --release --features custom-protocol

# Linux's answer to "no installer": a `codify` command and a menu entry, both under the
# user's own ~/.local, both pointing back at this checkout. See scripts/install-local.sh.
install-local: build-app
	scripts/install-local.sh install

uninstall-local:
	scripts/install-local.sh uninstall

run-engine:
	python3 -m engine

# The browser preview's chore, done once: start the engine, wait for the boot
# handshake, print the port and token as paste-ready localStorage assignments
# (also written to /tmp/codify-engine-preview.env, 0600). Stays in the
# foreground — the engine's parent watchdog makes this process its lease on
# life — and Ctrl-C stops the engine. The logic and its tests live in
# scripts/preview_engine.py and tests/test_preview_engine.py; this target is
# the one-line wiring, like run-engine.
run-engine-preview:
	python3 scripts/preview_engine.py

# Smoke tests, screenshots and verification runs: one home override moves both the
# database and the secrets file, and disables the OS keychain for this process, so
# this run cannot read or write the developer's real credentials.
run-engine-scratch:
	@echo "Isolated engine: all state under $(SCRATCH_HOME)"
	CODIFY_HOME=$(SCRATCH_HOME) python3 -m engine

# The fastest checks first, so the cheap failure is the one you read. These are the
# same targets the CI workflow names, split by toolchain (.github/workflows/check.yml),
# but only on the host interpreter.
# Benchmarks stay out of `check` and `ci` on purpose. The smoke tier is hermetic,
# but the configured tier spends real tokens on real models, and a gate that costs
# money on every push is a gate people learn to bypass. Run these deliberately.
bench:
	python3 -m benchmarks.runner --tier repo_scale

bench-smoke:
	python3 -m benchmarks.runner --tier smoke

check: lint typecheck test-ui typecheck-ui-tests test test-streams build-ui check-tauri
	@echo "All verifications passed successfully!"

# The declared-minimum leg. No machine is guaranteed a python $(PY_MIN), so this brings
# one up (uv when present, a system python$(PY_MIN) otherwise) rather than skipping: a gate
# that quietly drops its oldest leg is exactly how a PEP 701 f-string shipped from a 3.14
# laptop and died on the floor. The targets are the CI job's command list, unchanged.
ci-python-floor:
	scripts/ci-python-floor.sh $(PY_MIN) $(PY_MIN_VENV)
	@echo "==> python $(PY_MIN) leg"
	PATH=$(PY_MIN_VENV)/bin:$$PATH CODIFY_FLOOR_LEG=1 $(MAKE) --no-print-directory lint typecheck test test-streams

# The whole gate, locally, in one command: every leg CI covered — the host interpreter,
# the declared minimum, the UI and the Rust shell — with the floor provisioned on demand
# instead of assumed. The Actions workflow is manual-only (see the note in check.yml), so this
# is the gate; run it before calling a change done.
ci: ci-python-floor check
	@echo ""
	@echo "CI gate passed locally: python $(PY_MIN) (provisioned) + host + ui + rust."

# `make ci`, then say so on the commit. With the Actions workflow manual-only there is no check on a
# pull request unless someone starts it; this publishes the local verdict as a commit status (context
# local/make-ci) using the GitHub CLI, which is free because a status is an API call and
# not a workflow run. It refuses a dirty tree and needs the commit pushed — see the
# header of scripts/ci-report.sh for why each is a rule and not a courtesy.
ci-report:
	scripts/ci-report.sh

# A different question, so a separate target and deliberately NOT part of `check` or
# `ci`: does every commit in this range build, or only the one at the tip? `make ci`
# cannot answer that, and the gap is not academic — this repository's UI tests read
# source text as contract, so a commit that adds a component and a test asserting the
# shell mounts it is red until the commit that mounts it. Split one change into "the
# parts" and "the wiring" and you get a history that reads well and cannot be bisected.
#
# The range defaults to what a push would carry. A one-commit range is reported and
# skipped rather than checked: it has no middle, and `make ci` already gates the tip.
# That skip is what makes this affordable enough to run at pre-push at all — the case
# where a red middle is actually possible is the case where the check is worth minutes.
HISTORY_RANGE ?= $(shell git rev-parse --abbrev-ref HEAD 2>/dev/null | sed 's|^|origin/|')..HEAD
HISTORY_FLAGS ?=

check-history:
	python3 scripts/check_history.py --range '$(HISTORY_RANGE)' $(HISTORY_FLAGS)

# Install the versioned hooks: pre-commit runs the cheap legs (lint, typecheck) so the
# obvious failure lands in your hands rather than two minutes into a push, and pre-push
# runs the whole gate ('make ci'). A hook that lives only in .git/hooks is invisible to
# everyone else and to every fresh clone, which is why they are checked in under
# .githooks/ with the one-line install right here: `core.hooksPath` is the whole
# install, and `git config --unset core.hooksPath` is the whole uninstall. Neither hook
# skips a leg to be convenient — `--no-verify` is the deliberate override.
#
# The chmod is not decoration: git ignores a hook it cannot execute, and a fresh
# clone on a checkout without the mode bit (an archive, some filesystems) is exactly where
# a gate would go quiet while looking installed.
hooks:
	git config core.hooksPath .githooks
	chmod +x .githooks/*
	@echo "Hooks installed: pre-commit runs 'make lint typecheck', pre-push runs 'make ci'."
	@echo "(--no-verify bypasses either one: git commit --no-verify, git push --no-verify.)"

# Removes build artifacts and tool caches only. graphify-out's tracked entries were
# untracked from the index (kept on disk, ignored by .gitignore), so `clean` no longer
# deletes anything version-controlled — it may clear the directory's caches wholesale.
clean:
	rm -rf ui/dist src-tauri/target __pycache__ engine/__pycache__ tests/__pycache__ .pytest_cache .ruff_cache .mypy_cache graphify-out/cache coverage
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
