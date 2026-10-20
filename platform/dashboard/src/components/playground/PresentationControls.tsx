import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Maximize2, Minimize2, Moon, Sun } from "lucide-react";

import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { schemeAccent, useIsDark, useTheme, SCHEMES } from "@/lib/theme";
import { hslChannelsToHex } from "@/lib/color";
import {
  PLAYGROUND_ACCENTS,
  resolveAccent,
  usePlaygroundAccent,
  usePlaygroundPrefs,
} from "@/lib/playground_prefs";
import { PRESENT_URL, usePresentMode } from "@/lib/present";
import { cn } from "@/lib/utils";

/** The Playground's top-bar text button. */
export const BAR_BTN =
  "inline-flex h-8 items-center gap-1.5 rounded-md px-2 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:pointer-events-none disabled:opacity-40";

/** Pick the Playground-only accent: same as the app, a preset, or custom. */
export function AccentPicker() {
  const accent = usePlaygroundPrefs((s) => s.accent);
  const setAccent = usePlaygroundPrefs((s) => s.setAccent);
  const dark = useIsDark();
  const appScheme = useTheme((s) => s.scheme);
  const current = resolveAccent(accent, dark);
  // An id that no longer resolves (a removed preset) reads as "Same as app".
  const selected = current ? accent : "";
  // Draft hex for the native picker, committed on blur so dragging the slider
  // doesn't write localStorage on every move.
  const [draft, setDraft] = useState<string | null>(null);
  const hex =
    draft ??
    (accent.startsWith("#")
      ? accent
      : hslChannelsToHex(current ?? schemeAccent(appScheme, dark)));
  const vars = usePlaygroundAccent();
  const commitDraft = () => {
    if (draft !== null) {
      setAccent(draft);
      setDraft(null);
    }
  };
  const appLabel = SCHEMES.find((s) => s.id === appScheme)?.label ?? "Blue";
  return (
    // Closing the popover unmounts the input before it blurs: commit then.
    <Popover onOpenChange={(o) => !o && commitDraft()}>
      <PopoverTrigger asChild>
        <button
          type="button"
          title="Playground accent"
          aria-label="Playground accent"
          className={BAR_BTN}
        >
          <span
            className="size-4 rounded-full border border-border"
            style={{ background: "hsl(var(--primary))" }}
          />
        </button>
      </PopoverTrigger>
      {/* Portalled out of the Playground, so it carries the accent itself. */}
      <PopoverContent align="end" className="w-72 p-4" style={vars}>
        <div className="text-sm font-medium">Playground accent</div>
        <p className="mt-1 text-xs leading-snug text-muted-foreground">
          Only the Playground changes — handy for matching a customer's brand in
          a demo. The rest of Hexgate stays {appLabel}.
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => setAccent("")}
            aria-pressed={selected === ""}
            className={cn(
              "rounded-full border border-dashed border-border px-2.5 py-1 text-xs",
              selected === "" && "border-solid border-foreground",
            )}
          >
            Same as app
          </button>
          {PLAYGROUND_ACCENTS.map((p) => (
            <button
              key={p.id}
              type="button"
              title={p.label}
              aria-label={p.label}
              aria-pressed={selected === p.id}
              onClick={() => setAccent(p.id)}
              className={cn(
                "size-6 rounded-full ring-offset-2 ring-offset-popover transition",
                selected === p.id && "ring-2 ring-foreground",
              )}
              style={{ backgroundColor: `hsl(${dark ? p.dark : p.light})` }}
            />
          ))}
        </div>
        <label className="mt-3 flex cursor-pointer items-center justify-between gap-2 rounded-md border border-border px-3 py-2 text-xs text-muted-foreground">
          Custom
          <span className="flex items-center gap-2 font-mono text-foreground">
            {hex.toUpperCase()}
            <input
              type="color"
              value={hex}
              aria-label="Custom accent"
              onChange={(e) => setDraft(e.target.value)}
              onBlur={commitDraft}
              className="size-5 cursor-pointer rounded border border-border bg-transparent p-0"
            />
          </span>
        </label>
        <p className="mt-3 text-[11px] text-muted-foreground">
          Decision colours never change.
        </p>
      </PopoverContent>
    </Popover>
  );
}

/** Light/dark for the whole app (full screen has no app menu to do it). */
export function ThemeToggle() {
  const setMode = useTheme((s) => s.setMode);
  const dark = useIsDark();
  return (
    <button
      type="button"
      onClick={() => setMode(dark ? "light" : "dark")}
      title={dark ? "Switch to light" : "Switch to dark"}
      aria-label={dark ? "Switch to light" : "Switch to dark"}
      className={BAR_BTN}
    >
      {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
    </button>
  );
}

/** Open full screen in a new tab, or leave it. */
export function FullScreenToggle() {
  const present = usePresentMode();
  const navigate = useNavigate();
  if (present) {
    return (
      <button
        type="button"
        onClick={() => {
          // Close the tab when the browser allows it (refused after a reload),
          // and always drop ?present so leaving works either way.
          window.close();
          navigate("/playground");
        }}
        className={BAR_BTN}
      >
        <Minimize2 className="size-4" />
        Exit full screen
      </button>
    );
  }
  return (
    <button
      type="button"
      onClick={() => window.open(PRESENT_URL, "_blank", "noopener")}
      title="Open full screen in a new tab"
      aria-label="Open full screen"
      className={BAR_BTN}
    >
      <Maximize2 className="size-4" />
    </button>
  );
}
