/**
 * Policy editor theme: Tokyo Night Storm + a small overlay for editor
 * chrome (padding, font) and policy-semantic decoration colors.
 *
 * Tokyo Night Storm is a well-tuned community theme — same palette as
 * the VS Code original, ported to CodeMirror by @uiw. We use it
 * verbatim for syntax colors instead of hand-rolling, then layer on:
 *
 *   * `.cm-content` padding so YAML doesn't butt against the gutter
 *   * the dashboard mono face via `var(--font-mono)` (system mono /
 *     SF Mono first) to match the rest of the dashboard's mono surfaces
 *   * `.cm-policy-allow / -deny / -approval` colors used by the
 *     ViewPlugin in `./decorations.ts` — these still come from our
 *     `--semantic-*` CSS vars so the editor's outcome colors match
 *     the dashboard badges and audit dashboard.
 *
 * We start from Tokyo Night Storm but recolor its syntax palette toward a
 * Dracula-style violet (see `violetStyles` below) so the editor matches the
 * app's violet accent — YAML keys and keywords purple, strings green — and
 * override its bluish `#24283b` chrome via `settings` to a near-black, neutral
 * ground (`#0a0d14`), a clear dark code-pane against the lighter dashboard.
 */
import { EditorView } from "@codemirror/view";
import {
  tokyoNightStormInit,
  tokyoNightStormStyle,
} from "@uiw/codemirror-theme-tokyo-night-storm";

// Recolor Tokyo Night Storm's syntax palette toward a Dracula-style violet, so
// the editor matches the app's violet accent instead of Tokyo Night's blues:
// YAML keys (its blue `propertyName`) and keywords become purple, strings green,
// numbers orange, types/urls cyan, comments muted. We reuse the package's OWN
// exported style array — the tag objects are its lezer tags — and only swap
// colors, so this needs no extra theme dependency. The init appends our styles
// after its defaults, so same-tag entries override.
const VIOLET: Record<string, string> = {
  "#bb9af7": "#bd93f9", // keyword / operator → dracula purple
  "#7aa2f7": "#bd93f9", // propertyName / function (YAML keys) blue → purple
  "#9ece6a": "#50fa7b", // string → dracula green
  "#c0caf5": "#f8f8f2", // names → dracula foreground
  "#ff9e64": "#ffb86c", // number → dracula orange
  "#2ac3de": "#8be9fd", // type → dracula cyan
  "#b4f9f8": "#8be9fd", // url / regexp → cyan
  "#565f89": "#6272a4", // comment → dracula comment
  "#89ddff": "#ff79c6", // heading → dracula pink
};
const violetStyles = tokyoNightStormStyle.map((s) => ({
  ...s,
  color: s.color ? (VIOLET[s.color.toLowerCase()] ?? s.color) : s.color,
}));

/**
 * The near-black, neutral chrome that replaces Tokyo Night Storm's bluish
 * defaults on the standalone editor: brighter near-white text, a dim gutter,
 * and a quiet active-line + selection so the (now violet) syntax colors carry
 * the contrast. This assumes a DARK ground, so it is applied only to the
 * standalone editor, never the transparent one (which inherits a pane that may
 * be light).
 */
const nearBlackChrome = {
  foreground: "#f8f8f2",
  gutterForeground: "#4a5261",
  caret: "#bd93f9",
  selection: "rgba(189,147,249,0.22)",
  selectionMatch: "rgba(189,147,249,0.16)",
  lineHighlight: "rgba(255,255,255,0.035)",
} as const;

/**
 * Tokyo Night Storm — recolored to a violet palette — with our mono face and a
 * caller-chosen background. `chrome` carries the dark-ground overrides
 * (:data:`nearBlackChrome`) for the standalone editor; the transparent variant
 * passes none, so it stays legible on whatever surface hosts it.
 */
function tokyoNightWith(background: string, chrome: object = {}) {
  return tokyoNightStormInit({
    settings: {
      // Mono face — the dashboard's `--font-mono` (system mono / SF Mono first).
      fontFamily: "var(--font-mono)",
      // Match the dashboard's text-sm (14px).
      fontSize: "14px",
      background,
      gutterBackground: background,
      ...chrome,
    },
    styles: violetStyles,
  });
}

// Near-black neutral ground for the standalone editor (a visible dark code
// pane), matching the brand mock rather than Tokyo Night Storm's #24283b.
const tokyoNight = tokyoNightWith("#0a0d14", nearBlackChrome);

/**
 * Editor chrome + semantic decoration colors. Kept here (in
 * `EditorView.theme`) rather than the global stylesheet so the editor
 * styling is self-contained.
 */
const chromeAndDecorationsTheme = EditorView.theme({
  ".cm-content": { padding: "12px 16px" },
  ".cm-scroller": { fontFamily: "var(--font-mono)" },
  // Semantic mode-value coloring — applied by the ViewPlugin in
  // `./decorations.ts`. Colors come from `--semantic-*` CSS vars so the
  // editor matches the dashboard badges and audit-decision colors.
  ".cm-policy-allow": {
    color: "hsl(var(--semantic-allow))",
    fontWeight: "600",
  },
  ".cm-policy-deny": {
    color: "hsl(var(--semantic-deny))",
    fontWeight: "600",
  },
  ".cm-policy-approval": {
    color: "hsl(var(--semantic-approval))",
    fontWeight: "600",
  },
});

export const policyEditorTheme = [tokyoNight, chromeAndDecorationsTheme];

/**
 * Same theme with a transparent editor surface — for CodeMirror instances
 * embedded in a translucent pane (e.g. the Test panel's JSON box), so the
 * editor inherits the pane's shade instead of painting its own.
 */
export const policyEditorThemeTransparent = [
  tokyoNightWith("transparent"),
  chromeAndDecorationsTheme,
];
