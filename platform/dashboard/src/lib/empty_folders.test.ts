import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { PolicyFileRead } from "./api";
import { useEmptyFolders } from "./empty_folders";

function file(name: string): PolicyFileRead {
  return { name, content: "", content_hash: "h", updated_at: "t" };
}

const KEY = "hexgate-empty-folders:p1";

beforeEach(() => localStorage.clear());
afterEach(() => localStorage.clear());

describe("useEmptyFolders", () => {
  it("adds a folder and persists it to localStorage", () => {
    const { result } = renderHook(() => useEmptyFolders("p1", []));
    act(() => result.current.addFolder("team_a"));
    expect(result.current.folders).toEqual(["team_a"]);
    expect(JSON.parse(localStorage.getItem(KEY)!)).toEqual(["team_a"]);
  });

  it("rehydrates remembered folders on mount", () => {
    localStorage.setItem(KEY, JSON.stringify(["scratch"]));
    const { result } = renderHook(() => useEmptyFolders("p1", []));
    expect(result.current.folders).toEqual(["scratch"]);
  });

  it("drops a folder once a file lives under it", () => {
    localStorage.setItem(KEY, JSON.stringify(["caps"]));
    const { result } = renderHook(({ files }) => useEmptyFolders("p1", files), {
      initialProps: { files: [file("caps/x.yaml")] },
    });
    expect(result.current.folders).toEqual([]);
    expect(JSON.parse(localStorage.getItem(KEY)!)).toEqual([]);
  });

  it("ignores a prefix a file already lives under (derived, not remembered)", () => {
    const { result } = renderHook(() =>
      useEmptyFolders("p1", [file("caps/x.yaml")]),
    );
    act(() => result.current.addFolder("caps"));
    expect(result.current.folders).toEqual([]);
  });

  it("normalizes surrounding slashes and ignores blanks", () => {
    const { result } = renderHook(() => useEmptyFolders("p1", []));
    act(() => result.current.addFolder("/team_b/"));
    act(() => result.current.addFolder("   "));
    expect(result.current.folders).toEqual(["team_b"]);
  });

  it("removes a folder and its subfolders", () => {
    const { result } = renderHook(() => useEmptyFolders("p1", []));
    act(() => result.current.addFolder("a"));
    act(() => result.current.addFolder("a/b"));
    act(() => result.current.removeFolder("a"));
    expect(result.current.folders).toEqual([]);
  });

  it("is inert (no throw) when no project is scoped", () => {
    const { result } = renderHook(() => useEmptyFolders(null, []));
    act(() => result.current.addFolder("x"));
    // Nothing persisted under a project key.
    expect(localStorage.length).toBe(0);
  });
});
