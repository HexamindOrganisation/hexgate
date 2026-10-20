import { describe, expect, it } from "vitest";

import { usePlaygroundPrefs } from "./playground_prefs";
import { SCHEME_ACCENTS } from "./theme";

const migrate = usePlaygroundPrefs.persist.getOptions().migrate!;

describe("playground prefs migration", () => {
  it("repoints v0 presets whose values moved", () => {
    const old = { accent: "226 78% 65%", names: { p1: "Maya" } };
    expect(migrate(old, 0)).toEqual({
      accent: SCHEME_ACCENTS.blue.dark,
      names: { p1: "Maya" },
    });
    expect(migrate({ accent: "262 70% 58%", names: {} }, 0)).toMatchObject({
      accent: SCHEME_ACCENTS.violet.light,
    });
  });

  it("leaves custom accents and current state alone", () => {
    const custom = { accent: "12 34% 56%", names: {} };
    expect(migrate(custom, 0)).toEqual(custom);
    const current = { accent: "226 78% 65%", names: {} };
    expect(migrate(current, 1)).toEqual(current);
  });
});
