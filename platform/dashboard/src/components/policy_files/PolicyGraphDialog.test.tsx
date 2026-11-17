/**
 * PolicyGraphDialog tests. `@xyflow/react` needs a real layout engine
 * (ResizeObserver, measured DOM) that jsdom lacks, so it's replaced with a
 * light stub that still renders each node via `nodeTypes` and each edge via
 * `edgeTypes` and forwards the click/hover handlers. That keeps the component's
 * OWN logic under test: layered layout, node/edge construction, the verdict /
 * constraint / legend panels, role filtering, the constraint-bypass warning,
 * and the node-click transitive trace.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ApiError, type PolicyGraph } from "@/lib/api";
import {
  PolicyFlow,
  PolicyGraphDialog,
  type AgentGraphOptions,
} from "./PolicyGraphDialog";
import { renderWithProviders } from "@/test/render";

// --- @xyflow/react stub -----------------------------------------------------
// A minimal ReactFlow that renders the caller's node/edge components (so their
// code executes and counts) and wires the interaction callbacks the component
// relies on. Everything else is a no-op passthrough.
vi.mock("@xyflow/react", () => {
  type AnyProps = Record<string, unknown>;
  const ReactFlow = ({
    nodes,
    edges,
    nodeTypes,
    edgeTypes,
    children,
    onNodeClick,
    onEdgeClick,
    onPaneClick,
    onNodeMouseEnter,
    onNodeMouseLeave,
    onEdgeMouseEnter,
    onEdgeMouseLeave,
  }: AnyProps) => {
    const nodeList = nodes as Array<AnyProps>;
    const edgeList = edges as Array<AnyProps>;
    const nt = nodeTypes as Record<string, React.ComponentType<AnyProps>>;
    const et = edgeTypes as Record<string, React.ComponentType<AnyProps>>;
    return (
      <div data-testid="reactflow">
        {/* A dedicated pane element carries onPaneClick — clicking a node,
            edge, or panel must NOT count as a pane click. */}
        <div
          data-testid="pane"
          onClick={() => (onPaneClick as (() => void) | undefined)?.()}
        />
        {nodeList.map((n) => {
          const Cmp = nt[n.type as string];
          return (
            <div
              key={n.id as string}
              data-testid={`node-${n.id}`}
              onClick={(e) => {
                e.stopPropagation();
                (onNodeClick as (ev: unknown, node: unknown) => void)?.(e, n);
              }}
              onMouseEnter={(e) =>
                (onNodeMouseEnter as (ev: unknown, node: unknown) => void)?.(
                  e,
                  n,
                )
              }
              onMouseLeave={(e) =>
                (onNodeMouseLeave as (ev: unknown) => void)?.(e)
              }
            >
              <Cmp id={n.id} data={n.data} />
            </div>
          );
        })}
        <svg>
          {edgeList.map((ed) => {
            const Cmp = et[ed.type as string];
            return (
              <g
                key={ed.id as string}
                data-testid={`edge-${ed.id}`}
                onClick={(e) => {
                  e.stopPropagation();
                  (onEdgeClick as (ev: unknown, edge: unknown) => void)?.(
                    e,
                    ed,
                  );
                }}
                onMouseEnter={(e) =>
                  (onEdgeMouseEnter as (ev: unknown, edge: unknown) => void)?.(
                    e,
                    ed,
                  )
                }
                onMouseLeave={(e) =>
                  (onEdgeMouseLeave as (ev: unknown) => void)?.(e)
                }
              >
                <Cmp
                  id={ed.id}
                  source={ed.source}
                  target={ed.target}
                  sourceX={0}
                  sourceY={0}
                  targetX={120}
                  targetY={90}
                  data={ed.data}
                  selected={false}
                  markerEnd={ed.markerEnd}
                />
              </g>
            );
          })}
        </svg>
        {children as React.ReactNode}
      </div>
    );
  };
  return {
    ReactFlow,
    Background: () => <div data-testid="bg" />,
    Controls: () => <div data-testid="controls" />,
    Panel: ({ children }: AnyProps) => <div>{children as React.ReactNode}</div>,
    ViewportPortal: ({ children }: AnyProps) => (
      <div data-testid="viewport">{children as React.ReactNode}</div>
    ),
    Handle: () => <div data-testid="handle" />,
    BaseEdge: ({ path }: AnyProps) => <path d={path as string} />,
    EdgeLabelRenderer: ({ children }: AnyProps) => (
      <div>{children as React.ReactNode}</div>
    ),
    Position: { Left: "left", Right: "right", Top: "top", Bottom: "bottom" },
    MarkerType: { ArrowClosed: "arrowclosed" },
  };
});

// A realistic graph: a role admits a supervisor, which reaches a worker; both
// agents can call `refund` but the supervisor carries a constraint the worker
// lacks — a delegate-to-escape (constraint-bypass) path. The worker's reach to
// an MCP tool is denied (a dashed, dimmed edge).
const GRAPH: PolicyGraph = {
  nodes: [
    { id: "role:admin", kind: "role", label: "admin" },
    { id: "agent:supervisor", kind: "agent", label: "supervisor" },
    { id: "agent:worker", kind: "agent", label: "worker" },
    { id: "tool:refund", kind: "tool", label: "refund" },
    { id: "mcp:search", kind: "mcp", label: "search" },
  ],
  edges: [
    {
      source: "role:admin",
      target: "agent:supervisor",
      kind: "admission",
      via: null,
      verdict: "allow",
      constraints: [],
      roles: ["admin"],
    },
    {
      source: "agent:supervisor",
      target: "agent:worker",
      kind: "reach",
      via: "handoff",
      verdict: "allow",
      constraints: [],
      roles: [],
    },
    {
      source: "agent:supervisor",
      target: "tool:refund",
      kind: "call",
      via: null,
      verdict: "approval_required",
      constraints: ["args.amount <= 100"],
      roles: ["admin"],
    },
    {
      source: "agent:worker",
      target: "tool:refund",
      kind: "call",
      via: null,
      verdict: "allow",
      constraints: [],
      roles: ["admin"],
    },
    {
      source: "agent:worker",
      target: "mcp:search",
      kind: "reach",
      via: "tool",
      verdict: "deny",
      constraints: [],
      roles: [],
    },
  ],
  roles: ["admin"],
};

function renderFlow(graph: PolicyGraph, agent?: AgentGraphOptions) {
  return renderWithProviders(<PolicyFlow graph={graph} agent={agent} />);
}

/** worker's own graph (agentSubgraph puts the agent first). */
const WORKER: PolicyGraph = {
  roles: ["admin"],
  nodes: [
    { id: "agent:worker", kind: "agent", label: "worker" },
    { id: "agent:helper", kind: "agent", label: "helper" },
    { id: "tool:refund", kind: "tool", label: "refund" },
  ],
  edges: [
    {
      source: "agent:worker",
      target: "agent:helper",
      kind: "reach",
      via: "handoff",
      verdict: "allow",
      constraints: [],
      roles: [],
    },
    {
      source: "agent:worker",
      target: "tool:refund",
      kind: "call",
      via: null,
      verdict: "allow",
      constraints: [],
      roles: [],
    },
  ],
};

const options = (over: Partial<AgentGraphOptions> = {}): AgentGraphOptions => ({
  toolCount: () => 3,
  onOpenAgent: vi.fn(),
  onMoreRoles: vi.fn(),
  onMoreAgents: vi.fn(),
  ...over,
});

function renderDialog(
  props: Partial<Parameters<typeof PolicyGraphDialog>[0]> = {},
) {
  return renderWithProviders(
    <PolicyGraphDialog
      open
      onOpenChange={() => undefined}
      agent="worker"
      graph={WORKER}
      loading={false}
      error={null}
      options={options()}
      {...props}
    />,
  );
}

afterEach(() => vi.restoreAllMocks());

describe("PolicyGraphDialog", () => {
  it("builds nodes + edges and renders the legend once loaded", async () => {
    renderFlow(GRAPH);
    await waitFor(() =>
      expect(screen.getByTestId("node-agent:supervisor")).toBeInTheDocument(),
    );
    // Every kind renders its own node.
    expect(screen.getByTestId("node-tool:refund")).toBeInTheDocument();
    expect(screen.getByTestId("node-mcp:search")).toBeInTheDocument();
    expect(screen.getByTestId("node-role:admin")).toBeInTheDocument();
    // Legend + trace hint.
    expect(screen.getByText(/click a node to trace/i)).toBeInTheDocument();
    expect(screen.getByText("allow")).toBeInTheDocument();
    expect(screen.getByText("deny")).toBeInTheDocument();
  });

  it("opens the edge card with verdict, roles, and constraints", async () => {
    renderFlow(GRAPH);
    await waitFor(() =>
      expect(screen.getByTestId("edge-e2")).toBeInTheDocument(),
    );
    // Edge 2 = supervisor → refund (approval, constrained, role admin).
    await userEvent.click(screen.getByTestId("edge-e2"));
    expect(await screen.findByText(/args\.amount <= 100/)).toBeInTheDocument();
    expect(screen.getByText(/calls/i)).toBeInTheDocument();
  });

  it("shows the no-constraints edge card for a denied reach", async () => {
    renderFlow(GRAPH);
    await waitFor(() =>
      expect(screen.getByTestId("edge-e4")).toBeInTheDocument(),
    );
    // Edge 4 = worker → search (deny reach, no constraints, no roles).
    await userEvent.click(screen.getByTestId("edge-e4"));
    await waitFor(() =>
      expect(screen.getByText(/no constraints/i)).toBeInTheDocument(),
    );
  });

  it("pins a node to trace its transitive paths and toggles off", async () => {
    renderFlow(GRAPH);
    const node = await screen.findByTestId("node-agent:supervisor");
    await userEvent.click(node);
    await waitFor(() =>
      expect(
        screen.getByText(/click empty space to reset/i),
      ).toBeInTheDocument(),
    );
    // Clicking the same node again un-pins (back to the trace hint).
    await userEvent.click(node);
    await waitFor(() =>
      expect(screen.getByText(/click a node to trace/i)).toBeInTheDocument(),
    );
  });

  it("previews hover focus without pinning", async () => {
    renderFlow(GRAPH);
    const node = await screen.findByTestId("node-agent:worker");
    // Radix marks <body> pointer-events:none while the dialog is open, which
    // trips userEvent's pointer check — drive the hover via fireEvent instead.
    fireEvent.mouseEnter(node);
    fireEvent.mouseLeave(node);
    const edge = screen.getByTestId("edge-e1");
    fireEvent.mouseEnter(edge);
    fireEvent.mouseLeave(edge);
    // No pin happened — the trace hint is still the default.
    expect(screen.getByText(/click a node to trace/i)).toBeInTheDocument();
  });

  it("toggles the flow animation", async () => {
    renderFlow(GRAPH);
    await screen.findByTestId("node-agent:worker");
    const animateBtn = screen.getByRole("button", { name: /animate flow/i });
    await userEvent.click(animateBtn);
    expect(
      screen.getByRole("button", { name: /animating/i }),
    ).toBeInTheDocument();
  });

  it("explains a pinned agent in a side panel", async () => {
    renderFlow(GRAPH);
    await userEvent.click(await screen.findByTestId("node-agent:supervisor"));
    const panel = screen.getByRole("complementary", {
      name: "agent supervisor",
    });
    expect(panel).toHaveTextContent(
      "Calls 1 of 1 tool — 1 with constraints, 1 behind approval.",
    );
    expect(panel).toHaveTextContent("Admitted roles");
    expect(panel).toHaveTextContent("admin");
    expect(panel).toHaveTextContent("worker · handoff");
    await userEvent.click(
      screen.getByRole("button", { name: "Close details" }),
    );
    expect(
      screen.queryByRole("complementary", { name: "agent supervisor" }),
    ).not.toBeInTheDocument();
  });

  it("labels the columns in the viewport, not as nodes", async () => {
    renderFlow(GRAPH);
    const heading = await screen.findByText("Caller roles");
    expect(screen.getByTestId("viewport")).toContainElement(heading);
    expect(screen.getByText("Agents · hop 1")).toBeInTheDocument();
    expect(screen.queryByTestId(/^node-column:/)).toBeNull();
  });

  it("names a node's neighbours by label and counts in the singular", async () => {
    renderFlow({
      ...GRAPH,
      nodes: [
        ...GRAPH.nodes,
        { id: "mcp:mcp-demo-read", label: "demo-read", kind: "mcp" },
      ],
      edges: [
        ...GRAPH.edges,
        {
          source: "agent:worker",
          target: "mcp:mcp-demo-read",
          kind: "call",
          via: null,
          verdict: "allow",
          constraints: [],
          roles: [],
        },
      ],
    } as PolicyGraph);
    await userEvent.click(await screen.findByTestId("node-mcp:mcp-demo-read"));
    const panel = screen.getByRole("complementary");
    expect(panel).toHaveTextContent("Callable by 1 of 1 agent.");
    await userEvent.click(screen.getByTestId("node-agent:worker"));
    expect(screen.getByRole("complementary")).toHaveTextContent("demo-read");
    expect(screen.getByRole("complementary")).not.toHaveTextContent(
      "mcp-demo-read",
    );
    // The edge card names it the same way.
    await userEvent.click(screen.getByTestId("edge-e5"));
    expect(screen.getByText(/→ calls →/).parentElement).toHaveTextContent(
      "worker → calls → demo-read",
    );
    expect(document.body).not.toHaveTextContent("mcp-demo-read");
  });

  it("draws agents as circles, roles as rectangles, tools as hexagons", async () => {
    renderFlow(GRAPH);
    const agent = await screen.findByTestId("node-agent:worker");
    expect(agent.querySelector("circle")).not.toBeNull();
    expect(
      screen.getByTestId("node-role:admin").querySelector("rect"),
    ).not.toBeNull();
    expect(
      screen.getByTestId("node-tool:refund").querySelector("polygon"),
    ).not.toBeNull();
  });

  it("opens a sub-agent's own graph on click and captions its tools", async () => {
    const onOpenAgent = vi.fn();
    renderFlow(WORKER, options({ onOpenAgent }));
    const helper = await screen.findByTestId("node-agent:helper");
    expect(helper).toHaveTextContent("3 tools");
    await userEvent.click(helper);
    expect(onOpenAgent).toHaveBeenCalledWith("helper");
  });

  it("shows the dialog's error states, keeping a cached graph", async () => {
    const err = new ApiError(422, null, "modules don't compose");
    const { rerender } = renderDialog({ graph: null, error: err });
    expect(screen.getByText(/don't compose/)).toBeInTheDocument();
    rerender(
      <PolicyGraphDialog
        open
        onOpenChange={() => undefined}
        agent="worker"
        graph={WORKER}
        loading={false}
        error={new ApiError(500, null, "boom")}
        options={options()}
      />,
    );
    expect(
      screen.getByText(/showing the last graph loaded/),
    ).toBeInTheDocument();
    expect(screen.getByTestId("node-agent:worker")).toBeInTheDocument();
  });

  it("says so when the agent has nothing to draw", () => {
    renderDialog({ graph: { roles: [], nodes: [WORKER.nodes[0]], edges: [] } });
    expect(
      screen.getByText(/no roles, tools or sub-agents for worker/),
    ).toBeInTheDocument();
  });

  it("pins, not opens, a sub-agent with no page", async () => {
    const onOpenAgent = vi.fn();
    renderFlow(WORKER, options({ onOpenAgent, hasPage: () => false }));
    await userEvent.click(await screen.findByTestId("node-agent:helper"));
    expect(onOpenAgent).not.toHaveBeenCalled();
    expect(
      screen.getByRole("complementary", { name: "agent helper" }),
    ).toBeInTheDocument();
  });

  it("summarises many tools by verdict above the canvas, with a list", async () => {
    const tools = Array.from({ length: 10 }, (_, i) => `tool:t${i}`);
    const big: PolicyGraph = {
      roles: [],
      nodes: [
        { id: "agent:big", kind: "agent", label: "big" },
        ...tools.map((id) => ({
          id,
          kind: "tool" as const,
          label: id.slice(5),
        })),
      ],
      edges: tools.map((target, i) => ({
        source: "agent:big",
        target,
        kind: "call" as const,
        via: null,
        verdict: i < 7 ? ("allow" as const) : ("deny" as const),
        constraints: i === 0 ? ["args.n < 5"] : [],
        roles: [],
      })),
    };
    renderFlow(big, options());
    // No tool, and no made-up node, on the canvas.
    expect(screen.queryByTestId("node-tool:t0")).toBeNull();
    expect(screen.getAllByTestId(/^node-/)).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: /7 allowed/ }));
    const list = screen.getByRole("complementary", { name: "Allowed tools" });
    expect(within(list).getByText("t0")).toBeInTheDocument();
    expect(within(list).getByText("args.n < 5")).toBeInTheDocument();
    await userEvent.type(within(list).getByLabelText("Filter tools"), "t6");
    expect(within(list).queryByText("t0")).toBeNull();
  });

  it("counts roles and sub-agents left off the canvas, as buttons", async () => {
    const roles = Array.from({ length: 7 }, (_, i) => `role:r${i}`);
    const subs = Array.from({ length: 6 }, (_, i) => `agent:s${i}`);
    const g: PolicyGraph = {
      roles: [],
      nodes: [
        { id: "agent:a", kind: "agent", label: "a" },
        ...roles.map((id) => ({ id, kind: "role" as const, label: id })),
        ...subs.map((id) => ({ id, kind: "agent" as const, label: id })),
      ],
      edges: [
        ...roles.map((source) => ({
          source,
          target: "agent:a",
          kind: "admission" as const,
          via: null,
          verdict: "allow" as const,
          constraints: [],
          roles: [],
        })),
        ...subs.map((target) => ({
          source: "agent:a",
          target,
          kind: "reach" as const,
          via: "tool" as const,
          verdict: "allow" as const,
          constraints: [],
          roles: [],
        })),
      ],
    };
    const onMoreRoles = vi.fn();
    const onMoreAgents = vi.fn();
    renderFlow(g, options({ onMoreRoles, onMoreAgents }));
    await userEvent.click(
      screen.getByRole("button", { name: "+2 more roles" }),
    );
    expect(onMoreRoles).toHaveBeenCalled();
    await userEvent.click(
      screen.getByRole("button", { name: "+1 more sub-agent" }),
    );
    expect(onMoreAgents).toHaveBeenCalled();
  });
});
