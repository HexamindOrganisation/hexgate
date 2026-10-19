/**
 * Accent-color conversions for the Playground's custom-highlight picker.
 *
 * The app's design tokens store colors as HSL *channels* ("H S% L%", e.g.
 * "226 70% 52%") and consume them via `hsl(var(--primary))`. A native
 * <input type="color"> speaks hex, so the picker round-trips between the two,
 * and derives a readable foreground for text placed on a custom accent.
 */

/** "#rrggbb" (with or without '#') → normalized [r, g, b] in 0..1, or null for an
 * unparseable value (missing/short/non-hex). */
function hexToRgb(hex: string): [number, number, number] | null {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return null;
  const int = parseInt(m[1], 16);
  return [
    ((int >> 16) & 255) / 255,
    ((int >> 8) & 255) / 255,
    (int & 255) / 255,
  ];
}

/** "#rrggbb" → the "H S% L%" channel triple. Returns "" for an unparseable
 * value (missing/short/non-hex). */
export function hexToHslChannels(hex: string): string {
  const rgb = hexToRgb(hex);
  if (!rgb) return "";
  const [r, g, b] = rgb;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const l = (max + min) / 2;
  let h = 0;
  let s = 0;
  if (max !== min) {
    const d = max - min;
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    if (max === r) h = (g - b) / d + (g < b ? 6 : 0);
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h /= 6;
  }
  return `${Math.round(h * 360)} ${Math.round(s * 100)}% ${Math.round(l * 100)}%`;
}

/** "H S% L%" channels → "#rrggbb", for a controlled <input type="color"> so it
 * reflects the active accent. Returns "#000000" for an unparseable value. */
export function hslChannelsToHex(channels: string): string {
  const [hs, ss, ls] = channels.split(/\s+/);
  const h = Number(hs) / 360;
  const s = Number((ss ?? "").replace("%", "")) / 100;
  const l = Number((ls ?? "").replace("%", "")) / 100;
  if (![h, s, l].every(Number.isFinite)) return "#000000";
  const f = (n: number) => {
    const k = (n + h * 12) % 12;
    const c =
      l - s * Math.min(l, 1 - l) * Math.max(-1, Math.min(k - 3, 9 - k, 1));
    return Math.round(c * 255)
      .toString(16)
      .padStart(2, "0");
  };
  return `#${f(0)}${f(8)}${f(4)}`;
}

/** A readable foreground (near-black or near-white, as theme channels) for text
 * placed on an accent — chosen by the accent's perceptual luminance so that
 * --primary-foreground tracks a custom --primary instead of staying the theme
 * default (e.g. a light amber accent gets dark text, a deep blue gets light). */
export function accentForeground(accent: string): string {
  // hslChannelsToHex always returns a valid "#rrggbb" (or "#000000"), so the
  // parse can't fail here; default to black defensively.
  const [r, g, b] = hexToRgb(hslChannelsToHex(accent)) ?? [0, 0, 0];
  const luminance = 0.299 * r + 0.587 * g + 0.114 * b;
  return luminance > 0.5 ? "222 47% 11%" : "210 40% 98%";
}
