/**
 * Policy YAML editor (CodeMirror 6) — replaces the plain <textarea> at
 * the heart of /policies.
 *
 * Features:
 *   * YAML syntax highlighting via @codemirror/lang-yaml
 *   * Line numbers + lint gutter (inline error markers)
 *   * Server validation results piped in via the `diagnostics` prop —
 *     mapped to CodeMirror Diagnostics in `./diagnostics`, pushed
 *     imperatively via `setDiagnostics` so we control exactly when they
 *     show vs clear (the parent clears them on edit; we follow)
 *
 * Validation stays server-side. CodeMirror's lint extension is only the
 * rendering surface for diagnostics — the source of truth is the
 * platform's `/policies/validate` endpoint.
 */
import { useEffect, useMemo, useRef } from 'react'
import CodeMirror, { type ReactCodeMirrorRef } from '@uiw/react-codemirror'
import { yaml } from '@codemirror/lang-yaml'
import { lintGutter, linter, setDiagnostics } from '@codemirror/lint'

import type { PolicyValidationError } from '@/lib/api'
import { toCodemirrorDiagnostics } from './diagnostics'
import { policyEditorTheme } from './theme'

export interface PolicyEditorProps {
  value: string
  onChange: (next: string) => void
  /**
   * Server-side validation errors. Line-anchored ones render as gutter
   * markers; role-only errors (no line) are dropped here — the parent's
   * error list surfaces them above the editor instead.
   */
  diagnostics?: PolicyValidationError[] | null
  readOnly?: boolean
  className?: string
}

export function PolicyEditor({
  value,
  onChange,
  diagnostics,
  readOnly = false,
  className,
}: PolicyEditorProps) {
  const ref = useRef<ReactCodeMirrorRef>(null)

  // Extensions are memoized so CodeMirror doesn't rebuild the editor on
  // every parent re-render. The linter source is a no-op — we push
  // diagnostics imperatively below.
  const extensions = useMemo(() => [yaml(), lintGutter(), linter(() => [])], [])

  // Push the latest validation results into the lint state. Empty list
  // when `diagnostics` is null/empty clears the gutter markers.
  useEffect(() => {
    const view = ref.current?.view
    if (!view) return
    const next = diagnostics
      ? toCodemirrorDiagnostics(view.state, diagnostics)
      : []
    view.dispatch(setDiagnostics(view.state, next))
  }, [diagnostics])

  return (
    <CodeMirror
      ref={ref}
      value={value}
      onChange={onChange}
      extensions={extensions}
      readOnly={readOnly}
      theme={policyEditorTheme}
      basicSetup={{
        lineNumbers: true,
        // Folding is more annoying than useful at ~50-100 line policies.
        foldGutter: false,
        highlightActiveLine: true,
        highlightActiveLineGutter: true,
        // Defer until we wire JSON-schema-driven completions.
        autocompletion: false,
        // No meaningful bracket pairs in YAML.
        bracketMatching: false,
        closeBrackets: false,
        searchKeymap: true,
      }}
      className={className}
      height="100%"
      style={{ height: '100%', fontSize: '13px' }}
    />
  )
}
