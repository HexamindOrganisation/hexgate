import { describe, expect, it } from "vitest";

import { buildChatFrame } from "./playground";

describe("buildChatFrame", () => {
  it("sends a bare chat frame with no identity", () => {
    expect(buildChatFrame("hi")).toEqual({ type: "chat", message: "hi" });
    // An empty opts, empty name, and all-empty roles are all "no identity".
    expect(buildChatFrame("hi", {})).toEqual({ type: "chat", message: "hi" });
    expect(buildChatFrame("hi", { name: "  ", roles: ["", ""] })).toEqual({
      type: "chat",
      message: "hi",
    });
  });

  it("acts as a named, unroled caller when only a name is given", () => {
    const frame = buildChatFrame("deploy", { name: " alice " });
    expect(frame.user_attenuation).toEqual({ user: "alice", ttl_seconds: 300 });
  });

  it("attaches role facts, defaulting the user to playground", () => {
    const frame = buildChatFrame("deploy", { roles: ["release", "engineer"] });
    expect(frame.user_attenuation).toEqual({
      user: "playground",
      ttl_seconds: 300,
      roles: ["release", "engineer"],
      // Singular kept one release for a pinned older `hexgate serve`.
      role: "release",
    });
  });

  it("carries both name and roles, dropping empty role entries", () => {
    const frame = buildChatFrame("deploy", {
      name: "alice",
      roles: ["release", "", "engineer"],
    });
    expect(frame.user_attenuation).toEqual({
      user: "alice",
      ttl_seconds: 300,
      roles: ["release", "engineer"],
      role: "release",
    });
  });
});
