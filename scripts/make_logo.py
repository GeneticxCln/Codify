"""Generate Codify's animated logo: a black/green terminal-caret GIF.

The design is the brand contract applied to a mark. DESIGN.md §2 makes
`#0d1117` the app background and `#3fb950` the success tone, and §7 bans a
looping animation *in the UI* — which is exactly why the loop lives in a
pre-rendered asset: a GIF cannot honour `prefers-reduced-motion`, so the
component that renders it (`ui/src/components/ui/Logo.tsx`) swaps in the
static companion for users who opt out. The exception is documented in
DESIGN.md §7 in the same change that introduced the asset.

Everything here is deterministic — same interpreter, same bytes — so the
checked-in GIF is reproducible and a regeneration that changes nothing diffs
as nothing. Pillow is needed only when the assets are actually regenerated;
the script says so loudly rather than failing quietly.

    python3 scripts/make_logo.py          # write any stale or missing assets
    python3 scripts/make_logo.py --check  # exit 1 if the files would change

Run it by hand when the mark changes. Nothing in the test suite or the build
invokes it: a deliberate asset is checked in, not rebuilt on every install
with whatever Pillow version happens to be present.

The mark is a terminal prompt, read left to right: a chevron asks, a block
caret answers — both the success green on the app's own background, inside the
1px border token every raised control carries. The loop blinks the caret and
then highlights it in the text tone: the streaming-cursor moment DESIGN.md §7
already sanctions, drawn as the mark itself.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
from collections.abc import Callable
from pathlib import Path

# Imported up front rather than lazily: every path through this script renders
# something, so a missing Pillow is a missing prerequisite, and it is said so
# here — loudly, with the fix — instead of as a traceback mid-generation.
try:
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit(
        "scripts/make_logo.py needs Pillow to render the logo assets, and it "
        "is not installed. It is declared in pyproject.toml under the `dev` "
        "extra: run `pip install -e '.[dev]'` and try again. The checked-in "
        "assets keep working without it — this only blocks a regeneration, "
        "and it must say so rather than fail quietly."
    )

# The contract's own values, quoted rather than invented. `ui/tailwind.config.js`
# and DESIGN.md §2 are the owners; this file is another view of the same four
# hexes and must never diverge from them.
BG = (13, 17, 23)  # codify-bg      #0d1117
GREEN = (63, 185, 80)  # codify-success #3fb950
BORDER = (48, 54, 61)  # codify-border  #30363d
PRIMARY = (230, 237, 243)  # codify-primary #e6edf3

ROOT = Path(__file__).resolve().parent.parent
PUBLIC = ROOT / "ui" / "public"
TAURI_ICONS = ROOT / "src-tauri" / "icons"

GIF_PATH = PUBLIC / "logo.gif"
STATIC_PATH = PUBLIC / "logo-static.gif"
# The full set `src-tauri/tauri.conf.json` lists under `bundle.icon`. The
# desktop icon and the in-app mark are one mark — a bundle that ships the
# default Tauri squares beside a rebranded header is the drift this script
# exists to prevent.
PNG_PATHS = [
    TAURI_ICONS / "32x32.png",
    TAURI_ICONS / "128x128.png",
    TAURI_ICONS / "128x128@2x.png",  # 256px
]
# No `.ico` and no `.icns`: Codify is a Linux desktop app and ships no installer, so the
# Windows and macOS icon containers had no reader.

# Geometry is a 24×24 unit grid, scaled to the render size. One scale, so the
# mark at 32px is the mark at 256px — never a re-layout per size.
GRID = 24
# The chevron `>`, as three 2-unit strokes in the left half.
CHEVRON = [((4, 5), (11, 12)), ((11, 12), (4, 19)), ((4, 5), (4, 19))]
# The block caret, the thing the animation blinks.
CARET = (14, 6, 18, 18)

# 160ms per blink step, 550ms on the trailing bright frame: the pacing of a
# blink, not of a spinner.
FRAME_DELAYS = [160, 160, 550]
PHASES = [0, 1, 2]
STATIC_PHASE = 9  # not an animation phase — the resting pose for the stills


def _draw_frame(size: int, phase: int) -> Image.Image:
    """One animation frame. `phase` 0/1/2 is the loop; 9 is the static pose."""
    s = size / GRID
    img = Image.new("RGB", (size, size), BG)
    d = ImageDraw.Draw(img)

    # Frame border: the mark is a control, and controls carry `border`.
    d.rectangle([0, 0, size - 1, size - 1], outline=BORDER, width=max(1, round(s)))

    def line(p0: tuple[int, int], p1: tuple[int, int]) -> None:
        d.line(
            [(round(p0[0] * s), round(p0[1] * s)), (round(p1[0] * s), round(p1[1] * s))],
            fill=GREEN,
            width=max(1, round(2 * s)),
        )

    for p0, p1 in CHEVRON:
        line(p0, p1)

    x0, y0, x1, y1 = CARET
    box = [round(x0 * s), round(y0 * s), round(x1 * s), round(y1 * s)]
    if phase == 0:
        # The caret, solid — the "ready" frame.
        d.rectangle(box, fill=GREEN)
    elif phase == 1:
        # Half-blink: the caret hollow, its outline still visible so the loop
        # reads as a blink rather than a pop.
        d.rectangle(box, outline=GREEN, width=max(1, round(s)))
    elif phase == 2:
        # Streaming: the caret in the text tone — the moment the transcript
        # already shows as `▍`. The static pose is the solid green caret, so a
        # frozen page still reads as the prompt it is.
        d.rectangle(box, fill=PRIMARY)
    else:
        d.rectangle(box, fill=GREEN)
    return img


def _brand_palette_image() -> Image.Image:
    """A P-mode image carrying exactly the four brand colours as its palette.

    Frames are quantized onto this shared table rather than letting Pillow pick
    per-frame palettes, which is what would otherwise scatter colours into
    local tables and pad the global one with an unused black slot. One table,
    the contract's four hexes, in every frame.
    """
    palette_img = Image.new("P", (1, 1))
    flat: list[int] = []
    for colour in (BG, GREEN, BORDER, PRIMARY):
        flat.extend(colour)
    # Exactly four entries and no padding: a padded table would be written to
    # the file as 256 slots of which 252 are a lie. Pillow sizes the GIF's
    # global colour table from this list's length.
    palette_img.putpalette(flat)
    return palette_img


def _indexed(frame: Image.Image) -> Image.Image:
    """Map an RGB frame onto the shared brand palette, dithering off."""
    return frame.quantize(palette=_brand_palette_image(), dither=Image.Dither.NONE)


def _gif_payload() -> bytes:
    frames = [_indexed(_draw_frame(size=256, phase=p)) for p in PHASES]
    # Loop forever (0). Disposal 1 (leave as is) is safe because every frame
    # repaints the whole canvas, so no disposal artefacts can accumulate.
    gif = io.BytesIO()
    frames[0].save(
        gif,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=FRAME_DELAYS,
        loop=0,
        disposal=1,
        optimize=False,
    )
    return gif.getvalue()


def _static_gif_payload() -> bytes:
    buf = io.BytesIO()
    _indexed(_draw_frame(size=256, phase=STATIC_PHASE)).save(buf, format="GIF")
    return buf.getvalue()


def _png_payload(size: int) -> bytes:
    buf = io.BytesIO()
    # RGBA, because Tauri's `generate_context!` refuses anything else — it
    # panics at compile time on a non-RGBA icon, which is how the first cut of
    # this script failed `make check-tauri`. The mark has no transparency, but
    # the channel is cheap and the format is the contract.
    _draw_frame(size=size, phase=STATIC_PHASE).convert("RGBA").save(
        buf, format="PNG", optimize=True
    )
    return buf.getvalue()


def _sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _payloads() -> dict[Path, Callable[[], bytes]]:
    return {
        GIF_PATH: _gif_payload,
        STATIC_PATH: _static_gif_payload,
        PNG_PATHS[0]: lambda: _png_payload(32),
        PNG_PATHS[1]: lambda: _png_payload(128),
        PNG_PATHS[2]: lambda: _png_payload(256),
    }


def stale_assets() -> list[Path]:
    """The assets whose bytes on disk differ from what this script would write. Writes nothing."""
    return [
        path
        for path, make_payload in _payloads().items()
        if _sha256(path) != hashlib.sha256(make_payload()).hexdigest()
    ]


def generate(force: bool) -> list[Path]:
    """Write every asset whose bytes differ from disk. Returns what it wrote."""
    stale = set(stale_assets()) if not force else set()
    written: list[Path] = []
    for path, make_payload in _payloads().items():
        if force or path in stale:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(make_payload())
            written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Render Codify's logo assets.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if any asset would change, without writing anything",
    )
    args = parser.parse_args()

    if args.check:
        stale = [str(p.relative_to(ROOT)) for p in stale_assets()]
        if stale:
            sys.exit(
                "logo assets are stale — run `python3 scripts/make_logo.py` and "
                "commit the result: " + ", ".join(stale)
            )
        print("logo assets are current")
        return

    written = generate(force=False)
    for p in sorted(set(written)):
        print(f"wrote {p.relative_to(ROOT)} ({p.stat().st_size} bytes)")
    if not written:
        print("logo assets already current")


if __name__ == "__main__":
    main()
