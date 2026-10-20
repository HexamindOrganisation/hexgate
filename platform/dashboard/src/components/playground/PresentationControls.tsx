import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ExternalLink, Moon, Palette, Sun, X } from "lucide-react";

import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { useIsDark, useTheme, schemeAccent } from "@/lib/theme";
import {
  accentForeground,
  hexToHslChannels,
  hslChannelsToHex,
} from "@/lib/color";
import { usePlaygroundPrefs } from "@/lib/playground_prefs";
import { PRESENT_URL, usePresentMode } from "@/lib/present";
import { cn } from "@/lib/utils";

/** The shared round icon-button in the Playground's floating corners. */
export const ROUND_BTN =
  "grid size-8 place-items-center rounded-full border border-border/60 bg-card/70 text-muted-foreground backdrop-blur transition-colors hover:bg-accent hover:text-foreground";

/**
 * Paints a custom accent onto <html> (not just this subtree) so Radix popovers —
 * which portal to <body> — pick it up too, with a readable --primary-foreground;
 * cleaned up on unmount so it never leaks to other routes.
 *
 * Owns the accent subscription (renders nothing) so dragging the colour picker
 * repaints here instead of re-rendering the whole Playground transcript.
 */
export function AccentStyle() {
  const accent = usePlaygroundPrefs((s) => s.accent);
  useEffect(() => {
    if (!accent) return;
    const root = document.documentElement;
    root.style.setProperty("--primary", accent);
    root.style.setProperty("--ring", accent);
    root.style.setProperty("--primary-foreground", accentForeground(accent));
    return () => {
      root.style.removeProperty("--primary");
      root.style.removeProperty("--ring");
      root.style.removeProperty("--primary-foreground");
    };
  }, [accent]);
  return null;
}

// Curated accent swatches (HSL channels, matching the --primary token format).
// "Blue"/"Violet"/"Plum" mirror built-in schemes — read from theme's single
// source so they match the built-in in BOTH palettes, not just the mode whose
// value happened to be hardcoded; the rest are extra demo accents.
function accentPresets(dark: boolean): { name: string; value: string }[] {
  return [
    { name: "Blue", value: schemeAccent("blue", dark) },
    { name: "Violet", value: schemeAccent("violet", dark) },
    { name: "Plum", value: schemeAccent("plum", dark) },
    { name: "Rose", value: "340 75% 55%" },
    { name: "Amber", value: "38 92% 50%" },
    { name: "Emerald", value: "152 60% 40%" },
  ];
}

function AccentPicker() {
  const accent = usePlaygroundPrefs((s) => s.accent);
  const setAccent = usePlaygroundPrefs((s) => s.setAccent);
  const dark = useIsDark();
  const presets = useMemo(() => accentPresets(dark), [dark]);
  // Draft hex for the native picker, committed to the store on blur so dragging
  // the slider doesn't write localStorage (and repaint <html>) on every move.
  const [draft, setDraft] = useState<string | null>(null);
  const hex = draft ?? (accent ? hslChannelsToHex(accent) : "#3b82f6");
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          title="Customize the accent color"
          className={ROUND_BTN}
        >
          <Palette
            className="size-3.5"
            style={accent ? { color: `hsl(${accent})` } : undefined}
          />
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-56 p-3">
        <div className="mb-2 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
          Accent color
        </div>
        <div className="flex flex-wrap gap-2">
          {presets.map((p) => (
            <button
              key={p.name}
              type="button"
              title={p.name}
              onClick={() => setAccent(p.value)}
              className={cn(
                "size-6 rounded-full ring-offset-2 ring-offset-popover transition",
                accent === p.value && "ring-2 ring-foreground",
              )}
              style={{ backgroundColor: `hsl(${p.value})` }}
            />
          ))}
        </div>
        <div className="mt-3 flex items-center justify-between gap-2">
          <label className="flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
            <input
              type="color"
              value={hex}
              onChange={(e) => setDraft(e.target.value)}
              onBlur={() => {
                if (draft !== null) {
                  setAccent(hexToHslChannels(draft));
                  setDraft(null);
                }
              }}
              className="size-6 cursor-pointer rounded border border-border/60 bg-transparent p-0"
            />
            Custom
          </label>
          <button
            type="button"
            onClick={() => setAccent("")}
            className="text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
          >
            Reset
          </button>
        </div>
        <p className="mt-2 text-[11px] leading-snug text-muted-foreground">
          Repaints this playground's highlight (
          <span className="font-mono">--primary</span>) — your own
          agent-interface look.
        </p>
      </PopoverContent>
    </Popover>
  );
}

/** Top-right presentation controls: open in a new tab (or exit full screen),
 * toggle dark/light, and pick a custom accent — the bits that make the
 * playground a demo surface. */
export function PresentationControls() {
  const present = usePresentMode();
  const navigate = useNavigate();
  const setMode = useTheme((s) => s.setMode);
  const dark = useIsDark();
  return (
    <>
      {present ? (
        <button
          type="button"
          onClick={() => {
            // Always exit full-screen reliably (drop the ?present param); also
            // close the tab when the browser allows it — refused for a tab that
            // was reloaded, which is why navigate is the real action, not close.
            window.close();
            navigate("/playground");
          }}
          title="Exit full screen"
          className={ROUND_BTN}
        >
          <X className="size-3.5" />
        </button>
      ) : (
        <button
          type="button"
          onClick={() => window.open(PRESENT_URL, "_blank", "noopener")}
          title="Open full screen in a new tab"
          className={ROUND_BTN}
        >
          <ExternalLink className="size-3.5" />
        </button>
      )}
      <button
        type="button"
        onClick={() => setMode(dark ? "light" : "dark")}
        title={dark ? "Switch to light" : "Switch to dark"}
        className={ROUND_BTN}
      >
        {dark ? <Sun className="size-3.5" /> : <Moon className="size-3.5" />}
      </button>
      <AccentPicker />
    </>
  );
}
