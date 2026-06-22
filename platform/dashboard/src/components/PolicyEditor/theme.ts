/**
 * Policy editor theme: Tokyo Night Storm + a small overlay for editor
 * chrome (padding, font) and policy-semantic decoration colors.
 *
 * Tokyo Night Storm is a well-tuned community theme — same palette as
 * the VS Code original, ported to CodeMirror by @uiw. We use it
 * verbatim for syntax colors instead of hand-rolling, then layer on:
 *
 *   * `.cm-content` padding so YAML doesn't butt against the gutter
 *   * IBM Plex Mono via `var(--font-mono)` to match the rest of the
 *     dashboard's mono surfaces
 *   * `.cm-policy-allow / -deny / -approval` colors used by the
 *     ViewPlugin in `./decorations.ts` — these still come from our
 *     `--semantic-*` CSS vars so the editor's outcome colors match
 *     the dashboard badges and audit dashboard.
 *
 * Tokyo Night Storm brings its own background (~`#24283b`) which is
 * deliberately a touch different from the dashboard's `--background`
 * — gives the editor a visible code-pane affordance, same shape as
 * VS Code's editor vs. sidebar separation. Override the background
 * via `tokyoNightStormInit({ settings: { background: 'hsl(...)' } })`
 * later if we want them to match exactly.
 */
import { EditorView } from '@codemirror/view'
import { tokyoNightStormInit } from '@uiw/codemirror-theme-tokyo-night-storm'

const tokyoNight = tokyoNightStormInit({
  settings: {
    // Mono face — Tokyo Night Storm's default is a stack we don't use.
    fontFamily: 'var(--font-mono)',
    // Match the dashboard's text-sm (14px).
    fontSize: '14px',
  },
})

/**
 * Editor chrome + semantic decoration colors. Kept here (in
 * `EditorView.theme`) rather than the global stylesheet so the editor
 * styling is self-contained.
 */
const chromeAndDecorationsTheme = EditorView.theme({
  '.cm-content': { padding: '12px 16px' },
  '.cm-scroller': { fontFamily: 'var(--font-mono)' },
  // Semantic mode-value coloring — applied by the ViewPlugin in
  // `./decorations.ts`. Colors come from `--semantic-*` CSS vars so the
  // editor matches the dashboard badges and audit-decision colors.
  '.cm-policy-allow': {
    color: 'hsl(var(--semantic-allow))',
    fontWeight: '600',
  },
  '.cm-policy-deny': {
    color: 'hsl(var(--semantic-deny))',
    fontWeight: '600',
  },
  '.cm-policy-approval': {
    color: 'hsl(var(--semantic-approval))',
    fontWeight: '600',
  },
})

export const policyEditorTheme = [tokyoNight, chromeAndDecorationsTheme]
