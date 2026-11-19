/**
 * Read-only syntax highlighting for the quickstart's code blocks. YAML and
 * Python go through their Lezer parsers (the same grammars CodeMirror uses),
 * without an editor; shell and .env snippets are short enough for a small
 * quote-aware scanner. Tokens become `qs-tok-*` spans colored with the policy
 * editor's dark inks (`DARK_INK`), since the blocks are always dark.
 */

import type { ReactNode } from "react";
import { highlightCode, tagHighlighter, tags as t } from "@lezer/highlight";
import { parser as pythonParser } from "@lezer/python";
import { parser as yamlParser } from "@lezer/yaml";

import { DARK_INK } from "@/components/PolicyEditor/palette";

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

const INK: Record<string, string> = {
  "qs-tok-keyword": DARK_INK.keyword,
  "qs-tok-key": DARK_INK.keyword,
  "qs-tok-string": DARK_INK.string,
  "qs-tok-number": DARK_INK.number,
  "qs-tok-comment": DARK_INK.comment,
  "qs-tok-function": DARK_INK.type,
};

function span(text: string, cls: string, key: number): ReactNode {
  if (!cls) return text;
  const color = cls
    .split(" ")
    .map((c) => INK[c])
    .find(Boolean);
  return (
    <span key={key} className={cls} style={{ color }}>
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

/** A line as words and the whitespace between them, plus a trailing
 * comment: a `#` that starts a word outside quotes. Quoted text stays inside
 * its word, so `"a #b"` is not a comment. */
function scan(line: string): { parts: string[]; comment: string } {
  const parts: string[] = [];
  let cur = "";
  let quote: string | null = null;
  const isSpace = (ch: string) => /\s/.test(ch);
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (quote) {
      cur += ch;
      if (ch === quote) quote = null;
      continue;
    }
    // Starting whitespace after a word, or a word after whitespace.
    if (cur && isSpace(ch) !== isSpace(cur[0])) {
      parts.push(cur);
      cur = "";
    }
    if (ch === "#" && cur === "") return { parts, comment: line.slice(i) };
    if (ch === '"' || ch === "'") quote = ch;
    cur += ch;
  }
  if (cur) parts.push(cur);
  return { parts, comment: "" };
}

// After these, the next word is a command again.
const SEPARATORS = new Set(["&&", "||", ";", "|"]);

function shellLines(code: string, lang: "bash" | "env"): ReactNode[] {
  const out: ReactNode[] = [];
  // Whether this line continues the previous one (trailing backslash), so
  // its first word is an argument, not a command.
  let continued = false;
  code.split("\n").forEach((line, i) => {
    if (i > 0) out.push("\n");
    const { parts, comment } = scan(line);
    if (lang === "env") {
      const body = parts.join("");
      const kv = /^([A-Za-z_][A-Za-z0-9_]*)(=.*)$/s.exec(body);
      if (kv) out.push(span(kv[1], "qs-tok-key", out.length), kv[2]);
      else out.push(body);
    } else {
      let command = !continued;
      // A `;` written against a word (`install hexgate; ls`) ends it too.
      const words = parts.flatMap((p) =>
        p.length > 1 && p.endsWith(";") ? [p.slice(0, -1), ";"] : [p],
      );
      for (const p of words) {
        if (!/\S/.test(p)) {
          out.push(p);
        } else if (SEPARATORS.has(p)) {
          out.push(p);
          command = true;
        } else if (command) {
          out.push(span(p, "qs-tok-function", out.length));
          command = false;
        } else if (/^--?[\w-]+/.test(p)) {
          out.push(span(p, "qs-tok-keyword", out.length));
        } else {
          out.push(p);
        }
      }
      continued = !comment && /\\\s*$/.test(line);
    }
    if (comment) out.push(span(comment, "qs-tok-comment", out.length));
  });
  return out;
}

/** `code` as highlighted nodes, ready to go inside a `<code>`. */
export function highlight(code: string, lang: CodeLang): ReactNode[] {
  if (lang === "text") return [code];
  if (lang === "yaml" || lang === "python") return lezer(code, lang);
  return shellLines(code, lang);
}
