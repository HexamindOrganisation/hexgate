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
  /** Merged view of every concrete role's tools + top-level tools —
   * used by the graph/overview. When a tool appears in multiple roles
   * with different modes, we keep the strongest (deny > approval > allow)
   * so the graph shows the worst-case decision a caller could hit. */
  tools: Record<string, ToolPolicy>;
  /** Per-role tool maps, present when the YAML is inline-roles shape.
   * Empty on flat policy YAML. Callers that need per-role detail (e.g.
   * a future Graph filter "show me policy for role X") read this
   * instead of the merged `tools` above. */
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
  tools: string[];
  policy: ParsedPolicy;
  /** Tools that appear in agent.yaml but have no entry in policy.yaml → default_policy applies */
  missingInPolicy: string[];
}

function isMode(x: unknown): x is Mode {
  return x === "allow" || x === "deny" || x === "approval_required";
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

/** Priority order for merging per-role decisions on the same tool — the
 * graph shows the worst-case outcome any caller could hit, so `deny`
 * wins over `approval_required` wins over `allow`. */
const MODE_STRENGTH: Record<Mode, number> = {
  deny: 3,
  approval_required: 2,
  allow: 1,
};

function readToolMap(raw: unknown): Record<string, ToolPolicy> {
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

    // Two shapes coexist in the platform: (a) flat, with tools at top
    // level (older seed + hand-written examples), and (b) inline-roles,
    // with concrete roles under `roles.<name>.tools` (every seeded
    // multi-role agent). The graph reads a MERGED tool map so it
    // renders both shapes the same way.
    const flatTools = readToolMap(raw.tools);
    const rolesRaw = raw.roles;
    const roles: Record<string, Record<string, ToolPolicy>> = {};
    if (rolesRaw && typeof rolesRaw === "object" && !Array.isArray(rolesRaw)) {
      for (const [roleName, spec] of Object.entries(
        rolesRaw as Record<string, unknown>,
      )) {
        if (!spec || typeof spec !== "object") continue;
        // Skip mixins — they compose INTO concrete roles via `inherits`;
        // treating them as first-class would double-count decisions.
        if ((spec as { is_mixin?: unknown }).is_mixin === true) continue;
        const rt = readToolMap((spec as { tools?: unknown }).tools);
        if (Object.keys(rt).length > 0) roles[roleName] = rt;
      }
    }

    // Merge: start with flat tools, then union in each role's tools.
    // For a tool appearing in >1 role with different modes, keep the
    // strongest (deny > approval > allow) — the graph is a "what's the
    // worst that could happen?" summary, not a role-scoped view. A
    // future filter can render one role by reading `roles[<name>]`.
    const tools: Record<string, ToolPolicy> = { ...flatTools };
    for (const roleTools of Object.values(roles)) {
      for (const [toolName, policy] of Object.entries(roleTools)) {
        const current = tools[toolName];
        if (
          current === undefined ||
          MODE_STRENGTH[policy.mode] > MODE_STRENGTH[current.mode]
        ) {
          tools[toolName] = policy;
        }
      }
    }
    return {
      version: Number(raw.version ?? 1),
      default_policy: { mode: defaultMode },
      tools,
      roles,
    };
  } catch {
    return null;
  }
}

export function buildAgentView(agent: AgentRead): AgentView | null {
  const parsedAgent = parseAgent(agent.agent_yaml, agent.policy_yaml);
  const parsedPolicy = parsePolicy(agent.policy_yaml);
  if (!parsedAgent || !parsedPolicy) return null;
  // Displayable tool list = union of what agent.yaml declares AND every
  // tool that appears in any role's policy. Otherwise a role-only rule
  // (`billing.refund_order`) would never render on the graph, even
  // though it clearly maps to a callable tool at runtime.
  const toolSet = new Set<string>(parsedAgent.tools);
  for (const name of Object.keys(parsedPolicy.tools)) toolSet.add(name);
  const tools = Array.from(toolSet).sort();
  const missingInPolicy = tools.filter((t) => !(t in parsedPolicy.tools));
  return {
    name: parsedAgent.name || agent.name,
    model: parsedAgent.model,
    tools,
    policy: parsedPolicy,
    missingInPolicy,
  };
}

export function effectiveMode(view: AgentView, toolName: string): Mode {
  return view.policy.tools[toolName]?.mode ?? view.policy.default_policy.mode;
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
 * the concrete roles (mixin entries filtered out, ``default`` first). When
 * the document is a flat single-policy YAML, returns an empty list — the
 * caller (the Playground role picker) treats that as "this agent has no
 * per-role differentiation, run it as-is."
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
    if (
      spec &&
      typeof spec === "object" &&
      (spec as { is_mixin?: unknown }).is_mixin === true
    ) {
      continue;
    }
    concrete.push(name);
  }
  concrete.sort();
  if (concrete.includes("default")) {
    return ["default", ...concrete.filter((r) => r !== "default")];
  }
  return concrete;
}
