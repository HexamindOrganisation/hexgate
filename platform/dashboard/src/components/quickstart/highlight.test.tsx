/**
 * highlight — colors the quickstart's snippets without changing a character of
 * them, with the policy editor's colors: YAML keys, Python keywords and calls,
 * comments, shell commands and flags, .env keys.
 */

import { render } from "@testing-library/react";
import { tags as t } from "@lezer/highlight";
import { describe, expect, it } from "vitest";

import {
  ENV_FILE,
  FRAMEWORKS,
  INSTALL,
  POLICY_YAML,
  REGISTER,
  RUN_COMMAND,
  WRAP,
} from "@/components/quickstart/content";
import {
  highlight,
  ink,
  type CodeLang,
} from "@/components/quickstart/highlight";

function renderCode(code: string, lang: CodeLang) {
  const { container } = render(<code>{highlight(code, lang)}</code>);
  return container.querySelector("code")!;
}

function rgb(hex: string) {
  const n = parseInt(hex.slice(1), 16);
  return `rgb(${n >> 16}, ${(n >> 8) & 255}, ${n & 255})`;
}

/** The text of every token drawn in `color`. */
const tokens = (el: HTMLElement, color: string | undefined) =>
  [...el.querySelectorAll<HTMLElement>("span")]
    .filter((n) => n.style.color === rgb(color!))
    .map((n) => n.textContent);

const KEY = ink(t.propertyName); // the editor's YAML key color
const KEYWORD = ink(t.keyword);
const CALL = ink(t.function(t.variableName));
const STRING = ink(t.string);
const FLAG = ink(t.typeName);
const COMMENT = ink(t.comment);

describe("highlight", () => {
  const snippets: [string, string, CodeLang][] = [
    ["policy", POLICY_YAML, "yaml"],
    ["env", ENV_FILE, "env"],
    ["run", RUN_COMMAND, "bash"],
    ...FRAMEWORKS.flatMap(({ id }): [string, string, CodeLang][] => [
      [`register ${id}`, `${INSTALL}\n${REGISTER[id]}`, "bash"],
      [`wrap ${id}`, WRAP[id], "python"],
    ]),
  ];

  it.each(snippets)("keeps the %s snippet's text intact", (_, code, lang) => {
    expect(renderCode(code, lang).textContent).toBe(code);
  });

  it("colors YAML keys and comments", () => {
    const el = renderCode(POLICY_YAML, "yaml");
    expect(tokens(el, KEY)).toEqual(
      expect.arrayContaining(["version", "roles", "support", "constraints"]),
    );
    expect(tokens(el, COMMENT)).toContain(
      "# front-line: small USD refunds only",
    );
  });

  it("colors Python keywords, calls and strings", () => {
    const el = renderCode(WRAP.openai, "python");
    expect(tokens(el, KEYWORD)).toEqual(
      expect.arrayContaining(["import", "from", "def", "await"]),
    );
    expect(tokens(el, CALL)).toContain("HexgateRunner");
    expect(tokens(el, STRING).length).toBeGreaterThan(0);
  });

  it("colors shell commands and flags, reading continuations from the backslash", () => {
    const sh = renderCode(`${INSTALL}\n${REGISTER.langchain}`, "bash");
    expect(tokens(sh, CALL)).toEqual(["pip", "hexgate"]);
    expect(tokens(sh, FLAG)).toEqual(["--agent", "--tools", "--model"]);

    // An unindented continuation line starts with a flag, not a command.
    const cont = renderCode("hexgate register \\\n--tools x:T", "bash");
    expect(tokens(cont, CALL)).toEqual(["hexgate"]);
    expect(tokens(cont, FLAG)).toEqual(["--tools"]);
  });

  it("colors .env keys and whole-line comments only", () => {
    const env = renderCode(ENV_FILE, "env");
    expect(tokens(env, KEY)).toEqual(["HEXGATE_API_KEY", "OPENAI_API_KEY"]);
    expect(tokens(env, COMMENT)).toHaveLength(1);

    const quoted = renderCode('NOTE="a #b"', "env");
    expect(tokens(quoted, COMMENT)).toEqual([]);
  });

  it("uses the editor's colors, with comments readable on the dark block", () => {
    expect(KEY).toBe("#bd93f9");
    // WCAG AA for small text on the block's #0a0d14 ground.
    expect(contrast(COMMENT!, "#0a0d14")).toBeGreaterThanOrEqual(4.5);
  });
});

function contrast(a: string, b: string) {
  const lum = (hex: string) => {
    const [r, g, bl] = [1, 3, 5].map((i) => {
      const c = parseInt(hex.slice(i, i + 2), 16) / 255;
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * bl;
  };
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}
