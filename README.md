# Codify

**Codify** is a local-first, multi-agent AI coding assistant and desktop app. It turns a high-level goal
into an atomic, verified execution plan, run by specialised subagents under a pre-flight gate, with
sandboxed commands, scoped git commits and live telemetry.

<sub>The app connected to a local engine, wearing the <b>OLED CMatrix</b> theme — one of nineteen,
seventeen of them with an animated backdrop of their own and two deliberately flat. The clip is a real
turn: the gate ruled on it, the conductor answered, and the transcript says plainly that no file
changed. <a href="docs/demo.webm">Watch the video</a>.</sub>

Everything below is specified in [`docs/`](docs/00-codify-architecture-overview.md). That set is the
source of truth; this file is the tour.

---

## 🏛️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Codify Desktop App                       │
│  ┌─────────────────────────┐   ┌─────────────────────────┐  │
│  │  React 19 + TypeScript  │◄──┤      Tauri v2 Core      │  │
│  │   Tailwind CSS / Vite   │──►│   (Rust Subprocess Host)│  │
│  └─────────────────────────┘   └────────────┬────────────┘  │
└─────────────────────────────────────────────┼───────────────┘
                                              │ Spawns & supervises
                                              ▼ (Stdout Handshake)
┌─────────────────────────────────────────────────────────────┐
│                    Codify Engine Backend                    │
│  FastAPI (127.0.0.1:7430-7440, Bearer boot token)          │
│    │                                                        │
│    ├─ Laya System-1 gate — typed decisions, before any LLM  │
│    └─ Conductor loop — picks from 7 role slots              │
│       via moves + a loaded skill                            │
│       │                                                     │
│  ┌────┴─────────┬──────────────┬────────────┐  ┌─────────┐  │
│  │ FileSystem   │ Sandbox      │ Git        │  │ SQLite  │  │
│  │ (path jails) │ (allowlist)  │ (commits)  │  │  WAL    │  │
│  └──────────────┴──────────────┴────────────┘  └─────────┘  │
└─────────────────────────────────────────────────────────────┘
```

The component list behind that diagram and the invariants the engine holds to are in
[`docs/00`](docs/00-codify-architecture-overview.md). Read it before changing either tier.

### Eight roles, and a loop that orders them

A **pre-flight gate** rules on every goal before any model is called, then a **conductor** decides
which of the remaining seven roles run and in what order.

| Role | Runs | Ability |
|---|---|---|
| **Laya** (gate) | once per goal, before any model call | Typed decisions: intent, risk, calibrated prompt-injection. Blocks hostile requests; never writes files. |
| **Librarian** | once per goal, before planning | **Read only** — files, search, git history. Returns a checked evidence pack: paths it actually opened, conventions, the command this repo really runs. |
| **Design** | once per goal | Reasons only. Locks the direction — tokens, components, constraints — obeying the workspace's brand contract when it has one. Emits the contract as text; never writes it. |
| **Planner** | once per goal | Reasons only. Decomposes goal + evidence into 1–20 steps with target paths. |
| **Fixer** | once per step | **The only writer.** Proposes file creates, updates, deletions. |
| **Verifier** | once per step | **The only role that runs a command.** One allowlisted command; reports what actually happened. |
| **Critic** | once per step | Audits the diff. `request-changes` pauses for a human — there is no auto-fix loop. |
| **Scribe** | once per step | Progress notes and a conventional commit message. |

The familiar librarian → design → planner → fixer → verifier → critic → scribe order is the built-in
`ship-a-change` **skill**, not a compiled path: a request needing three of those seven gets three. The
slot count is fixed; the order is not. Independent steps of a `parallel` goal run concurrently, bounded
by a configurable width. See [`docs/01`](docs/01-subagent-orchestration-spec.md), [`docs/05`](docs/05-laya-system-1-gate.md).

### Model discovery — no catalog in the build

Codify ships **no model list**. Models are discovered live from each provider's own API, using that
provider's stored credential. Saving a key is enough to see its models; one the provider starts serving
later appears on **Refresh**, with no upgrade. Every provider reports its own status, so one invalid
key never empties the picker, and a role whose stored model its provider no longer reports is flagged
while you are looking at the setting rather than mid-goal. See
[`docs/06`](docs/06-model-discovery.md).

### A fallback per role, and a diagnosis when it is used

Every role can name a second target — provider and model — for when the primary cannot be used at all.
It is per role because the honest fallback differs by job: a local model is fine for the scribe and a
bad idea for the fixer. It is tried once per call and only for those failures; anything else is a
binding bug, and the chat names the model that actually answered.

**Fix the roles that can't run** is one action that points exactly the stranded roles at a model the
engine has already discovered, and leaves every working role alone. It reports the reason for each role
it changed *and* each it skipped, and a provider that merely failed to answer is treated as unknown
rather than broken — so a network hiccup is never reported as *"your model was retired"*.

Settings is the **only** place a role's provider and model can change, enforced at three layers rather
than by convention: the API exposes exactly one mutator, the Tauri layer one command, and every other
surface renders a role's config as text it can deep-link from but not edit. See
[`docs/02`](docs/02-settings-app-spec.md).

When a goal fails, the error block offers **Why did this fail?** — reading the role's config, its
credential state and the provider's live catalog, then ranking cause above symptom. A missing key is
reported as a missing key, not as an unreachable provider, and every finding links to the screen that
fixes it. See [`docs/04`](docs/04-engine-data-and-runtime.md).

### Memory, and a browser the model can read

`recall` lets the conductor ask this workspace's own history a question — *has this happened here
before, and did we get past it?* — returning the specific past events, newest first. It is bounded and
allow-listed: only named fields from named event types, never stored diffs or third-party text, and
results arrive labelled *recorded outcomes, not evidence about the current code*. See
[`docs/10`](docs/10-agent-memory.md).

Pages open **inside** Codify, as a real embedded webview, and the same tab is readable by the model:
`read_page` returns the address, title, text and links, so a turn can depend on a documentation page
instead of guessing. Loopback stays blocked, and a model-proposed URL meets the same guard your click
goes through. See [`docs/09`](docs/09-workspace-shell.md).

---

## 🎨 Appearance

Nineteen themes ship in the box, and choosing one is a **runtime swap, not a rebuild** — a theme is a
map of CSS custom properties, so the whole app repaints with no component change and no restart.

<p align="center">
  <img src="docs/themes.gif" alt="All nineteen themes side by side, each running its own animated backdrop at the same time, except the two that ship none: Codify Dark and Still" width="1000">
</p>

<sub>All nineteen at once, each drawing its own backdrop — except <b>Codify Dark</b> and <b>Still</b>,
the two that ship no canvas at all. <b>OLED CMatrix</b> is the pure-black one with the green rain.</sub>

| Theme | What it is |
|---|---|
| **Codify Dark** | The default contract: blue-grey surfaces, green reserved for status. No canvas. |
| **OLED CMatrix** | Pure black, with the rain drawn in `#003300` — the video above. |
| **Cyberpunk Neon** | Violet-black, a neon horizon grid and a banded sun in cyan and magenta, under a CRT veil. |
| **Neural Constellation** | A slow web of nodes that brightens and pulses while the agent is working. |
| **Cyberpunk HUD** | Amber instrumentation: corner brackets, two slow radar sweeps, a tick ladder. |
| **Bioluminescent Abyss** | Spores rising in the left and right gutters, fading out before the middle. |
| **Solar Flare** | A starfield and slow indigo-to-ultraviolet streams along the window's edges. |
| **Monochrome ASCII Rain** | The same rain in hex bytes and grey — no component of its own, just different tokens. |
| **Still** | No canvas at all. For long sessions, and for anyone who would rather have no weather than quiet weather. |
| **Winter Snow** | A clear cold night, and snow falling through the whole window. No lights — just weather. |
| **Festive Night** | Evergreen dark, gold and berry, and warm lights drifting up through the snow. |
| **Solarized Flare** | A monochrome amber CRT. Solar wind runs the gutters, and a heat pulse crosses them while a run is in flight. |
| **Cyber Organism** | Electric blue and cyan. Glowing nodes and branching tendrils in the margins, firing while a run is in flight. |
| **Event Horizon** | Obsidian and violet. Orbital streams curve away at the margins, and beams ignite while a run is in flight. |
| **Vector Wireframe** | Green or hot pink 1px line art. Rotating wireframe solids in the gutters, with a target reticle while a run is in flight. |
| **Liquid Mercury** | Platinum, steel and titanium. Viscous metallic waves climb the margins and go turbulent under load. |
| **Electric Arc** | Ink-blue and a lightning-white core. Arcs strike down both gutters, fork, and go out as fast as they came. |
| **Toxic Lab** | A fume hood at 3am. Reagent-green bubbles climb the gutters, wobble, and burst; failure is the amber the hazard tape is. |
| **Anon Fluid** | Green on black and no name on it. Data runs sideways through the gutters as a liquid column, and a run in flight shears it into blocks. |

Four constraints hold every one of them:

- **A theme is data.** It publishes CSS variables; a backdrop asks whether the active theme published
  them. The last theme added was five lines of tokens and no new code.
- **One clock, one budget.** Every effect runs through `useAtmosphereCanvas`, which owns the canvas size
  cap, a 30 FPS accumulator, and exactly one static frame under `prefers-reduced-motion`.
- **Decoration is never under the words being read.** Content surfaces stay opaque and the canvas is
  masked to fade before it reaches the chrome.
- **A theme may restate the status tones; it may not renegotiate them.** All six or none, and `danger`
  and `warning` stay on the warm arc — a user who learned that orange means *needs attention* was owed
  that much.

Any theme can be **recoloured without being forked**: the Appearance pane offers each theme's accent
plus the weather variables its own backdrop reads, labelled with what they draw. A proposal is
**clamped, not accepted blindly** — it keeps its hue and saturation and has its lightness moved until it
clears 4.5:1, and a colour that cannot be made readable is refused outright. Schemes export and import
as one readable JSON file.

One surface is not reachable by swapping custom properties, and it is the honest exception to *the
whole app*: xterm.js paints its own canvas and takes concrete colour strings. The one surface CSS cannot
reach is the one that needed it. The terminal follows the theme by reading it out of the document —
`ui/src/terminalTheme.ts` maps all twenty of xterm's colours onto variables a theme publishes, and the
pane re-reads them on every switch, so a terminal opened before you changed theme is not left behind.
Before that, it was the one grey-blue box in an OLED app that was otherwise black and phosphor.

---

## 🔒 Security & Invariants

1. **Loopback only.** The engine binds `127.0.0.1` on ports `7430–7440`.
2. **Boot token.** Every HTTP and WS request requires `Authorization: Bearer <token>`, created once per
   state directory and kept at `~/.codify/boot_token` (`0600`) so a client stays authenticated across
   restarts. `CODIFY_BOOT_TOKEN` overrides it.
3. **Path containment.** All file operations verify realpath containment; escapes raise
   `PathEscapeError`, and a workspace root that is your home, a system directory or a credentials
   directory is refused — so an approved goal cannot rewrite `~/.bashrc`.
4. **Command sandboxing.** Commands pass an allowlist (`pytest`, `npm test`, `cargo test`, `go test`,
   read-only git). The librarian's run in `read_only` mode, so reconnaissance can never change the
   workspace.
5. **Human in the loop.** When the critic requests changes the step pauses and the goal transitions to
   `PAUSED`; only a human resumes it.
6. **Key storage.** Keys never reach SQLite and are never echoed by the API. They go to the OS keyring
   when one is usable, otherwise to an owner-only `~/.codify/secrets.json` — because a machine without a
   keyring must still be able to store a key. The settings screen says which is in force.
7. **Commit scope.** A step commits *only* the paths it wrote (`git commit -- <paths>`). The engine
   never runs a bare `git add -A`, so work you had staged in the same tree is neither committed under
   Codify's message nor staged by it.
8. **The gate is not optional.** Injection probability `≥ 0.85` fails the goal before the planner runs.
   An unavailable gate logs that it was skipped and the pipeline proceeds — it is never a silent failure.

Each is specified with its reasoning in [`docs/03`](docs/03-security-and-roadmap.md).

---

## 🚀 Quickstart

Ask the machine what it needs — a list of versions in prose is where drift lives:

```bash
make doctor     # checks everything below, prints the install command for what is missing
```

It looks for **Python 3.10+** with `venv` (3.10 is a real deployment floor — the shell boots the engine
as `python3 -m engine`), **Node 22.22.2+ / 24.15+ / 26+** (jsdom's range, which the UI suite inherits),
**Rust stable** with WebKitGTK 4.1, GTK 3, libsoup 3, librsvg, OpenSSL and `pkg-config`, and a display
for the Tauri leg (`xvfb` is used automatically on a headless machine).

```bash
git clone https://github.com/GeneticxCln/Codify.git
cd Codify
make setup      # creates .venv, installs the engine and dev tools, runs npm ci
```

A virtualenv rather than `pip install -r`: stock Ubuntu 24.04, Debian 12+ and Fedora refuse `pip
install` into the system Python (PEP 668). The Makefile and the shell both use `./.venv` when it exists,
so nothing has to be activated.

### Running the app

```bash
make run-app      # builds the UI, embeds it, launches the app
make dev-app      # hot reload against the Vite dev server (needs cargo-tauri)
```

> **Do not launch it with a bare `cargo run`.** That is a *dev* build: the window loads
> `http://localhost:5173` and shows a connection error unless a Vite dev server is already running,
> which looks exactly like an app whose panes are not wired.

Codify targets Linux desktops and ships **no installer or AppImage**. What it has instead:

```bash
make install-local     # release build, plus a `codify` command and a menu entry
codify                 # from any directory, or from the applications menu
make uninstall-local   # removes exactly those three files
```

Everything lands under your own `~/.local`; no root is involved. **Moving or deleting the checkout
breaks the entry** — the launcher finds the project root from where it lives.

> **The app needs the checkout and a Python 3.10+ with the engine's dependencies.** The shell spawns
> `python3 -m engine` in the project root and reads the `CODIFY_ENGINE token=… port=…` handshake from
> its stdout. If no engine is found the window says so rather than opening empty.

**If the window opens blank or black** (WebKitGTK 4.1 quirks, none of which apply on a normal GNOME or
KDE session): try `WEBKIT_DISABLE_DMABUF_RENDERER=1 codify` on some NVIDIA/Wayland combinations, or
`GDK_BACKEND=x11 codify` to go through XWayland.

**Engine without the shell**, for UI work:

```bash
python3 -m engine      # terminal 1
make dev-ui            # terminal 2 — the standalone browser build
```

A browser tab has no shell to ask for a boot token, so it needs one pasted into the console once; the
app tells you the exact two `localStorage` lines when it needs them.

**Never touches your real `~/.codify`:** `CODIFY_HOME=/tmp/anything python3 -m engine` moves both the
database and the secrets file, and disables the OS keychain for that process, so a run pointed at its
own store cannot reach the real one.

---

## 🧪 The gate

```bash
make check     # lint, typecheck, UI tests, Python suite, stream isolation, build, cargo
make ci        # the same Python legs again on the declared minimum (3.10)
```

`make check` only ever runs the interpreter you have installed. `make ci` runs the Python legs again on
**3.10**, fetching that interpreter on demand rather than skipping the leg. 3.10 matters: a construct
only 3.12+ parses is invisible on a modern interpreter and fatal on the declared minimum.

`make hooks` installs the versioned hooks: the cheap checks at commit time, the full gate before every
push. The suite is **hermetic by construction** — `tests/hermetic.py` points every test at a throwaway
state directory and disables the keychain, and two tests assert that, so removing the bootstrap fails
the suite rather than silently rewriting a real store.

Determinism is structural rather than a matter of discipline: every process the engine, the benchmark
harness or a script starts goes through one spawn guard that makes it die with the process that started
it, and a test freezes that list, so a new spawn cannot appear unannounced. See
[`docs/07`](docs/07-spawn-guard-and-deterministic-tests.md). Benchmark numbers live in a separate
harness with an explicit rule about what one may claim — *does this pipeline still do its job*, never
*is the model good* — and it fails rather than scoring a repository it was not given, so no third-party
source is committed here. See [`docs/08`](docs/08-benchmarks.md).

---

## 📂 Layout

```
engine/     FastAPI backend — app.py (routes), conductor.py (the loop), executor.py,
            laya.py (gate), providers.py, sandbox.py, fs.py, git.py, db.py, watchdog.py
ui/         React 19 + TS + Vite — App.tsx, components/, hooks/, api.ts, appearance.ts
src-tauri/  Tauri v2 Rust shell — lib.rs (supervisor + IPC), the render-starvation watchdog
tests/      Python suite; hermetic.py is the shared isolation bootstrap
scripts/    fake_ollama.py, drive_a_turn.py, replay_trace.py, make_logo.py, check_history.py
docs/       00–10, the specifications this README summarises
```

## 📄 License

MIT. See [LICENSE](LICENSE).
