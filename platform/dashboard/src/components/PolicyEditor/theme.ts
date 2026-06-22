/**
 * CodeMirror theme for the policy YAML editor.
 *
 * Colors are pulled from the dashboard's `--*` HSL CSS vars (defined in
 * `src/index.css`) so the editor visually matches the rest of the app and
 * automatically follows any future palette tweak. Today the dashboard is
 * hard-coded dark (`main.tsx` adds `class="dark"` on <html>), so only the
 * dark variant is built — adding a light variant later is a 10-line
 * `createTheme({ theme: 'light', ...})` mirror.
 *
 * Token-to-color mapping is intentionally tied to policy semantics:
 *   key      → primary (brand blue) — the YAML field names *are* the policy
 *   string   → allow green          — strings are usually role names / tool ids
 *   number   → approval amber       — numeric thresholds / constraints
 *   bool     → approval amber       — `true`/`false` look like a "decision"
 *   comment  → muted-foreground     — recede
 *   default  → foreground           — punctuation, separators
 */
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
}

export const policyEditorTheme = createTheme({
  theme: 'dark',
  settings,
  styles: [
    {
      tag: [t.propertyName, t.attributeName],
      color: 'hsl(var(--primary))',
      fontWeight: '600',
    },
    {
      tag: [t.string, t.special(t.string)],
      color: 'hsl(var(--semantic-allow))',
    },
    { tag: t.number, color: 'hsl(var(--semantic-approval))' },
    {
      tag: t.bool,
      color: 'hsl(var(--semantic-approval))',
      fontWeight: '600',
    },
    { tag: t.atom, color: 'hsl(var(--semantic-approval))' },
    {
      tag: [t.comment, t.lineComment, t.blockComment],
      color: 'hsl(var(--muted-foreground))',
      fontStyle: 'italic',
    },
    {
      tag: [t.punctuation, t.separator, t.bracket],
      color: 'hsl(var(--foreground))',
    },
  ],
})
