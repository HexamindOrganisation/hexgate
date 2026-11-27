/**
 * FilesDiff renders a change's file as a unified diff: the removed lines
 * above the new text, and a created file as all added.
 */

import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";

import { FilesDiff } from "./FilesDiff";

const BEFORE = "a: 1\nrefund_cap: 1000\nb: 2\n";
const AFTER = "a: 1\nrefund_cap: 500\nb: 2\n";

describe("FilesDiff", () => {
  it("shows the removed line above the line that replaced it", () => {
    const { container } = render(<FilesDiff before={BEFORE} after={AFTER} />);
    const deleted = container.querySelector(".cm-deletedChunk");
    expect(deleted).toHaveTextContent("refund_cap: 1000");
    const changed = [...container.querySelectorAll(".cm-changedLine")];
    expect(changed.map((l) => l.textContent)).toEqual(["refund_cap: 500"]);
  });

  it("shows a created file as added, with nothing removed", () => {
    const { container } = render(<FilesDiff before={null} after={AFTER} />);
    const removed = [...container.querySelectorAll(".cm-deletedChunk")];
    expect(removed.map((c) => c.textContent).join("")).toBe("");
    expect(container.querySelectorAll(".cm-changedLine").length).toBe(3);
  });

  it("cannot be edited", () => {
    const { container } = render(<FilesDiff before={BEFORE} after={AFTER} />);
    expect(
      container.querySelector(".cm-content")?.getAttribute("contenteditable"),
    ).toBe("false");
  });
});
