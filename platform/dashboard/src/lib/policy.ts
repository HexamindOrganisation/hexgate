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
  tools: Record<string, ToolPolicy>;
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
    const rawTools = (raw.tools ?? {}) as Record<string, unknown>;
    const tools: Record<string, ToolPolicy> = {};
    for (const [toolName, entry] of Object.entries(rawTools)) {
      const e = entry as { mode?: unknown; file_scope?: unknown };
      if (!isMode(e?.mode)) continue;
      tools[toolName] = {
        mode: e.mode,
        file_scope: e.file_scope as ToolPolicy["file_scope"],
      };
    }
    return {
      version: Number(raw.version ?? 1),
      default_policy: { mode: defaultMode },
      tools,
    };
  } catch {
    return null;
  }
}

export function buildAgentView(agent: AgentRead): AgentView | null {
  const parsedAgent = parseAgent(agent.agent_yaml, agent.policy_yaml);
  const parsedPolicy = parsePolicy(agent.policy_yaml);
  if (!parsedAgent || !parsedPolicy) return null;
  const missingInPolicy = parsedAgent.tools.filter(
    (t) => !(t in parsedPolicy.tools),
  );
  return {
    name: parsedAgent.name || agent.name,
    model: parsedAgent.model,
    tools: parsedAgent.tools,
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
