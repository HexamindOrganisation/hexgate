/**
 * Policy editor theme — a light and a dark variant, selected by the app theme
 * (see `codeTheme` / `lib/theme`). Both start from Tokyo Night Storm's syntax
 * *structure* (its lezer-tag style array) and only swap colors: a violet
 * palette that matches the app accent — keys/keywords purple, strings green —
 * dark on a near-black ground, dark-on-light for the light variant.
 *
 * The surface (`background`) is `hsl(var(--editor))`, which is mode-aware, so
 * the editor ground tracks the `bg-editor` panes around it. Caret/selection use
 * `hsl(var(--primary))`, so the blue/plum accent flows through automatically.
 * Semantic `.cm-policy-*` colors come from `--semantic-*`, matching the badges.
 */
import { EditorView } from "@codemirror/view";
import {
  tokyoNightStormInit,
  tokyoNightStormStyle,
} from "@uiw/codemirror-theme-tokyo-night-storm";

// Map Tokyo Night Storm's own syntax colors → a dark-mode violet palette.
const DARK_VIOLET: Record<string, string> = {
  "#bb9af7": "#bd93f9", // keyword / operator → dracula purple
  "#7aa2f7": "#bd93f9", // propertyName / function (YAML keys) → purple
  "#9ece6a": "#50fa7b", // string → green
  "#c0caf5": "#f8f8f2", // names → foreground
  "#ff9e64": "#ffb86c", // number → orange
  "#2ac3de": "#8be9fd", // type → cyan
  "#b4f9f8": "#8be9fd", // url / regexp → cyan
  "#565f89": "#6272a4", // comment → muted
  "#89ddff": "#ff79c6", // heading → pink
};

// The same tags recolored for a LIGHT ground — darker, saturated inks that stay
// legible on near-white.
const LIGHT_VIOLET: Record<string, string> = {
  "#bb9af7": "#7c3aed", // keyword / operator → violet
  "#7aa2f7": "#7c3aed", // propertyName / function (YAML keys) → violet
  "#9ece6a": "#15803d", // string → green
  "#c0caf5": "#2a2a3c", // names → near-black ink
  "#ff9e64": "#c2410c", // number → orange
  "#2ac3de": "#0e7490", // type → cyan
  "#b4f9f8": "#0e7490", // url / regexp → cyan
  "#565f89": "#8a8aa0", // comment → muted gray
  "#89ddff": "#be185d", // heading → pink
};

const recolor = (map: Record<string, string>) =>
  tokyoNightStormStyle.map((s) => ({
    ...s,
    color: s.color ? (map[s.color.toLowerCase()] ?? s.color) : s.color,
  }));

// Chrome overrides per mode. Caret/selection use the accent var so they follow
// the blue/plum scheme; the background is the mode-aware editor surface.
const darkChrome = {
  foreground: "#f8f8f2",
  gutterForeground: "#4a5261",
  caret: "hsl(var(--primary))",
  selection: "hsl(var(--primary) / 0.24)",
  selectionMatch: "hsl(var(--primary) / 0.16)",
  lineHighlight: "rgba(255,255,255,0.035)",
} as const;

const lightChrome = {
  foreground: "#2a2a3c",
  gutterForeground: "#b4b4c4",
  caret: "hsl(var(--primary))",
  selection: "hsl(var(--primary) / 0.16)",
  selectionMatch: "hsl(var(--primary) / 0.12)",
  lineHighlight: "rgba(0,0,0,0.03)",
} as const;

function base(
  background: string,
  chrome: object,
  styles: typeof tokyoNightStormStyle,
) {
  return tokyoNightStormInit({
    settings: {
      fontFamily: "var(--font-mono)", // dashboard mono (system mono / SF Mono)
      fontSize: "14px", // matches text-sm
      background,
      gutterBackground: background,
      ...chrome,
    },
    styles,
  });
}

/** Editor chrome (content padding) + semantic decoration colors, mode-agnostic
 * (the `.cm-policy-*` colors are semantic vars, same in both modes). */
const chromeAndDecorationsTheme = EditorView.theme({
  ".cm-content": { padding: "12px 16px" },
  ".cm-scroller": { fontFamily: "var(--font-mono)" },
  ".cm-policy-allow": {
    color: "hsl(var(--semantic-allow))",
    fontWeight: "600",
  },
  ".cm-policy-deny": { color: "hsl(var(--semantic-deny))", fontWeight: "600" },
  ".cm-policy-approval": {
    color: "hsl(var(--semantic-approval))",
    fontWeight: "600",
  },
});

const darkTheme = [
  base("hsl(var(--editor))", darkChrome, recolor(DARK_VIOLET)),
  chromeAndDecorationsTheme,
];
const lightTheme = [
  base("hsl(var(--editor))", lightChrome, recolor(LIGHT_VIOLET)),
  chromeAndDecorationsTheme,
];

/** The editor theme for the current app mode. */
export function codeTheme(dark: boolean) {
  return dark ? darkTheme : lightTheme;
}

/** Transparent-surface variant (for a CodeMirror embedded in a translucent pane,
 * e.g. the Test panel's JSON box) — inherits the pane's shade but keeps the
 * mode-appropriate syntax palette. */
export function codeThemeTransparent(dark: boolean) {
  return [
    base(
      "transparent",
      dark ? darkChrome : lightChrome,
      recolor(dark ? DARK_VIOLET : LIGHT_VIOLET),
    ),
    chromeAndDecorationsTheme,
  ];
}
