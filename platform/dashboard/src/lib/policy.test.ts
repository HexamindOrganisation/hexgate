import { describe, expect, it } from "vitest";
import {
  buildAgentView,
  effectiveMode,
  isMixinSpec,
  mergedTools,
  MODE_STRENGTH,
  parsePolicy,
  parseRolesFromPolicy,
  worstMode,
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
  it("keeps parsed.tools separate from parsed.roles (no auto-merge)", () => {
    // Pre-fix, parsePolicy merged role tools into `parsed.tools`, which
    // silently upgraded a flat `tools.web_search.mode: allow` to `deny`
    // if any role denied it (reviewer finding #2). Now `parsed.tools`
    // is ONLY the top-level flat block; role tools live under
    // `parsed.roles[<name>]`. Callers who need the union across roles
    // use ``mergedTools(policy)``.
    const p = parsePolicy(INLINE_ROLES_POLICY);
    expect(p).not.toBeNull();
    // INLINE_ROLES_POLICY has no top-level tools: block → empty.
    expect(p!.tools).toEqual({});
  });

  it("mergedTools() exposes the worst-case cross-role view for the graph", () => {
    // The overview graph asks "what's the worst mode any caller could
    // hit?" — mergedTools answers by combining every concrete role's
    // tools with worstMode ordering.
    const p = parsePolicy(INLINE_ROLES_POLICY);
    const merged = mergedTools(p!);
    // web_search lives ONLY in the read_only mixin (filtered out); the
    // dashboard doesn't resolve `inherits:` client-side yet, so
    // web_search doesn't appear in the merged view. Documented
    // limitation — pinned so a future inheritance resolver flips the
    // assertion, not silently changes behavior.
    expect(Object.keys(merged)).toEqual(["refund_order"]);
    // Two roles allow refund_order, one denies → worst-case is deny.
    expect(merged.refund_order?.mode).toBe("deny");
  });

  it("keeps flat top-level tools authoritative — roles can't override", () => {
    // Reviewer finding #2: a user editing the flat tools.web_search.mode
    // to allow must see that take visible effect on the graph, even if
    // some role denies it. The merge preserves flat entries verbatim.
    const p = parsePolicy(`
version: 1
default_policy: { mode: deny }
tools:
  web_search: { mode: allow }
roles:
  strict:
    tools:
      web_search: { mode: deny }
`);
    const merged = mergedTools(p!);
    // Flat entry wins — the strict role's deny does NOT upgrade it.
    expect(merged.web_search?.mode).toBe("allow");
  });

  it("exposes per-role tool maps (including empty-tools roles)", () => {
    // Reviewer finding #1: parsePolicy.roles must agree with
    // parseRolesFromPolicy on which roles are concrete — including
    // roles with an empty tools map (they fall back to default_policy
    // at runtime and are still valid selectable roles).
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

  it("worstMode picks the correct mode across the priority ordering", () => {
    // approval_required beats allow, deny beats both.
    expect(worstMode(["allow", "approval_required"])).toBe("approval_required");
    expect(worstMode(["allow", "deny", "approval_required"])).toBe("deny");
    expect(worstMode(["allow"])).toBe("allow");
    expect(worstMode([])).toBeNull();
  });

  it("MODE_STRENGTH orders modes deny > approval > allow", () => {
    // Pin the source-of-truth ordering so a future refactor that
    // reorders these silently doesn't slip past review.
    expect(MODE_STRENGTH.deny).toBeGreaterThan(MODE_STRENGTH.approval_required);
    expect(MODE_STRENGTH.approval_required).toBeGreaterThan(
      MODE_STRENGTH.allow,
    );
  });
});

describe("buildAgentView — agent.yaml is authoritative for callable tools", () => {
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

  it("does NOT include role-only policy tools as callable (no phantom edges)", () => {
    // Reviewer finding #3: runtime tool-gating comes from the SDK-
    // registered @agent_tool functions (mirrored in agent.yaml), NOT
    // from policy. Unioning role-only tools into AgentView.tools drew
    // phantom capability edges on the graph — an agent that can't
    // actually invoke refund_order still had an edge to it.
    //
    // agent.yaml declares [web_search]; the policy mentions refund_order
    // in multiple roles. The graph must show ONLY web_search — that's
    // what the agent can call at runtime.
    const view = buildAgentView(
      agent({ yaml: AGENT_YAML, policy: INLINE_ROLES_POLICY }),
    );
    expect(view).not.toBeNull();
    expect(view!.tools).toEqual(["web_search"]);
  });

  it("preserves agent.yaml declaration order (no alphabetical sort)", () => {
    // Reviewer finding #10: previously we sorted the tool list,
    // silently changing the contract. Restore source order so any
    // future non-set consumer sees what the author wrote.
    const yaml = `
name: default
model: gpt-5.4
tools:
  - web_search
  - fetch
  - upload
`;
    const view = buildAgentView(agent({ yaml, policy: FLAT_POLICY }));
    expect(view!.tools).toEqual(["web_search", "fetch", "upload"]);
  });

  it("falls back to default_policy for tools not in policy (or in mixin only)", () => {
    // web_search lives only in the read_only mixin (filtered out).
    // Without client-side `inherits:` resolution it falls to
    // default_policy=deny. Documented limitation — pinned so a
    // future inheritance resolver flips the assertion, not silently
    // changes behavior. The wasm bundle at runtime enforces `allow`
    // via inheritance.
    const view = buildAgentView(
      agent({ yaml: AGENT_YAML, policy: INLINE_ROLES_POLICY }),
    );
    expect(effectiveMode(view!, "web_search")).toBe("deny");
    expect(view!.missingInPolicy).toContain("web_search");
  });

  it("effectiveMode returns worst-case across roles for a policy tool", () => {
    // If agent.yaml declared refund_order (so it's callable) and the
    // policy has it in multiple roles with different modes, the
    // effective mode shown on the graph is the worst case.
    const yaml = `
name: support_bot
model: gpt-5.4
tools:
  - refund_order
`;
    const view = buildAgentView(agent({ yaml, policy: INLINE_ROLES_POLICY }));
    expect(effectiveMode(view!, "refund_order")).toBe("deny");
  });

  it("flat policy still reads correctly (no regression)", () => {
    const yaml = `
name: default
model: gpt-5.4
tools:
  - web_search
  - fetch
`;
    const view = buildAgentView(agent({ yaml, policy: FLAT_POLICY }));
    expect(view).not.toBeNull();
    expect(view!.tools).toEqual(["web_search", "fetch"]);
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

describe("isMixinSpec — coerced truthy check", () => {
  it("accepts is_mixin: true", () => {
    expect(isMixinSpec({ is_mixin: true })).toBe(true);
  });

  it("accepts coerced truthy variants (reviewer finding #7)", () => {
    // A hand-edited YAML with `is_mixin: "true"` (quoted) or
    // `is_mixin: 1` used to sneak through the strict `=== true` check
    // and get treated as a concrete role → merged tools double-counted.
    expect(isMixinSpec({ is_mixin: "true" })).toBe(true);
    expect(isMixinSpec({ is_mixin: "yes" })).toBe(true);
    expect(isMixinSpec({ is_mixin: 1 })).toBe(true);
  });

  it("rejects everything else", () => {
    expect(isMixinSpec({ is_mixin: false })).toBe(false);
    expect(isMixinSpec({ is_mixin: "false" })).toBe(false);
    expect(isMixinSpec({ is_mixin: 0 })).toBe(false);
    expect(isMixinSpec({})).toBe(false);
    expect(isMixinSpec(null)).toBe(false);
    expect(isMixinSpec("not-an-object")).toBe(false);
  });
});

// ---- Edge cases — cover the partial branches Codecov flagged --------------

describe("parsePolicy — malformed / missing input", () => {
  it("returns null on yaml that throws during load", () => {
    // Triggers the try/catch. js-yaml only throws on genuinely
    // malformed syntax (e.g. an unclosed tag) — most "garbage"
    // strings still parse to a scalar. Use a colon-only tag that
    // js-yaml rejects.
    expect(parsePolicy("!!!!invalid: [")).toBeNull();
  });

  it("returns null on a yaml document that isn't an object", () => {
    // Guard: `if (!raw || typeof raw !== 'object')` — scalars, empty
    // strings, and yaml `null` must not slip past the type narrowing.
    // (Arrays typeof-as "object" in JS, so they hit the entries()
    // path and produce an empty-but-valid result — that's fine.)
    expect(parsePolicy("42")).toBeNull();
    expect(parsePolicy('"just a string"')).toBeNull();
    expect(parsePolicy("null")).toBeNull();
    expect(parsePolicy("")).toBeNull();
  });

  it("defaults version to 1 when absent", () => {
    const p = parsePolicy(`
default_policy: { mode: allow }
tools:
  ping: { mode: allow }
`);
    expect(p!.version).toBe(1);
  });

  it("defaults default_policy.mode to deny when missing / invalid", () => {
    // Fail-closed: unknown or missing mode string must not silently
    // become allow. Both cases should land on deny.
    const missing = parsePolicy(`
version: 1
tools:
  ping: { mode: allow }
`);
    expect(missing!.default_policy.mode).toBe("deny");

    const bogus = parsePolicy(`
version: 1
default_policy: { mode: whatever }
tools:
  ping: { mode: allow }
`);
    expect(bogus!.default_policy.mode).toBe("deny");
  });
});

describe("parsePolicy — malformed roles", () => {
  it("ignores roles: when it's an array, not a mapping", () => {
    // Array.isArray guard.
    const p = parsePolicy(`
version: 1
default_policy: { mode: deny }
roles:
  - default
  - support
`);
    expect(p!.roles).toEqual({});
    expect(p!.tools).toEqual({});
  });

  it("skips role entries whose spec isn't an object", () => {
    // `if (!spec || typeof spec !== 'object') continue`
    const p = parsePolicy(`
version: 1
default_policy: { mode: deny }
roles:
  default: null
  support: "not an object"
  billing:
    tools:
      x: { mode: allow }
`);
    expect(Object.keys(p!.roles)).toEqual(["billing"]);
  });

  it("keeps empty-tools roles listed (they fall back to default_policy)", () => {
    // Reviewer finding #1: parsePolicy.roles previously dropped roles
    // whose tools map was empty, but parseRolesFromPolicy still listed
    // them in the picker. A future "filter by role" reading
    // `parsed.roles[selectedRole]` got `undefined` for a role the
    // picker just offered → empty graph or crash. Now both parsers
    // agree.
    const p = parsePolicy(`
version: 1
default_policy: { mode: deny }
roles:
  default:
    tools: {}
  support:
    tools:
      x: { mode: allow }
`);
    expect(Object.keys(p!.roles).sort()).toEqual(["default", "support"]);
    expect(p!.roles.default).toEqual({});
  });

  it("skips tool entries with invalid mode strings", () => {
    // readToolMap's isMode guard filters bogus modes before they land.
    const p = parsePolicy(`
version: 1
default_policy: { mode: deny }
tools:
  good: { mode: allow }
  bad: { mode: sometimes }
`);
    expect(Object.keys(p!.tools)).toEqual(["good"]);
  });

  it("preserves file_scope on parsed tools", () => {
    // Passthrough of the nested file_scope field — pin so a future
    // refactor doesn't accidentally drop it.
    const p = parsePolicy(`
version: 1
default_policy: { mode: deny }
tools:
  read_file:
    mode: allow
    file_scope:
      allowed_paths: ["/workspace/**"]
`);
    expect(p!.tools.read_file?.file_scope?.allowed_paths).toEqual([
      "/workspace/**",
    ]);
  });
});

describe("buildAgentView — malformed input", () => {
  const agent = (opts: { yaml: string; policy: string }): AgentRead =>
    ({
      name: "test",
      agent_yaml: opts.yaml,
      policy_yaml: opts.policy,
    }) as unknown as AgentRead;

  it("returns null when the agent yaml is unparseable", () => {
    expect(
      buildAgentView(agent({ yaml: ":::garbage", policy: FLAT_POLICY })),
    ).toBeNull();
  });

  it("returns null when the policy yaml is unparseable", () => {
    expect(
      buildAgentView(agent({ yaml: "name: x", policy: ":::garbage" })),
    ).toBeNull();
  });

  it("falls back to AgentRead.name when agent.yaml has no name", () => {
    // `parsedAgent.name || agent.name` — if the yaml omits the name key,
    // we still label the agent by its DB name.
    const view = buildAgentView(
      agent({ yaml: "model: gpt-5.4\ntools: []", policy: FLAT_POLICY }),
    );
    expect(view!.name).toBe("test");
  });
});
