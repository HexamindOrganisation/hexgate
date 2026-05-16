/**
 * Build a React Flow graph from an inline-roles ``policy.yaml``.
 *
 * Visualization shape (node-edge):
 *
 *     roles (left)              tools (right)
 *     ───────────                ────────────
 *
 *     read_only [mixin]  ─ ─ ┐
 *                            │ inherits
 *     default ◄──────────────┤
 *                            │
 *     support  ──── allow ───┼──> web_search
 *                            │
 *                ──── allow ─┼──> refund_order  (≤50 USD)
 *                            │
 *     billing  ──── allow ───┼──> refund_order  (≤500 USD/EUR)
 *
 * Edges:
 *   * role → tool, color encodes mode (allow / approval_required / deny)
 *   * role → role, dashed, labeled "inherits" (mixin role on the right
 *     of the inheritance arrow)
 *
 * Inheritance is rendered as edges (the "innovative" node-edge view the
 * user picked over a flat matrix). Mixin roles are dimmed via the
 * RoleNode's ``muted`` flag.
 *
 * This module is intentionally a pure builder — no React, no state. The
 * Graph tab in /policies passes the output to <ReactFlow> verbatim.
 */

import yaml from 'js-yaml'
import type { Edge, Node } from '@xyflow/react'

export type Mode = 'allow' | 'deny' | 'approval_required'

interface ToolPolicySpec {
  mode?: Mode
  constraints?: string[]
}

interface RoleSpec {
  is_mixin?: boolean
  inherits?: string[]
  tools?: Record<string, ToolPolicySpec>
  default_policy?: { mode?: Mode }
}

interface InlinePolicy {
  version?: number
  roles?: Record<string, RoleSpec>
}

const MODE_COLOR: Record<Mode, string> = {
  allow: 'hsl(var(--semantic-allow))',
  approval_required: 'hsl(var(--semantic-approval))',
  deny: 'hsl(var(--semantic-deny))',
}

export interface PolicyGraph {
  nodes: Node[]
  edges: Edge[]
  /** True if the YAML parsed and has at least one role; false → tab should render an empty/invalid placeholder. */
  ok: boolean
}

/**
 * Parse and lay out the policy graph. Returns ok=false when the YAML is
 * malformed or contains no ``roles:`` section — caller renders a friendly
 * "fix the YAML to see the graph" message.
 */
export function buildPolicyGraph(policyYaml: string): PolicyGraph {
  let parsed: unknown
  try {
    parsed = yaml.load(policyYaml)
  } catch {
    return { nodes: [], edges: [], ok: false }
  }
  if (!parsed || typeof parsed !== 'object') {
    return { nodes: [], edges: [], ok: false }
  }
  const doc = parsed as InlinePolicy
  const rolesMap = doc.roles
  if (!rolesMap || typeof rolesMap !== 'object') {
    return { nodes: [], edges: [], ok: false }
  }

  const roleNames = Object.keys(rolesMap)
  // Tools = union across all roles, source order (first occurrence wins).
  const toolSet = new Set<string>()
  for (const role of roleNames) {
    const tools = rolesMap[role]?.tools ?? {}
    for (const t of Object.keys(tools)) toolSet.add(t)
  }
  const toolNames = Array.from(toolSet)

  // Layout constants: two columns, role boxes vertically stacked on the left,
  // tool nodes on the right. Vertical spacing tuned so the columns visually
  // balance for typical policies (≤8 roles, ≤8 tools).
  const COL_ROLES_X = 0
  const COL_TOOLS_X = 480
  const ROLE_GAP_Y = 110
  const TOOL_GAP_Y = 80
  const ROLE_Y_START = 0
  const TOOL_Y_START =
    Math.max(0, (roleNames.length * ROLE_GAP_Y - toolNames.length * TOOL_GAP_Y) / 2)

  const nodes: Node[] = []
  const edges: Edge[] = []

  // Role nodes — mixin entries rendered with the muted flag so they read
  // as inheritance helpers rather than active personas.
  roleNames.forEach((role, idx) => {
    const spec = rolesMap[role]
    nodes.push({
      id: `role:${role}`,
      type: 'role',
      position: { x: COL_ROLES_X, y: ROLE_Y_START + idx * ROLE_GAP_Y },
      data: {
        label: role,
        muted: spec?.is_mixin === true,
      },
    })
  })

  // Tool nodes — mode defaults to 'default' (renders as the muted strip);
  // the tool's mode varies per role, so this represents "the tool exists"
  // not "what mode it's in for any specific role."
  toolNames.forEach((tool, idx) => {
    nodes.push({
      id: `tool:${tool}`,
      type: 'tool',
      position: { x: COL_TOOLS_X, y: TOOL_Y_START + idx * TOOL_GAP_Y },
      data: { name: tool, mode: 'default' },
    })
  })

  // Inheritance edges — role → parent role, dashed, secondary color.
  for (const role of roleNames) {
    const parents = rolesMap[role]?.inherits ?? []
    for (const parent of parents) {
      if (!rolesMap[parent]) continue
      edges.push({
        id: `inh:${role}->${parent}`,
        source: `role:${role}`,
        target: `role:${parent}`,
        type: 'smoothstep',
        animated: false,
        style: {
          stroke: 'hsl(var(--muted-foreground))',
          strokeDasharray: '4 4',
          strokeWidth: 1.5,
        },
        label: 'inherits',
        labelStyle: {
          fontSize: 10,
          fill: 'hsl(var(--muted-foreground))',
        },
        labelBgStyle: { fill: 'hsl(var(--background))' },
        labelBgPadding: [4, 2],
      })
    }
  }

  // Mode edges — role → tool, color encodes the policy mode. Constraint
  // count surfaces in the label so the user knows the rule has gates
  // without opening the YAML.
  for (const role of roleNames) {
    const spec = rolesMap[role]
    if (spec?.is_mixin) continue // mixins don't terminate; their tools surface via children
    const tools = spec?.tools ?? {}
    for (const [tool, toolSpec] of Object.entries(tools)) {
      const mode = (toolSpec?.mode ?? 'deny') as Mode
      const constraintCount = toolSpec?.constraints?.length ?? 0
      edges.push({
        id: `mode:${role}->${tool}`,
        source: `role:${role}`,
        target: `tool:${tool}`,
        type: 'smoothstep',
        animated: mode === 'allow',
        style: {
          stroke: MODE_COLOR[mode],
          strokeWidth: 2,
        },
        label:
          constraintCount > 0
            ? `${mode} · ${constraintCount} check${constraintCount === 1 ? '' : 's'}`
            : mode,
        labelStyle: {
          fontSize: 10,
          fill: 'hsl(var(--foreground))',
        },
        labelBgStyle: { fill: 'hsl(var(--background))' },
        labelBgPadding: [4, 2],
      })
    }
  }

  return { nodes, edges, ok: true }
}
