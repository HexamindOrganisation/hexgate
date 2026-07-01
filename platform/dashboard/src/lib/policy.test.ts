import { describe, expect, it } from "vitest";
import {
  buildAgentView,
  effectiveMode,
  parsePolicy,
  parseRolesFromPolicy,
} from "./policy";
import type { AgentRead } from "./api";

/**
 * Regression tests for the two policy YAML shapes the platform seeds
 * and hand-writers use. Before the fix, ``parsePolicy`` only read
 * top-level ``tools:`` — every inline-roles agent (support_bot, the
 * seeded template) collapsed to ``{tools: {}}`` so the Graph page
 * rendered every edge under ``default_policy.mode`` (usually deny →
 * all red or all missing).
 */

// Flat: tools declared at top level. Used by the "default" and
// "read_only" seeds + hand-written examples.
const FLAT_POLICY = `
version: 1
default_policy:
  mode: deny
tools:
  web_search:
    mode: allow
  fetch:
    mode: allow
`;

// Inline-roles: tools live under each concrete role. Used by every
// seeded multi-role agent (support_bot, etc.). Mixins (is_mixin: true)
// compose via `inherits` and are NOT selectable roles.
const INLINE_ROLES_POLICY = `
version: 1
default_policy:
  mode: deny
roles:
  read_only:
    is_mixin: true
    tools:
      web_search: { mode: allow }
  default:
    inherits: [read_only]
    tools:
      refund_order: { mode: deny }
  support:
    inherits: [read_only]
    tools:
      refund_order: { mode: allow }
  billing:
    inherits: [read_only]
    tools:
      refund_order: { mode: allow }
`;

describe("parsePolicy — flat shape", () => {
  it("reads top-level tools", () => {
    const p = parsePolicy(FLAT_POLICY);
    expect(p).not.toBeNull();
    expect(p!.tools.web_search?.mode).toBe("allow");
    expect(p!.tools.fetch?.mode).toBe("allow");
    expect(p!.default_policy.mode).toBe("deny");
    expect(p!.roles).toEqual({});
  });
});

describe("parsePolicy — inline-roles shape", () => {
  it("merges tools from every concrete role into the top-level view", () => {
    // Before the fix, `refund_order` would be MISSING from `p.tools`
    // entirely (parsePolicy only read top-level `tools:`), and the
    // graph would render its edge as default_policy=deny.
    //
    // `web_search` only lives in the `read_only` mixin — the current
    // resolver skips mixins to avoid double-counting via `inherits`.
    // If a future PR resolves inheritance client-side, this test will
    // legitimately need updating; for now the assertion pins the
    // "concrete roles only" contract.
    const p = parsePolicy(INLINE_ROLES_POLICY);
    expect(p).not.toBeNull();
    expect(Object.keys(p!.tools)).toEqual(["refund_order"]);
  });

  it("picks the worst-case mode when a tool differs across roles", () => {
    // Two roles allow refund_order, one denies — the graph is a
    // "worst case" summary, so deny wins.
    const p = parsePolicy(INLINE_ROLES_POLICY);
    expect(p!.tools.refund_order?.mode).toBe("deny");
  });

  it("exposes per-role tool maps for future role-scoped views", () => {
    // The Graph today shows a merged view, but the shape is right for
    // a future "filter by role" affordance.
    const p = parsePolicy(INLINE_ROLES_POLICY);
    expect(Object.keys(p!.roles).sort()).toEqual([
      "billing",
      "default",
      "support",
    ]);
    // Mixin excluded — is_mixin: true roles compose INTO concrete ones
    // via `inherits`; treating them first-class would double-count.
    expect(p!.roles.read_only).toBeUndefined();
    expect(p!.roles.support?.refund_order.mode).toBe("allow");
    expect(p!.roles.default?.refund_order.mode).toBe("deny");
  });

  it("preserves parseRolesFromPolicy's mixin filter (unchanged)", () => {
    const roles = parseRolesFromPolicy(INLINE_ROLES_POLICY);
    // `default` first (idiomatic), no mixins.
    expect(roles).toEqual(["default", "billing", "support"]);
  });

  it("returns priority order for merge deterministically", () => {
    const p = parsePolicy(`
version: 1
default_policy: { mode: deny }
roles:
  a:
    tools:
      x: { mode: allow }
  b:
    tools:
      x: { mode: approval_required }
`);
    // approval_required beats allow in the worst-case merge.
    expect(p!.tools.x?.mode).toBe("approval_required");
  });
});

describe("buildAgentView — tool-list union", () => {
  const agent = (opts: { yaml: string; policy: string }): AgentRead =>
    ({
      // Fields the parser touches; the rest of AgentRead is irrelevant here.
      name: "test",
      agent_yaml: opts.yaml,
      policy_yaml: opts.policy,
    }) as unknown as AgentRead;

  const AGENT_YAML = `
name: support_bot
model: gpt-5.4
tools:
  - web_search
`;

  it("includes tools declared only in a role, not in agent.yaml", () => {
    // agent.yaml lists web_search; policy adds refund_order via roles.
    // The graph must render BOTH — otherwise a role-only rule is
    // invisible even though it's callable at runtime.
    const view = buildAgentView(
      agent({ yaml: AGENT_YAML, policy: INLINE_ROLES_POLICY }),
    );
    expect(view).not.toBeNull();
    expect(view!.tools).toEqual(["refund_order", "web_search"]);
  });

  it("effectiveMode returns worst-case for tools in multiple roles", () => {
    const view = buildAgentView(
      agent({ yaml: AGENT_YAML, policy: INLINE_ROLES_POLICY }),
    );
    // Two roles allow refund_order, one denies — merged view shows
    // deny so the graph colors that edge red instead of the misleading
    // green a "pick any role" heuristic would produce.
    expect(effectiveMode(view!, "refund_order")).toBe("deny");
  });

  it("falls back to default_policy for mixin-only tools", () => {
    // web_search only lives in the `read_only` mixin, which is filtered
    // out of parsePolicy's roles map. Without inheritance resolution
    // it falls to default_policy=deny. This documents the current
    // limitation — a future inheritance-aware resolver would flip this
    // to `allow`, matching what the wasm bundle actually enforces.
    const view = buildAgentView(
      agent({ yaml: AGENT_YAML, policy: INLINE_ROLES_POLICY }),
    );
    expect(effectiveMode(view!, "web_search")).toBe("deny");
    expect(view!.missingInPolicy).toContain("web_search");
  });

  it("flat policy still reads exactly as before (no regression)", () => {
    const yaml = `
name: default
model: gpt-5.4
tools:
  - web_search
  - fetch
`;
    const view = buildAgentView(agent({ yaml, policy: FLAT_POLICY }));
    expect(view).not.toBeNull();
    expect(view!.tools).toEqual(["fetch", "web_search"]);
    expect(effectiveMode(view!, "web_search")).toBe("allow");
    expect(effectiveMode(view!, "fetch")).toBe("allow");
  });

  it("marks tools from agent.yaml with no policy entry as missingInPolicy", () => {
    const yaml = `
name: default
model: gpt-5.4
tools:
  - web_search
  - unpoliced_tool
`;
    const view = buildAgentView(agent({ yaml, policy: FLAT_POLICY }));
    expect(view!.missingInPolicy).toEqual(["unpoliced_tool"]);
    // Falls back to default_policy.
    expect(effectiveMode(view!, "unpoliced_tool")).toBe("deny");
  });
});
