import { beforeAll, describe, expect, it } from "vitest";

import { readIndexCss } from "@/test/index-css";
import { cn, FONT_SIZES } from "./utils";

describe("cn", () => {
  it("keeps a named font size next to a text colour", () => {
    expect(cn("text-meta", "text-muted-foreground")).toBe(
      "text-meta text-muted-foreground",
    );
  });

  it("lets a later named size override an earlier one", () => {
    expect(cn("text-sm", "text-caption")).toBe("text-caption");
  });
});

describe("FONT_SIZES", () => {
  let css = "";
  beforeAll(async () => {
    css = await readIndexCss();
  });

  it("names every --text-* size declared in index.css", () => {
    const declared = [
      ...css.matchAll(/^\s*--text-([a-z]+(?:-[a-z]+)*):/gm),
    ].map((m) => m[1]);
    expect([...FONT_SIZES].sort()).toEqual([...new Set(declared)].sort());
  });
});
