/**
 * Read-only syntax highlighting for the quickstart's code blocks. YAML and
 * Python go through their Lezer parsers (the same grammars CodeMirror uses),
 * without an editor; shell and .env snippets are short enough for a few
 * regexes. Tokens become `qs-tok-*` spans, colored in index.css from the policy
 * editor's dark palette, since the blocks are always dark.
 */

import type { ReactNode } from "react";
import { highlightCode, tagHighlighter, tags as t } from "@lezer/highlight";
import { parser as pythonParser } from "@lezer/python";
import { parser as yamlParser } from "@lezer/yaml";

/** "text" is left as is (program output). */
export type CodeLang = "yaml" | "python" | "bash" | "env" | "text";

const highlighter = tagHighlighter([
  {
    tag: [
      t.keyword,
      t.controlKeyword,
      t.definitionKeyword,
      t.moduleKeyword,
      t.operatorKeyword,
      t.modifier,
      t.meta,
    ],
    class: "qs-tok-keyword",
  },
  { tag: [t.string, t.special(t.string)], class: "qs-tok-string" },
  { tag: [t.number, t.bool, t.null], class: "qs-tok-number" },
  { tag: t.lineComment, class: "qs-tok-comment" },
  {
    tag: [
      t.function(t.variableName),
      t.function(t.propertyName),
      t.definition(t.className),
    ],
    class: "qs-tok-function",
  },
  // YAML keys, and Python function names where they're defined.
  {
    tag: [
      t.definition(t.propertyName),
      t.function(t.definition(t.variableName)),
    ],
    class: "qs-tok-key",
  },
]);

function span(text: string, cls: string, key: number): ReactNode {
  return cls ? (
    <span key={key} className={cls}>
      {text}
    </span>
  ) : (
    text
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

// A `#` at the start of a line or after a space starts a comment.
const COMMENT = /(^|\s)#.*$/;

function shellLine(line: string, lang: "bash" | "env", out: ReactNode[]) {
  const m = COMMENT.exec(line);
  const body = m ? line.slice(0, m.index + m[1].length) : line;
  const comment = m ? line.slice(m.index + m[1].length) : "";
  if (lang === "env") {
    const kv = /^([A-Za-z_][A-Za-z0-9_]*)(=.*)$/.exec(body);
    if (kv) {
      out.push(span(kv[1], "qs-tok-key", out.length), kv[2]);
    } else {
      out.push(body);
    }
  } else {
    // The command word of each line, then --flags; continuation lines
    // (indented, after a trailing backslash) only have flags.
    const parts = body.split(/(\s+)/);
    parts.forEach((p, i) => {
      if (i === 0 && p && !line.startsWith(" ")) {
        out.push(span(p, "qs-tok-function", out.length));
      } else if (/^--?[\w-]+/.test(p)) {
        out.push(span(p, "qs-tok-keyword", out.length));
      } else {
        out.push(p);
      }
    });
  }
  if (comment) out.push(span(comment, "qs-tok-comment", out.length));
}

/** `code` as highlighted nodes, ready to go inside a `<code>`. */
export function highlight(code: string, lang: CodeLang): ReactNode[] {
  if (lang === "text") return [code];
  if (lang === "yaml" || lang === "python") return lezer(code, lang);
  const out: ReactNode[] = [];
  code.split("\n").forEach((line, i) => {
    if (i > 0) out.push("\n");
    shellLine(line, lang, out);
  });
  return out;
}
