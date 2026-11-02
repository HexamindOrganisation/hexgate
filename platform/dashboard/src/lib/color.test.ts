import { describe, expect, it } from "vitest";

import { accentForeground, hexToHslChannels, hslChannelsToHex } from "./color";

describe("hexToHslChannels", () => {
  it("converts the primary colors", () => {
    expect(hexToHslChannels("#ff0000")).toBe("0 100% 50%");
    expect(hexToHslChannels("#00ff00")).toBe("120 100% 50%");
    expect(hexToHslChannels("#0000ff")).toBe("240 100% 50%");
  });

  it("converts greys to zero saturation", () => {
    expect(hexToHslChannels("#000000")).toBe("0 0% 0%");
    expect(hexToHslChannels("#ffffff")).toBe("0 0% 100%");
    expect(hexToHslChannels("#808080")).toBe("0 0% 50%");
  });

  it("tolerates a missing # and uppercase", () => {
    expect(hexToHslChannels("FF0000")).toBe("0 100% 50%");
  });

  it("returns '' for an unparseable value", () => {
    expect(hexToHslChannels("nope")).toBe("");
    expect(hexToHslChannels("#fff")).toBe(""); // 3-digit shorthand unsupported
    expect(hexToHslChannels("")).toBe("");
  });
});

describe("hslChannelsToHex", () => {
  it("converts the primary channels", () => {
    expect(hslChannelsToHex("0 100% 50%")).toBe("#ff0000");
    expect(hslChannelsToHex("120 100% 50%")).toBe("#00ff00");
    expect(hslChannelsToHex("240 100% 50%")).toBe("#0000ff");
  });

  it("round-trips a preset accent through hex", () => {
    const hex = hslChannelsToHex("226 70% 52%");
    expect(hexToHslChannels(hex)).toBe("226 70% 52%");
  });

  it("returns #000000 for an unparseable value", () => {
    expect(hslChannelsToHex("")).toBe("#000000");
    expect(hslChannelsToHex("x y z")).toBe("#000000");
  });
});

describe("accentForeground", () => {
  it("picks dark text on a light accent", () => {
    expect(accentForeground("38 92% 50%")).toBe("222 47% 11%"); // amber
    expect(accentForeground("0 0% 100%")).toBe("222 47% 11%"); // white
  });

  it("picks light text on a dark accent", () => {
    expect(accentForeground("226 70% 52%")).toBe("210 40% 98%"); // blue
    expect(accentForeground("0 0% 0%")).toBe("210 40% 98%"); // black
  });

  it("falls back to light on an unparseable accent", () => {
    expect(accentForeground("")).toBe("210 40% 98%");
  });
});
