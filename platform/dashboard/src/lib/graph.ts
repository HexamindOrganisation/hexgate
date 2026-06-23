import type { Edge, Node } from "@xyflow/react";
import type { AgentRead } from "./api";
import {
  buildAgentView,
  effectiveMode,
  MODE_COLOR,
  type AgentView,
  type Mode,
} from "./policy";

/** Column-based layout constants. */
const COL = {
  role: 40,
  agent: 360,
  tool: 720,
} as const;

const ROW_H = 72;

export interface OverviewGraph {
  nodes: Node[];
  edges: Edge[];
  agentViews: AgentView[];
}

/** Build the full project overview: everyone → all agents → all tools. */
export function buildOverviewGraph(agents: AgentRead[]): OverviewGraph {
  const agentViews = agents
    .map(buildAgentView)
    .filter((a): a is AgentView => a !== null);

  const uniqueTools = new Set<string>();
  for (const view of agentViews) {
    for (const tool of view.tools) uniqueTools.add(tool);
  }
  const toolList = Array.from(uniqueTools).sort();

  const nodes: Node[] = [];
  const edges: Edge[] = [];

  // Everyone role at centre y of agents
  const agentCenterY = ((agentViews.length - 1) * ROW_H) / 2;
  nodes.push({
    id: "role:everyone",
    type: "role",
    position: { x: COL.role, y: agentCenterY },
    data: { label: "everyone", muted: true },
    draggable: false,
  });

  agentViews.forEach((view, i) => {
    const agentId = `agent:${view.name}`;
    nodes.push({
      id: agentId,
      type: "agent",
      position: { x: COL.agent, y: i * ROW_H },
      data: {
        name: view.name,
        model: view.model,
        toolCount: view.tools.length,
      },
      draggable: false,
    });

    // everyone → agent
    edges.push({
      id: `e:everyone->${agentId}`,
      source: "role:everyone",
      target: agentId,
      style: {
        stroke: "hsl(var(--muted-foreground))",
        strokeWidth: 1,
        opacity: 0.5,
      },
    });

    // agent → tools
    for (const toolName of view.tools) {
      const mode = effectiveMode(view, toolName);
      edges.push({
        id: `e:${agentId}->tool:${toolName}`,
        source: agentId,
        target: `tool:${toolName}`,
        style: edgeStyle(mode),
        animated: mode === "approval_required",
      });
    }
  });

  const toolCenterY = ((toolList.length - 1) * ROW_H) / 2;
  const shift = agentCenterY - toolCenterY;
  toolList.forEach((toolName, i) => {
    // Determine the "worst" mode for the left strip on the tool node
    const modesForTool: Mode[] = agentViews
      .filter((v) => v.tools.includes(toolName))
      .map((v) => effectiveMode(v, toolName));
    const mode = pickStripMode(modesForTool);
    nodes.push({
      id: `tool:${toolName}`,
      type: "tool",
      position: { x: COL.tool, y: i * ROW_H + shift },
      data: { name: toolName, mode },
      draggable: false,
    });
  });

  return { nodes, edges, agentViews };
}

/** Build a per-agent local graph: the agent + its tools only. */
export function buildAgentGraph(agent: AgentRead): {
  nodes: Node[];
  edges: Edge[];
  view: AgentView | null;
} {
  const view = buildAgentView(agent);
  if (!view) return { nodes: [], edges: [], view: null };

  const nodes: Node[] = [];
  const edges: Edge[] = [];

  const centerY = ((view.tools.length - 1) * ROW_H) / 2;

  nodes.push({
    id: "agent:self",
    type: "agent",
    position: { x: 40, y: centerY },
    data: { name: view.name, model: view.model, toolCount: view.tools.length },
    draggable: false,
  });

  view.tools.forEach((toolName, i) => {
    const mode = effectiveMode(view, toolName);
    nodes.push({
      id: `tool:${toolName}`,
      type: "tool",
      position: { x: 380, y: i * ROW_H },
      data: { name: toolName, mode },
      draggable: false,
    });
    edges.push({
      id: `e:agent->${toolName}`,
      source: "agent:self",
      target: `tool:${toolName}`,
      style: edgeStyle(mode),
      animated: mode === "approval_required",
    });
  });

  return { nodes, edges, view };
}

function edgeStyle(mode: Mode): React.CSSProperties {
  const base = { stroke: MODE_COLOR[mode], strokeWidth: 1.75 };
  if (mode === "approval_required") {
    return { ...base, strokeDasharray: "6 4" };
  }
  if (mode === "deny") {
    return { ...base, strokeWidth: 1.5, opacity: 0.85 };
  }
  return base;
}

function pickStripMode(modes: Mode[]): Mode | "default" {
  if (modes.includes("deny")) return "deny";
  if (modes.includes("approval_required")) return "approval_required";
  if (modes.includes("allow")) return "allow";
  return "default";
}
