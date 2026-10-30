import { describe, expect, it } from "vitest";

import { cn } from "./utils";

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
