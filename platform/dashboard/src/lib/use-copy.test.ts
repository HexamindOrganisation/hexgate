/**
 * useCopy — "copied" on success, "failed" when the clipboard refuses (callers
 * copying a show-once secret rely on that), and back to null after the reset.
 */

import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useCopy } from "@/lib/use-copy";

function stubClipboard(writeText: (t: string) => Promise<void>) {
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText },
    configurable: true,
  });
}

describe("useCopy", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("reports copied, then resets", async () => {
    vi.useFakeTimers();
    const write = vi.fn(async () => {});
    stubClipboard(write);
    const { result } = renderHook(() => useCopy(1000));
    await act(() => result.current[1]("secret"));
    expect(write).toHaveBeenCalledWith("secret");
    expect(result.current[0]).toBe("copied");
    act(() => vi.advanceTimersByTime(1000));
    expect(result.current[0]).toBeNull();
  });

  it("reports failed when the clipboard refuses", async () => {
    stubClipboard(async () => {
      throw new Error("denied");
    });
    const { result } = renderHook(() => useCopy());
    await act(() => result.current[1]("secret"));
    expect(result.current[0]).toBe("failed");
  });
});
