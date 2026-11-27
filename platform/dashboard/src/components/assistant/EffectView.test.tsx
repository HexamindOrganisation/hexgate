/**
 * EffectView tests: one row per grant change with its constraints, the agent
 * and gate changes, and an effect stored without some of its lists.
 */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import type { PolicyDiff } from "@/lib/api";
import { EffectView } from "./EffectView";

const EFFECT: PolicyDiff = {
  changes: [
    {
      agent: "support",
      role: "member",
      tool: "refund_order",
      before: { mode: "allow", constraints: ["amount <= 1000"] },
      after: { mode: "approval_required", constraints: ["amount <= 500"] },
      constraints_added: ["amount <= 500"],
      constraints_removed: ["amount <= 1000"],
    },
    {
      agent: "support",
      role: "guest",
      tool: "delete_user",
      before: { mode: "allow" },
      after: null,
    },
  ],
  agent_changes: [
    {
      agent: "support",
      key: "usage",
      before: { max_calls: 10 },
      after: { max_calls: 5 },
    },
  ],
  gate_changes: [
    { agent: "support", gate: "reach", before: false, after: true },
  ],
  agents_affected: ["support"],
  composes: { before: true, after: true },
};

describe("EffectView", () => {
  it("shows a row per grant change, before to after, with constraints", () => {
    render(<EffectView effect={EFFECT} />);
    const rows = screen.getByLabelText("Grant changes").querySelectorAll("li");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("support · member · refund_order");
    expect(rows[0]).toHaveTextContent("allowed");
    expect(rows[0]).toHaveTextContent("needs approval");
    expect(rows[0]).toHaveTextContent("+ amount <= 500");
    expect(rows[0]).toHaveTextContent("− amount <= 1000");
    expect(rows[1]).toHaveTextContent("allowedno access");
  });

  it("shows the agent changes and the gates switched", () => {
    render(<EffectView effect={EFFECT} />);
    const agent = screen.getByLabelText("Agent changes");
    expect(agent).toHaveTextContent("support · usage");
    expect(agent).toHaveTextContent('{"max_calls":10}');
    expect(agent).toHaveTextContent('{"max_calls":5}');
    expect(screen.getByLabelText("Gate changes")).toHaveTextContent(
      "support · reach switched on: now restricted to the roles that list it",
    );
    expect(screen.getByLabelText("Agent changes")).not.toHaveTextContent(
      "null",
    );
  });

  it("says when only the constraints changed, and when a gate opens up", () => {
    render(
      <EffectView
        effect={{
          composes: { before: true, after: true },
          changes: [
            {
              agent: "a",
              role: "r",
              tool: "t",
              before: { mode: "allow", constraints: ["x <= 1000"] },
              after: { mode: "allow", constraints: ["x <= 500"] },
              constraints_added: ["x <= 500"],
              constraints_removed: ["x <= 1000"],
            },
          ],
          agent_changes: [
            { agent: "a", key: "usage", before: null, after: { max: 5 } },
          ],
          gate_changes: [
            { agent: "a", gate: "admission", before: true, after: false },
          ],
        }}
      />,
    );
    const row = screen.getByLabelText("Grant changes");
    expect(row).toHaveTextContent("allowed, constraints changed");
    expect(row.querySelector("[aria-label=becomes]")).toBeNull();
    expect(screen.getByLabelText("Gate changes")).toHaveTextContent(
      "now open to every caller",
    );
    expect(screen.getByLabelText("Agent changes")).not.toHaveTextContent("−");
  });

  it.each([
    ["admission", true, "now restricted to the roles that list it"],
    ["admission", false, "now open to every caller"],
    ["reach", true, "now restricted to the roles that list it"],
    ["reach", false, "now open to every caller"],
    ["tool_reach", true, "now governed by the roles that list it"],
    ["tool_reach", false, "falls back to the tool's own rule"],
    ["skills", true, "now governed by the roles that list it"],
    ["skills", false, "falls back to the tool's own rule"],
  ])("says what the %s gate does when switched to %s", (gate, on, text) => {
    render(
      <EffectView
        effect={{
          composes: { before: true, after: true },
          gate_changes: [{ agent: "a", gate, before: !on, after: on }],
        }}
      />,
    );
    const row = screen.getByLabelText("Gate changes");
    expect(row).toHaveTextContent(
      `a · ${gate} switched ${on ? "on" : "off"}: ${text}`,
    );
    // Only the gates that open to every caller are flagged when they go off.
    const flagged = row.querySelector(".text-deny") !== null;
    expect(flagged).toBe(!on && (gate === "admission" || gate === "reach"));
  });

  it("shows a gate this build does not know without a claim", () => {
    render(
      <EffectView
        effect={{
          composes: { before: true, after: true },
          gate_changes: [
            { agent: "a", gate: "novel", before: true, after: false },
          ],
        }}
      />,
    );
    expect(screen.getByLabelText("Gate changes")).toHaveTextContent(
      /^a · novel switched off$/,
    );
  });

  it("shows an unknown mode as it came", () => {
    render(
      <EffectView
        effect={{
          changes: [
            {
              agent: "a",
              role: "r",
              tool: "t",
              before: null,
              after: { mode: "quarantine" },
            },
          ],
        }}
      />,
    );
    expect(screen.getByText("quarantine")).toBeInTheDocument();
  });

  it("tolerates an effect stored without its lists, or without an effect", () => {
    const { rerender } = render(<EffectView effect={{}} />);
    expect(screen.getByText(/No agent gains or loses/)).toBeInTheDocument();
    rerender(<EffectView effect={null} />);
    expect(screen.getByText(/No agent gains or loses/)).toBeInTheDocument();
  });

  it("says when the policy does not compose on a side", () => {
    const { rerender } = render(
      <EffectView effect={{ composes: { before: true, after: false } }} />,
    );
    expect(screen.getByText(/does not compose after/)).toBeInTheDocument();
    expect(screen.queryByText(/No agent gains or loses/)).toBeNull();
    rerender(
      <EffectView effect={{ composes: { before: false, after: true } }} />,
    );
    expect(screen.getByText(/did not compose before/)).toBeInTheDocument();
    rerender(
      <EffectView effect={{ composes: { before: false, after: false } }} />,
    );
    expect(screen.getByText(/neither before nor after/)).toBeInTheDocument();
  });
});
