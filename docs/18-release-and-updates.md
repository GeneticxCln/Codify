# 18 — Release and updates

How a Codify release is cut, what the packages hold, and what the About tab's update button does
and refuses.

`docs/00` §6 owns the invariants; this file owns the release process and the one request this app
makes that is not a model provider's. It is written by the change that added packaging
(`.github/workflows/release.yml`, `src-tauri/tauri.conf.json` §bundle, `src-tauri/src/update.rs`,
the update section of `ui/src/components/AboutPane.tsx`), and the repository had no release story
at all before it: `bundle.active` was `false`, `README.md` said in as many words that Codify ships
"**no installer or AppImage**", and the only way to get one was to build it.

## 1. A release is a tag

```bash
git tag v1.0.0 && git push origin v1.0.0
```

That is the whole of it. `.github/workflows/release.yml` runs on `push: tags: ["v*"]`, builds the
packages, and publishes them as a GitHub release on that tag. By hand (`workflow_dispatch`) it either
rebuilds an existing tag's release (`publish` on, with that `tag`) or, by default, makes a dry run
that builds and smoke-tests both packages from any ref and publishes nothing (§3).

Four things carry the version, three of them by hand:

| What | Who reads it |
|---|---|
| `src-tauri/tauri.conf.json` | the About tab (`package_info`), the update check, and the bundle file names |
| `src-tauri/Cargo.toml` | the crate's own version |
| `ui/package.json` | npm; nothing at runtime reads it |
| the tag | the release page, and the update check again |

`scripts/release_notes.py` compares all three files against the tag **before anything is built**,
and exits 1 with a sentence naming the file that disagrees. The failure it prevents is not cosmetic.
The update check compares the running version against the newest release's tag, so a `v1.0.0`
release whose app still says `0.2.0` would offer every copy of itself an update to the version it
already is — for as long as that release is the latest one — and pressing the button would download
the same version again. The same script prints the release page's body, so the file names in it are
generated from the version rather than typed once and left to drift.

`tests/test_release_notes.py` holds both halves: the refusals, and that the three real files agree
with one another, which is what catches a bump that reached two of them.

## 2. What the packages hold, and what they do not

`bundle.targets` in `tauri.conf.json` is `["deb", "appimage"]`, so `cargo tauri build` — and
`make package-app`, which is the same command — produces exactly two files, named by Tauri with the
Debian spelling of the architecture:

| File | Install | Why someone would |
|---|---|---|
| `Codify_<version>_amd64.deb` | `sudo apt install ./Codify_<version>_amd64.deb` | wants it owned by the package manager, removed by `apt remove` |
| `Codify_<version>_amd64.AppImage` | `chmod +x Codify_<version>_amd64.AppImage && ./Codify_<version>_amd64.AppImage` | wants one file, no root, nothing installed |

Both hold the **desktop shell** — the React UI, the Tauri window, the PTYs, the webview bridge, the
machine tab — **and the engine**, with the Python it runs on, at `/usr/lib/Codify/engine-bundle` in
the deb and the same path inside the AppImage. Neither needs a checkout or a Python of the user's, and
the shell runs the engine its own package carries (`docs/04` §6.2), so the window and the engine it
talks to always come from one commit. Up to 1.2.0 the packages held the shell alone, and a package
installed with no checkout anywhere opened a window that said "No engine to run".

`make bundle-engine` (`scripts/bundle_engine.py`) builds that half into `build/engine-bundle`, and
`make package-app` (the release's own command) adds it through `src-tauri/tauri.bundle.conf.json`:

| In the bundle | Where it comes from | Why that way |
|---|---|---|
| `python/` | python-build-standalone's CPython, the build `packaging/python.json` names, checked against its SHA-256 | relocatable and needs only glibc 2.17, so it runs from wherever a package puts it; the minor version is the newest `check.yml` tests, so the interpreter people run is one CI ran the suite on |
| its `site-packages` | the wheels `packaging/engine.lock` names, installed with `--require-hashes --no-deps --only-binary=:all:` for the glibc 2.35 floor (§3) | a wheel that changed on PyPI under the same version stops the build instead of shipping; `make lock-engine` regenerates the lock with that same interpreter's pip |
| `engine/` | this checkout's `engine/`, without bytecode or `requirements.txt` | the engine the shell beside it was built with |
| `BUNDLE.json` | written by the build | the app version, the interpreter and the lock's hash, so an installed package can say what its engine is |

The tree is compiled with `unchecked-hash` bytecode, because neither package keeps a file's
modification time and the bundle is read-only where it lands: timestamp-checked bytecode would be
stale on every start and could not be rewritten. Pruned, because the engine never runs them: Tcl/Tk
and `tkinter`, IDLE, `ensurepip` and `pip` itself, the headers and manual pages, and the shared
`libpython` (`bin/python3` is linked statically). The interpreter is one file, not three names for
it: the bundler copies a symlink as the file it points at. What is left is about 116 MB unpacked,
which makes the deb about 40 MB larger and the AppImage about the same.

**What the packages redistribute.** CPython (PSF licence) and the 34 locked wheels, each under the
licence its lock line names: MIT, BSD, Apache-2.0 and PSF, plus the MPL's file-level copyleft for
`certifi`, `orjson` and `tld`. Each wheel's own licence files are kept in its `.dist-info`, and
`scripts/lock_engine.py` refuses a dependency whose licence is outside that list, so a new one is a
decision made in the open rather than a transitive dependency that arrived with an upgrade.

**Deliberately left out: the Laya gate's SDK.** It brings torch, which would multiply the package
several times over. A bundled engine's gate uses its LLM fallback (`docs/05`); to use the SDK, run
`make setup-laya` in a checkout and start the app against it:
`CODIFY_ROOT=/path/to/Codify codify-desktop`. The same
variable is how a packaged app runs a checkout's engine for any other reason, such as one's own
changes.

**What a package still needs from the machine:** `git`, which the engine runs for every workspace —
the deb depends on it, so `apt` installs it, and an AppImage uses the system's. The folder picker uses
the system `python3` with GTK, as it always has (`engine/app.py`); without one, the engine says the
dialog is unavailable and a folder can still be typed.

**What the AppImage's launcher changes, and the shell takes back.** linuxdeploy's `AppRun` sets, for
the whole process tree, `LD_LIBRARY_PATH` to the libraries the image carries (Ubuntu 22.04's GLib,
OpenSSL, PCRE2 and the rest), `PYTHONHOME` and `PYTHONPATH` to directories inside the image, and,
through the GTK plugin's hook, `GI_TYPELIB_PATH`, `GIO_MODULE_DIR`, `GTK_PATH`,
`GDK_PIXBUF_MODULE_FILE` and their siblings. The shell's own WebKit needs them; nothing it starts does.
In 1.3.0 they reached the engine and, through it, the system `python3` behind the folder dialog, which
loaded the image's GLib 2.72 under the system's newer GObject introspection and died with
`undefined symbol: g_once_init_leave_pointer`; `git` and a project's own test commands ran under the
same library path. `src-tauri/src/appimage_env.rs` now removes every entry that points inside the
image (`$APPDIR`) from the engine's environment, the login-shell probe's and the terminal pane's
shells', and removes a variable left with nothing. That leaves each list as it was before the
launcher prepended to it. Two values it sets cannot be told from a person's own and are left:
`PYTHONDONTWRITEBYTECODE=1` and the hook's `GTK_THEME`. The machine tab clears its environment
entirely and never saw any of it.

`scripts/install-local.sh` remains for working out of a checkout: a `codify` command and a menu entry
under the user's own `~/.local`, no root, nothing copied out of the checkout. It is not a second way
to package the app and does not try to be.

**Deliberately not bundled: GStreamer.** `bundleMediaFramework` is `false`, so audio playback in an
AppImage uses the machine's own GStreamer plugins. Voice read-aloud is the feature this touches, and
every machine that can render the window at all already has the plugins WebKitGTK needs. Setting it
to `true` would carry the plugins inside the AppImage and, as Tauri's own AppImage guide warns, may
make the file hard to distribute (the `ugly` plugin set's licensing) — a licensing decision about a
published artefact, so it is the owner's to make and not a build flag to switch on quietly. The deb
is unaffected either way: `libwebkit2gtk-4.1-0` depends on the GStreamer packages it needs.

## 3. The workflow

Five decisions in `.github/workflows/release.yml` are deliberate, and each one is a claim that would
otherwise have to be rediscovered:

* **`runs-on: ubuntu-22.04`, pinned, never `ubuntu-latest`.** The base image is the floor for glibc,
  not a convenience — Tauri's guide says to build on the oldest base system intended to be
  supported, and 22.04 is the oldest that carries `libwebkit2gtk-4.1-dev` from its own
  repositories. Built on 24.04, the same files refuse to start on 22.04 with
  `GLIBC_2.39 not found`, which is a failure that appears on someone else's machine after the
  release is out. Bumping this line is a decision about who can install Codify, not upkeep.
* **`dtolnay/rust-toolchain@stable`, not the 1.88 in `Cargo.toml`.** `rust-version` is a real
  promise and the *gate* is where it is kept (`scripts/check_msrv.py` against the locked dependency
  graph, run by `make check-tauri`). Pinning the release build to 1.88 would instead make
  `cargo install tauri-cli` fail the day the CLI's own MSRV passes 1.88 — a build tool's
  requirement blocking the artefact, in a workflow whose entire job is to produce one.
* **It is not a second gate.** `make ci` is the gate, and it runs before a commit reaches master
  (`make hooks` installs the pre-push hook; `check.yml` runs the same targets on GitHub's runners
  for every pull request and every master push). This workflow builds and publishes. If the tag's
  commit was never gated, that is a fact about how the tag was cut, and re-running the suite here
  would not turn it into a different one. The single check it does make is the version agreement
  above, which nothing else can make.
* **`cargo install tauri-cli --version "^2" --locked`.** The CLI's own dependency graph is pinned,
  so a release cannot be broken by a build tool's transitive dependency publishing a bad version
  between two tags. It is also the slowest step in the run, which is why the job has a
  `timeout-minutes` rather than no bound at all.
* **`APPIMAGE_EXTRACT_AND_RUN=1`.** `appimagetool` is itself an AppImage and needs a mounted FUSE
  filesystem to run, which a runner does not have. Without this the bundling step fails with
  "failed to run linuxdeploy" and says nothing about FUSE.

Before the build, `python3 scripts/bundle_engine.py` builds the engine both packages carry (§2). The
runner's Python only starts the script; the interpreter that installs the lock and compiles the
bundle is the pinned one, and **the wheels are chosen for the glibc of the runner this job is pinned
to** (`GLIBC_FLOOR = (2, 35)`, newest tag first, as pip would choose on Ubuntu 22.04), so a wheel that
needs a newer glibc cannot reach a package that promises to run there.

The build step is one command, `cargo tauri build --config tauri.bundle.conf.json`, because the
target list lives in `tauri.conf.json`; a list repeated in the workflow would be a second place to
forget. Tauri's own `beforeBuildCommand` builds the UI first, so the window carries the real
frontend. **The engine is added by that second config file alone**, which maps
`../build/engine-bundle` to `engine-bundle` under the resources. In `tauri.conf.json` it would be read
by `tauri-build` on every compile, and every `cargo check` — the gate's Rust leg included — would
need a 116 MB bundle that only a download can make. A Rust test holds the shell's `BUNDLE_DIR` to the
name this file maps it to.

**Both hooks say where to run, and that is not decoration.** A Tauri hook runs with its working
directory set to the *frontend* directory — the resolved `frontendDist`, here `ui/dist` — not to
`src-tauri`, so the obvious `"cd ../ui && npm run build"` is wrong in a way nothing local shows: it
reads correctly and fails on the runner with `sh: 1: cd: can't cd to ../ui`. The CLI changes into the
tauri directory itself first (`set_current_dir(dirs.tauri)` in `tauri-cli`), and a hook's own `cwd`
overrides the frontend default, so `{"script": "npm run build", "cwd": "../ui"}` is the checkout's UI
folder under both `cargo tauri build` and `cargo tauri dev` — every caller reaches the CLI from
`src-tauri`, which is what makes `../ui` unambiguous. This was the first release's only failure, and
it is written here rather than left in the workflow log.

After it, one step collects `*.deb` and `*.AppImage` into `dist/`, writes `SHA256SUMS`, and **fails
if the bundler produced fewer than two files**. A release with no assets looks published and cannot
be installed, which is worse than a failed run: the update button would offer a version with nothing
behind it.

**Then each package's engine is booted, before anything is published.** `scripts/smoke_bundle.py`
unpacks the deb (`dpkg-deb -x`) and the AppImage (`--appimage-extract`) without installing or
running either, and starts the engine inside each the way the shell does: its own Python, the
isolation environment of `docs/04` §6.2, a throwaway `CODIFY_HOME`. It passes only when the engine
prints its handshake, answers `GET /health` with 200, and leaves every file in its bundle exactly as
it found it — a deb's files belong to root and an AppImage is read-only, so a write that works on the
runner fails on every installed copy, and the comparison is made on the tree rather than on file
modes because CI runs as root. `--version` also requires the bundle's `BUNDLE.json` to name this
release. This is the second check the workflow makes that nothing else can, after the version
agreement: the gate never builds a package. `make check-bundle` runs the same smoke on a local
`build/engine-bundle`.

**A dry run builds and smoke-tests a release without publishing one.** Started by hand with
`publish` off (the default), the workflow builds from the ref it was started on, holds the three
version files to each other under the version they carry, runs every step above, and uploads `dist/`
as a workflow artifact kept for a week. There is no tag and no release, so no copy of the app can
see it. It is how a change to the packaging is tried before the release that would ship it. With
`publish` on and an existing `tag`, a run by hand rebuilds and re-uploads that release, as before.

The release is published rather than drafted. The update check reads GitHub's `releases/latest`,
whose answer is the newest release that is neither a draft nor a pre-release — so a draft is a
release nobody's app can see. Publishing is also the moment the button starts offering the new
version, which is why the version agreement runs first. A re-run uploads over the same tag with
`gh release upload --clobber` instead of failing on "release already exists", which is the only way
to fix an asset that went up wrong. To take a release back, `gh release delete <tag>` (the tag stays;
delete it separately if the version should never be published at all).

## 4. The update button

Settings → About has a Check for updates button (`ui/src/components/AboutPane.tsx` →
`codify_update_check`), which offers the package for this install and, on a second press,
`codify_update_download` fetches it. Everything that decides *what* is offered is in
`src-tauri/src/update.rs`, pure enough for `cargo test` to hold it; the command handlers in `lib.rs`
add the app handle and nothing else.

**What it asks, and when.** One unauthenticated GET to
`https://api.github.com/repos/GeneticxCln/Codify/releases/latest` with an `Accept` header and a
`User-Agent` naming this app's version, and nothing else: no key, no account, no identifier, nothing
about the machine. It runs **only when a person presses the button** — no timer, nothing on mount —
so Codify is not a program that phones home on every launch. It is the only request this app makes
that is not the engine or a model provider, which is why the About tab's own sentence names it.

**Which package this install takes** is decided from evidence, not from a preference:

| Evidence | Package | What is offered |
|---|---|---|
| `$APPIMAGE` is set (the AppImage runtime exports the path it is running from) | `appimage` | the `.AppImage` |
| the binary runs from `/usr/` and `/etc/os-release` names Debian, Ubuntu or a derivative | `deb` | the `.deb` |
| the binary runs from `/usr/` on another distribution | `distro` | no package: update it with that distribution's package manager, with the release page named |
| anything else — a build out of a checkout, or the launcher `scripts/install-local.sh` writes | `source` | no package at all: `git pull` in the checkout, with the release page named |

The last row is the important one. Offering a deb to someone running from a checkout would install
a *second* Codify beside the one they have. The `$APPIMAGE` check comes first because an AppImage
unpacks itself under `/tmp` and runs the binary from there, which would otherwise read as a checkout.
`/opt` is deliberately not evidence of a deb: hand-built and distribution packages live there too, and
naming `apt` for one would be a guess. The Debian check reads `/etc/os-release` (`ID`, or a derivative's
`ID_LIKE`, naming `debian` or `ubuntu`); an Arch or Fedora install under `/usr` is `distro` and is told
to update through the manager that owns it, never handed a `.deb`.

**What the four answers are.** `available` says a newer stable release exists; `asset` says there is
a file for this install; `message` is the sentence to show when there is not. So

* offer: `available` with an `asset` (and its size, and the release page as text);
* up to date: neither, with "1.0.0 is the newest release.";
* a newer release this install cannot use: `available` without an `asset` — a checkout, or a release
  with no package for this machine's architecture;
* a check that could not run: neither, with the reason — GitHub unreachable, rate-limited
  (unauthenticated checks are 60 an hour per address, and the sentence says that limit is GitHub's),
  or no release published yet.

A check that could not run is an **answer**, not an error: the pane prints the sentence either way,
and only one of the two is a bug in this app. `Result<_, String>` is kept for the download, where a
person asked for an action and it did not happen.

**What it refuses**, each because accepting it would be worse than saying no:

* a tag that is not a version (`nightly`) — named as unreadable rather than reported as "you are up
  to date", which would be a claim about a comparison that never happened;
* a pre-release (`2.0.0-rc1`), even though GitHub's `latest` endpoint already excludes prereleases,
  because a tag that says `-rc1` is one whether or not GitHub was told;
* an asset built for another architecture, or a release with two candidates and no architecture to
  choose between them — never "the wrong package, just in case";
* any host but GitHub's own, checked with a written-out list rather than a suffix test (`evil-github.com`
  and `github.com.evil.example` both end in something a suffix test would accept) and read through the
  `url` crate so `https://github.com@evil.example/` is judged by the host it actually reaches. The
  same function is the client's redirect policy, which is what makes the guarantee true of a redirect
  and not only of the first request;
* a file whose name is a path (`../../.bashrc`), an empty body, or a body past 256 MiB;
* a download with no downloads folder to land in — a sentence naming the release page, not a new
  directory somewhere unexpected.

**What it writes, and what it never does.** The package goes to the directory Tauri's path resolver
calls the download directory (`$XDG_DOWNLOAD_DIR`, falling back to `$HOME/Downloads`), created if it
does not exist yet, named exactly as the release names it, and replaced if a second attempt brings
the same file. That is the only thing this app writes outside a workspace and `~/.codify`, and it is
written when a person asks for it. `codify_update_download` **takes no argument**, so a page — or a
skill, or anything else that can call `invoke` — cannot choose what is fetched or where it goes: the
asset is re-derived in Rust from the same release the check read.

**How far it has come, while it runs.** A package is tens of megabytes and the download takes
minutes, which is long enough that a button with one sentence under it reads as a button that did
nothing. So the body is read in chunks as the server sends them, and the shell announces its receipts
on `update-progress`; the pane draws a bar and a line from them (`updateProgress.ts`, and `DownloadBar`
in `AboutPane.tsx`). Three things about it are decisions rather than details:

* **The announcements are throttled to the bar's own resolution.** One message per network chunk would
  be thousands of hops into the webview that is drawing the bar, so `announce` in `update.rs` sends one
  per whole percent — plus the first byte, which is what moves the pane off "starting", and the last,
  which is what stops a bar at 97% — and a 90 MB AppImage is a hundred-odd messages rather than a
  thousand. A server that sends no `Content-Length` leaves no percentage to divide by, and the
  interval becomes one message every 4 MB.
* **A bar with no figure to report is not a bar at zero.** When neither the response nor the release
  metadata carries a size, the pane says how many bytes have arrived and draws no fill, rather than
  dividing by a number nobody has. Nothing is reported to assistive technology as a percentage in that
  state, and the percentage that *is* reported is floored: a rounded bar at 100% while bytes are still
  arriving says the download is finished when it is not.
* **The payload is only a byte count.** Bytes received, and the size the server announced — no address,
  no path, nothing about the machine. Nothing acts on it either: the command's own answer is still what
  says where the file landed, and the subscription ends with the call that started it (`downloadUpdate`
  in `api.ts`), so a failed download or a pane that went away leaves nobody listening.

It installs nothing. The deb is handed over with the `apt` command to run; the AppImage with
`chmod +x`. That text is *text*: this module never spawns a process, never escalates and never
replaces the copy that is running, and it writes no configuration — agent config comes from the
settings routes alone (`docs/02` §1). The ACL manifest names both commands one by one like every
other (`allow-codify-update-check`, `allow-codify-update-download` in
`src-tauri/permissions/shell.json`, referenced only by `capabilities/default.json`'s
`webviews: ["main"]`), so a `browser-*` page holds none of it.

## 5. Checking what you downloaded

A connection that drops part-way leaves a file `apt` will happily try to unpack, and an asset can be
re-uploaded after the page you read was rendered. The release workflow writes `sha256sum` output into
a `SHA256SUMS` asset beside the two packages (§3), so there is something to check against, and **the
Download button checks it before it writes anything** (audit of 2026-10-29: it used to write whatever
arrived). It reads the release's `SHA256SUMS` first, from the same hosts and under the same redirect
rule as the package (`update::fetch_sums`), so a release that publishes none is refused before tens of
megabytes are spent; then it hashes what arrived and compares it with the release's own line for that
name (`update::check_against_sums`, which reads the file exactly as `scripts/verify_package.py`'s
`parse_sums` does). A mismatch, a package the file does not list, and a file that is not `sha256sum`
output are each a sentence, and in each case nothing is written. A match is said in the answer
(`next_step`, and `sha256` beside it), with the same caveat as below.

For a package you downloaded some other way, or to check again:

```
make verify-package                                # whatever Codify packages are in ~/Downloads
make verify-package FILES=~/Downloads/Codify_1.0.0_amd64.deb
make verify-package SUMS=~/Downloads/SHA256SUMS     # the checksums you already downloaded
make verify-package TAG=v1.0.0                      # a named release instead of the newest
```

`scripts/verify_package.py` hashes each file in blocks — an AppImage is tens of megabytes, and there
is no reason to hold one in memory for its digest — and compares it with the release's own line for
that name. Three failures are told apart, because each needs a different action: a file that is not
there; a file the release never listed, which is a different release or a browser's `(1)` suffix on
the name and *not* a corrupt download; and a file whose bytes are not the published ones, which
prints both digests so the reader can see which side is wrong. A `SHA256SUMS` file that is not
`sha256sum` output at all is refused by line number rather than reported as "nothing matched", and a
tag that is not a tag (`--tag ../../etc`) never reaches an address. It exits 0 when everything
matched, 1 when anything did not, and 2 when there was nothing to check.

**It is not a signature, and it says so in its own output.** The checksums come from the same place
the package did, over the same connection, so anyone who could serve a wrong package could serve
matching checksums beside it. What a pass claims is the case that actually happens: the file is the
one that release published, and it is whole.

**Authenticity is the attestation.** Every published package and `SHA256SUMS` is a subject of a
GitHub build-provenance attestation, signed through Sigstore with a short-lived certificate for this
repository's release workflow — no key is stored in CI, and none can be forgotten or leaked. Check a
download with:

```
gh attestation verify ~/Downloads/Codify_1.3.6_amd64.deb --repo GeneticxCln/Codify
gh attestation verify ~/Downloads/Codify_1.3.6_amd64.AppImage --repo GeneticxCln/Codify
```

The attestation lives with the repository, not beside the file, which is why `--repo` is required. It
says these bytes were built by that workflow from that repository at that commit. It does not say a
person reviewed the code, and it does not replace the checksum: the checksum still catches a truncated
or re-uploaded download, while the attestation catches a file that was never built here.

Local, and deliberately not part of `make check` or `make ci`: it reaches GitHub for the checksums
unless you point it at a file you already have, and the gate may not depend on the network.

## 6. What is not here, and what it would take

* **No self-updating.** The Tauri updater plugin would let the app replace its own binary after
  checking a signature. That is a different decision from this one, not a later step of it: the
  build-provenance attestation in §5 authenticates a download a person chose, but it is not the
  updater plugin's signature format. Self-updating still needs a signing key held in CI, a public key
  pinned in `tauri.conf.json`, and a published policy for what happens when an update is half-applied
  — and it makes a running process replace itself, which is exactly the act invariant 9 keeps agents
  away from. It is left to the owner, deliberately.

**Dependency advisories are a CI job.** `make audit` runs `cargo audit --deny warnings` against the
shell's locked graph; `src-tauri/audit.toml` names every transitive advisory accepted today and the
Tauri/GTK constraint that keeps it there (the recipe passes the list as `--ignore` flags, because
cargo-audit 0.22 reads no config file). It is not part of `make check` because it needs the network
and a tool that is not a build dependency; `.github/workflows/check.yml` runs it on every push, and a
new advisory fails until it is fixed or deliberately recorded.
* **No check on launch, no timer, no background polling.** See §4: the request happens when someone
  presses the button.
* **No channels, no beta ring, no rollback.** `releases/latest` is the whole notion of "newer" here,
  and a pre-release is not offered.
* **Linux x86_64 only.** No rpm, no Flatpak, no AUR, no `aarch64` leg, and no Windows or macOS
  packaging: the crate is Linux-only (`gtk`, the machine tab's jail), so the other package formats
  are a porting question rather than a bundling flag.
* **No in-place AppImage self-replacement.** The new AppImage is downloaded beside the old one and the
  person swaps it. Replacing a file that is currently executing is a thing AppImageUpdate does with
  the runtime's cooperation, and doing it here would mean this app writing over the binary it is
  running from.

## 7. Cross-platform: a spike, and what it found

**Status: a spike, not a commitment.** The question was what a macOS or Windows build would have to replace. This is
the inventory, read from the code on 2026-11-02, and a decision at the end. Nothing here changes what the engine or the
window does today. "Linux only" in §6 is the current fact; this section says *which parts* make it so.

### 7.1 What ties Codify to Linux

| Part | Where | What it relies on | Elsewhere |
|---|---|---|---|
| **The command sandbox** | `engine/sandbox.py` (`bwrap` at `:523`, probe at `:226`), `engine/capabilities.py:137` | bubblewrap: user and mount namespaces, a read-only root, no network | **Not portable.** The jail is invariant 6's second layer. macOS has Seatbelt (`sandbox-exec`, deprecated) and no mount namespaces; Windows has nothing equivalent short of a container or WSL. `validate_argv` still holds, but "the filesystem is read-only except the workspace" would have to be re-earned per OS. |
| **The machine tab's jail and overlay** | `src-tauri/src/machine.rs`, `machine/layer.rs`, `machine/guard.rs` | `bwrap`, `unshare`, an unprivileged overlay mount with `userxattr` (`layer.rs:41`) | **Not portable.** The project is the lower layer of an overlay that is never written. There is no unprivileged overlay on macOS or Windows. |
| **The embedded browser pane** | `src-tauri/src/browser/` (`mod.rs`, `page_layer.rs`), `lib.rs`; `gtk = 0.18` in `Cargo.toml` | GTK 3 and WebKitGTK through wry: each page is a `gtk::Fixed` inside a `gtk::Overlay` the crate owns | **Rewrite.** wry builds child webviews differently on macOS (WKWebView) and Windows (WebView2). The layering that keeps a page under the DOM overlays is written against GTK widgets. |
| **Microphone capture** | `engine/speech.py` (`pw-record`, `pw-dump`) | PipeWire's command-line tools, and the session's sound server socket | **Replace.** CoreAudio or WASAPI through a library, or the webview's own `getUserMedia` (which was rejected on purpose: audio is recorded in the engine, not the page). |
| **Process teardown** | `engine/sandbox.py:746-799`, `engine/spawn_guard.py`, `engine/library.py:368`, `engine/app.py:1368` | `start_new_session`, `os.killpg`, `os.getpgid`, `prctl(PR_SET_PDEATHSIG)` (`spawn_guard.py:175`) | **Port.** POSIX process groups work on macOS. `PR_SET_PDEATHSIG` is Linux-only, but its polling fallback exists (`spawn_guard.py:219`). Windows needs job objects. |
| **Resource limits** | `engine/spawn_guard.py:96-132` | `resource.setrlimit` (`RLIMIT_AS`, `CPU`, `FSIZE`, `CORE`) | **Port on macOS** (some limits are not enforced there). **Absent on Windows**: the `resource` module does not exist. |
| **The state lock** | `engine/app.py:4` and `:227`, `engine/providers.py:1601` | `fcntl.flock` | **Port.** `fcntl` does not import on Windows; `msvcrt.locking` or a lock file with a PID would replace it. |
| **The desktop shell's child tracking** | `src-tauri/src/lib.rs:320-381` | `pidfd_open` and `pidfd_send_signal` (Linux 5.3), `libc::geteuid`, `SIGKILL` | **Port.** A pidfd names one process and never a reused pid; macOS has no equivalent, so the guarantee would weaken to a pid plus a start-time check. |
| **The folder dialog** | `engine/app.py:1282-1408` | `zenity` or `kdialog`, found with `shutil.which`, and `pgrep -f` over `/proc/<pid>/cmdline` | **Replace.** The shell's native dialog is the normal answer on every OS. |
| **Packaging and CI** | `tauri.conf.json` (`deb`, `appimage`), `.github/workflows/*` (`ubuntu-latest`, `ubuntu-22.04`) | Linux packages and runners; no `aarch64` leg | **New work**: dmg or pkg and notarization on macOS, msi or nsis and signing on Windows, a runner per OS, and the §5 attestation per artifact. |

### 7.2 What is already portable

Python 3.10 without the modules above; FastAPI, uvicorn and httpx; SQLite; `keyring` (it has macOS and Windows backends);
`git` and `rg` (separate programs the engine runs by name); the UI (React, Vite; no OS API); and the terminal tab, which
uses `portable-pty` (`Cargo.toml:52`). `lib.rs` already gates some Linux behaviour behind `#[cfg(target_os = "linux")]`
with a `not(...)` branch (`:326-346`, `:779-795`), so the shell is partly shaped for a second target. The engine's
filesystem code uses `getattr(os, "O_NOFOLLOW", 0)` (`fs.py:574`), which degrades rather than failing to import.

### 7.3 What it would take, by target

* **Linux-only today, and the table's first three rows are why.** Everything else is a port. Those three are the
  product's safety story: a read-only jail for commands, an overlay for the machine, and a page layer.
* **macOS** is the nearer target: POSIX process groups, `fcntl`, `git` and `keyring` all work. The cost is the sandbox
  (Seatbelt is deprecated and weaker than namespaces), the browser pane (WKWebView layering), audio and packaging. A
  macOS engine **without** the machine tab and with a weaker, clearly labelled sandbox is the smallest honest slice,
  and "clearly labelled" would mean `sandbox_isolation` reporting what it cannot do, as it already does when `bwrap` is
  missing (`engine/capabilities.py`). Without a jail `auto` already refuses the project's own code until a person sets
  `sandbox_unjailed_ok`, so a port starts closed rather than open.
* **Windows** is close to a different product. No namespaces, no `fcntl`, no `resource`, no process groups; the sandbox
  and machine tab would be a container or WSL requirement, which is not "local-first" in the way the rest of this
  document uses it.

### 7.4 Decision

**Stay Linux-only. The spike is closed and nothing is committed to.** The reasons are the table's first three rows: the
safety guarantees in `docs/00` §6.6 are stated in terms of a jail that exists on one OS, and a port that quietly
weakened them would make the invariant a different sentence on a different machine. This is a recommendation from the
inventory, not a measurement of effort (no port was attempted, so no estimate is claimed).

It would be worth reopening if one of these becomes true: a person needs the **engine and CLI** (`codify-cli`, `docs/09`
§10.23) on macOS without the desktop shell, which needs only the "Port" rows; or the sandbox gets a second backend with
a stated guarantee per OS. The first is the cheapest experiment, because the engine and the CLI are plain Python over
HTTP and the shell is optional for them.
