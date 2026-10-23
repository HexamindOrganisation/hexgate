import { describe, expect, it } from "vitest";

import {
  accentVars,
  resolveAccent,
  usePlaygroundPrefs,
} from "./playground_prefs";
import { SCHEME_ACCENTS } from "./theme";

const migrate = usePlaygroundPrefs.persist.getOptions().migrate!;

describe("playground prefs migration", () => {
  it("turns stored preset values into ids, from v0 and v1", () => {
    // v0's old dark blue, v1's light violet and an extra preset's value.
    expect(
      migrate({ accent: "226 78% 65%", names: { p1: "Maya" } }, 0),
    ).toEqual({ accent: "blue", names: { p1: "Maya" } });
    expect(
      migrate({ accent: SCHEME_ACCENTS.violet.light, names: {} }, 1),
    ).toMatchObject({ accent: "violet" });
    expect(migrate({ accent: "340 75% 55%", names: {} }, 1)).toMatchObject({
      accent: "rose",
    });
  });

  it("keeps a custom colour as hex and an unset accent unset", () => {
    expect(migrate({ accent: "0 100% 50%", names: {} }, 1)).toMatchObject({
      accent: "#ff0000",
    });
    expect(migrate({ accent: "", names: {} }, 1)).toMatchObject({ accent: "" });
    const current = { accent: "rose", names: {} };
    expect(migrate(current, 2)).toEqual(current);
  });
});

describe("resolveAccent", () => {
  it("follows the palette for a preset and is null for the app's", () => {
    expect(resolveAccent("blue", false)).toBe(SCHEME_ACCENTS.blue.light);
    expect(resolveAccent("blue", true)).toBe(SCHEME_ACCENTS.blue.dark);
    expect(resolveAccent("", true)).toBeNull();
    expect(resolveAccent("#ff0000", true)).toBe("0 100% 50%");
  });

  it("drives main buttons as well as the highlight", () => {
    const vars = accentVars("340 75% 55%") as Record<string, string>;
    expect(vars["--primary"]).toBe("340 75% 55%");
    expect(vars["--action"]).toBe("340 75% 55%");
    expect(vars["--action-foreground"]).toBe(vars["--primary-foreground"]);
    expect(accentVars(null)).toBeUndefined();
  });
});
