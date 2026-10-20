import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PageHeader } from "./page-header";

describe("PageHeader", () => {
  it("renders the title as the page heading with description and actions", () => {
    render(
      <PageHeader
        title="Audit"
        description="Every policy decision"
        actions={<button type="button">Export</button>}
      />,
    );
    expect(
      screen.getByRole("heading", { level: 1, name: "Audit" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Every policy decision")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export" })).toBeInTheDocument();
  });

  it("omits the description and actions when not given", () => {
    const { container } = render(<PageHeader title="Bans" />);
    expect(container.querySelector("p")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
