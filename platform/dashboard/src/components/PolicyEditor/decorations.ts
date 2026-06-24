/**
 * Semantic value decorations for policy mode values.
 *
 * YAML's grammar treats `allow` / `deny` / `approval_required` as ordinary
 * identifiers — same token class as any other unquoted scalar. To make
 * them carry policy-outcome colors in the editor we overlay a CSS class
 * on the value range using a ViewPlugin.
 *
 * The plugin scans the whole document each update. Policy YAMLs are
 * small (≤100 lines in practice) so a whole-doc rescan on every edit is
 * trivial; if that ever changes, switch to a viewport-only walk via
 * `view.visibleRanges`.
 *
 * The regex matches `mode: <value>` on its own line, allowing arbitrary
 * leading whitespace (the YAML can be nested any depth). It does NOT
 * match `default_policy.mode` literal lookups (no such syntax exists in
 * our YAML — `default_policy: { mode: deny }` is multi-line in practice,
 * and the inline-flow form still has `mode: deny` on a key-value pair
 * the regex matches inside the braces).
 */
import { RangeSetBuilder } from "@codemirror/state";
import {
  Decoration,
  type DecorationSet,
  type EditorView,
  ViewPlugin,
  type ViewUpdate,
} from "@codemirror/view";

const CLASS_FOR_VALUE: Record<string, string> = {
  allow: "cm-policy-allow",
  deny: "cm-policy-deny",
  approval_required: "cm-policy-approval",
};

// `mode: deny`, `  mode: allow`, `    mode:    approval_required`, etc.
// Captured group is the value; we use match.index + leading-text length
// to compute its absolute position.
const MODE_LINE = /^(\s*mode:\s+)(allow|deny|approval_required)\b/gm;

function buildDecorations(view: EditorView): DecorationSet {
  const builder = new RangeSetBuilder<Decoration>();
  const doc = view.state.doc.toString();
  // Reset stateful regex between calls (`g` flag keeps lastIndex).
  MODE_LINE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = MODE_LINE.exec(doc)) !== null) {
    const [, prefix, value] = match;
    const from = match.index + prefix.length;
    const to = from + value.length;
    const className = CLASS_FOR_VALUE[value];
    if (className) {
      builder.add(from, to, Decoration.mark({ class: className }));
    }
  }
  return builder.finish();
}

export const policyModeDecorations = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet;
    constructor(view: EditorView) {
      this.decorations = buildDecorations(view);
    }
    update(update: ViewUpdate) {
      // Rebuild only when content changed — viewport scrolls don't move
      // ranges. (For our doc sizes this is cheap; viewport-aware logic
      // would matter past ~10k lines.)
      if (update.docChanged) {
        this.decorations = buildDecorations(update.view);
      }
    }
  },
  {
    decorations: (instance) => instance.decorations,
  },
);
