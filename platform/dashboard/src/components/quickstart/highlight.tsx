/**
 * Read-only syntax highlighting for the quickstart's code blocks, colored by
 * the policy editor's own dark syntax styles (`darkSyntax`), since the blocks
 * are always dark. YAML and Python go through their Lezer parsers (the same
 * grammars CodeMirror uses) without an editor; the few shell and .env lines
 * are colored by line shape.
 */

import type { ReactNode } from "react";
import {
  highlightCode,
  tagHighlighter,
  tags as t,
  type Tag,
} from "@lezer/highlight";
import { parser as pythonParser } from "@lezer/python";
import { parser as yamlParser } from "@lezer/yaml";

import { darkSyntax } from "@/components/PolicyEditor/theme";

/** "text" is left as is (program output). */
export type CodeLang = "yaml" | "python" | "bash" | "env" | "text";

/** Comments are lighter than the editor's: at the dialog's 11.5px on its
 * near-black ground, the editor's comment color is under 4.5:1 contrast. */
const COMMENT = "#7a86b4";

// The editor's tag → color table, with each color as the "class" so a token's
// class is its color. Later entries win, so the comment override applies.
const highlighter = tagHighlighter([
  ...darkSyntax.flatMap((s) =>
    s.color ? [{ tag: s.tag, class: String(s.color) }] : [],
  ),
  { tag: [t.comment, t.meta], class: COMMENT },
]);

/** The color the editor gives `tag`, for tokens colored by hand. */
export const ink = (tag: Tag): string | undefined =>
  highlighter.style([tag]) ?? undefined;

function span(text: string, color: string | undefined, key: number) {
  if (!color) return text;
  return (
    // Nested styles come as space-separated classes; the innermost is last.
    <span key={key} style={{ color: color.split(" ").pop() }}>
      {text}
    </span>
  );
}

function lezer(code: string, lang: "yaml" | "python"): ReactNode[] {
  const tree = (lang === "yaml" ? yamlParser : pythonParser).parse(code);
  const out: ReactNode[] = [];
  highlightCode(
    code,
    tree,
    highlighter,
    (text, cls) => out.push(span(text, cls, out.length)),
    () => out.push("\n"),
  );
  return out;
}

/** Shell and .env: a whole-line `#` comment, a .env `KEY=`, a command word
 * (unless the line continues the last one with a trailing `\`) and its
 * `--flags`. Enough for the dialog's fixed snippets, not a shell grammar. */
function shellLines(code: string, lang: "bash" | "env"): ReactNode[] {
  const out: ReactNode[] = [];
  let continued = false;
  code.split("\n").forEach((line, i) => {
    if (i > 0) out.push("\n");
    if (/^\s*#/.test(line)) {
      out.push(span(line, COMMENT, out.length));
      return;
    }
    if (lang === "env") {
      const kv = /^([A-Za-z_]\w*)(=.*)$/.exec(line);
      if (kv) out.push(span(kv[1], ink(t.propertyName), out.length), kv[2]);
      else out.push(line);
      return;
    }
    line.split(/(\s+)/).forEach((word, j) => {
      if (j === 0 && word && !continued) {
        out.push(span(word, ink(t.function(t.variableName)), out.length));
      } else if (/^--?\w/.test(word)) {
        out.push(span(word, ink(t.typeName), out.length));
      } else {
        out.push(word);
      }
    });
    continued = /\\\s*$/.test(line);
  });
  return out;
}

/** `code` as highlighted nodes, ready to go inside a `<code>`. */
export function highlight(code: string, lang: CodeLang): ReactNode[] {
  if (lang === "text") return [code];
  if (lang === "yaml" || lang === "python") return lezer(code, lang);
  return shellLines(code, lang);
}
