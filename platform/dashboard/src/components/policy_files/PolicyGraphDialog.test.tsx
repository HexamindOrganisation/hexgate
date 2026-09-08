/**
 * PolicyGraphDialog tests. `@xyflow/react` needs a real layout engine
 * (ResizeObserver, measured DOM) that jsdom lacks, so it's replaced with a
 * light stub that still renders each node via `nodeTypes` and each edge via
 * `edgeTypes` and forwards the click/hover handlers. That keeps the component's
 * OWN logic under test: force layout, node/edge construction, the verdict /
 * constraint / legend panels, role filtering, the constraint-bypass warning,
 * and the node-click transitive trace.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { PolicyGraph } from "@/lib/api";
import { PolicyGraphDialog } from "./PolicyGraphDialog";
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
};

function stubGraph(payload: unknown, status = 200): ReturnType<typeof vi.fn> {
  const spy = vi.fn();
  vi.spyOn(window, "fetch").mockImplementation(
    async (input: RequestInfo | URL) => {
      const url = String(input);
      spy(url);
      return new Response(JSON.stringify(payload), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    },
  );
  return spy;
}

function renderDialog(
  props: Partial<Parameters<typeof PolicyGraphDialog>[0]> = {},
) {
  return renderWithProviders(
    <PolicyGraphDialog
      open
      onOpenChange={() => undefined}
      projectId="p1"
      roleNames={["admin", "support"]}
      {...props}
    />,
  );
}

afterEach(() => vi.restoreAllMocks());

describe("PolicyGraphDialog", () => {
  it("builds nodes + edges and renders the legend once loaded", async () => {
    stubGraph(GRAPH);
    renderDialog();
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

  it("flags the constraint-bypass path and focuses it on click", async () => {
    stubGraph(GRAPH);
    renderDialog();
    await waitFor(() =>
      expect(screen.getByText(/constraint-bypass path/i)).toBeInTheDocument(),
    );
    // The warning names the strict + loose agents and the shared tool.
    expect(screen.getByText(/can delegate to/i)).toBeInTheDocument();
    await userEvent.click(screen.getByText(/can delegate to/i));
    // Focusing swaps the legend hint to the reset copy.
    await waitFor(() =>
      expect(
        screen.getByText(/click empty space to reset/i),
      ).toBeInTheDocument(),
    );
  });

  it("opens the edge card with verdict, roles, and constraints", async () => {
    stubGraph(GRAPH);
    renderDialog();
    await waitFor(() =>
      expect(screen.getByTestId("edge-e2")).toBeInTheDocument(),
    );
    // Edge 2 = supervisor → refund (approval, constrained, role admin). The
    // constraint text now appears twice: the warning panel + the edge card.
    await userEvent.click(screen.getByTestId("edge-e2"));
    await waitFor(() =>
      expect(screen.getAllByText(/args\.amount <= 100/).length).toBeGreaterThan(
        1,
      ),
    );
    expect(screen.getByText(/calls/i)).toBeInTheDocument();
  });

  it("shows the no-constraints edge card for a denied reach", async () => {
    stubGraph(GRAPH);
    renderDialog();
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
    stubGraph(GRAPH);
    renderDialog();
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
    stubGraph(GRAPH);
    renderDialog();
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
    stubGraph(GRAPH);
    renderDialog();
    await screen.findByTestId("node-agent:worker");
    const animateBtn = screen.getByRole("button", { name: /animate flow/i });
    await userEvent.click(animateBtn);
    expect(
      screen.getByRole("button", { name: /animating/i }),
    ).toBeInTheDocument();
  });

  it("refetches with a role filter when the role select changes", async () => {
    const spy = stubGraph(GRAPH);
    renderDialog();
    await screen.findByTestId("node-agent:worker");
    await userEvent.selectOptions(screen.getByRole("combobox"), "admin");
    await waitFor(() =>
      expect(spy.mock.calls.some(([u]) => u.includes("role=admin"))).toBe(true),
    );
  });

  it("shows the doesn't-compose error state on a 422", async () => {
    stubGraph({ detail: "modules don't compose" }, 422);
    renderDialog();
    await waitFor(() =>
      expect(screen.getByText(/don't compose/i)).toBeInTheDocument(),
    );
  });

  it("shows the empty state when no agents are bound", async () => {
    stubGraph({ nodes: [], edges: [] });
    renderDialog();
    await waitFor(() =>
      expect(screen.getByText(/no agents bound yet/i)).toBeInTheDocument(),
    );
  });
});
