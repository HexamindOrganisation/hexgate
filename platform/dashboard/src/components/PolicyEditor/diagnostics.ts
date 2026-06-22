/**
 * Adapter between the server's `PolicyValidationError` payload and
 * CodeMirror's `Diagnostic` shape (used by `@codemirror/lint` for inline
 * gutter markers + hover tooltips).
 *
 * Server-side errors carry `line: number | null`. Errors without a line
 * (e.g. "role X is referenced but not defined" — a whole-document concern)
 * can't be anchored to a position in the editor; we drop them here and
 * leave the existing error list above the editor to surface them.
 */
import type { Diagnostic } from '@codemirror/lint'
import type { EditorState } from '@codemirror/state'

import type { PolicyValidationError } from '@/lib/api'

export function toCodemirrorDiagnostics(
  state: EditorState,
  errors: PolicyValidationError[],
): Diagnostic[] {
  const total = state.doc.lines
  return errors
    .filter(
      (e): e is PolicyValidationError & { line: number } =>
        typeof e.line === 'number' && e.line >= 1,
    )
    .map((err) => {
      // Clamp out-of-range lines (validator may return a line that no
      // longer exists if the user typed since) to the last line.
      const lineNo = Math.min(err.line, total)
      const line = state.doc.line(lineNo)
      return {
        from: line.from,
        to: line.to,
        severity: 'error',
        message: err.role ? `${err.role}: ${err.message}` : err.message,
      } satisfies Diagnostic
    })
}
