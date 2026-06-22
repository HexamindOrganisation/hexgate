/**
 * CodeMirror theme for the policy YAML editor.
 *
 * Mirrors the landing-page editor mock: keys render as plain foreground
 * (structure recedes), values carry the color. Numbers go to a purple
 * accent (`--syntax-purple`), strings stay in our "allow green"
 * (`--semantic-allow`) — so a quoted string token looks indistinguishable
 * from an `allow` mode value, which is intentional: both signal "this
 * is the affirmative case."
 *
 * Mode-value semantic coloring (`mode: deny` red, `mode: approval_required`
 * amber, `mode: allow` green) lives in a separate ViewPlugin in
 * `./decorations.ts` — YAML's grammar doesn't recognize those identifiers
 * as anything special, so the plugin pattern-matches them and overlays
 * Decorations.
 *
 * Colors pull from CSS vars in `src/index.css` so the editor follows any
 * future palette tweak automatically. Today the dashboard is dark-only
 * (`main.tsx` adds `class="dark"` on <html>); a light variant is a 10-line
 * `createTheme({ theme: 'light', ... })` mirror.
 */
import { EditorView } from '@codemirror/view'
import { tags as t } from '@lezer/highlight'
import { createTheme } from '@uiw/codemirror-themes'

const settings = {
  background: 'hsl(var(--background))',
  foreground: 'hsl(var(--foreground))',
  caret: 'hsl(var(--primary))',
  selection: 'hsl(var(--primary) / 0.20)',
  selectionMatch: 'hsl(var(--primary) / 0.10)',
  lineHighlight: 'hsl(var(--muted) / 0.30)',
  gutterBackground: 'hsl(var(--background))',
  gutterForeground: 'hsl(var(--muted-foreground))',
  gutterActiveForeground: 'hsl(var(--foreground))',
  gutterBorder: 'hsl(var(--border))',
  fontFamily: 'var(--font-mono)',
  fontSize: '14px',
}

const colorTheme = createTheme({
  theme: 'dark',
  settings,
  styles: [
    // Keys (`version:`, `mode:`, `tools:`, …): plain foreground. The
    // structure of a policy is the same across files; the values are
    // what changes between policies, so the values should pop, not
    // the keys.
    {
      tag: [t.propertyName, t.attributeName],
      color: 'hsl(var(--foreground))',
    },
    // Quoted strings (`"USD"`, `"admin"`). Same green as the `allow`
    // outcome — a quoted string is the "positive case" in policy YAML
    // (a value that authorizes something, e.g. an allowed currency).
    {
      tag: [t.string, t.special(t.string)],
      color: 'hsl(var(--semantic-allow))',
    },
    // Numbers (constraint thresholds like `500`) and booleans. Purple
    // matches the landing page mock — distinct from both `allow` green
    // and the semantic mode-value coloring set up in decorations.ts.
    {
      tag: [t.number, t.bool, t.atom],
      color: 'hsl(var(--syntax-purple))',
    },
    {
      tag: [t.comment, t.lineComment, t.blockComment],
      color: 'hsl(var(--muted-foreground))',
      fontStyle: 'italic',
    },
    // Comparators in constraint strings (`<=`, `==`, etc.) and structural
    // punctuation. Slightly dimmed so they don't compete with values.
    {
      tag: [t.punctuation, t.separator, t.bracket],
      color: 'hsl(var(--muted-foreground))',
    },
  ],
})

/**
 * Editor chrome + semantic decoration colors. Keeping these in
 * `EditorView.theme` (rather than the global stylesheet) keeps the editor
 * styling self-contained — drop the package and the CSS goes with it.
 *
 * The `.cm-policy-*` classes are applied by the ViewPlugin in
 * `./decorations.ts`; defining the colors here keeps theme concerns in
 * one file.
 */
const chromeAndDecorationsTheme = EditorView.theme({
  '.cm-content': { padding: '12px 16px' },
  '.cm-scroller': { fontFamily: 'var(--font-mono)' },
  // Semantic mode-value coloring — matches the policy outcomes the
  // operator is signalling. Same colors as the badge in the dashboard.
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

export const policyEditorTheme = [colorTheme, chromeAndDecorationsTheme]
