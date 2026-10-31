//! Checking for a newer release, and fetching the package this install can use.
//!
//! Until this module existed, finding out whether a newer Codify existed meant reading the
//! repository, and getting one meant building it. The About tab answers both now, and
//! everything it answers with lives here rather than in the command handlers, because the
//! parts that decide *what* is offered — which package this install is, whether a tag is
//! newer, which asset belongs to that package — are the parts a test can hold, and
//! `cargo test` runs them (see `check-tauri` in the Makefile). What is left in `lib.rs` is
//! the app handle, which needs a running app.
//!
//! Four decisions are worth stating, because each one is a guarantee someone could
//! otherwise assume away:
//!
//! * **One address, one host.** The check asks `api.github.com` for this repository's
//!   newest release and nothing else, and a redirect anywhere but GitHub's own hosts is
//!   refused rather than followed ([`allowed_url`]). It is unauthenticated, so it carries
//!   no key; and it carries nothing *about* the person either — a GET of a public JSON
//!   document, with a User-Agent naming this app's version, is the whole of it.
//! * **A person starts it.** Nothing here runs on a timer or on mount, so Codify is not a
//!   program that phones home on every launch. The check runs when someone clicks it.
//! * **While a download runs, it says how far it has come — and nothing else.** A package is tens
//!   of megabytes and takes minutes, which is long enough that a button with one sentence under it
//!   reads as a button that did nothing. So the shell announces its receipts as they arrive
//!   ([`announce`], one message per whole percent rather than one per network chunk) and the window
//!   draws a bar from them. The payload is a byte count and the size the server announced: no
//!   address, no path, nothing about the machine, and nothing in the window acts on it.
//! * **It installs nothing.** A package is written where downloads go, and the sentence
//!   saying what to do next is *text*. This module never spawns a process, never escalates
//!   and never touches the copy of Codify that is running: no `sudo`, no `dpkg`, no
//!   replacing a binary (`docs/18` §4).
//! * **It refuses more than it accepts.** A tag it cannot read is not "a newer version", a
//!   pre-release is not offered over a stable release, an asset built for another
//!   architecture is not fetched "just in case", and a name that is not a plain file name
//!   is not written anywhere.

use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

/// The one document this module asks for.
pub(crate) const RELEASES_API: &str =
    "https://api.github.com/repos/GeneticxCln/Codify/releases/latest";

/// Where a person is sent when this module will not fetch something for them.
pub(crate) const RELEASES_PAGE: &str = "https://github.com/GeneticxCln/Codify/releases";

/// The event the shell emits while a download is running, naming how far it has come.
///
/// Named here rather than in `lib.rs` so that the module owning the download owns the name, and so
/// the test at the bottom of this file can read `ui/src/shellEvents.ts` and fail if the window has
/// stopped looking for it — the same move `terminal.rs` makes for its three.
pub(crate) const UPDATE_PROGRESS_EVENT: &str = "update-progress";

/// The hosts an answer — or a redirect — may come from.
///
/// Written out, never matched as a suffix: `evil-github.com` and
/// `github.com.evil.example` both end in something a suffix test would accept, and the
/// second one is the whole shape of the attack this list exists to refuse.
const ALLOWED_HOSTS: [&str; 4] = [
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
];

/// The most this module will pull into memory, and therefore the largest package it will
/// write.
///
/// A Linux AppImage for this app is tens of megabytes and a deb is smaller, so this is
/// headroom rather than a working limit: it is here so that a wrong URL which streams
/// forever ends in a sentence instead of in an out-of-memory kill.
const MAX_ASSET_BYTES: u64 = 256 * 1024 * 1024;

/// How long a check may take end to end, and how long any connection may take to open. A
/// check a person is waiting on gets an answer or a sentence; it does not hang.
const CHECK_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(30);
const CONNECT_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(10);

/// A download is bigger than a check and runs on a slower clock, so it gets its own bound.
/// Ten minutes is still a failure rather than a hang, which is the point of having one.
const DOWNLOAD_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(600);

// ── Which package this install takes ────────────────────────────────────────

/// Which package a copy of Codify would be replaced by.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub(crate) enum Package {
    /// Running as an AppImage. The runtime exports the path of the file it is running in
    /// `$APPIMAGE`, and that one file is the whole install.
    AppImage,
    /// Installed by Debian's package manager: a `.deb` puts the binary under `/usr`.
    Deb,
    /// Installed under `/usr` by a distribution that is not Debian-family. The binary is
    /// a package, but this app cannot name the manager that owns it, so offering a `.deb`
    /// and printing `apt` would be instructions for the wrong system.
    Distro,
    /// A build out of a checkout — `make run-app`, or the launcher
    /// `scripts/install-local.sh` writes. There is no package for this, and offering one
    /// would put a *second* Codify beside it, so the honest answer is how to update the
    /// checkout.
    Source,
}

impl Package {
    /// The extension a package for this install must carry, or `None` for a checkout.
    fn extension(self) -> Option<&'static str> {
        match self {
            Package::AppImage => Some(".appimage"),
            Package::Deb => Some(".deb"),
            Package::Distro | Package::Source => None,
        }
    }

    /// What a package of this kind is called, for a sentence that has to name one.
    fn noun(self) -> &'static str {
        match self {
            Package::AppImage => "AppImage",
            Package::Deb => "deb",
            Package::Distro => "distribution package",
            Package::Source => "package",
        }
    }
}

/// Which package a copy running from `exe` with `appimage` in its environment would take.
///
/// Pure, and takes its evidence as arguments rather than reading the environment, so all
/// three answers can be tested without being any of them. `appimage` is `$APPIMAGE`, set
/// by the AppImage runtime to the path of the file being run; `exe` is
/// `std::env::current_exe()`. The order matters: an AppImage unpacks itself under `/tmp`
/// and runs the binary from there, so the variable has to be read before the path is, or
/// every AppImage would be mistaken for a checkout.
pub(crate) fn install_package(exe: Option<&Path>, appimage: Option<&str>) -> Package {
    if appimage.is_some_and(|path| !path.trim().is_empty()) {
        return Package::AppImage;
    }
    match exe {
        // A deb puts its binary under `/usr`. `/opt` is where a hand-built or
        // distribution package goes, and this app cannot tell a `.deb` there from an
        // Arch package; naming apt for one would be a guess. A checkout is the one
        // answer that does not claim a package manager it did not see.
        Some(exe) if exe.starts_with("/usr/") => Package::Deb,
        _ => Package::Source,
    }
}

/// Whether `/etc/os-release` names Debian, Ubuntu, or a derivative of either.
///
/// `ID_LIKE` is what Linux Mint (`ID=linuxmint`) and Pop!_OS (`ID=pop`) use to say they
/// are Ubuntu-based; Debian itself says `ID=debian`. Anything else — Arch, Fedora,
/// openSUSE — is not a system where `apt` is the answer.
fn is_debian_family(os_release: &str) -> bool {
    os_release.lines().any(|line| {
        let Some((key, value)) = line.split_once('=') else {
            return false;
        };
        if key != "ID" && key != "ID_LIKE" {
            return false;
        }
        value
            .trim_matches('"')
            .split_whitespace()
            .any(|id| id.eq_ignore_ascii_case("debian") || id.eq_ignore_ascii_case("ubuntu"))
    })
}

fn this_distro_is_debian_family() -> bool {
    std::fs::read_to_string("/etc/os-release")
        .map(|text| is_debian_family(&text))
        .unwrap_or(false)
}

/// The package for the copy that is running now.
///
/// The path evidence says "a package under `/usr`", and the release file decides *which*
/// package manager that path belongs to. Keeping the two apart is what lets the answer be
/// tested without being any one distribution.
pub(crate) fn package_for_this_install() -> Package {
    let exe = std::env::current_exe().ok();
    let appimage = std::env::var("APPIMAGE").ok();
    let resolved = exe.as_deref().and_then(|path| path.canonicalize().ok());
    classify_install(
        resolved.as_deref().or(exe.as_deref()),
        appimage.as_deref(),
        this_distro_is_debian_family(),
    )
}

fn classify_install(exe: Option<&Path>, appimage: Option<&str>, debian_family: bool) -> Package {
    match install_package(exe, appimage) {
        Package::Deb if !debian_family => Package::Distro,
        package => package,
    }
}

/// This machine's architecture in the spelling a package name uses.
///
/// Tauri names its bundles with the Debian architecture, not Rust's: an `x86_64` build is
/// `Codify_1.0.0_amd64.deb`. This is that translation and nothing else.
fn package_arch() -> &'static str {
    match std::env::consts::ARCH {
        "x86_64" => "amd64",
        "aarch64" => "arm64",
        "i686" | "i586" => "i386",
        other => other,
    }
}

// ── Is the release newer, and is it one we offer? ───────────────────────────

/// A version as three numbers, and whether it arrived with a suffix, or `None` when the
/// string is not a version at all.
///
/// `v` is stripped (tags carry it; `tauri.conf.json` does not), a missing minor or patch
/// counts as zero (`1.2` and `1.2.0` are the same release), and `1.2.3-rc1` / `1.2.3+build`
/// parse with the suffix noted — which [`is_newer`] then refuses to offer.
fn parse_version(raw: &str) -> Option<(u64, u64, u64, bool)> {
    let trimmed = raw.trim();
    let body = trimmed.strip_prefix('v').unwrap_or(trimmed);
    let (core, suffixed) = match body.find(['-', '+']) {
        Some(at) => (&body[..at], true),
        None => (body, false),
    };
    let mut numbers = [0u64; 3];
    let mut fields = 0;
    for (index, part) in core.split('.').enumerate() {
        // Four components is not a version this app will compare, and neither is an empty
        // one: `1..2` is a tag someone mistyped, not a release.
        if index > 2 || part.is_empty() {
            return None;
        }
        numbers[index] = part.parse().ok()?;
        fields += 1;
    }
    if fields == 0 {
        return None;
    }
    Some((numbers[0], numbers[1], numbers[2], suffixed))
}

/// Is `candidate` numerically ahead of `current`, ignoring any suffix on either?
fn is_ahead(candidate: &str, current: &str) -> bool {
    match (parse_version(candidate), parse_version(current)) {
        (Some((major, minor, patch, _)), Some(now)) => {
            (major, minor, patch) > (now.0, now.1, now.2)
        }
        _ => false,
    }
}

/// Is `candidate` a newer *stable* release than `current`?
///
/// Three ways this says no, each deliberate. A version either side cannot be read — a tag
/// like `nightly`, or something with a fourth field — is never called newer, because a
/// button that trusts a string it cannot parse is worse than one that says nothing. A
/// pre-release (`2.0.0-rc1`) is not offered to someone on a stable release, even though
/// GitHub's own "latest release" endpoint already excludes prereleases: a tag that says
/// `-rc1` is a pre-release whether or not GitHub was told, and this is the only check
/// standing between a release-candidate tag and everybody's pane. And equal versions are
/// equal, so `1.2` does not offer an update to `1.2.0`.
pub(crate) fn is_newer(candidate: &str, current: &str) -> bool {
    parse_version(candidate).is_some_and(|(_, _, _, suffixed)| !suffixed)
        && is_ahead(candidate, current)
}

/// Whether a string is a name this module will write a file under.
///
/// An asset name comes from a release this repository publishes, so this is not defence
/// against an attacker so much as a refusal to let a name mean a path: `../../.bashrc` is
/// never a package, and neither is a hidden file or an empty string.
fn is_plain_file_name(name: &str) -> bool {
    !name.is_empty()
        && name != "."
        && name != ".."
        && !name.starts_with('.')
        && !name.contains(['/', '\\'])
}

// ── The release, as GitHub describes it ─────────────────────────────────────

#[derive(Debug, Clone, Deserialize)]
struct ReleasePayload {
    #[serde(default)]
    tag_name: Option<String>,
    #[serde(default)]
    published_at: Option<String>,
    #[serde(default)]
    // Read, and never trusted over the tag itself: a release GitHub does not call a
    // pre-release can still be tagged `2.0.0-rc1`, which is what a person would be running.
    prerelease: bool,
    #[serde(default)]
    html_url: Option<String>,
    #[serde(default)]
    assets: Vec<AssetPayload>,
}

#[derive(Debug, Clone, Deserialize)]
struct AssetPayload {
    #[serde(default)]
    name: String,
    #[serde(default)]
    size: u64,
    #[serde(default)]
    browser_download_url: String,
}

/// The architecture an asset's name carries, in the Debian spelling this app runs as.
///
/// `amd64` is the tag Tauri writes; `x86_64` and `x64` are the same machine under other
/// names. `None` means the name says nothing about an architecture, which is the only
/// case a single candidate may be accepted without one.
fn named_arch(name: &str) -> Option<&'static str> {
    let lower = name.to_ascii_lowercase();
    const NAMES: [(&str, &[&str]); 3] = [
        ("amd64", &["amd64", "x86_64", "x64"]),
        ("arm64", &["arm64", "aarch64"]),
        ("i386", &["i386", "i686"]),
    ];
    NAMES
        .iter()
        .find(|(_, aliases)| aliases.iter().any(|alias| lower.contains(alias)))
        .map(|(arch, _)| *arch)
}

/// The asset that belongs to this install, or `None` when the release has none.
///
/// The extension decides the kind, and the architecture in the name decides between two
/// builds of that kind: a release carrying an `arm64` deb must not be handed to an `amd64`
/// person. An asset whose name has no architecture is accepted only when it is the *only*
/// candidate — a single unnamed `.deb` is unambiguous, two of them would be a guess. A
/// single candidate that names another architecture is not a fallback: an `amd64` package
/// cannot run on `arm64`, and offering it would turn "no build for you" into a broken
/// download.
///
/// A name is only ever a name: the address that gets fetched comes from the payload's own
/// `browser_download_url`, and is subject to the same host check as everything else here.
fn pick_asset<'a>(
    assets: &'a [AssetPayload],
    package: Package,
    arch: &str,
) -> Option<&'a AssetPayload> {
    let extension = package.extension()?;
    let matches = |asset: &AssetPayload| {
        is_plain_file_name(&asset.name) && asset.name.to_lowercase().ends_with(extension)
    };
    let candidates: Vec<&AssetPayload> = assets.iter().filter(|asset| matches(asset)).collect();
    if let Some(found) = candidates
        .iter()
        .find(|asset| named_arch(&asset.name) == Some(arch))
    {
        return Some(*found);
    }
    match candidates.as_slice() {
        [only] if named_arch(&only.name).is_none() => Some(*only),
        _ => None,
    }
}

/// May this module speak to this URL?
///
/// `https` and an explicit host, both read through the `url` crate rather than by
/// string-matching, so a URL carrying credentials
/// (`https://github.com@evil.example/`) is judged by the host it actually reaches. This is
/// the same function the redirect policy consults for every hop, which is what makes "it
/// only ever talks to GitHub" true of a redirect as well as of the first request.
fn allowed_url(url: &str) -> bool {
    let Ok(parsed) = url::Url::parse(url) else {
        return false;
    };
    parsed.scheme() == "https"
        && parsed
            .host_str()
            .is_some_and(|host| ALLOWED_HOSTS.contains(&host))
}

/// The release's own page, when the payload's is one this app would accept, and this
/// repository's releases page otherwise.
///
/// Never used to fetch anything: it is text for a person to read, because this app opens
/// no external browser (`docs/02` §3.3).
fn release_url(payload: &ReleasePayload) -> String {
    payload
        .html_url
        .as_deref()
        .filter(|url| allowed_url(url))
        .unwrap_or(RELEASES_PAGE)
        .to_string()
}

/// A client that will only ever reach GitHub, and a `User-Agent` naming this app.
///
/// GitHub's API refuses a request with no `User-Agent`, and the honest one names the app
/// and its version rather than pretending to be a browser.
fn client(timeout: std::time::Duration, current: &str) -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .user_agent(format!("Codify/{current}"))
        .connect_timeout(CONNECT_TIMEOUT)
        .timeout(timeout)
        .redirect(reqwest::redirect::Policy::custom(|attempt| {
            if attempt.previous().len() >= 5 {
                attempt.error("too many redirects")
            } else if allowed_url(attempt.url().as_str()) {
                attempt.follow()
            } else {
                attempt.error("refused a redirect away from GitHub")
            }
        }))
        .build()
        .map_err(|err| format!("could not prepare the request ({err})"))
}

/// The newest release as GitHub reports it — `Ok(None)` when there is none yet.
///
/// Every failure is a sentence meant to be read, because this is the one place the app
/// talks to a server that is neither the engine nor a model provider, and a refusal has to
/// explain itself in the pane (`docs/18` §4).
async fn fetch_release(client: &reqwest::Client) -> Result<Option<ReleasePayload>, String> {
    let response = client
        .get(RELEASES_API)
        .header("Accept", "application/vnd.github+json")
        .send()
        .await
        .map_err(|err| format!("could not reach GitHub ({err})"))?;
    let status = response.status();
    if status == reqwest::StatusCode::NOT_FOUND {
        return Ok(None);
    }
    if !status.is_success() {
        let message = if status == reqwest::StatusCode::FORBIDDEN
            || status == reqwest::StatusCode::TOO_MANY_REQUESTS
        {
            // GitHub allows sixty unauthenticated requests an hour per address, and this
            // app sends at most one per click. It is still the likeliest refusal there is,
            // so it gets a sentence naming whose limit it is.
            format!(
                "GitHub is rate-limiting this machine's checks ({status}); the limit is GitHub's, \
                 not this app's, and it passes. The release page is {RELEASES_PAGE}"
            )
        } else {
            format!("GitHub answered {status} for the newest release")
        };
        return Err(message);
    }
    response
        .json::<ReleasePayload>()
        .await
        .map(Some)
        .map_err(|err| format!("GitHub's answer was not a release this app could read ({err})"))
}

// ── What the pane is shown ──────────────────────────────────────────────────

/// What the About tab shows after a check.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub(crate) struct UpdateCheck {
    /// The running app's version, from `tauri.conf.json` — the same string the pane prints
    /// above the button.
    pub current: String,
    /// The newest release's version, when its tag was one this app could read.
    pub latest: Option<String>,
    /// The tag as published, which is what a person recognises from the repository.
    pub tag: Option<String>,
    pub published_at: Option<String>,
    /// The release's page, as text. This app opens no browser from here.
    pub url: String,
    /// Which package this install would take.
    pub package: Package,
    /// The file name of the package for this install, when there is one to fetch.
    pub asset: Option<String>,
    pub asset_bytes: Option<u64>,
    /// Whether a newer stable release exists. `true` with no `asset` is a real answer —
    /// a checkout has nothing to download — so `message` says what to do instead.
    pub available: bool,
    /// The sentence to show when there is nothing to download: up to date, a request that
    /// could not run, or a package this install cannot use. `None` exactly when there is an
    /// `asset` to offer.
    pub message: Option<String>,
}

/// What the pane is shown after a package has been written.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub(crate) struct UpdateDownload {
    /// Where it landed.
    pub path: String,
    pub bytes: u64,
    pub package: Package,
    pub tag: String,
    /// What to do with it, as text. The shell runs none of this.
    pub next_step: String,
    /// The package's SHA-256, which matched the release's `SHA256SUMS` before it was written.
    pub sha256: String,
}

/// An empty envelope, so every answer is built the same way.
fn checked(current: &str, package: Package) -> UpdateCheck {
    UpdateCheck {
        current: current.to_string(),
        latest: None,
        tag: None,
        published_at: None,
        url: RELEASES_PAGE.to_string(),
        package,
        asset: None,
        asset_bytes: None,
        available: false,
        message: None,
    }
}

/// An answer with nothing to download and a sentence saying why.
fn unavailable(current: &str, package: Package, message: String) -> UpdateCheck {
    let mut check = checked(current, package);
    check.message = Some(message);
    check
}

/// Turn the release GitHub reported into what the pane shows.
///
/// Pure: the payload, the running version, the package and the architecture go in, the
/// answer comes out, and every branch here is a test. The order of the refusals is the
/// contract — unreadable tag, then pre-release, then "not newer", then "newer but no
/// package for this machine" — because each one is a different thing to tell a person and
/// the wrong one is misleading rather than merely unhelpful.
fn decide(payload: &ReleasePayload, current: &str, package: Package, arch: &str) -> UpdateCheck {
    let tag = payload.tag_name.clone().unwrap_or_default();
    let mut check = checked(current, package);
    check.tag = (!tag.trim().is_empty()).then(|| tag.clone());
    check.latest =
        parse_version(&tag).map(|(major, minor, patch, _)| format!("{major}.{minor}.{patch}"));
    check.published_at = payload.published_at.clone();
    check.url = release_url(payload);

    if !is_newer(&tag, current) {
        check.message = Some(match parse_version(&tag) {
            // A tag nobody can compare: say which tag, rather than claim to be current
            // against a version that was never read.
            None => format!(
                "the newest release is tagged {}, which is not a version this app can compare. \
                 Its page is {}",
                check.tag.clone().unwrap_or_else(|| "(unnamed)".to_string()),
                check.url
            ),
            // GitHub's "latest release" excludes prereleases, so this is a tag that says
            // it is one without GitHub being told — and it is ahead, so it is not "you are
            // up to date" either.
            Some((_, _, _, true)) if payload.prerelease || is_ahead(&tag, current) => {
                format!(
                    "the newest release is the pre-release {}, and this app offers stable \
                     releases only. Its page is {}",
                    tag, check.url
                )
            }
            Some(_) => format!("{} is the newest release.", check.current),
        });
        return check;
    }

    // A newer stable release, so there is something to offer. A checkout cannot take a
    // package — installing one would put a second Codify beside it — so the answer is how
    // to update the checkout itself.
    check.available = true;
    if package == Package::Distro {
        check.message = Some(format!(
            "{tag} is newer, and this copy was installed by your distribution's package \
             manager. Update it there, or take the AppImage from {}",
            check.url
        ));
        return check;
    }
    if package == Package::Source {
        check.message = Some(format!(
            "{tag} is newer, and this copy runs from a checkout: `git pull` in it, or take a \
             package from {}",
            check.url
        ));
        return check;
    }
    match pick_asset(&payload.assets, package, arch) {
        Some(asset) => {
            check.asset = Some(asset.name.clone());
            check.asset_bytes = Some(asset.size);
        }
        None => {
            check.message = Some(format!(
                "{tag} is newer, but that release carries no {} built for this machine \
                 ({arch}). Its page is {}",
                package.noun(),
                check.url
            ));
        }
    }
    check
}

/// What the update button shows: the release, or the reason there is none.
///
/// Never an `Err`. A check that could not run is one of the answers this command has — the
/// pane prints the sentence either way — and making it an error would put a failed network
/// request in the same channel as a bug in this app.
pub(crate) async fn check(current: &str, package: Package) -> UpdateCheck {
    let arch = package_arch();
    let client = match client(CHECK_TIMEOUT, current) {
        Ok(client) => client,
        Err(message) => return unavailable(current, package, message),
    };
    match fetch_release(&client).await {
        Ok(Some(payload)) => decide(&payload, current, package, arch),
        Ok(None) => unavailable(
            current,
            package,
            format!(
                "no release has been published yet, so there is nothing to compare against. \
                 The first one appears at {RELEASES_PAGE}"
            ),
        ),
        Err(message) => unavailable(current, package, message),
    }
}

// ── How far the download has come ───────────────────────────────────────────

/// How far a download has come, as the About pane's bar needs it.
///
/// The window is told this and nothing else — a byte count and, when the server sent one, the size
/// it announced up front. No address, no path, nothing about the machine: the person clicked
/// Download in this window, and this is the same fact the button already draws (docs/18 §4).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub(crate) struct UpdateProgress {
    /// Bytes received so far.
    pub received: u64,
    /// The size the server announced, or `None` when it announced none. The pane falls back to the
    /// size the release reported, and shows a size-less wait only when neither of them exists.
    pub total: Option<u64>,
}

/// How far apart the announcements are when there is no size to take a percentage of.
const ANNOUNCE_EVERY_WITHOUT_A_TOTAL: u64 = 4 * 1024 * 1024;

/// Is this receipt worth a message to the window?
///
/// A response body arrives in thousands of chunks, and one message each would be thousands of IPC
/// messages to move a bar from 0 to 100 — every one of them a hop into the webview that is already
/// doing the work of drawing it. So this decides, purely, which receipts are sent: the first byte
/// (which is what moves the pane off "starting"), one per whole percent, and — from the caller, not
/// from here — the last. `announced` is the byte count that was last sent.
///
/// A server that sends no `Content-Length` leaves no percentage to divide by, and the interval
/// becomes a size instead. That is why this is a function of three values and not a loop: the one
/// interesting decision in the download path is testable without a network.
pub(crate) fn announce(received: u64, total: Option<u64>, announced: Option<u64>) -> bool {
    if received == 0 {
        return false;
    }
    match (announced, total) {
        (None, _) => true,
        (Some(last), Some(total)) if total > 0 => {
            // `saturating_mul`, because a `Content-Length` is the server's number and can claim
            // more bytes than a percentage of them fits in.
            let percent = |count: u64| count.min(total).saturating_mul(100) / total;
            percent(received) > percent(last)
        }
        (Some(last), _) => received >= last.saturating_add(ANNOUNCE_EVERY_WITHOUT_A_TOTAL),
    }
}

// ── Writing the package ─────────────────────────────────────────────────────

/// Where a downloaded package is written.
///
/// `tauri` is what Tauri's own path resolver answered (the XDG download directory, falling
/// back to `$HOME/Downloads`), and `home` is the environment it could be resolved from when
/// it answered nothing. Deliberately no third guess: writing a package into a home
/// directory, or into the directory the app happens to be running from, would be this app
/// choosing a place for someone's file. A Downloads folder that does not exist *yet* is
/// made, because a person clicking Download has already said where this goes.
pub(crate) fn download_dir_for(
    tauri: Option<PathBuf>,
    home: Option<String>,
) -> Result<PathBuf, String> {
    if let Some(dir) = tauri.filter(|dir| !dir.as_os_str().is_empty()) {
        return Ok(dir);
    }
    if let Some(home) = home.filter(|home| !home.trim().is_empty()) {
        return Ok(PathBuf::from(home).join("Downloads"));
    }
    Err(format!(
        "this app could not work out where your downloads go, so nothing was written. \
         The package is on {RELEASES_PAGE}"
    ))
}

/// Write `bytes` as `name` in `into`, and report where they went.
///
/// The only place this module touches a disk. Both refusals above the write are the reason
/// it is a separate function: the size cap is checked against what actually arrived rather
/// than against what the release claimed, and the name has to be a plain file name, so
/// nothing here can write outside `into` or over a file whose name a release chose to be
/// clever.
pub(crate) async fn write_asset(into: &Path, name: &str, bytes: &[u8]) -> Result<PathBuf, String> {
    if !is_plain_file_name(name) {
        return Err(format!(
            "the release names a file this app will not write ({name:?}), so nothing was written"
        ));
    }
    if bytes.is_empty() {
        return Err(format!("{name} came back empty, so nothing was written"));
    }
    if bytes.len() as u64 > MAX_ASSET_BYTES {
        return Err(format!(
            "{name} is {} bytes, past this app's limit of {MAX_ASSET_BYTES}; take it from \
             {RELEASES_PAGE}",
            bytes.len()
        ));
    }
    tokio::fs::create_dir_all(into)
        .await
        .map_err(|err| format!("could not use {} ({err})", into.display()))?;
    let path = into.join(name);
    // Written beside the destination and renamed over it, so a crash or a full disk
    // leaves a `.part` file rather than a truncated package under the real name. The
    // rename also replaces a symlink instead of following it: without this, a link at
    // `~/Downloads/Codify.deb` made the download write through it to wherever it pointed.
    static PART: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let part = into.join(format!(
        ".{name}.{}.{}.part",
        std::process::id(),
        PART.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
    ));
    tokio::fs::write(&part, bytes).await.map_err(|err| {
        let _ = std::fs::remove_file(&part);
        format!("could not write {} ({err})", path.display())
    })?;
    if let Err(err) = tokio::fs::rename(&part, &path).await {
        let _ = tokio::fs::remove_file(&part).await;
        return Err(format!("could not write {} ({err})", path.display()));
    }
    Ok(path)
}

// ── Checking the package against the release's own checksums ────────────────

/// The name the release workflow publishes its checksums under, beside the packages (`docs/18`).
const SUMS_ASSET: &str = "SHA256SUMS";

/// A checksums file lists a handful of packages. Anything larger is not one, and is not read into memory.
const MAX_SUMS_BYTES: u64 = 64 * 1024;

/// `{file name: digest}` from the text of a `SHA256SUMS` file, digests lowercased.
///
/// The same reading as `scripts/verify_package.py`'s `parse_sums`, so the button and `make verify-package` agree
/// about what a checksums file says: a line that is not `sha256sum` output (`<64 hex> <space or *><name>`) is
/// refused with its number rather than skipped, since a mangled file would otherwise read as "this package is not
/// listed"; and a file that lists nothing is refused, since comparing against nothing proves nothing.
pub(crate) fn parse_sums(text: &str) -> Result<std::collections::HashMap<String, String>, String> {
    let mut sums = std::collections::HashMap::new();
    for (number, line) in text.lines().enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        let digest = line
            .get(..64)
            .filter(|d| d.bytes().all(|b| b.is_ascii_hexdigit()));
        let separator = line.get(64..66).filter(|s| *s == "  " || *s == " *");
        let name = line.get(66..).filter(|n| !n.is_empty());
        match (digest, separator, name) {
            (Some(digest), Some(_), Some(name)) => {
                sums.insert(name.to_string(), digest.to_ascii_lowercase());
            }
            _ => {
                return Err(format!(
                    "line {} of the release's {SUMS_ASSET} is not `<sha256>  <name>`, so it is not \
                     `sha256sum` output",
                    number + 1
                ))
            }
        }
    }
    if sums.is_empty() {
        return Err(format!(
            "the release's {SUMS_ASSET} lists no files, so there is nothing to check the package against"
        ));
    }
    Ok(sums)
}

/// The SHA-256 of `bytes`, as the lowercase hex `sha256sum` prints.
fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// Whether `bytes`, downloaded as `name`, are the file the release's checksums list under that name.
///
/// `Ok` carries the digest, for the sentence that says it matched. Every refusal says that nothing was written,
/// because nothing is: this runs before [`write_asset`]. It is not a signature, and nothing here says it is: the
/// checksums come from the same release as the package, so what a match proves is that the file is whole and is the
/// one that release published.
pub(crate) fn check_against_sums(
    name: &str,
    bytes: &[u8],
    sums_text: &str,
) -> Result<String, String> {
    let sums = parse_sums(sums_text)?;
    let Some(expected) = sums.get(name) else {
        return Err(format!(
            "the release's {SUMS_ASSET} does not list {name}, so it could not be checked and nothing was \
             written. Take it from {RELEASES_PAGE}"
        ));
    };
    let actual = sha256_hex(bytes);
    if &actual != expected {
        return Err(format!(
            "{name} does not match the release's {SUMS_ASSET} (expected {expected}, got {actual}), so nothing \
             was written. The download was damaged or changed on the way; try again, or take it from \
             {RELEASES_PAGE}"
        ));
    }
    Ok(actual)
}

/// The release's checksums, as text, from the same hosts and under the same redirect rule as everything else here.
async fn fetch_sums(client: &reqwest::Client, payload: &ReleasePayload) -> Result<String, String> {
    let asset = payload
        .assets
        .iter()
        .find(|asset| asset.name == SUMS_ASSET)
        .ok_or_else(|| {
            format!(
                "that release publishes no {SUMS_ASSET}, so a package from it could not be checked and none was \
                 downloaded. Its page is {RELEASES_PAGE}"
            )
        })?;
    if !allowed_url(&asset.browser_download_url) {
        return Err(format!(
            "that release's {SUMS_ASSET} is at an address this app will not fetch, so nothing was downloaded. \
             Its page is {RELEASES_PAGE}"
        ));
    }
    let response = client
        .get(&asset.browser_download_url)
        .send()
        .await
        .map_err(|err| format!("could not reach GitHub for the release's {SUMS_ASSET} ({err})"))?;
    if !response.status().is_success() {
        return Err(format!(
            "GitHub answered {} for the release's {SUMS_ASSET}, so nothing was downloaded",
            response.status()
        ));
    }
    if response
        .content_length()
        .is_some_and(|length| length > MAX_SUMS_BYTES)
    {
        return Err(format!(
            "the release's {SUMS_ASSET} is larger than a checksums file is, so it was not read"
        ));
    }
    let body = response
        .bytes()
        .await
        .map_err(|err| format!("the release's {SUMS_ASSET} stopped part-way ({err})"))?;
    if body.len() as u64 > MAX_SUMS_BYTES {
        return Err(format!(
            "the release's {SUMS_ASSET} is larger than a checksums file is, so it was not read"
        ));
    }
    Ok(String::from_utf8_lossy(&body).into_owned())
}

/// What to do with the file, in a sentence.
///
/// Text, not an action. A deb needs a package manager and a person's own decision about
/// it; an AppImage becomes the app by being made executable and run. This app does neither
/// of those things for you, and it does not replace the copy you are running either.
fn next_step(package: Package, path: &Path) -> String {
    let shown = path.display();
    match package {
        Package::Deb => format!(
            "Install it with:  sudo apt install {shown}\n\
             That is yours to run: this app never escalates, and it does not replace the copy \
             you are running."
        ),
        Package::AppImage => format!(
            "Make it executable and run it:  chmod +x {shown} && {shown}\n\
             Or replace this AppImage with it once you are sure the new one starts."
        ),
        Package::Distro => format!(
            "This copy was installed by your distribution's package manager. Update it there, \
             or take the AppImage from {RELEASES_PAGE}."
        ),
        Package::Source => String::new(),
    }
}

/// Download the package this install can use, into `into`.
///
/// Takes no argument from the frontend, and that is the point: there is no way for a page —
/// or a skill, or anything else that can call `invoke` — to choose what is fetched or where
/// it is written. The asset is re-derived here from the same release the check saw, for
/// *this* install, and its address has to pass [`allowed_url`] like every other request
/// this module makes.
pub(crate) async fn fetch_package(
    current: &str,
    package: Package,
    into: PathBuf,
    on_progress: &(dyn Fn(UpdateProgress) + Send + Sync),
) -> Result<UpdateDownload, String> {
    if package.extension().is_none() {
        return Err(format!(
            "this copy runs from a checkout, so there is no package to download — `git pull` in \
             it, or take one from {RELEASES_PAGE}"
        ));
    }
    let arch = package_arch();
    let client = client(DOWNLOAD_TIMEOUT, current)?;
    let payload = fetch_release(&client).await?.ok_or_else(|| {
        format!(
            "no release has been published yet, so there is nothing to download. {RELEASES_PAGE}"
        )
    })?;
    let update = decide(&payload, current, package, arch);
    let name = match update.asset.clone() {
        Some(name) => name,
        None => {
            return Err(update.message.clone().unwrap_or_else(|| {
                format!(
                    "the newest release ({}) has no package for this install. Its page is {}",
                    update.tag.clone().unwrap_or_else(|| "unnamed".to_string()),
                    update.url
                )
            }))
        }
    };
    let url = payload
        .assets
        .iter()
        .find(|asset| asset.name == name)
        .map(|asset| asset.browser_download_url.clone())
        .ok_or_else(|| {
            // Not impossible: the release could be edited between the two requests, and the check
            // a person just read would then name a file that is no longer there.
            "the release changed between the check and the download; check again".to_string()
        })?;
    if !allowed_url(&url) {
        return Err(format!(
            "that release's download address is not one this app will fetch ({url}). \
             The package is on {RELEASES_PAGE}"
        ));
    }
    if let Some(size) = update.asset_bytes.filter(|size| *size > MAX_ASSET_BYTES) {
        return Err(format!(
            "{name} is {size} bytes, past this app's limit of {MAX_ASSET_BYTES}; take it from \
             {RELEASES_PAGE}"
        ));
    }
    // Read first: it is a few hundred bytes, and a release that cannot be checked is refused before tens of
    // megabytes are spent on a package that would then not be written.
    let sums = fetch_sums(&client, &payload).await?;
    let mut response = client
        .get(&url)
        .send()
        .await
        .map_err(|err| format!("could not reach GitHub to download {name} ({err})"))?;
    let status = response.status();
    if !status.is_success() {
        return Err(format!(
            "GitHub answered {status} for {name}, so nothing was written"
        ));
    }
    // Read as the server sends it rather than in one `bytes()`, because a download a person is
    // watching has to be able to say how far it has come before it is over. Everything the size
    // checks below do is still done — the announced length is refused up front, and what actually
    // arrives is counted as it arrives, so a URL that streams forever ends in a sentence instead of
    // in an out-of-memory kill.
    let total = response.content_length();
    if let Some(length) = total.filter(|length| *length > MAX_ASSET_BYTES) {
        return Err(format!(
            "{name} is {length} bytes, past this app's limit of {MAX_ASSET_BYTES}; take it from \
             {RELEASES_PAGE}"
        ));
    }
    let mut bytes: Vec<u8> = Vec::with_capacity(total.unwrap_or(0).min(MAX_ASSET_BYTES) as usize);
    let mut announced: Option<u64> = None;
    loop {
        let chunk = response
            .chunk()
            .await
            .map_err(|err| format!("the download of {name} stopped part-way ({err})"))?;
        let Some(chunk) = chunk else { break };
        bytes.extend_from_slice(&chunk);
        let received = bytes.len() as u64;
        if received > MAX_ASSET_BYTES {
            return Err(format!(
                "{name} passed this app's limit of {MAX_ASSET_BYTES} bytes while it was being read, \
                 so nothing was written; take it from {RELEASES_PAGE}"
            ));
        }
        if announce(received, total, announced) {
            announced = Some(received);
            on_progress(UpdateProgress { received, total });
        }
    }
    // The end is always announced, so a bar cannot stop at 97% — and only when there is an end to
    // announce: an empty body is refused by `write_asset` below, with its own sentence.
    let arrived = bytes.len() as u64;
    if arrived > 0 && announced != Some(arrived) {
        on_progress(UpdateProgress {
            received: arrived,
            total,
        });
    }
    // Before anything is written: a package that is not the one the release lists never reaches the downloads folder.
    let sha256 = check_against_sums(&name, &bytes, &sums)?;
    let path = write_asset(&into, &name, &bytes).await?;
    Ok(UpdateDownload {
        path: path.display().to_string(),
        bytes: bytes.len() as u64,
        package,
        tag: update.tag.clone().unwrap_or_default(),
        next_step: format!(
            "{}\nIts SHA-256 matches the release's {SUMS_ASSET} ({sha256}): the file is whole, and is the one the \
             release published. That is not a signature, since both came from the same release.",
            next_step(package, &path)
        ),
        sha256,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A release payload shaped exactly the way GitHub's API answers, as a string, so the
    /// deserialization is under test too and not only the decision on top of it.
    fn payload(tag: &str, assets: &[(&str, u64)]) -> ReleasePayload {
        let assets: Vec<String> = assets
            .iter()
            .map(|(name, size)| {
                format!(
                    r#"{{"name":"{name}","size":{size},"browser_download_url":"https://github.com/GeneticxCln/Codify/releases/download/{tag}/{name}"}}"#
                )
            })
            .collect();
        let json = format!(
            r#"{{"tag_name":"{tag}","published_at":"2026-10-26T09:00:00Z","prerelease":false,"draft":false,
                 "html_url":"https://github.com/GeneticxCln/Codify/releases/tag/{tag}",
                 "assets":[{}]}}"#,
            assets.join(",")
        );
        serde_json::from_str(&json).expect("GitHub's shape parses")
    }

    const DEB: &str = "Codify_1.0.1_amd64.deb";
    const APPIMAGE: &str = "Codify_1.0.1_amd64.AppImage";

    #[test]
    fn an_appimage_knows_itself_from_the_environment_not_from_its_path() {
        // The AppImage runtime unpacks under /tmp, so the path alone would say "checkout".
        let unpacked = Path::new("/tmp/.mount_Codify1a2b3c/usr/bin/codify-desktop");
        assert_eq!(
            Package::AppImage,
            install_package(Some(unpacked), Some("/home/me/Codify_1.0.0_amd64.AppImage"))
        );
        assert_eq!(Package::Source, install_package(Some(unpacked), None));
        // An AppImage runtime that exported an empty variable is not an AppImage.
        assert_eq!(
            Package::Source,
            install_package(Some(unpacked), Some("   "))
        );
    }

    #[test]
    fn an_installed_copy_is_a_package_and_a_checkout_is_not() {
        assert_eq!(
            Package::Deb,
            install_package(Some(Path::new("/usr/bin/codify-desktop")), None)
        );
        // The same path on a non-Debian distribution is a package, but not one apt owns.
        assert_eq!(
            Package::Deb,
            classify_install(Some(Path::new("/usr/bin/codify-desktop")), None, true)
        );
        assert_eq!(
            Package::Distro,
            classify_install(Some(Path::new("/usr/bin/codify-desktop")), None, false)
        );
        // `/opt` is where a hand-built or distribution package goes, and it is not
        // necessarily a deb: claiming one would print apt instructions for an Arch
        // package. It is a checkout as far as this app can prove.
        assert_eq!(
            Package::Source,
            install_package(Some(Path::new("/opt/codify/codify-desktop")), None)
        );
        // `make run-app`, and the launcher `scripts/install-local.sh` writes.
        assert_eq!(
            Package::Source,
            install_package(
                Some(Path::new(
                    "/home/me/Codify/src-tauri/target/debug/codify-desktop"
                )),
                None
            )
        );
        // Nowhere to look at all: refuse to claim a package rather than guess one.
        assert_eq!(Package::Source, install_package(None, None));
    }

    #[test]
    fn a_debian_family_is_recognised_from_its_own_release_file() {
        assert!(is_debian_family("ID=debian\n"));
        assert!(is_debian_family("ID=ubuntu\nID_LIKE=debian\n"));
        // Derivatives name Ubuntu in `ID_LIKE`, sometimes beside Debian and in quotes.
        assert!(is_debian_family(
            "ID=linuxmint\nID_LIKE=\"ubuntu debian\"\n"
        ));
        assert!(!is_debian_family("ID=arch\n"));
        assert!(!is_debian_family("ID=fedora\nID_LIKE=\"fedora\"\n"));
        // A missing or empty file is not a Debian system, so no deb is claimed for it.
        assert!(!is_debian_family(""));
    }

    #[test]
    fn versions_are_compared_as_numbers_not_as_text() {
        assert!(is_newer("v1.10.0", "1.9.0"), "1.10.0 is newer than 1.9.0");
        assert!(is_newer("1.0.1", "1.0.0"));
        assert!(is_newer("2.0", "1.9.9"), "a missing patch counts as zero");
        assert!(!is_newer("1.0.0", "1.0.0"), "a re-tag offers no update");
        assert!(
            !is_newer("1.2", "1.2.0"),
            "1.2 and 1.2.0 are the same release"
        );
        assert!(!is_newer("0.9.9", "1.0.0"), "an older tag is not an update");
    }

    #[test]
    fn a_tag_that_is_not_a_version_is_never_an_update() {
        for tag in [
            "",
            "   ",
            "nightly",
            "1.2.x",
            "1.2.3.4",
            "v",
            "release-one",
            "1.2.-3",
        ] {
            assert!(!is_newer(tag, "1.0.0"), "{tag:?} was compared as a version");
        }
    }

    #[test]
    fn a_prerelease_is_not_offered_over_a_stable_release() {
        // Ahead, and still refused: the suffix is a pre-release whether or not GitHub was
        // told to flag it.
        assert!(is_ahead("2.0.0-rc1", "1.0.0"));
        assert!(!is_newer("2.0.0-rc1", "1.0.0"));
        assert!(!is_newer("1.0.1+build5", "1.0.0"));
    }

    #[test]
    fn only_github_is_dialled_and_only_over_https() {
        assert!(allowed_url(RELEASES_API));
        assert!(allowed_url(
            "https://github.com/GeneticxCln/Codify/releases"
        ));
        assert!(allowed_url(
            "https://objects.githubusercontent.com/github-production-release-asset/x"
        ));
        assert!(allowed_url(
            "https://release-assets.githubusercontent.com/github-production-release-asset/x"
        ));
        for url in [
            // The lookalikes a suffix test would accept.
            "https://evil-github.com/x",
            "https://github.com.evil.example/x",
            "https://objects.githubusercontent.com.evil.example/x",
            "https://evilgithub.com/x",
            // Credentials in the authority: the host is what `url` resolves, not the text.
            "https://github.com@evil.example/x",
            // Not https, and not a URL at all.
            "http://github.com/x",
            "ftp://github.com/x",
            "file:///etc/passwd",
            "github.com/releases",
            "",
        ] {
            assert!(
                !allowed_url(url),
                "{url:?} was accepted as a place to fetch from"
            );
        }
    }

    #[test]
    fn the_check_asks_this_repository_and_nothing_else() {
        // The one address, spelled out: a change here is a change of who this app talks to,
        // and it should have to edit an assertion to do it.
        assert_eq!(
            "https://api.github.com/repos/GeneticxCln/Codify/releases/latest",
            RELEASES_API
        );
        assert!(RELEASES_API.starts_with("https://"));
    }

    #[test]
    fn the_asset_is_the_one_for_this_install_and_this_machine() {
        let assets = vec![
            AssetPayload {
                name: "Codify_1.0.1_amd64.deb".to_string(),
                size: 18_000_000,
                browser_download_url: "https://github.com/GeneticxCln/Codify/releases/download/v1.0.1/Codify_1.0.1_amd64.deb".to_string(),
            },
            AssetPayload {
                name: "Codify_1.0.1_arm64.deb".to_string(),
                size: 17_000_000,
                browser_download_url: "https://github.com/GeneticxCln/Codify/releases/download/v1.0.1/Codify_1.0.1_arm64.deb".to_string(),
            },
            AssetPayload {
                name: "Codify_1.0.1_amd64.AppImage".to_string(),
                size: 90_000_000,
                browser_download_url: "https://github.com/GeneticxCln/Codify/releases/download/v1.0.1/Codify_1.0.1_amd64.AppImage".to_string(),
            },
            AssetPayload {
                name: "Codify_1.0.1_amd64.deb.sig".to_string(),
                size: 400,
                browser_download_url: "https://github.com/x".to_string(),
            },
            AssetPayload {
                name: "SHA256SUMS".to_string(),
                size: 200,
                browser_download_url: "https://github.com/x".to_string(),
            },
            AssetPayload {
                name: "Codify_1.0.1_amd64.AppImage.tar.gz".to_string(),
                size: 90_000_000,
                browser_download_url: "https://github.com/x".to_string(),
            },
            AssetPayload {
                name: "source.tar.gz".to_string(),
                size: 4_000_000,
                browser_download_url: "https://github.com/x".to_string(),
            },
        ];
        assert_eq!(
            Some("Codify_1.0.1_amd64.deb"),
            pick_asset(&assets, Package::Deb, "amd64").map(|asset| asset.name.as_str())
        );
        assert_eq!(
            Some("Codify_1.0.1_amd64.AppImage"),
            pick_asset(&assets, Package::AppImage, "amd64").map(|asset| asset.name.as_str())
        );
        // The same asset list read on an arm64 machine picks the arm64 build, so the
        // architecture is what chose and not the order of the list.
        assert_eq!(
            Some("Codify_1.0.1_arm64.deb"),
            pick_asset(&assets, Package::Deb, "arm64").map(|asset| asset.name.as_str())
        );
        // A checkout is offered nothing at all.
        assert!(pick_asset(&assets, Package::Source, "amd64").is_none());

        // This repository's releases carry amd64 only. An arm64 install must be
        // told there is no package, not handed the amd64 one.
        let amd64_only = vec![AssetPayload {
            name: DEB.to_string(),
            size: 18_000_000,
            browser_download_url: "https://github.com/x".to_string(),
        }];
        assert!(pick_asset(&amd64_only, Package::Deb, "arm64").is_none());
        assert!(pick_asset(&amd64_only, Package::AppImage, "arm64").is_none());
    }

    #[test]
    fn a_release_without_a_package_for_this_machine_is_refused() {
        // A release of debs, asked for an AppImage.
        assert!(pick_asset(
            &[AssetPayload {
                name: DEB.to_string(),
                size: 1,
                browser_download_url: "https://github.com/x".to_string(),
            }],
            Package::AppImage,
            "amd64",
        )
        .is_none());

        // Two unnamed-architecture candidates is a guess between two, so neither is picked.
        assert!(pick_asset(
            &[
                AssetPayload {
                    name: "Codify.deb".to_string(),
                    size: 1,
                    browser_download_url: String::new()
                },
                AssetPayload {
                    name: "Codify-new.deb".to_string(),
                    size: 1,
                    browser_download_url: String::new()
                },
            ],
            Package::Deb,
            "amd64",
        )
        .is_none());

        // One is unambiguous, and accepted even without an architecture in its name.
        assert_eq!(
            Some("Codify.deb"),
            pick_asset(
                &[AssetPayload {
                    name: "Codify.deb".to_string(),
                    size: 1,
                    browser_download_url: String::new()
                }],
                Package::Deb,
                "amd64",
            )
            .map(|asset| asset.name.as_str())
        );
    }

    #[test]
    fn a_name_is_a_name_and_never_a_path() {
        assert!(!is_plain_file_name("../../.bashrc"));
        assert!(!is_plain_file_name("nested/Codify.deb"));
        assert!(!is_plain_file_name("nested\\Codify.deb"));
        assert!(!is_plain_file_name(".hidden.deb"));
        assert!(!is_plain_file_name(""));
        assert!(!is_plain_file_name(".."));
        assert!(is_plain_file_name("Codify_1.0.0_amd64.deb"));
        // And the picker refuses such a name before it is ever offered.
        assert!(pick_asset(
            &[AssetPayload {
                name: "../../Codify.deb".to_string(),
                size: 1,
                browser_download_url: "https://github.com/x".to_string(),
            }],
            Package::Deb,
            "amd64",
        )
        .is_none());
    }

    #[test]
    fn a_newer_release_with_a_package_is_offered_with_its_size_and_page() {
        let check = decide(
            &payload("v1.0.1", &[(DEB, 18_000_000), (APPIMAGE, 90_000_000)]),
            "1.0.0",
            Package::Deb,
            "amd64",
        );
        assert!(check.available);
        assert_eq!(Some("1.0.1".to_string()), check.latest);
        assert_eq!(Some("v1.0.1".to_string()), check.tag);
        assert_eq!(Some(DEB.to_string()), check.asset);
        assert_eq!(Some(18_000_000), check.asset_bytes);
        assert_eq!(
            None, check.message,
            "an offer and a sentence cannot both be true"
        );
        assert_eq!("1.0.0", check.current);
        assert_eq!(
            "https://github.com/GeneticxCln/Codify/releases/tag/v1.0.1",
            check.url
        );
        assert_eq!(Some("2026-10-26T09:00:00Z".to_string()), check.published_at);
    }

    #[test]
    fn the_same_release_reads_as_up_to_date() {
        let check = decide(
            &payload("v1.0.0", &[(DEB, 1)]),
            "1.0.0",
            Package::Deb,
            "amd64",
        );
        assert!(!check.available);
        assert_eq!(None, check.asset);
        assert_eq!(
            Some("1.0.0 is the newest release.".to_string()),
            check.message
        );
    }

    #[test]
    fn a_release_with_no_package_for_this_install_says_so_rather_than_offering_one() {
        let check = decide(
            &payload("v1.0.1", &[("source.tar.gz", 10)]),
            "1.0.0",
            Package::Deb,
            "amd64",
        );
        assert!(check.available, "a newer release does exist");
        assert_eq!(None, check.asset, "nothing to download was offered");
        let message = check.message.expect("a sentence explaining the gap");
        assert!(
            message.contains("carries no deb built for this machine"),
            "{message}"
        );
        assert!(message.contains("amd64"), "{message}");
    }

    #[test]
    fn a_checkout_is_told_to_pull_rather_than_given_a_package() {
        let check = decide(
            &payload("v1.0.1", &[(DEB, 18_000_000)]),
            "1.0.0",
            Package::Source,
            "amd64",
        );
        assert!(check.available, "it is a newer release");
        assert_eq!(None, check.asset, "a package would install a second Codify");
        let message = check.message.expect("a sentence saying how to update");
        assert!(message.contains("git pull"), "{message}");
        assert!(message.contains("v1.0.1"), "{message}");
    }

    #[test]
    fn an_unreadable_tag_is_named_rather_than_called_current() {
        let check = decide(&payload("nightly", &[]), "1.0.0", Package::Deb, "amd64");
        assert!(!check.available);
        assert_eq!(None, check.latest);
        let message = check.message.expect("a sentence naming the tag");
        assert!(message.contains("nightly"), "{message}");
    }

    #[test]
    fn a_prerelease_tag_is_named_as_one() {
        let check = decide(
            &payload("v2.0.0-rc1", &[(DEB, 1)]),
            "1.0.0",
            Package::Deb,
            "amd64",
        );
        assert!(!check.available);
        assert_eq!(None, check.asset);
        let message = check.message.expect("a sentence naming the pre-release");
        assert!(message.contains("pre-release"), "{message}");
        assert!(message.contains("v2.0.0-rc1"), "{message}");
    }

    #[test]
    fn a_check_that_cannot_run_says_so_rather_than_claiming_you_are_current() {
        let check = unavailable(
            "1.0.0",
            Package::Deb,
            "could not reach GitHub (connection refused)".to_string(),
        );
        assert!(!check.available);
        assert_eq!(None, check.latest, "no version was ever read");
        assert_eq!(None, check.asset);
        assert_eq!(RELEASES_PAGE, check.url);
        let message = check
            .message
            .expect("a sentence saying the check did not run");
        assert!(message.contains("could not reach GitHub"), "{message}");
    }

    #[test]
    fn where_a_package_goes_is_the_downloads_folder_or_nothing() {
        let scratch = std::env::temp_dir().join(format!("codify-update-{}", std::process::id()));
        assert_eq!(
            Ok(scratch.clone()),
            download_dir_for(Some(scratch.clone()), Some("/home/me".to_string())),
            "the resolver's answer wins"
        );
        assert_eq!(
            Ok(PathBuf::from("/home/me/Downloads")),
            download_dir_for(None, Some("/home/me".to_string()))
        );
        // An empty answer from either side is an answer of nothing.
        assert_eq!(
            Ok(PathBuf::from("/home/me/Downloads")),
            download_dir_for(Some(PathBuf::new()), Some("/home/me".to_string()))
        );
        let refused = download_dir_for(None, None).expect_err("no home and no resolver answer");
        assert!(refused.contains("nothing was written"), "{refused}");
        assert!(refused.contains(RELEASES_PAGE), "{refused}");
    }

    #[test]
    fn the_package_is_written_where_it_was_asked_for_and_named_as_the_release_names_it() {
        let scratch =
            std::env::temp_dir().join(format!("codify-update-write-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&scratch);
        // A folder that does not exist yet is made: the person clicked Download, which is
        // where a downloads folder comes from.
        let into = scratch.join("Downloads");
        let written = block_on(write_asset(&into, DEB, b"a deb")).expect("the package is written");
        assert_eq!(into.join(DEB), written);
        assert_eq!(std::fs::read(&written).expect("readable"), b"a deb");
        // Twice, over the same file: a second click replaces the package rather than
        // failing on a name that is already there.
        let again = block_on(write_asset(&into, DEB, b"a newer deb")).expect("written again");
        assert_eq!(written, again);
        assert_eq!(std::fs::read(&again).expect("readable"), b"a newer deb");
        // The write is renamed into place, so a successful one leaves no temporary
        // beside the package for a later crash to look like an install.
        let leftovers: Vec<_> = std::fs::read_dir(&into)
            .expect("readable")
            .filter_map(Result::ok)
            .map(|entry| entry.file_name().to_string_lossy().into_owned())
            .filter(|name| name.ends_with(".part"))
            .collect();
        assert!(
            leftovers.is_empty(),
            "temporary files left behind: {leftovers:?}"
        );
        let _ = std::fs::remove_dir_all(&scratch);
    }

    #[test]
    fn a_package_does_not_write_through_a_symlink_in_the_downloads_folder() {
        let scratch =
            std::env::temp_dir().join(format!("codify-update-link-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&scratch);
        let into = scratch.join("Downloads");
        std::fs::create_dir_all(&into).expect("the folder exists");
        let outside = scratch.join("outside.deb");
        std::fs::write(&outside, b"not ours").expect("the outside file exists");
        let link = into.join(DEB);
        std::os::unix::fs::symlink(&outside, &link).expect("the link exists");

        let written = block_on(write_asset(&into, DEB, b"a deb")).expect("the package is written");

        assert_eq!(std::fs::read(&outside).expect("readable"), b"not ours");
        assert!(!std::fs::symlink_metadata(&written)
            .expect("writable")
            .file_type()
            .is_symlink());
        assert_eq!(std::fs::read(&written).expect("readable"), b"a deb");
        let _ = std::fs::remove_dir_all(&scratch);
    }

    /// Drive one of this module's two async functions inside a test.
    ///
    /// The other async functions reach GitHub, so they are not driven anywhere here; what
    /// this covers is the one function that touches a disk.
    fn block_on<F: std::future::Future>(future: F) -> F::Output {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("a test runtime")
            .block_on(future)
    }

    #[test]
    fn nothing_is_written_under_a_name_that_is_a_path_or_for_an_empty_body() {
        let scratch =
            std::env::temp_dir().join(format!("codify-update-refuse-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&scratch);
        for name in ["../escape.deb", "nested/Codify.deb", ".hidden"] {
            assert!(
                block_on(write_asset(&scratch, name, b"x")).is_err(),
                "{name} was written"
            );
        }
        let empty = block_on(write_asset(&scratch, DEB, b"")).expect_err("empty");
        assert!(empty.contains("empty"), "{empty}");
        assert!(!scratch.exists(), "a refused write left a directory behind");
    }

    #[test]
    fn progress_is_announced_a_whole_percent_at_a_time() {
        // The first byte is always worth saying: it is what moves the pane off "starting".
        assert!(announce(1, Some(1_000), None));
        assert!(!announce(0, Some(1_000), None), "nothing has arrived");
        // Then once a percent, and not once a chunk.
        assert!(announce(20, Some(1_000), Some(10)), "2% after 1%");
        assert!(!announce(19, Some(1_000), Some(10)), "still 1%");
        assert!(!announce(10, Some(1_000), Some(10)));
        // A total of zero is a server's answer and not a percentage to divide by.
        assert!(!announce(5, Some(0), Some(1)));
        // Past the announced size — a lying `Content-Length` — the bar is full and stays full.
        assert!(!announce(2_000, Some(1_000), Some(1_000)));
    }

    #[test]
    fn a_download_that_never_says_how_big_it_is_is_announced_every_few_megabytes() {
        assert!(announce(1, None, None));
        assert!(
            !announce(1_000, None, Some(1)),
            "a kilobyte later is not news"
        );
        assert!(announce(1 + ANNOUNCE_EVERY_WITHOUT_A_TOTAL, None, Some(1)));
    }

    #[test]
    fn a_whole_appimage_is_a_hundred_messages_and_not_a_thousand() {
        // An AppImage arrives as about a thousand 90 KB chunks. One message each would be a
        // thousand hops into the webview that is drawing the bar; one per percent is the point of
        // `announce`, so this is the count that says it holds.
        let (chunk, total) = (90_000u64, 90_000_000u64);
        let mut announced: Option<u64> = None;
        let mut messages = 0u64;
        for step in 1..=(total / chunk) {
            let received = step * chunk;
            if announce(received, Some(total), announced) {
                announced = Some(received);
                messages += 1;
            }
        }
        // The first byte, one per whole percent, and the end: a hundred and one, or thereabouts.
        assert!(
            (100..=102).contains(&messages),
            "{messages} messages to move one bar from 0 to 100"
        );
    }

    #[test]
    fn the_window_names_the_event_progress_is_announced_with() {
        // No type system spans a Rust string and a TypeScript constant, so the file that must agree
        // with this literal is read — the same move `terminal.rs` and `machine.rs` make.
        let path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .expect("src-tauri has a parent")
            .join("ui/src/shellEvents.ts");
        let text = std::fs::read_to_string(&path).expect("ui/src/shellEvents.ts");
        assert!(
            text.contains(&format!("\"{UPDATE_PROGRESS_EVENT}\"")),
            "ui/src/shellEvents.ts does not name {UPDATE_PROGRESS_EVENT:?} — the shell emits it and \
             the pane's bar would never move"
        );
    }

    #[test]
    fn the_next_step_is_text_a_person_runs_themselves() {
        let deb = next_step(
            Package::Deb,
            Path::new("/home/me/Downloads/Codify_1.0.1_amd64.deb"),
        );
        assert!(
            deb.contains("sudo apt install /home/me/Downloads/Codify_1.0.1_amd64.deb"),
            "{deb}"
        );
        assert!(deb.contains("never escalates"), "{deb}");
        let appimage = next_step(
            Package::AppImage,
            Path::new("/home/me/Downloads/Codify.AppImage"),
        );
        assert!(
            appimage.contains("chmod +x /home/me/Downloads/Codify.AppImage"),
            "{appimage}"
        );
    }

    /// `sha256sum` of the five bytes `hello`, as the release workflow's `sha256sum` would print it.
    const HELLO_SHA256: &str = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824";

    #[test]
    fn the_checksums_are_read_the_way_sha256sum_writes_them() {
        let text = format!(
            "{HELLO_SHA256}  Codify_1.0.1_amd64.deb\r\n\n{}  Codify_1.0.1_amd64.AppImage\n",
            HELLO_SHA256.to_uppercase()
        );
        let sums = parse_sums(&text).expect("sha256sum output");
        assert_eq!(sums["Codify_1.0.1_amd64.deb"], HELLO_SHA256);
        assert_eq!(
            sums["Codify_1.0.1_amd64.AppImage"], HELLO_SHA256,
            "digests are lowercased, as `make verify-package` reads them"
        );
        let binary_mode =
            parse_sums(&format!("{HELLO_SHA256} *Codify.deb")).expect("binary-mode line");
        assert_eq!(binary_mode["Codify.deb"], HELLO_SHA256);
    }

    #[test]
    fn a_checksums_file_that_is_not_one_is_refused_by_line_rather_than_skipped() {
        for (text, says) in [
            (format!("# checksums\n{HELLO_SHA256}  a.deb"), "line 1"),
            (
                format!("{HELLO_SHA256}  a.deb\nnot a digest  b.deb"),
                "line 2",
            ),
            (format!("{HELLO_SHA256} a.deb"), "line 1"),
            (format!("{HELLO_SHA256}  "), "line 1"),
            ("\n  \n".to_string(), "lists no files"),
        ] {
            let refused = parse_sums(&text).expect_err(&text);
            assert!(refused.contains(says), "{refused:?} should say {says:?}");
        }
    }

    #[test]
    fn a_package_is_written_only_when_its_digest_is_the_one_the_release_lists() {
        let sums = format!("{HELLO_SHA256}  Codify_1.0.1_amd64.deb\n");
        assert_eq!(
            check_against_sums("Codify_1.0.1_amd64.deb", b"hello", &sums),
            Ok(HELLO_SHA256.to_string())
        );
        let damaged = check_against_sums("Codify_1.0.1_amd64.deb", b"hellO", &sums)
            .expect_err("a changed byte");
        assert!(damaged.contains("does not match"), "{damaged}");
        assert!(damaged.contains("nothing was written"), "{damaged}");
        let unlisted = check_against_sums("Codify_1.0.1_amd64.AppImage", b"hello", &sums)
            .expect_err("not listed");
        assert!(unlisted.contains("does not list"), "{unlisted}");
        assert!(unlisted.contains("nothing was written"), "{unlisted}");
    }
}
