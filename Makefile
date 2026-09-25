.PHONY: help test test-engine test-streams test-ui lint typecheck build-ui dev-ui check-tauri build-tauri run-engine run-engine-scratch check ci ci-python-floor hooks clean

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
	@echo "  make test-ui      - Run the React/TypeScript unit tests (node --test, needs Node 22.6+)"
	@echo "  make lint         - Lint engine, tests and scripts with ruff (rules pinned in pyproject.toml)"
	@echo "  make typecheck    - Static-type-check engine, tests and scripts with mypy (config in pyproject.toml)"
	@echo "  make build-ui     - Typecheck and build React frontend (Vite)"
	@echo "  make dev-ui       - Start Vite dev server"
	@echo "  make check-tauri  - Cargo check Tauri Rust backend"
	@echo "  make build-tauri  - Build Tauri desktop application"
	@echo "  make run-engine   - Start Codify Python engine standalone"
	@echo "  make run-engine-scratch - Start an engine isolated under $(SCRATCH_HOME) (real ~/.codify untouched)"
	@echo "  make check        - Run all verifications (ruff + UI tests + Python tests + stream tests + UI build + Tauri check)"
	@echo "  make ci           - Run the whole CI gate locally: make check plus the declared $(PY_MIN) leg"
	@echo "  make ci-python-floor - Run only the $(PY_MIN) leg (uv or a system python$(PY_MIN) covers it)"
	@echo "  make hooks        - Install the git hooks (pre-commit: lint+typecheck, pre-push: 'make ci')"
	@echo "  make clean        - Remove caches and build artifacts"
	@echo ""
	@echo "Prerequisites: engine needs \`pip install -r engine/requirements.txt\` (plus \`pip install ruff mypy\`"
	@echo "for the checks, or one \`pip install -e \".[dev]\"\`); the ui targets need \`npm install\` in ui/,"
	@echo "and \`make test-ui\` needs Node 22.6+."
	@echo "\`make ci\` additionally needs a python$(PY_MIN) or uv, so its floor leg runs instead of skipping."

test: test-engine

test-engine:
	python3 -m unittest discover -s tests -p "test_*.py" -v

# The discovery pattern above already collects these two modules (and the
# stream_isolation.py helper they import). Running them again by name is
# deliberate: it keeps the isolation guarantees in `make check` even if the
# discovery pattern is ever narrowed, a module is renamed, or the helper is
# dropped from the import graph — exactly the slow drifts a pattern-based
# inclusion cannot catch.
test-streams:
	python3 -m unittest tests.test_concurrent_streams tests.test_concurrent_streams_ws -v

# `npm run build` typechecks and bundles; it does not *execute* the tests in
# ui/tests/, which assert the client-side rules the chat depends on (goal stream
# framing, model ordering, failure diagnosis, stats-history merging). They run
# directly through `node --test` with type stripping, so they need no build step
# and no bundler — but that flag landed in Node 22.6.
test-ui:
	cd ui && npm test

# Ruff's rule selection is pinned in pyproject.toml (target-version = the declared
# minimum Python), so this is the same check on every machine and in CI. It covers
# tests/ and scripts/ too: a dead local in a test is a lost assertion, and this
# project's whole bar is what the suite proves.
lint:
	ruff check engine tests scripts

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

# Smoke tests, screenshots and verification runs: one home override moves both the
# database and the secrets file, and disables the OS keychain for this process, so
# this run cannot read or write the developer's real credentials.
run-engine-scratch:
	@echo "Isolated engine: all state under $(SCRATCH_HOME)"
	CODIFY_HOME=$(SCRATCH_HOME) python3 -m engine

# The fastest checks first, so the cheap failure is the one you read. These are the
# same targets the CI workflow names, split by toolchain (.github/workflows/check.yml),
# but only on the host interpreter.
check: lint typecheck test-ui test test-streams build-ui check-tauri
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
