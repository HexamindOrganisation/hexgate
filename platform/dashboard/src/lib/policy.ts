import yaml from "js-yaml";
import type { AgentRead } from "./api";

export type Mode = "allow" | "deny" | "approval_required";

export interface ToolPolicy {
  mode: Mode;
  file_scope?: {
    allowed_paths?: string[];
  };
}

export interface ParsedPolicy {
  version: number;
  default_policy: { mode: Mode };
  /** Tools declared at the TOP level of policy.yaml (flat shape). Empty
   *  on inline-roles YAMLs. NOT augmented by role decisions — the user's
   *  edit at this level is authoritative. See `mergedTools` for the
   *  worst-case union across roles. */
  tools: Record<string, ToolPolicy>;
  /** Concrete roles present in the yaml. Empty on flat policy YAML.
   *  Mixins are filtered (composed via `inherits:` into concrete roles
   *  at wasm-compile time). A concrete role with no `tools:` map is
   *  still listed here as `{}` so this map agrees with
   *  parseRolesFromPolicy on which roles the picker offers. */
  roles: Record<string, Record<string, ToolPolicy>>;
}

export interface ParsedAgent {
  name: string;
  model: string;
  tools: string[];
}

export interface AgentView {
  name: string;
  model: string;
  /** Tools the agent can invoke — equals agent.yaml's `tools:` list,
   *  in declared order. Role-only policy entries do NOT create display
   *  tools here; the runtime can't invoke what agent.yaml didn't
   *  declare. */
  tools: string[];
  policy: ParsedPolicy;
  /** Tools that appear in agent.yaml but have no policy entry (neither
   *  at flat top level nor in any role) → default_policy applies. */
  missingInPolicy: string[];
}

function isMode(x: unknown): x is Mode {
  return x === "allow" || x === "deny" || x === "approval_required";
}

/**
 * Return true for role specs that should be treated as MIXINS —
 * composed INTO concrete roles via `inherits:` at compile time, never
 * selectable on their own.
 *
 * Accepts the strict-true bool AND coerced truthy values (`"true"`,
 * `1`) so a hand-written `is_mixin: "true"` doesn't sneak through as
 * a "concrete role" and get double-counted in the merged view.
 * Exported so parsePolicy + parseRolesFromPolicy + any future consumer
 * agree on which roles are concrete.
 */
export function isMixinSpec(spec: unknown): boolean {
  if (!spec || typeof spec !== "object") return false;
  const raw = (spec as { is_mixin?: unknown }).is_mixin;
  if (raw === true) return true;
  if (raw === "true" || raw === "yes" || raw === "on" || raw === 1) return true;
  return false;
}

/**
 * Priority ordering when the graph needs a single mode for a tool
 * called from multiple roles: deny > approval > allow. Exported so
 * graph.ts + policy_graph.ts share one source of truth — a future
 * `redacted` mode landing between deny and approval only has to be
 * added here.
 */
export const MODE_STRENGTH: Record<Mode, number> = {
  deny: 3,
  approval_required: 2,
  allow: 1,
};

/** Return the strongest mode from a list, or ``null`` if empty. */
export function worstMode(modes: readonly Mode[]): Mode | null {
  if (modes.length === 0) return null;
  return modes.reduce((worst, m) =>
    MODE_STRENGTH[m] > MODE_STRENGTH[worst] ? m : worst,
  );
}

/**
 * Read a `{tool_name: {mode, file_scope?}}` map from an arbitrary
 * unknown-typed value. Filters entries whose mode isn't one of the
 * three canonical strings (fail-closed against typos). Exported so
 * policy_graph.ts and any future parser share the same isMode gate.
 */
export function readToolMap(raw: unknown): Record<string, ToolPolicy> {
  if (!raw || typeof raw !== "object") return {};
  const out: Record<string, ToolPolicy> = {};
  for (const [toolName, entry] of Object.entries(
    raw as Record<string, unknown>,
  )) {
    const e = entry as { mode?: unknown; file_scope?: unknown };
    if (!isMode(e?.mode)) continue;
    out[toolName] = {
      mode: e.mode,
      file_scope: e.file_scope as ToolPolicy["file_scope"],
    };
  }
  return out;
}

export function parseAgent(
  agentYaml: string,
  policyYaml: string,
): ParsedAgent | null {
  try {
    const agent = yaml.load(agentYaml) as Partial<ParsedAgent> | null;
    if (!agent || typeof agent !== "object") return null;
    return {
      name: String(agent.name ?? ""),
      model: String(agent.model ?? ""),
      tools: Array.isArray(agent.tools) ? agent.tools.map(String) : [],
    };
  } catch {
    return null;
  }
  void policyYaml;
}

export function parsePolicy(policyYaml: string): ParsedPolicy | null {
  try {
    // `yaml.load` returns `unknown`; the shape checks below narrow it
    // before we dereference. Using `unknown` over `any` so a missed
    // check is a compile error rather than a silent dereference.
    const raw = yaml.load(policyYaml) as
      | Record<string, unknown>
      | null
      | undefined;
    if (!raw || typeof raw !== "object") return null;
    const defaultPolicy = raw.default_policy as { mode?: unknown } | undefined;
    const defaultMode = isMode(defaultPolicy?.mode)
      ? defaultPolicy.mode
      : "deny";

    // Two shapes coexist: (a) flat, with tools at top level; (b)
    // inline-roles, with concrete roles under `roles.<name>.tools`.
    // We parse BOTH but keep them separate — the flat block is
    // authoritative when the user writes at that level, roles carry
    // the per-role variations. Downstream callers pick which they
    // want via `tools` (flat) vs `roles[name]` vs `mergedTools(policy)`.
    const flatTools = readToolMap(raw.tools);
    const rolesRaw = raw.roles;
    const roles: Record<string, Record<string, ToolPolicy>> = {};
    if (rolesRaw && typeof rolesRaw === "object" && !Array.isArray(rolesRaw)) {
      for (const [roleName, spec] of Object.entries(
        rolesRaw as Record<string, unknown>,
      )) {
        if (!spec || typeof spec !== "object") continue;
        // Mixins compose INTO concrete roles at wasm-compile time; they
        // aren't selectable roles and mustn't count toward the graph.
        if (isMixinSpec(spec)) continue;
        // Empty-tools roles ARE valid (they fall back to default_policy
        // at runtime). Keep them as `{}` here so parsePolicy.roles
        // agrees with parseRolesFromPolicy on which roles exist.
        roles[roleName] = readToolMap((spec as { tools?: unknown }).tools);
      }
    }

    return {
      version: Number(raw.version ?? 1),
      default_policy: { mode: defaultMode },
      tools: flatTools,
      roles,
    };
  } catch {
    return null;
  }
}

/**
 * Worst-case merged view of tool decisions across every concrete role
 * PLUS the flat top-level `tools:` block. Used by the overview graph
 * when it needs one edge color per tool without picking a role first.
 *
 * Precedence rules:
 *   1. If the flat top-level `tools:` block declares a tool, that
 *      entry wins — the user's explicit top-level edit is
 *      authoritative and roles can't silently upgrade a flat `allow`
 *      to `deny`.
 *   2. Otherwise, the strongest per-role mode wins (`deny` >
 *      `approval_required` > `allow`). Later roles beat earlier ones
 *      on equal strength — keeps whichever declaration carries a
 *      more-specific `file_scope`.
 *
 * Every consumer that wants "the color for this tool on the graph"
 * routes through this helper; the raw `policy.tools` / `policy.roles`
 * fields stay pure representations of the yaml.
 */
export function mergedTools(policy: ParsedPolicy): Record<string, ToolPolicy> {
  const merged: Record<string, ToolPolicy> = { ...policy.tools };
  for (const roleTools of Object.values(policy.roles)) {
    for (const [toolName, roleEntry] of Object.entries(roleTools)) {
      // Flat entry wins — never override an explicit top-level rule.
      if (toolName in policy.tools) continue;
      const current = merged[toolName];
      if (current === undefined) {
        merged[toolName] = roleEntry;
        continue;
      }
      // Later role wins on equal strength (>=) so the last-declared
      // file_scope survives; strictly-stronger always upgrades.
      if (MODE_STRENGTH[roleEntry.mode] >= MODE_STRENGTH[current.mode]) {
        merged[toolName] = roleEntry;
      }
    }
  }
  return merged;
}

export function buildAgentView(agent: AgentRead): AgentView | null {
  const parsedAgent = parseAgent(agent.agent_yaml, agent.policy_yaml);
  const parsedPolicy = parsePolicy(agent.policy_yaml);
  if (!parsedAgent || !parsedPolicy) return null;
  // Display tool list = agent.yaml declaration ONLY. Role-only tool
  // entries in policy don't create callable tools — the runtime
  // registers tools from agent.tools (the SDK-decorated functions);
  // policy just gates them. Including role-only tools here would draw
  // phantom capability edges on the graph.
  const tools = [...parsedAgent.tools];
  const merged = mergedTools(parsedPolicy);
  const missingInPolicy = tools.filter((t) => !(t in merged));
  return {
    name: parsedAgent.name || agent.name,
    model: parsedAgent.model,
    tools,
    policy: parsedPolicy,
    missingInPolicy,
  };
}

export function effectiveMode(view: AgentView, toolName: string): Mode {
  const entry = mergedTools(view.policy)[toolName];
  return entry?.mode ?? view.policy.default_policy.mode;
}

export const MODE_COLOR: Record<Mode, string> = {
  allow: "hsl(var(--semantic-allow))",
  deny: "hsl(var(--semantic-deny))",
  approval_required: "hsl(var(--semantic-approval))",
};

/**
 * Extract the selectable role names from an inline-roles ``policy.yaml``.
 *
 * The wire format is one canonical document per agent. When the document
 * declares a top-level ``roles:`` map, this function returns the names of
 * the concrete roles (mixin entries filtered out via ``isMixinSpec``,
 * ``default`` first). When the document is a flat single-policy YAML,
 * returns an empty list — the caller (the Playground role picker) treats
 * that as "this agent has no per-role differentiation, run it as-is."
 *
 * Pure parse — no validation of the policy's correctness; that's the
 * server's /validate endpoint.
 */
export function parseRolesFromPolicy(policyYaml: string): string[] {
  let parsed: unknown;
  try {
    parsed = yaml.load(policyYaml);
  } catch {
    return [];
  }
  if (!parsed || typeof parsed !== "object") return [];
  const roles = (parsed as { roles?: unknown }).roles;
  if (!roles || typeof roles !== "object" || Array.isArray(roles)) return [];

  const concrete: string[] = [];
  for (const [name, spec] of Object.entries(roles as Record<string, unknown>)) {
    if (isMixinSpec(spec)) continue;
    concrete.push(name);
  }
  concrete.sort();
  if (concrete.includes("default")) {
    return ["default", ...concrete.filter((r) => r !== "default")];
  }
  return concrete;
}
