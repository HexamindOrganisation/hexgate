import { describe, expect, it, vi } from "vitest";

import { saveBlob } from "@/lib/ai-act";

describe("saveBlob", () => {
  it("does not revoke the object URL in the click's own tick", async () => {
    // Safari starts the download asynchronously after the click, so revoking
    // synchronously can hand the user a truncated or zero-byte file.
    const createObjectURL = vi.fn(() => "blob:x");
    const revokeObjectURL = vi.fn();
    URL.createObjectURL = createObjectURL;
    URL.revokeObjectURL = revokeObjectURL;

    saveBlob(new Blob(["{}"]), "report.json");

    expect(createObjectURL).toHaveBeenCalledOnce();
    expect(revokeObjectURL).not.toHaveBeenCalled();

    await new Promise((r) => setTimeout(r, 0));
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:x");
  });
});
