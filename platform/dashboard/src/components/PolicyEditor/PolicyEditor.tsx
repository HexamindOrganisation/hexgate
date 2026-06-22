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
import { useEffect, useRef } from 'react'
import CodeMirror, { type ReactCodeMirrorRef } from '@uiw/react-codemirror'
import { yaml } from '@codemirror/lang-yaml'
import { lintGutter, setDiagnostics } from '@codemirror/lint'

import type { PolicyValidationError } from '@/lib/api'
import { policyModeDecorations } from './decorations'
import { toCodemirrorDiagnostics } from './diagnostics'
import { policyEditorTheme } from './theme'

// @uiw/react-codemirror dispatches `StateEffect.reconfigure` whenever
// `extensions`, `basicSetup`, `onChange`, etc. change reference. Defining
// these once at module scope (where stability is free) prevents the
// editor from reconfiguring on every parent re-render — important because
// the parent of <PolicyEditor> re-renders on every keystroke as `value`
// updates.
//
// `setDiagnostics` (used in the effect below) writes into the lint state
// field that `lintGutter()` installs, so we don't need a `linter()` source
// callback — the imperative push is the only signal the gutter needs.
const EXTENSIONS = [yaml(), lintGutter(), policyModeDecorations]

const BASIC_SETUP = {
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
} as const

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

  // Push the latest validation results into the lint state. Empty list
  // when `diagnostics` is null/empty clears the gutter markers. The view
  // is created in a child effect that runs before ours (child-before-parent
  // effect ordering), so `ref.current.view` is populated by this point on
  // initial mount; the `if (!view) return` is a defensive backstop.
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
      extensions={EXTENSIONS}
      readOnly={readOnly}
      theme={policyEditorTheme}
      basicSetup={BASIC_SETUP}
      className={className}
      height="100%"
    />
  )
}
