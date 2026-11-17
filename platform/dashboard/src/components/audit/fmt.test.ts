import { describe, expect, it } from "vitest";

import { argsPreview } from "./fmt";

describe("argsPreview", () => {
  it("clips a long value", () => {
    expect(argsPreview({ body: "x".repeat(100) })).toBe(
      `(body="${"x".repeat(38)}…)`,
    );
  });

  it("says when arguments were left out", () => {
    const args = Object.fromEntries(
      ["a", "b", "c", "d", "e", "f", "g"].map((k) => [k, 1]),
    );
    expect(argsPreview(args)).toBe("(a=1, b=1, c=1, d=1, e=1, f=1, …)");
  });

  it("is empty for no arguments", () => {
    expect(argsPreview({})).toBe("");
  });
});
