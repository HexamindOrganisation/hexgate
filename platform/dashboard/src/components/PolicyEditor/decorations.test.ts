import { describe, expect, it } from "vitest";
import { EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";

import { policyModeDecorations } from "./decorations";

/**
 * Build a real EditorView, run the ViewPlugin, return the ranges it
 * decorated as `[from, to, class]` tuples. Avoids JSDOM-rendered DOM
 * inspection — we're testing the decoration set the plugin produces,
 * not how CodeMirror paints it.
 */
function decoratedRanges(doc: string): Array<[number, number, string]> {
  const view = new EditorView({
    state: EditorState.create({ doc, extensions: [policyModeDecorations] }),
  });
  const plugin = view.plugin(policyModeDecorations);
  const out: Array<[number, number, string]> = [];
  // The plugin's decoration set carries one mark per match; iterate
  // via the public RangeSet cursor.
  const cursor = plugin?.decorations?.iter();
  while (cursor?.value) {
    // `Decoration.mark({ class })` stores spec in `value.spec`.
    const cls = (cursor.value.spec as { class?: string }).class ?? "";
    out.push([cursor.from, cursor.to, cls]);
    cursor.next();
  }
  view.destroy();
  return out;
}

describe("policyModeDecorations", () => {
  it("decorates `mode: deny` with cm-policy-deny", () => {
    const doc = "default_policy:\n  mode: deny\n";
    const ranges = decoratedRanges(doc);
    expect(ranges).toHaveLength(1);
    const [from, to, cls] = ranges[0];
    expect(doc.slice(from, to)).toBe("deny");
    expect(cls).toBe("cm-policy-deny");
  });

  it("decorates `mode: allow` with cm-policy-allow", () => {
    const ranges = decoratedRanges(
      "tools:\n  refund_order:\n    mode: allow\n",
    );
    expect(ranges).toHaveLength(1);
    expect(ranges[0][2]).toBe("cm-policy-allow");
  });

  it("decorates `mode: approval_required` with cm-policy-approval", () => {
    const ranges = decoratedRanges("  mode: approval_required\n");
    expect(ranges).toHaveLength(1);
    expect(ranges[0][2]).toBe("cm-policy-approval");
  });

  it("produces one decoration per matching line", () => {
    const doc = [
      "default_policy:",
      "  mode: deny",
      "tools:",
      "  read_file:",
      "    mode: allow",
      "  write_file:",
      "    mode: approval_required",
      "",
    ].join("\n");
    const ranges = decoratedRanges(doc);
    expect(ranges.map((r) => r[2])).toEqual([
      "cm-policy-deny",
      "cm-policy-allow",
      "cm-policy-approval",
    ]);
  });

  it("ignores `mode:` followed by an unknown value", () => {
    // The validator catches unknown modes; the editor doesn't try to
    // semantic-color them. Plain foreground is the right "I don't know
    // what this is" fallback.
    expect(decoratedRanges("  mode: maybe\n")).toEqual([]);
  });

  it("ignores `mode:` lines that lack a value", () => {
    expect(decoratedRanges("  mode:\n  mode:    \n")).toEqual([]);
  });

  it("does not match values used outside a `mode:` key", () => {
    // The word `allow` appears inside a comment / quoted string here —
    // neither is a YAML key-value pair on its own line, so the regex
    // anchored to ^...mode:...$ correctly leaves them alone.
    const doc = '# the allow path\ndescription: "allow refunds"\n';
    expect(decoratedRanges(doc)).toEqual([]);
  });
});
