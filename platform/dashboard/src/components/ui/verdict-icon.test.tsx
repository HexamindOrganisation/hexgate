import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { VerdictIcon } from "./verdict-icon";

// Allow and deny sit close under red-green colour blindness, so the shape is
// the cue that must not regress: check for allow, cross for deny.
describe("VerdictIcon", () => {
  it.each([
    ["allow", "lucide-check"],
    ["deny", "lucide-x"],
    ["needs_approval", "lucide-circle-dashed"],
    ["approval_required", "lucide-circle-dashed"],
  ] as const)("draws %s as %s", (verdict, cls) => {
    const { container } = render(<VerdictIcon verdict={verdict} />);
    expect(container.querySelector("svg")).toHaveClass(cls);
  });
});
