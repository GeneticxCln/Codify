.PHONY: help test test-engine test-streams smoke-embed test-ui typecheck-ui-tests lint typecheck build-ui dev-ui check-tauri build-tauri run-engine run-engine-preview run-engine-scratch check ci ci-python-floor check-history hooks clean bench bench-smoke

# mypy is a dev tool, installed like ruff (`pip install mypy` or `pip install -e ".[dev]"`);
# when it is only in the project venv, fall back to that so `make check` works unactivated.
MYPY ?= $(shell command -v mypy 2>/dev/null || echo .venv/bin/mypy)

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
	@echo "  make test         - Run full Python test suite (includes the concurrency/stream tests)"
	@echo "  make test-streams - Run the concurrency/stream-isolation tests explicitly, by name"
	@echo "  make smoke-embed  - Run the embedded-browser first-paint smoke test (needs a display)"
	@echo "  make smoke-tabs   - Run the tab-restoration smoke test (needs a display; never touches ~/.codify)"
	@echo "  make test-ui      - Run the React/TypeScript unit tests (node --test, needs Node 22.6+)"
	@echo "  make typecheck-ui-tests - Type-check the React/TypeScript test suite (tsc over src+tests)"
	@echo "  make lint         - Lint engine, tests and scripts with ruff (rules pinned in pyproject.toml)"
	@echo "  make typecheck    - Static-type-check engine, tests and scripts with mypy (config in pyproject.toml)"
	@echo "  make build-ui     - Typecheck and build React frontend (Vite)"
	@echo "  make dev-ui       - Start Vite dev server"
	@echo "  make check-tauri  - Cargo check Tauri Rust backend"
	@echo "  make build-tauri  - Build Tauri desktop application"
	@echo "  make run-engine   - Start Codify Python engine standalone"
	@echo "  make run-engine-preview - Start the engine and print the token/port for the browser preview"
	@echo "  make run-engine-scratch - Start an engine isolated under $(SCRATCH_HOME) (real ~/.codify untouched)"
	@echo "  make bench-smoke  - Run the hermetic benchmark tier (no network, no models, no spend)"
	@echo "  make bench        - Run the repo_scale tier against your configured models (spends tokens)"
	@echo "  make check        - Run all verifications (ruff + UI tests + Python tests + stream tests + UI build + Tauri check)"
	@echo "  make ci           - Run the whole CI gate locally: make check plus the declared $(PY_MIN) leg"
	@echo "  make check-history - Check that every commit in HISTORY_RANGE builds, not just the tip (default: origin/\$$branch..HEAD)"
	@echo "  make ci-python-floor - Run only the $(PY_MIN) leg (uv or a system python$(PY_MIN) covers it)"
	@echo "  make hooks        - Install the git hooks (pre-commit: lint+typecheck, pre-push: 'make ci')"
	@echo "  make clean        - Remove caches and build artifacts"
	@echo ""
	@echo "Prerequisites: engine needs \`pip install -r engine/requirements.txt\` (plus \`pip install ruff mypy\`"
	@echo "for the checks, or one \`pip install -e \".[dev]\"\`); the ui targets need \`npm install\` in ui/,"
	@echo "and \`make test-ui\` needs Node 22.6+."
	@echo "\`make ci\` additionally needs a python$(PY_MIN) or uv, so its floor leg runs instead of skipping."

test: test-engine

# Through `scripts/run_tests.py`, not `python3 -m unittest` directly, and the
# wrapper is the point rather than the ceremony: it fails the leg when the test
# process exits 0 without printing unittest's summary. A suite cut short by
# `os._exit` — which is what `engine.app.main` used to do to its own test runner,
# silently, halfway through — exits 0, so the exit code alone called it a pass
# and half the engine suite went unrun while the gate stayed green. See the
# module's docstring.
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
# GitHub Actions is unavailable to this repository anyway (see check.yml).
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
# and no bundler — but that flag landed in Node 22.6.
test-ui:
	cd ui && npm test

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

check-tauri:
	cd src-tauri && cargo check
	# The shell's pure pieces — handshake parsing, the project-root guess, role
	# validation — live in src/engine_protocol.rs exactly so this can reach them. The
	# rest of the crate (spawn, read, kill) needs a live process and is not unit-testable.
	cd src-tauri && cargo test
	cd src-tauri && cargo fmt --check

# NOTE: this only compiles the Rust lib (`cargo build`). It does NOT produce a
# desktop installer — that needs the Tauri CLI, which is not vendored in ui/
# (`npm i -D @tauri-apps/cli`, then `npx tauri build` from the repo root).
build-tauri:
	cd src-tauri && cargo build

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
	PATH=$(PY_MIN_VENV)/bin:$$PATH $(MAKE) --no-print-directory lint typecheck test test-streams

# The whole gate, locally, in one command: every leg CI covered — the host interpreter,
# the declared minimum, the UI and the Rust shell — with the floor provisioned on demand
# instead of assumed. GitHub Actions is unavailable (see the note in check.yml), so this
# is the gate; run it before calling a change done.
ci: ci-python-floor check
	@echo ""
	@echo "CI gate passed locally: python $(PY_MIN) (provisioned) + host + ui + rust."

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
# clone on a checkout without the mode bit (Windows) is exactly where a gate would
# go quiet while looking installed.
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
