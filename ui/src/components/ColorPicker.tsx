import React, { useEffect, useRef, useState } from "react";

/**
 * A themed, in-app colour picker replacing the browser's native colour input.
 *
 * WebKitGTK delegates native colour inputs to GTK3's `GtkColorChooserDialog`, which renders
 * broken/blank black on Wayland dark sessions and ignores `--codify-*` tokens — the same toolkit
 * defect that replaced native select (`SelectMenu.tsx`) and native checkbox (`Checkbox.tsx`).
 * Drawing the picker in the page guarantees it works identically on every compositor and wears
 * the active theme.
 */

export interface ColorPickerProps {
  /** Current colour as `#rrggbb`. */
  value: string;
  /** Accessible label for the swatch button (e.g. `"Interface accent"`). */
  label: string;
  /** Called with a lowercase `#rrggbb` whenever the user picks or enters a valid colour. */
  onChange: (hex: string) => void;
}

const PRESETS: readonly string[] = [
  "#000000", "#0d1117", "#161b22", "#21262d",
  "#ef4444", "#f97316", "#f59e0b", "#eab308",
  "#22c55e", "#00ff88", "#06b6d4", "#3b82f6",
  "#6366f1", "#a855f7", "#ec4899", "#f8fafc",
];

/** Normalise `#abc123` or `abc123` into lowercase `#rrggbb`, or `null` when invalid. */
export function normalizeHex(raw: string): string | null {
  const cleaned = raw.trim().replace(/^#/, "");
  return /^[0-9a-f]{6}$/i.test(cleaned) ? `#${cleaned.toLowerCase()}` : null;
}

/** `#rrggbb` → `[h (0..360), s (0..100), v (0..100)]`. */
export function hexToHsv(hex: string): [number, number, number] {
  const norm = normalizeHex(hex) ?? "#808080";
  const n = parseInt(norm.slice(1), 16);
  const r = ((n >> 16) & 255) / 255;
  const g = ((n >> 8) & 255) / 255;
  const b = (n & 255) / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const d = max - min;
  let h = 0;
  if (d !== 0) {
    if (max === r) h = ((g - b) / d) % 6;
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h *= 60;
    if (h < 0) h += 360;
  }
  const s = max === 0 ? 0 : (d / max) * 100;
  return [h, s, max * 100];
}

/** `[h (0..360), s (0..100), v (0..100)]` → lowercase `#rrggbb`. */
export function hsvToHex(h: number, s: number, v: number): string {
  const hn = ((h % 360) + 360) % 360;
  const sn = Math.max(0, Math.min(100, s)) / 100;
  const vn = Math.max(0, Math.min(100, v)) / 100;
  const c = vn * sn;
  const x = c * (1 - Math.abs(((hn / 60) % 2) - 1));
  const m = vn - c;
  let rgb: [number, number, number];
  if (hn < 60) rgb = [c, x, 0];
  else if (hn < 120) rgb = [x, c, 0];
  else if (hn < 180) rgb = [0, c, x];
  else if (hn < 240) rgb = [0, x, c];
  else if (hn < 300) rgb = [x, 0, c];
  else rgb = [c, 0, x];
  return (
    "#" +
    rgb
      .map((ch) => Math.max(0, Math.min(255, Math.round((ch + m) * 255))).toString(16).padStart(2, "0"))
      .join("")
  );
}

const clamp = (n: number, lo: number, hi: number): number => Math.max(lo, Math.min(hi, n));

export const ColorPicker: React.FC<ColorPickerProps> = ({ value, label, onChange }) => {
  const safeValue = normalizeHex(value) ?? "#808080";
  const [open, setOpen] = useState(false);
  const [flipUp, setFlipUp] = useState(false);
  const [alignRight, setAlignRight] = useState(false);
  const [hsv, setHsv] = useState<[number, number, number]>(() => hexToHsv(safeValue));
  const [hexDraft, setHexDraft] = useState(safeValue);
  const hsvRef = useRef<[number, number, number]>(hsv);
  hsvRef.current = hsv;
  const draggingRef = useRef<"pad" | "hue" | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const padRef = useRef<HTMLDivElement | null>(null);
  const hueRef = useRef<HTMLDivElement | null>(null);

  // Sync external updates (Reset, Undo, theme switch, contrast clamp) while keeping hue steady on greys.
  // Skip resetting the live HSV cursor while the user is actively dragging so contrast clamping never
  // fights their pointer mid-gesture.
  useEffect(() => {
    setHexDraft(safeValue);
    if (draggingRef.current !== null) return;
    setHsv((prev) => {
      const [h, s, v] = hexToHsv(safeValue);
      const next: [number, number, number] = [s === 0 || v === 0 ? prev[0] : h, s, v];
      hsvRef.current = next;
      return next;
    });
  }, [safeValue]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent): void => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent): void => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown, true);
    };
  }, [open]);

  const toggle = (): void => {
    if (!open && triggerRef.current && typeof window !== "undefined") {
      const rect = triggerRef.current.getBoundingClientRect();
      setFlipUp(window.innerHeight - rect.bottom < 280 && rect.top > 280);
      setAlignRight(window.innerWidth - rect.left < 252 && rect.right > 252);
    }
    setOpen((prev) => !prev);
  };

  const emitHsv = (h: number, s: number, v: number): void => {
    const next: [number, number, number] = [clamp(h, 0, 360), clamp(s, 0, 100), clamp(v, 0, 100)];
    hsvRef.current = next;
    setHsv(next);
    const hex = hsvToHex(next[0], next[1], next[2]);
    setHexDraft(hex);
    onChange(hex);
  };

  const endDrag = (): void => {
    if (draggingRef.current === null) return;
    draggingRef.current = null;
    const [h, s, v] = hexToHsv(safeValue);
    const next: [number, number, number] = [s === 0 || v === 0 ? hsvRef.current[0] : h, s, v];
    hsvRef.current = next;
    setHsv(next);
  };

  const pickFromPad = (clientX: number, clientY: number): void => {
    const el = padRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    if (!(rect.width > 0 && rect.height > 0)) return;
    const s = clamp(((clientX - rect.left) / rect.width) * 100, 0, 100);
    const v = clamp((1 - (clientY - rect.top) / rect.height) * 100, 0, 100);
    emitHsv(hsvRef.current[0], s, v);
  };

  const pickFromHue = (clientX: number): void => {
    const el = hueRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    if (!(rect.width > 0)) return;
    const h = clamp(((clientX - rect.left) / rect.width) * 360, 0, 360);
    // Starting from pure black, white, or neutral grey (`s < 5` or `v < 5`),
    // rotating hue alone would keep emitting the exact same grey/black hex and
    // appear broken; lift chroma/value into a visible tint immediately.
    const [, curS, curV] = hsvRef.current;
    const s = curS < 5 ? 70 : curS;
    const v = curV < 5 ? 55 : curV;
    emitHsv(h, s, v);
  };

  const onHexInput = (raw: string): void => {
    setHexDraft(raw);
    const parsed = normalizeHex(raw);
    if (!parsed) return;
    const [h, s, v] = hexToHsv(parsed);
    setHsv((prev) => {
      const next: [number, number, number] = [s === 0 || v === 0 ? prev[0] : h, s, v];
      hsvRef.current = next;
      return next;
    });
    onChange(parsed);
  };

  return (
    <div ref={rootRef} className="relative shrink-0">
      <button
        ref={triggerRef}
        type="button"
        data-color-well="true"
        aria-label={`${label} colour`}
        aria-haspopup="dialog"
        aria-expanded={open}
        title={`${label} — ${safeValue}`}
        onClick={toggle}
        className="w-7 h-7 rounded-sm border border-codify-border-strong cursor-pointer shrink-0 block focus:outline-hidden focus:ring-1 focus:ring-codify-accent"
        style={{ backgroundColor: safeValue }}
      />

      {open && (
        <div
          role="dialog"
          aria-label={`${label} colour picker`}
          className={
            "absolute z-50 w-60 rounded-xl border border-codify-border-strong bg-codify-surface p-2.5 shadow-xl space-y-2.5 " +
            (alignRight ? "right-0 " : "left-0 ") +
            (flipUp ? "bottom-full mb-1.5" : "top-full mt-1.5")
          }
        >
          <div className="flex items-center justify-between gap-2">
            <span className="text-2xs font-medium text-codify-primary truncate">{label}</span>
            <span className="font-mono text-2xs text-codify-muted">{safeValue}</span>
          </div>

          {/* 2D Saturation × Brightness pad */}
          <div
            ref={padRef}
            data-color-pad="true"
            onPointerDown={(e) => {
              draggingRef.current = "pad";
              e.currentTarget.setPointerCapture?.(e.pointerId);
              pickFromPad(e.clientX, e.clientY);
            }}
            onPointerMove={(e) => {
              if (draggingRef.current === "pad" || (e.buttons & 1) === 1) {
                pickFromPad(e.clientX, e.clientY);
              }
            }}
            onPointerUp={endDrag}
            onPointerCancel={endDrag}
            onLostPointerCapture={endDrag}
            className="relative h-28 w-full rounded-lg border border-codify-border overflow-hidden cursor-crosshair select-none touch-none"
            style={{ backgroundColor: `hsl(${Math.round(hsv[0])} 100% 50%)` }}
          >
            <div
              className="absolute inset-0"
              style={{ background: "linear-gradient(to right, #ffffff, transparent)" }}
            />
            <div
              className="absolute inset-0"
              style={{ background: "linear-gradient(to bottom, transparent, #000000)" }}
            />
            <span
              aria-hidden="true"
              className="pointer-events-none absolute w-3.5 h-3.5 -ml-[7px] -mt-[7px] rounded-full border-2 shadow-sm"
              style={{
                left: `${hsv[1]}%`,
                top: `${100 - hsv[2]}%`,
                backgroundColor: safeValue,
                borderColor: "var(--codify-primary)",
              }}
            />
          </div>

          {/* Hue bar */}
          <div
            ref={hueRef}
            role="slider"
            tabIndex={0}
            aria-label={`${label} hue`}
            aria-valuemin={0}
            aria-valuemax={360}
            aria-valuenow={Math.round(hsv[0])}
            onPointerDown={(e) => {
              draggingRef.current = "hue";
              e.currentTarget.setPointerCapture?.(e.pointerId);
              pickFromHue(e.clientX);
            }}
            onPointerMove={(e) => {
              if (draggingRef.current === "hue" || (e.buttons & 1) === 1) {
                pickFromHue(e.clientX);
              }
            }}
            onPointerUp={endDrag}
            onPointerCancel={endDrag}
            onLostPointerCapture={endDrag}
            onKeyDown={(e) => {
              if (e.key === "ArrowRight" || e.key === "ArrowUp") {
                e.preventDefault();
                const [h, curS, curV] = hsvRef.current;
                emitHsv((h + 6) % 360, curS < 5 ? 70 : curS, curV < 5 ? 55 : curV);
              } else if (e.key === "ArrowLeft" || e.key === "ArrowDown") {
                e.preventDefault();
                const [h, curS, curV] = hsvRef.current;
                emitHsv((h + 354) % 360, curS < 5 ? 70 : curS, curV < 5 ? 55 : curV);
              }
            }}
            className="relative h-3.5 w-full rounded-full border border-codify-border cursor-ew-resize select-none touch-none"
            style={{
              background:
                "linear-gradient(to right, #ff0000 0%, #ffff00 17%, #00ff00 33%, #00ffff 50%, #0000ff 67%, #ff00ff 83%, #ff0000 100%)",
            }}
          >
            <span
              aria-hidden="true"
              className="pointer-events-none absolute top-1/2 w-3.5 h-3.5 -ml-[7px] -mt-[7px] rounded-full border-2 bg-codify-surface shadow-sm"
              style={{ left: `${(hsv[0] / 360) * 100}%`, borderColor: "var(--codify-primary)" }}
            />
          </div>

          {/* Curated quick swatches */}
          <div className="grid grid-cols-8 gap-1">
            {PRESETS.map((swatch) => (
              <button
                key={swatch}
                type="button"
                aria-label={`Preset ${swatch}`}
                title={swatch}
                onClick={() => onHexInput(swatch)}
                className={
                  "h-5 w-full rounded-sm border cursor-pointer transition-transform hover:scale-110 " +
                  (swatch === safeValue ? "border-codify-border-strong ring-1 ring-codify-accent" : "border-codify-border")
                }
                style={{ backgroundColor: swatch }}
              />
            ))}
          </div>

          {/* Hex entry + close */}
          <div className="flex items-center gap-1.5">
            <input
              type="text"
              aria-label={`${label} hex value`}
              value={hexDraft}
              onChange={(e) => onHexInput(e.target.value)}
              onBlur={() => setHexDraft(safeValue)}
              spellCheck={false}
              maxLength={7}
              placeholder="#00ff88"
              className="min-w-0 flex-1 bg-codify-bg border border-codify-border rounded-md px-2 py-1 font-mono text-xs text-codify-primary focus:outline-hidden focus:border-codify-accent"
            />
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="shrink-0 text-2xs px-2 py-1 rounded-md border border-codify-border bg-codify-raised text-codify-secondary hover:text-codify-primary transition-colors"
            >
              Done
            </button>
          </div>
        </div>
      )}
    </div>
  );
};
