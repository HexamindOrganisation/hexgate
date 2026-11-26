import { useMemo } from "react";
import CodeMirror, { EditorView } from "@uiw/react-codemirror";
import { yaml } from "@codemirror/lang-yaml";
import { unifiedMergeView } from "@codemirror/merge";

import { policyModeDecorations } from "@/components/PolicyEditor/decorations";
import { codeTheme } from "@/components/PolicyEditor/theme";
import { useIsDark } from "@/lib/theme";

const BASIC_SETUP = {
  lineNumbers: true,
  foldGutter: false,
  highlightActiveLine: false,
  highlightActiveLineGutter: false,
  autocompletion: false,
} as const;

/**
 * One file of a change, before and after, as a unified diff: the new text
 * with each removed line shown above the line that replaced it. Read-only,
 * and runs of unchanged lines fold away. A file the change created has no
 * "before", so all of it reads as added.
 */
export function FilesDiff({
  before,
  after,
}: {
  before: string | null;
  after: string;
}) {
  const dark = useIsDark();
  const theme = useMemo(() => codeTheme(dark), [dark]);
  const extensions = useMemo(
    () => [
      yaml(),
      policyModeDecorations,
      EditorView.lineWrapping,
      unifiedMergeView({
        original: before ?? "",
        mergeControls: false,
        gutter: true,
        collapseUnchanged: { margin: 2, minSize: 4 },
      }),
    ],
    [before],
  );
  return (
    <CodeMirror
      value={after}
      extensions={extensions}
      editable={false}
      readOnly
      theme={theme}
      basicSetup={BASIC_SETUP}
      className="max-h-80 overflow-auto text-[11px]"
    />
  );
}
