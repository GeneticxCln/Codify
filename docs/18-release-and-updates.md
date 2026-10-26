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
packages, and publishes them as a GitHub release on that tag. `workflow_dispatch` takes an existing
tag as well, for a release that has to be built again.

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

Both hold the **desktop shell**: the React UI, the Tauri window, the PTYs, the webview bridge, the
machine tab. Neither holds the engine, and neither can: the shell starts `python3 -m engine` from a
project checkout (`docs/04` §6's token handshake), so the app is one half of a pair that is expected
to sit beside a checkout. A package installed with no checkout anywhere opens a window that says so
in its own words — "No engine to run at … this shell starts `python3 -m engine` from the project
checkout …" — rather than opening empty, and there are two ways out, both honest:

* keep a checkout and launch the app from it (`make run-app`), or point the packaged shell at one
  with `CODIFY_ROOT=/path/to/Codify codify-desktop`;
* install the engine's Python dependencies into that checkout (`make setup` in the repository).

`scripts/install-local.sh` remains for the first of those: a `codify` command and a menu entry under
the user's own `~/.local`, no root, nothing copied out of the checkout. It is not a second way to
package the app and does not try to be.

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

The build step is one command, `cargo tauri build`, because the target list lives in
`tauri.conf.json`; a list repeated in the workflow would be a second place to forget. Tauri's own
`beforeBuildCommand` builds the UI first, so the window carries the real frontend.

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
| the binary runs from `/usr/` or `/opt/` | `deb` | the `.deb` |
| anything else — a build out of a checkout, or the launcher `scripts/install-local.sh` writes | `source` | no package at all: `git pull` in the checkout, with the release page named |

That third row is the important one. Offering a deb to someone running from a checkout would install
a *second* Codify beside the one they have. The `$APPIMAGE` check comes first because an AppImage
unpacks itself under `/tmp` and runs the binary from there, which would otherwise read as a checkout.

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

It installs nothing. The deb is handed over with the `apt` command to run; the AppImage with
`chmod +x`. That text is *text*: this module never spawns a process, never escalates and never
replaces the copy that is running, and it writes no configuration — agent config comes from the
settings routes alone (`docs/02` §1). The ACL manifest names both commands one by one like every
other (`allow-codify-update-check`, `allow-codify-update-download` in
`src-tauri/permissions/shell.json`, referenced only by `capabilities/default.json`'s
`webviews: ["main"]`), so a `browser-*` page holds none of it.

## 5. What is not here, and what it would take

* **No self-updating.** The Tauri updater plugin would let the app replace its own binary after
  checking a signature. That is a different decision from this one, not a later step of it: it needs
  a signing key held in CI, a public key pinned in `tauri.conf.json`, and a published policy for what
  happens when an update is half-applied — and it makes a running process replace itself, which is
  exactly the act invariant 9 keeps agents away from. It is left to the owner, deliberately.
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
