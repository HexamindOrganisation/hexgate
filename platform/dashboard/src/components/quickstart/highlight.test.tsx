/**
 * highlight — colors the quickstart's snippets without changing a character of
 * them, and tags the tokens a reader looks for (YAML keys, Python keywords,
 * comments, shell commands and flags, .env keys).
 */

import { render } from "@testing-library/react";
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
import { highlight, type CodeLang } from "@/components/quickstart/highlight";
import { DARK_INK } from "@/components/PolicyEditor/palette";

function renderCode(code: string, lang: CodeLang) {
  const { container } = render(<code>{highlight(code, lang)}</code>);
  return container.querySelector("code")!;
}

const tokens = (el: HTMLElement, cls: string) =>
  [...el.querySelectorAll(`.${cls}`)].map((n) => n.textContent);

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

  it("colors YAML keys, strings and comments", () => {
    const el = renderCode(POLICY_YAML, "yaml");
    expect(tokens(el, "qs-tok-key")).toEqual(
      expect.arrayContaining(["version", "roles", "support", "constraints"]),
    );
    expect(tokens(el, "qs-tok-comment")).toContain(
      "# front-line: small USD refunds only",
    );
  });

  it("colors Python keywords, strings and calls", () => {
    const el = renderCode(WRAP.openai, "python");
    expect(tokens(el, "qs-tok-keyword")).toEqual(
      expect.arrayContaining(["import", "from", "async", "def", "await"]),
    );
    expect(tokens(el, "qs-tok-function")).toContain("HexgateRunner");
    expect(tokens(el, "qs-tok-key")).toContain("refund_as");
    expect(tokens(el, "qs-tok-string").length).toBeGreaterThan(0);
  });

  it("colors shell commands, flags and comments, and .env keys", () => {
    const sh = renderCode(`${INSTALL}\n${REGISTER.langchain}`, "bash");
    expect(tokens(sh, "qs-tok-function")).toEqual(["pip", "hexgate"]);
    expect(tokens(sh, "qs-tok-keyword")).toEqual(
      expect.arrayContaining(["--agent", "--tools", "--model"]),
    );

    const env = renderCode(ENV_FILE, "env");
    expect(tokens(env, "qs-tok-key")).toEqual([
      "HEXGATE_API_KEY",
      "OPENAI_API_KEY",
    ]);
    expect(tokens(env, "qs-tok-comment")).toHaveLength(1);
  });

  it("reads shell continuations from the backslash, and commands after && or ;", () => {
    const el = renderCode(
      "hexgate register \\\n--tools x:T\ncd app && pip install hexgate; ls",
      "bash",
    );
    expect(tokens(el, "qs-tok-function")).toEqual([
      "hexgate",
      "cd",
      "pip",
      "ls",
    ]);
    expect(tokens(el, "qs-tok-keyword")).toEqual(["--tools"]);
  });

  it("doesn't start a comment at a # inside quotes", () => {
    const sh = renderCode('git commit -m "fix #1" # amend later', "bash");
    expect(tokens(sh, "qs-tok-comment")).toEqual(["# amend later"]);

    const env = renderCode('NOTE="a #b"\n# a comment', "env");
    expect(tokens(env, "qs-tok-key")).toEqual(["NOTE"]);
    expect(tokens(env, "qs-tok-comment")).toEqual(["# a comment"]);
    expect(env.textContent).toBe('NOTE="a #b"\n# a comment');
  });

  it("colors tokens with the policy editor's inks", () => {
    const el = renderCode(POLICY_YAML, "yaml");
    const key = el.querySelector<HTMLElement>(".qs-tok-key")!;
    expect(key.style.color).toBe(hexToRgb(DARK_INK.keyword));
  });
});

function hexToRgb(hex: string) {
  const n = parseInt(hex.slice(1), 16);
  return `rgb(${n >> 16}, ${(n >> 8) & 255}, ${n & 255})`;
}
