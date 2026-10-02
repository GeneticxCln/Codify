# Contributing to Codify

Thanks for helping. The bar for a change here is simple: **`make check` must pass.**

From a fresh clone, get to a machine that can run it in two commands:

```
make setup     # .venv, the engine, the dev tools, npm ci
make doctor    # what is still missing (system libraries, Node, a display, a stale .venv), and how to fix it
```

`make setup` uses a virtualenv because a modern distribution refuses `pip install` into the system
Python (PEP 668). Once `./.venv` exists the Makefile puts it first on `PATH`, so nothing has to be
activated; the desktop shell looks for the same interpreter. `make doctor` is read-only. It also checks that
the `.venv` has every dependency `pyproject.toml` declares, because one made before a dev dependency was added
keeps working and keeps warning; `make setup` again is the fix (it installs into the existing `.venv`).

```
make check
```

runs everything in one pass:

| Step | What it does |
|---|---|
| `make lint` | `ruff check engine tests scripts benchmarks` — rules and target Python pinned in `pyproject.toml` |
| `make typecheck` | `mypy` over the same files — config (3.10 floor, pydantic plugin) pinned in `pyproject.toml` |
| `make test-ui` | the React/TypeScript unit tests in `ui/tests/` (needs Node 22.22.2+, 24.15+ or 26+; `npm test` says so itself when it is older) |
| `make typecheck-ui-tests` | `tsc` over `ui/src` **and** `ui/tests` — the check `npm test` cannot do |
| `make test` | full Python suite (411 tests today — the number moves, so trust the run) |
| `make test-streams` | the concurrency/stream-isolation tests, **by name** (not just via discovery) |
| `make build-ui` | TypeScript check (`src` only) + Vite production build |
| `make check-tauri` | `cargo check` + `cargo fmt --check` on the desktop shell |
| `make check-history` | every commit in `HISTORY_RANGE` (default `origin/<branch>..HEAD`) builds, not just the tip |

The Python suite must run on **3.10 and 3.14** — 3.10 because `pyproject.toml`
declares it and the desktop shell boots the engine with whatever `python3` is on
`PATH`, 3.14 because that is what the newest interpreter does with this code. A
version-specific break has shipped before: a PEP 701 f-string parsed on 3.14 and was
a `SyntaxError` on 3.10, in a module no test could even import.

`make check` only ever exercises the interpreter you happen to have installed, so the
gate that covers both ends is:

```
make ci
```

which runs everything above and then the same Python targets again on the declared
minimum, bringing that interpreter up on demand (it downloads one with `uv`, or uses a
`python3.10` you already have). The floor leg never skips: if it cannot be provisioned,
`make ci` fails and says what to install. `.github/workflows/check.yml` describes the
same targets split by toolchain. That workflow is manual-only (its header says why
and what the earlier billing lock was): start it by hand for a second opinion from
GitHub's own runners. **`make ci` is the gate.**

### Getting the verdict onto a pull request

Two commands, both free. Run `make hooks` once per clone, so `git push` runs `make ci`
first and refuses to send a red tree. Then, with a commit pushed and a clean working
tree:

```
make ci-report
```

runs `make ci` and publishes the result on the commit as a GitHub status named
local/make-ci, which a pull request shows like any other check. It uses the GitHub
CLI (`gh auth login` once; the token needs permission to write commit statuses) and is
an API call, not a workflow run, so it needs no runner and no billing. It refuses a
dirty working tree and a gate that changes the tree, because a status is a claim about
a *commit* and would otherwise describe files that are not in it. The status is
self-attested — it says what this machine saw — which is the same trust the pre-push
hook already places in the developer.

### The gate is about the tip; the history is a separate question

`make ci` asks whether the tree you are about to share is green. It cannot ask
whether every commit on the way to that tip was green, and in this repository
those come apart more easily than you would expect: the UI suite asserts
against *source text* as much as behaviour, so a commit that adds a component
and a test asserting the shell mounts it is red until the commit that mounts
it. Split a coherent change into "the parts" and "the wiring" and you get a
history that reads well and that `git bisect` cannot walk.

```
make check-history
```

walks the range and runs the decisive legs at each commit, in its own detached
`git worktree` outside the repository — your working tree is never checked out,
so this is safe to run with unsaved work in flight. It reports the commit, the
leg, and the first line of the failure, because a table of thirty "FAIL"s with
no reasons is a table nobody acts on.

It is deliberately **not** part of `check` or `ci`: it asks a different question
of different commits, and the declared-minimum and Rust legs stay out because
they cannot be what decides a split and they cost minutes per commit. A
one-commit range has no middle, so the target says so and exits rather than
walking anything — which is what makes it affordable enough to run on every
push that could possibly need it.

Install the hooks once per clone and the cheap failures stop reaching a push:

```
make hooks
```

| Hook | Runs | Why |
|---|---|---|
| `pre-commit` | `make lint typecheck` | seconds, so the obvious failure lands while the change is still in your hands — it checks the working tree, so pre-push remains the real proof |
| `pre-push` | `make ci`, then `make check-history` | the whole gate on the tip, then the commits under it. Ordered that way because a red tip is the cheaper thing to be told about and the per-commit sweep is minutes |

Both are versioned in `.githooks/` (`core.hooksPath` is the whole install, `git config
--unset core.hooksPath` the whole uninstall), so they work on a fresh clone instead of
living in one person's `.git/hooks`. `git commit --no-verify` and `git push
--no-verify` are the deliberate escape hatches — neither hook skips a leg to be
convenient, and a linter that cannot be found is a refusal with install instructions,
never a pass.

## Rules of thumb

**Engine changes need a test that fails without them.** The suite exists to
catch the things this project has actually gotten wrong: hung test commands,
swept git commits, crossed event streams, credentials escaping to the real
`~/.codify`. If your fix can't fail a test, the next refactor will undo it.

**No test may skip itself out of a guarantee.** A test that quietly skips when a
dependency, a binary or a platform is missing is a guarantee nobody is checking:
the wire-level stream-isolation test used to skip whenever `websockets` was
absent, and `websockets` was not declared. If a test needs something, declare it
(and let the suite fail loudly without it).

**Isolation guarantees live in `tests/stream_isolation.py`.** Both concurrency
test layers (service-level in `test_concurrent_streams.py`, over-the-wire in
`test_concurrent_streams_ws.py`) assert through that one shared helper. If you
change a stream guarantee, change it *there* — both layers pick it up and
cannot drift apart.

**A card is tested by its markup, and the loader is what makes that possible.**
`ui/tests/tsxLoader.ts` teaches `node --test` to import `.tsx` (node rejects the
extension during format detection, before any `load` hook can answer) and gives
`src/` the one browser global a module reads while it loads — `localStorage`, for
`api.ts`'s boot token. That read is why every card `ChatTimeline` draws used to
be unrenderable: the harness could only reach components that had been extracted
out of it. Render through `react-dom/server` and assert on the output, because
whether the sentence about a file lands on screen is a fact about the JSX and not
about the function that returns it — a body rendered inside a branch that never
evaluates, a path that prints as `undefined`, a disabled control that computes its
reason and never says it. Logic tests still earn their place; they are cheaper,
and `pinReadiness` deciding correctly says nothing about whether the reason is on
screen. Two rules fall out of it. A new `ui/src/components/ui/*.tsx` has to be
added to the table in `componentLoader.test.ts`, so a shared primitive nobody
rendered is a primitive whose markup nothing checks. And a render must produce no
React warning at all: a list child without a unique key reconciles by index, which
is a state-mixing bug rather than a cosmetic one.

**And when a control has to be *used*, there is now a DOM for it.** The rules
above are all about `react-dom/server`, and a static render has one blind spot
that is not small: it runs no effects and no events, so a button wired to nothing
produces perfect markup. Six of this repo's own tests were written to cover
that blind spot by opening the **source file** and matching a regex against it —
which is a test of the file, and which passed happily while the radiogroup's
arrow keys, the colour wells, the file input, the palette's Escape key and the
transcript's audit badges were unreachable by anything.

`ui/tests/dom.ts` is the answer, and it is a `jsdom` document with
`React.act` around every interaction. `withDom` is opt-in and a callback rather
than setup/teardown, because the teardown is the part that gets forgotten and a
leaked document produces tests that pass alone and fail together.

Seven things to know before using it.

- **Import the component from inside the callback.** `react-dom/client` reads
  `document` while it is being *imported*, so a static import at the top of a
  test file gives React no document and the harness mounts nothing while every
  assertion passes. `appearanceInteraction.test.ts` shows the shape.
- **Seed `localStorage` before rendering.** Components read storage in a
  `useState` initialiser, so anything written afterwards is too late and a test
  that forgets asserts about the default theme without saying so.
- **The `fetch` in place refuses, on purpose.** `api.ts` builds its URL from the
  engine's port and calls the bare global, so a component that fetches on mount
  would otherwise open a real socket to `127.0.0.1` — the developer's engine, or
  a stranger's. The installed one records the attempt in `dom.fetches` and
  rejects, naming the URL. Assign `globalThis.fetch` **from inside** the
  callback — the harness installs its own as it sets up, so a stub assigned from
  outside is overwritten before the component ever runs. `settle()` after the
  answer you installed, so the update lands inside `act`.
- **Scrolling is answered and recorded.** jsdom has no `Element.prototype
  .scrollIntoView` at all, and no document with no layout has a scroll position;
  the element that was named goes into `dom.scrolls` and nothing moves. "The
  second edit was brought into view" is a claim about *which* element was
  named, and that is all there is to claim.
- **There is no assertion about pixels here.** The canvas is stubbed so a
  component can mount; a claim about what was painted belongs in
  `painter.test.ts`, which drives painters against a recording context.
- **Write tests in `.ts` with `React.createElement`.** The suite's glob is
  `tests/*.test.ts` and node's type stripping refuses JSX outside `.tsx`.
- **Never hand an element to `assert.equal`.** `assert.equal(el, null)` builds
  its `AssertionError` from the element's object graph, and a mounted one carries
  React fiber pointers into the whole app: when the assertion *fails*, that took
  25 GB and got a developer's desktop session killed by the OS. Compare a
  boolean, `assert.ok(el === null, msg)`, or a list's `.length`. `sourceRules
  .test.ts` scans for the shapes it knows. `make test-ui` also runs under a 6 GB
  systemd memory cap where `systemd-run --user` exists, so a leak that gets past
  the rule ends one test run and not your session.

A static render is still the right default — it is much faster and it is the
right claim when the question is "is this sentence on screen". Reach for the DOM
when the question is "can a person do this", and the difference is not academic:
breaking `commitTints` so a pick stops persisting fails four interaction tests
and passes all fifty-eight static ones. The same holds for the surfaces that
were the worst of it: the palette (`paletteInteraction.test.ts`), the tab strip
(`tabStripInteraction.test.ts`) and the transcript (`transcriptInteraction
.test.ts`) each have a file whose claims are about the second listener and not
about the words — a key that stops at the input, an × that closes a tab without
selecting it, a goal action carrying the version it was rendered from.

**What the DOM does not reach.** `App.tsx` itself is not mounted by any test, and
this is a decision rather than an oversight: it is the shell that boots the
engine, opens sockets and drives a Tauri webview, and a jsdom document would
prove nothing about it that its own files do not. The claims that live only in
`App.tsx` — that a tab strip sits inside the main split, that a terminal pane is
handed its *tab's* workspace — stay as source reads, and the reason they are not
being replaced is worth saying: a regex over a file is a weak claim, but a DOM
test for a component that only exists inside the shell is a *fake* one. Test the
component where the component lives.

**Never touch the developer's real state.** Tests run hermetically via
`tests/hermetic.py`; an isolated engine moves *both* the database and the
credentials with `CODIFY_HOME` and disables the OS keychain (see
`engine/home.py`). If your change needs state, put it under `CODIFY_HOME`.

**`CLAUDE.md` is a distillation, not a second source of truth.** It is the
agent-facing version of this file and `docs/00`–`11`, and it is deliberately
short: depth is a pointer, not a copy. When the two disagree, this file and
`docs/` win and `CLAUDE.md` is the bug —
`tests/test_claude_md_contracts.py` fails if the invariants it quotes drift
from `docs/00` §6. The agent definitions in `.claude/agents/` and the commands
in `.claude/commands/` are hand-written from the contracts above for the same
reason: the `claude-code-templates` catalog was deliberately *not* installed,
because it ships a competing role taxonomy next to the eight `ROLES` in
`engine/models.py` and would need third-party source committed against the
stance in `benchmarks/manifest.json`.

**No third-party source in this repository, and no benchmark that needs it to
pass.** `benchmarks/manifest.json` ships with `repos: []`, and
`benchmarks/vendor.py` is the only way a snapshot gets here: a permissive
SPDX licence named by a person, a pinned commit SHA, a recorded reason, and a
`NOTICE.md` per repository. A task whose repository has not been fetched fails
with that instruction rather than scoring an empty run as perfect. See
`docs/08-benchmarks.md`.

**Benchmarks are not in the gate.** `make bench-smoke` is hermetic; `make bench`
spends real tokens on real models. A gate that costs money on every push is a
gate people learn to bypass. Run them deliberately.

**No hardcoded model lists.** Models come from live provider discovery
(`docs/06-model-discovery.md`). "The provider knows its models" is a design
decision, not an oversight.

**Don't let the gate depend on your machine.** `make lint` reads its rule set and
target Python from `pyproject.toml`, because a linter's defaults are not a
contract: the ruff on one machine checked ~400 rules and reported 189 findings no
one had triaged, while a stock ruff reported none of them. Pin the behaviour, then
widen the ruleset deliberately — with the fixes — rather than inheriting whatever
the installed version does today. That is how the wider sets landed: bugbear,
the bandit-style security rules and pyupgrade are all on, and every security
finding they raise is triaged per site — the `per-file-ignores` block in
`pyproject.toml` names each exemption with the reason it is safe (argv-list
subprocess, engine-owned SQL identifiers, documented best-effort paths). A new
`S` finding is a decision to make, not noise to silence. The same logic pins
`make typecheck`: mypy's config (the 3.10 floor, the pydantic plugin, the file
set) lives in `pyproject.toml`, not in anyone's command line.

**Type annotations are checked, not decorative.** `make typecheck` only sees what
is already annotated, but that is the point: an annotation like
`tuple[int, callable]` names the *builtin* `callable`, not a type, and no runtime
test fires on it — it ships wrong for years until a caller trusts it. If you
write a type, mypy must pass on it; if you silence a finding with `type:
ignore`, the exact error code goes on the line (`# type:
ignore[attr-defined]`), and an ignore that stops being needed is itself an error
(`warn_unused_ignores`).

## Manual verification

For UI-facing changes, a hermetic end-to-end run:

```
make run-engine-scratch   # isolated engine on :7430 (first free in 7430-7440), real ~/.codify untouched
make dev-ui               # Vite dev server
```

`scripts/fake_ollama.py` serves a fake AI provider (role-aware canned replies,
model catalog) so you can drive a full goal without any API keys:

```
python3 scripts/fake_ollama.py   # serves :11435
```

then point the roles' base_url at `http://127.0.0.1:11435` in Settings.

## Docs

Architecture lives in `docs/00`–`11` (`07` is the spawn guard and the
deterministic tests; `08` is the benchmark harness and the vendoring policy;
`10` is the agent-memory model; `11` is the Ruflo audit). If
your change alters a documented contract — orchestration, settings, security,
model discovery, the Laya gate, the spawn guard, what a benchmark number is
allowed to claim — update the matching doc in the same PR. The docs have lied
before; don't add to it.
