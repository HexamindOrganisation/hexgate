import { createContext, useContext, useMemo, useState } from "react";
import {
  BaseEdge,
  Background,
  Controls,
  type Edge,
  type EdgeProps,
  EdgeLabelRenderer,
  Handle,
  MarkerType,
  type Node,
  type NodeProps,
  Panel,
  Position,
  ReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useNavigate } from "react-router-dom";
import { AlertTriangle, Maximize2, Network, Play, X } from "lucide-react";

import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { usePolicyGraph } from "@/lib/policy_files";
import type { PolicyGraph, PolicyGraphEdge } from "@/lib/api";
import { cn } from "@/lib/utils";
import { HEX_H, HEX_W, layeredLayout } from "@/lib/graph-layout";

// Verdict → edge color (green allow, amber approval, red deny) — mid-tone
// hexes that read on both the light and dark graph ground.
const VERDICT: Record<string, string> = {
  allow: "#3f9166",
  approval_required: "#b9802a",
  deny: "#c84d62",
};

// Hexagon palette per node kind, as CSS vars (see --gr-* in index.css) so the
// graph re-themes light/dark with no re-render.
const KIND: Record<
  string,
  { fill: string; stroke: string; text: string; tag: string; label: string }
> = {
  // Fills are OPAQUE (not translucent) so edges — which render below the nodes —
  // are hidden where they pass under a hexagon, and an edge's endpoint (at the
  // node center) is covered by the node.
  agent: {
    fill: "var(--gr-agent-fill)",
    stroke: "var(--gr-agent-stroke)",
    text: "var(--gr-agent-text)",
    tag: "var(--gr-agent-tag)",
    label: "agent",
  },
  tool: {
    fill: "var(--gr-tool-fill)",
    stroke: "var(--gr-tool-stroke)",
    text: "var(--gr-tool-text)",
    tag: "var(--gr-tool-tag)",
    label: "tool",
  },
  mcp: {
    fill: "var(--gr-mcp-fill)",
    stroke: "var(--gr-mcp-stroke)",
    text: "var(--gr-mcp-text)",
    tag: "var(--gr-mcp-tag)",
    label: "mcp",
  },
  role: {
    fill: "var(--gr-role-fill)",
    stroke: "var(--gr-role-stroke)",
    text: "var(--gr-role-text)",
    tag: "var(--gr-role-tag)",
    label: "role",
  },
};

// A dimmed variant: recede a node by pulling its fill/border/text toward the
// background, NOT by lowering opacity — the node stays OPAQUE so edges still
// pass hidden under it and never show through a faded hexagon.
const DIM = {
  fill: "var(--gr-dim-fill)",
  stroke: "var(--gr-dim-stroke)",
  text: "var(--gr-dim-text)",
  tag: "var(--gr-dim-tag)",
};

type NodeData = { label: string; kind: string };
type EdgeData = Pick<
  PolicyGraphEdge,
  "kind" | "via" | "verdict" | "constraints" | "roles"
> & {
  /** Points the edge passes through (layout coordinates), so it routes
   * around nodes it doesn't touch. */
  bends?: { x: number; y: number }[];
};

// The hover-focus state is passed through CONTEXT, not through node/edge data,
// so the nodes/edges arrays stay stable on hover — recomputing them (and the
// per-edge markerEnd) made React Flow regenerate markers and flash the graph.
// A null active-set means nothing is hovered (everything is lit).
type FocusValue = {
  activeNodes: Set<string> | null;
  activeEdges: Set<number> | null;
  hoverNode: string | null;
  hoverEdge: number | null;
  // `animate` turns on the flowing-dot animation; `depth` is each node's
  // column (role → agent hops → tool), used to stagger the flow outward.
  animate: boolean;
  depth: Map<string, number>;
};
const FocusCtx = createContext<FocusValue>({
  activeNodes: null,
  activeEdges: null,
  hoverNode: null,
  hoverEdge: null,
  animate: false,
  depth: new Map(),
});

/** A hexagon node (SVG outline + centered label), styled by kind. */
function HexNode({ id, data }: NodeProps<Node<NodeData>>) {
  const k = KIND[data.kind] ?? KIND.tool;
  const focus = useContext(FocusCtx);
  const hovered = focus.hoverNode === id;
  // Both handles sit at the node CENTER (overlapping, invisible), so edges
  // connect center-to-center and their inner segment is hidden under the node.
  const centerHandle = {
    left: "50%",
    top: "50%",
    transform: "translate(-50%,-50%)",
    width: 1,
    height: 1,
    minWidth: 1,
    minHeight: 1,
    border: 0,
    background: "transparent",
    opacity: 0,
    pointerEvents: "none" as const,
  };
  // Dim by swapping to near-bg colors (stays opaque); highlight with a glow +
  // brighter outline. No opacity/scale — those caused edges to show through and
  // a cursor glitch on hover.
  const dim = focus.activeNodes ? !focus.activeNodes.has(id) : false;
  const fill = dim ? DIM.fill : k.fill;
  const stroke = dim ? DIM.stroke : k.stroke;
  const text = dim ? DIM.text : k.text;
  const tag = dim ? DIM.tag : k.tag;
  const glow = hovered ? 20 : dim ? 0 : 6;
  return (
    <div className="relative" style={{ width: HEX_W, height: HEX_H }}>
      <Handle type="target" position={Position.Left} style={centerHandle} />
      <svg
        width={HEX_W}
        height={HEX_H}
        viewBox={`0 0 ${HEX_W} ${HEX_H}`}
        className="absolute inset-0"
        style={{
          // color-mix (not a hex-alpha suffix) so the glow alpha works with the
          // `var(--gr-*)` stroke value.
          filter: glow
            ? `drop-shadow(0 0 ${glow}px color-mix(in srgb, ${k.stroke} ${
                hovered ? 80 : 27
              }%, transparent))`
            : undefined,
          transition: "filter 140ms",
        }}
      >
        {/* fill/stroke go through `style`, not presentation attributes — CSS
            var() resolves in inline style but not in SVG attributes. */}
        <polygon
          points="34,6 102,6 136,59 102,112 34,112 0,59"
          strokeWidth={hovered ? 2.6 : 1.5}
          style={{ fill, stroke, transition: "fill 140ms, stroke 140ms" }}
        />
        {hovered && (
          <polygon
            points="41.5,17.7 94.5,17.7 121,59 94.5,100.3 41.5,100.3 15,59"
            fill="none"
            strokeWidth="1"
            opacity="0.7"
            style={{ stroke: k.stroke }}
          />
        )}
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 px-3 text-center">
        <span
          className="font-mono text-[9px] uppercase tracking-wider"
          style={{ color: tag, transition: "color 140ms" }}
        >
          {k.label}
        </span>
        <span
          className="max-w-[104px] truncate font-mono text-[11px] font-medium"
          style={{
            color: text,
            fontWeight: hovered ? 700 : undefined,
            transition: "color 140ms",
          }}
          title={data.label}
        >
          {data.label}
        </span>
      </div>
      <Handle type="source" position={Position.Right} style={centerHandle} />
    </div>
  );
}

// End the visible path just off the target hex's side tip (the path arrives
// level), so the arrowhead sits on the border, not under the node.
const ARROW_GAP = HEX_W / 2 + 4;

type EdgeGeometry = { path: string; labelX: number; labelY: number };

/** Through any bend points with horizontal tangents, so an edge leaves and
 * enters its nodes level and runs level through each lane. The label sits
 * midway along the bends. */
function bentPath(points: { x: number; y: number }[]): EdgeGeometry {
  const last = points[points.length - 1];
  const before = points[points.length - 2];
  // Arrive at the tip facing the side the edge comes from.
  const end = {
    x: last.x + (before.x > last.x ? ARROW_GAP : -ARROW_GAP),
    y: last.y,
  };
  const pts = [...points.slice(0, -1), end];
  let path = `M${pts[0].x},${pts[0].y}`;
  for (let k = 1; k < pts.length; k++) {
    const p = pts[k - 1];
    const q = pts[k];
    const mx = (p.x + q.x) / 2;
    path += ` C${mx},${p.y} ${mx},${q.y} ${q.x},${q.y}`;
  }
  // Midway along the bends, or along the line when there are none.
  const via = points.length > 2 ? points.slice(1, -1) : [pts[0], end];
  const mid = {
    x: via.reduce((a, p) => a + p.x, 0) / via.length,
    y: via.reduce((a, p) => a + p.y, 0) / via.length,
  };
  return { path, labelX: mid.x, labelY: mid.y };
}

/** Verdict-colored routed edge with a small kind/via label. Deny + handoff
 * render dashed; a denied edge is dimmed. */
function PolicyEdge({
  id,
  source,
  sourceX,
  sourceY,
  targetX,
  targetY,
  data,
  selected,
  markerEnd,
}: EdgeProps<Edge<EdgeData>>) {
  const focus = useContext(FocusCtx);
  const idx = Number(id.slice(1)) || 0;
  const dimmed = focus.activeEdges ? !focus.activeEdges.has(idx) : false;
  const hovered = focus.hoverEdge === idx;
  const inFocus = focus.activeEdges ? focus.activeEdges.has(idx) : true;
  const { path, labelX, labelY } = bentPath([
    { x: sourceX, y: sourceY },
    ...(data?.bends ?? []),
    { x: targetX, y: targetY },
  ]);
  const color = VERDICT[data?.verdict ?? "allow"] ?? "#8a93a3";
  const dashed = data?.verdict === "deny" || data?.via === "handoff";
  const label =
    data?.kind === "reach"
      ? data.via
      : data?.kind === "admission"
        ? "admit"
        : "";
  const active = selected || hovered;
  // Resting width sits close to the focused width (3.4) so edges read clearly at
  // a glance; the focus state still stands out via its glow + full 3.4 stroke.
  const baseWidth = data?.verdict === "deny" ? 2.2 : 2.6;
  // A flowing dot travels source→target when the edge is selected/hovered, or
  // when Animate is on and the edge is in focus. The start is staggered by the
  // source's depth from a role, so a click's paths light role → agent → tool in
  // sequence.
  const flowing = selected || hovered || (focus.animate && inFocus);
  const flowDelay = (focus.depth.get(source) ?? 0) * 0.4;
  return (
    <>
      <BaseEdge
        path={path}
        markerEnd={markerEnd}
        interactionWidth={26}
        style={{
          stroke: color,
          strokeWidth: active ? 3.4 : baseWidth,
          strokeDasharray: dashed ? "5 4" : undefined,
          opacity: dimmed
            ? 0.08
            : data?.verdict === "deny" && !active
              ? 0.6
              : 1,
          filter: active ? `drop-shadow(0 0 5px ${color})` : undefined,
          transition: "stroke-width 120ms, opacity 120ms",
        }}
      />
      {flowing && (
        <circle
          r={3.6}
          fill={color}
          style={{ filter: `drop-shadow(0 0 5px ${color})` }}
        >
          <animateMotion
            dur="1.5s"
            begin={`${flowDelay}s`}
            repeatCount="indefinite"
            path={path}
          />
        </circle>
      )}
      {label && (
        <EdgeLabelRenderer>
          <div
            className="nodrag nopan pointer-events-none rounded px-1.5 py-0.5 font-mono text-[9px]"
            style={{
              position: "absolute",
              transform: `translate(-50%,-50%) translate(${labelX}px,${labelY}px)`,
              background: "hsl(var(--editor))",
              color,
              border: `1px solid ${color}66`,
              opacity: dimmed ? 0.15 : 1,
            }}
          >
            {label}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

/** A column heading; it pans and zooms with the graph. */
function ColumnLabel({ data }: NodeProps<Node<NodeData>>) {
  return (
    <div
      style={{ width: HEX_W }}
      className="text-center font-mono text-[13px] font-medium uppercase tracking-wider text-muted-foreground"
    >
      {data.label}
    </div>
  );
}

const nodeTypes = { hex: HexNode, column: ColumnLabel };
const edgeTypes = { policy: PolicyEdge };

const nodeLabel = (id: string) => id.slice(id.indexOf(":") + 1);
const COLUMN_PREFIX = "column:";

const verdictLabel = (v: string) =>
  v === "approval_required" ? "approval" : v;

/** A verdict chip; `constraints` add a "*" with the conditions on hover. */
function VerdictTag({
  verdict,
  constraints = [],
}: {
  verdict: string;
  constraints?: string[];
}) {
  return (
    <span
      className="shrink-0 rounded px-1.5 py-0.5 text-[9px] uppercase tracking-wider"
      title={constraints.join(" ∧ ") || undefined}
      style={{
        color: VERDICT[verdict],
        border: `1px solid ${VERDICT[verdict]}66`,
      }}
    >
      {verdictLabel(verdict)}
      {constraints.length > 0 && " *"}
    </span>
  );
}

/** One list in the side panel: the far end of each edge plus its verdict. */
function EdgeList({
  title,
  edges,
  end,
}: {
  title: string;
  edges: PolicyGraphEdge[];
  end: "source" | "target";
}) {
  if (edges.length === 0) return null;
  return (
    <div className="mt-3">
      <div className="mb-1 text-[10px] uppercase tracking-wider text-muted-foreground">
        {title}
      </div>
      <ul className="space-y-1">
        {edges.map((e, i) => (
          <li key={i} className="flex items-center justify-between gap-2">
            <span className="truncate">
              {nodeLabel(e[end])}
              {e.kind === "reach" && (
                <span className="text-muted-foreground"> · {e.via}</span>
              )}
            </span>
            <VerdictTag verdict={e.verdict} constraints={e.constraints} />
          </li>
        ))}
      </ul>
    </div>
  );
}

/** What the pinned node reaches, and who reaches it. */
function NodePanel({
  graph,
  id,
  onClose,
}: {
  graph: PolicyGraph;
  id: string;
  onClose: () => void;
}) {
  const node = graph.nodes.find((n) => n.id === id);
  if (!node) return null;
  const from = graph.edges.filter((e) => e.source === id);
  const to = graph.edges.filter((e) => e.target === id);
  const calls = from.filter((e) => e.kind === "call");
  const open = calls.filter((e) => e.verdict !== "deny");
  const summary =
    node.kind === "agent"
      ? `Calls ${open.length} of ${calls.length} tools — ${
          open.filter((e) => e.constraints.length > 0).length
        } with constraints, ${
          open.filter((e) => e.verdict === "approval_required").length
        } behind approval.`
      : node.kind === "role"
        ? `Admitted by ${from.filter((e) => e.verdict !== "deny").length} of ${from.length} agents.`
        : `Callable by ${to.filter((e) => e.verdict !== "deny").length} of ${to.length} agents.`;
  return (
    <aside
      aria-label={`${node.kind} ${node.label}`}
      className="max-h-[45vh] w-72 overflow-y-auto rounded-lg border border-border bg-card/95 p-3 font-mono text-[11px] text-foreground shadow-xl backdrop-blur"
    >
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
            {KIND[node.kind]?.label ?? node.kind}
          </div>
          <div className="text-sm">{node.label}</div>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close details"
          className="text-muted-foreground hover:text-foreground"
        >
          <X size={13} />
        </button>
      </div>
      <p className="mt-1 font-sans text-xs text-muted-foreground">{summary}</p>
      {node.kind === "agent" && (
        <>
          <EdgeList
            title="Admitted roles"
            edges={to.filter((e) => e.kind === "admission")}
            end="source"
          />
          <EdgeList
            title="Reached by"
            edges={to.filter((e) => e.kind === "reach")}
            end="source"
          />
          <EdgeList title="Tools" edges={calls} end="target" />
          <EdgeList
            title="Reaches"
            edges={from.filter((e) => e.kind === "reach")}
            end="target"
          />
        </>
      )}
      {node.kind === "role" && (
        <EdgeList title="Agents" edges={from} end="target" />
      )}
      {(node.kind === "tool" || node.kind === "mcp") && (
        <EdgeList title="Called by" edges={to} end="source" />
      )}
    </aside>
  );
}

type BypassWarning = {
  toolId: string;
  tool: string;
  strict: string;
  loose: string;
  strictC: string[];
  looseC: string[];
};

/** Flag a constraint-bypass: a tool T is callable by two agents A and B with
 * *different* constraints, and A can (transitively) reach B — so A can delegate
 * to B to call T beyond its own limit. Computed from the graph's call/reach
 * edges + their constraints. */
function bypassWarnings(graph: PolicyGraph): BypassWarning[] {
  const reachOut = new Map<string, string[]>();
  graph.edges.forEach((e) => {
    // Only a permitted reach can be used to delegate — a denied one can't, so it
    // never enables a bypass.
    if (e.kind === "reach" && e.verdict !== "deny") {
      const a = reachOut.get(e.source) ?? [];
      a.push(e.target);
      reachOut.set(e.source, a);
    }
  });
  const closure = (start: string) => {
    const seen = new Set<string>();
    const q = [...(reachOut.get(start) ?? [])];
    while (q.length) {
      const u = q.shift() as string;
      if (seen.has(u)) continue;
      seen.add(u);
      for (const v of reachOut.get(u) ?? []) q.push(v);
    }
    return seen;
  };
  const callers = new Map<string, { agent: string; constraints: string[] }[]>();
  graph.edges.forEach((e) => {
    if (e.kind === "call" && e.verdict !== "deny") {
      const arr = callers.get(e.target) ?? [];
      arr.push({ agent: e.source, constraints: e.constraints });
      callers.set(e.target, arr);
    }
  });
  const out: BypassWarning[] = [];
  const seen = new Set<string>();
  callers.forEach((cs, toolId) => {
    for (const a of cs) {
      const reach = closure(a.agent);
      for (const b of cs) {
        if (a.agent === b.agent || !reach.has(b.agent)) continue;
        // Only a real bypass when `a` carries a limit `b` lacks — then delegating
        // to `b` lets `a` escape it. If `b` is at least as restricted (or `a` is
        // unconstrained) there is nothing to gain, so don't warn.
        const escapes = a.constraints.some((c) => !b.constraints.includes(c));
        if (a.constraints.length === 0 || !escapes) continue;
        const key = `${toolId}|${a.agent}|${b.agent}`;
        if (seen.has(key)) continue;
        seen.add(key);
        out.push({
          toolId,
          tool: nodeLabel(toolId),
          strict: nodeLabel(a.agent),
          loose: nodeLabel(b.agent),
          strictC: a.constraints,
          looseC: b.constraints,
        });
      }
    }
  });
  return out;
}

export function PolicyFlow({ graph }: { graph: PolicyGraph }) {
  const [selected, setSelected] = useState<PolicyGraphEdge | null>(null);
  // A hovered node/edge is a temporary preview; a *pinned* node (clicked) shows
  // the persistent transitive focus.
  const [hover, setHover] = useState<{ node?: string; edge?: number } | null>(
    null,
  );
  const [pinned, setPinned] = useState<string | null>(null);
  const [animate, setAnimate] = useState(false);
  const lay = useMemo(() => layeredLayout(graph), [graph]);
  const warnings = useMemo(() => bypassWarnings(graph), [graph]);

  // Directed adjacency (out + in) for transitive reachability.
  const adj = useMemo(() => {
    const out = new Map<string, string[]>();
    const inc = new Map<string, string[]>();
    graph.nodes.forEach((n) => {
      out.set(n.id, []);
      inc.set(n.id, []);
    });
    graph.edges.forEach((e) => {
      out.get(e.source)?.push(e.target);
      inc.get(e.target)?.push(e.source);
    });
    return { out, inc };
  }, [graph]);

  // Each node's column staggers the flow animation, so dots ripple outward
  // role → agent → tool in the order the columns show.
  const depth = useMemo(() => new Map(Object.entries(lay.col)), [lay]);

  // Provided via context so it never rebuilds the nodes/edges arrays (which
  // would flash the graph).
  const focus = useMemo(() => {
    // Pinned node → every node on a directed path TO or FROM it. Forward BFS =
    // everything it can reach (its tools + sub-agents + their tools); backward
    // BFS = everything that can reach it (agents/roles, even via a sub-agent).
    // An edge lights up if its source is forward-reachable OR its target is
    // backward-reachable (i.e. it lies on such a path); arrowheads show flow.
    if (pinned) {
      const bfs = (from: Map<string, string[]>) => {
        const seen = new Set([pinned]);
        const q = [pinned];
        while (q.length) {
          const u = q.shift() as string;
          for (const v of from.get(u) ?? [])
            if (!seen.has(v)) {
              seen.add(v);
              q.push(v);
            }
        }
        return seen;
      };
      const fwd = bfs(adj.out);
      const bwd = bfs(adj.inc);
      const activeNodes = new Set<string>([...fwd, ...bwd]);
      const activeEdges = new Set<number>();
      graph.edges.forEach((e, i) => {
        if (fwd.has(e.source) || bwd.has(e.target)) activeEdges.add(i);
      });
      return { activeNodes, activeEdges, hoverNode: pinned, hoverEdge: null };
    }
    // Hover preview → the node/edge + its direct neighbors.
    if (hover) {
      const activeNodes = new Set<string>();
      const activeEdges = new Set<number>();
      if (hover.node) {
        activeNodes.add(hover.node);
        graph.edges.forEach((e, i) => {
          if (e.source === hover.node || e.target === hover.node) {
            activeEdges.add(i);
            activeNodes.add(e.source);
            activeNodes.add(e.target);
          }
        });
      } else if (hover.edge != null) {
        const e = graph.edges[hover.edge];
        if (e) {
          activeEdges.add(hover.edge);
          activeNodes.add(e.source);
          activeNodes.add(e.target);
        }
      }
      return {
        activeNodes,
        activeEdges,
        hoverNode: hover.node ?? null,
        hoverEdge: hover.edge ?? null,
      };
    }
    return {
      activeNodes: null,
      activeEdges: null,
      hoverNode: null,
      hoverEdge: null,
    };
  }, [pinned, hover, graph, adj]);

  // Stable across hover (depend only on graph and its layout), so React Flow
  // doesn't regenerate markers or thrash edge mouse events.
  const nodes: Node<NodeData>[] = useMemo(
    () => [
      ...lay.columns.map((c, i) => ({
        id: `${COLUMN_PREFIX}${i}`,
        type: "column",
        position: { x: c.x - HEX_W / 2, y: c.top - 40 },
        data: { label: c.title, kind: "column" },
        draggable: false,
        selectable: false,
      })),
      ...graph.nodes.map((n) => ({
        id: n.id,
        type: "hex",
        position: {
          x: (lay.pos[n.id]?.x ?? 0) - HEX_W / 2,
          y: (lay.pos[n.id]?.y ?? 0) - HEX_H / 2,
        },
        data: { label: n.label, kind: n.kind },
        draggable: false,
      })),
    ],
    [graph, lay],
  );

  const edges: Edge<EdgeData>[] = useMemo(
    () =>
      graph.edges.map((e, i) => ({
        id: `e${i}`,
        source: e.source,
        target: e.target,
        type: "policy",
        markerEnd: {
          type: MarkerType.ArrowClosed,
          width: 16,
          height: 16,
          color: VERDICT[e.verdict] ?? "#8a93a3",
        },
        data: {
          kind: e.kind,
          via: e.via,
          verdict: e.verdict,
          constraints: e.constraints,
          roles: e.roles,
          bends: lay.bends[i],
        },
      })),
    [graph, lay],
  );

  const ctxValue = useMemo<FocusValue>(
    () => ({ ...focus, animate, depth }),
    [focus, animate, depth],
  );

  return (
    <FocusCtx.Provider value={ctxValue}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onEdgeClick={(_, edge) => {
          const idx = Number(edge.id.slice(1));
          setSelected(graph.edges[idx] ?? null);
        }}
        onNodeClick={(_, node) => {
          // A heading is part of the canvas: clicking it clears, like the pane.
          if (node.type === "column") {
            setSelected(null);
            setPinned(null);
            return;
          }
          setPinned((p) => (p === node.id ? null : node.id));
        }}
        onPaneClick={() => {
          setSelected(null);
          setPinned(null);
        }}
        onNodeMouseEnter={(_, node) => {
          if (node.type !== "column") setHover({ node: node.id });
        }}
        onNodeMouseLeave={() => setHover(null)}
        onEdgeMouseEnter={(_, edge) =>
          setHover({ edge: Number(edge.id.slice(1)) })
        }
        onEdgeMouseLeave={() => setHover(null)}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        proOptions={{ hideAttribution: true }}
        minZoom={0.3}
        maxZoom={1.8}
        // Map-like feel: drag empty space OR two-finger trackpad scroll to pan;
        // pinch (or the +/- controls) to zoom. Default scroll-to-zoom reads as
        // "can't pan" on a Mac trackpad.
        panOnDrag
        panOnScroll
        zoomOnScroll={false}
        zoomOnPinch
        style={{
          background: "hsl(var(--editor))",
          width: "100%",
          height: "100%",
        }}
      >
        <Background color="var(--gr-dots)" gap={22} />
        <Controls
          showInteractive={false}
          style={{
            background: "hsl(var(--card))",
            border: "1px solid hsl(var(--border))",
            borderRadius: 8,
          }}
        />
        <Panel position="top-right" className="flex flex-col items-end gap-2">
          <button
            onClick={() => setAnimate((a) => !a)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 font-mono text-[11px] backdrop-blur transition-colors",
              animate
                ? "border-primary/60 bg-primary/20 text-foreground"
                : "border-border bg-card/90 text-muted-foreground hover:text-foreground",
            )}
            title={
              pinned
                ? "Animate invocation flow along the focused paths"
                : "Animate invocation flow along every path"
            }
          >
            <Play size={12} />
            {animate ? "Animating…" : "Animate flow"}
          </button>
          {pinned && (
            <NodePanel
              graph={graph}
              id={pinned}
              onClose={() => setPinned(null)}
            />
          )}
          {/* Stacked under the node panel so the two never overlap. */}
          {selected && (
            <div className="w-64 rounded-lg border border-border bg-card/95 p-3 font-mono text-[11px] text-foreground shadow-xl backdrop-blur">
              <div className="mb-2 flex items-center justify-between">
                <VerdictTag verdict={selected.verdict} />
                <button
                  onClick={() => setSelected(null)}
                  className="text-muted-foreground hover:text-foreground"
                >
                  <X size={13} />
                </button>
              </div>
              <div className="mb-1 text-foreground">
                {nodeLabel(selected.source)}
                <span className="text-muted-foreground/50">
                  {" "}
                  {selected.kind === "reach"
                    ? `→ ${selected.via} →`
                    : selected.kind === "admission"
                      ? "→ admits →"
                      : "→ calls →"}{" "}
                </span>
                {nodeLabel(selected.target)}
              </div>
              {selected.roles.length > 0 && (
                <div className="mb-2 text-[10px] text-muted-foreground">
                  role{selected.roles.length > 1 ? "s" : ""}:{" "}
                  {selected.roles.join(", ")}
                </div>
              )}
              {selected.constraints.length > 0 ? (
                <div className="space-y-1">
                  <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                    constraints
                  </div>
                  {selected.constraints.map((c, i) => (
                    <div
                      key={i}
                      className="rounded bg-muted px-2 py-1 text-[10.5px] text-foreground"
                    >
                      {c}
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-[10px] text-muted-foreground">
                  no constraints
                </div>
              )}
            </div>
          )}
        </Panel>
        <Panel position="top-left">
          <div className="flex flex-wrap gap-3 rounded-lg border border-border bg-card/90 px-3 py-2 font-mono text-[10px] text-muted-foreground backdrop-blur">
            {(["allow", "approval_required", "deny"] as const).map((v) => (
              <span key={v} className="inline-flex items-center gap-1.5">
                <i
                  className="inline-block h-2 w-4 rounded"
                  style={{ background: VERDICT[v] }}
                />
                {verdictLabel(v)}
              </span>
            ))}
            <span className="text-muted-foreground/40">·</span>
            <span>
              {pinned
                ? "focused — click empty space to reset"
                : "click a node to trace its paths"}
            </span>
          </div>
        </Panel>
        {warnings.length > 0 && (
          <Panel position="bottom-center">
            <div className="max-w-[560px] rounded-lg border border-deny/50 bg-deny/10 px-3 py-2 font-mono text-[10.5px] text-deny shadow-xl backdrop-blur">
              <div className="mb-1.5 flex items-center gap-1.5 font-semibold text-deny">
                <AlertTriangle size={12} />
                review carefully · {warnings.length} constraint-bypass path
                {warnings.length > 1 ? "s" : ""}
              </div>
              <div className="space-y-1">
                {warnings.slice(0, 3).map((w, i) => (
                  <button
                    key={i}
                    onClick={() => setPinned(w.toolId)}
                    className="block w-full text-left leading-relaxed hover:text-deny"
                  >
                    <b className="text-deny">{w.strict}</b> can delegate to{" "}
                    <b className="text-deny">{w.loose}</b> to call{" "}
                    <b className="text-deny">{w.tool}</b> beyond its own limit
                    <span className="text-muted-foreground">
                      {" "}
                      — {w.strict}: {w.strictC.join(" ∧ ") || "no limit"} ·{" "}
                      {w.loose}: {w.looseC.join(" ∧ ") || "no limit"}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          </Panel>
        )}
      </ReactFlow>
    </FocusCtx.Provider>
  );
}

const GRAPH_ACTION_BTN =
  "inline-flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-[11px] font-medium text-foreground transition-colors hover:bg-accent";

/** The graph itself — a header (title, role filter, and an optional
 * `headerRight` action) over the ReactFlow canvas. Reused by the inline dialog
 * and the full-page `/graph` route. */
export function PolicyGraphView({
  projectId,
  roleNames = [],
  enabled = true,
  headerRight,
}: {
  projectId: string;
  /** Load-time fallback for the role filter; once the graph resolves, its own
   * `roles` (every role across all agents) take over. */
  roleNames?: string[];
  enabled?: boolean;
  headerRight?: React.ReactNode;
}) {
  const [role, setRole] = useState<string>("");
  const graph = usePolicyGraph(projectId, role || undefined, enabled);
  const roleOptions = graph.data?.roles ?? roleNames;

  return (
    <div
      className="flex h-full flex-col"
      style={{ background: "hsl(var(--editor))" }}
    >
      <div className="flex flex-row items-center justify-between gap-3 border-b border-border px-4 py-3">
        <div className="flex items-center gap-2 text-sm text-foreground">
          <Network size={16} className="text-primary" />
          Policy graph
        </div>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-2 font-mono text-[11px] text-muted-foreground">
            role
            <select
              value={role}
              onChange={(e) => setRole(e.target.value)}
              className="rounded border border-border bg-card px-1.5 py-0.5 text-[11px] text-foreground"
            >
              <option value="">all roles</option>
              {roleOptions.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </label>
          {headerRight}
        </div>
      </div>
      <div className="relative flex-1">
        {graph.isLoading && (
          <div className="grid h-full place-items-center font-mono text-xs text-muted-foreground">
            resolving graph…
          </div>
        )}
        {graph.isError && (
          <div className="grid h-full place-items-center px-8 text-center font-mono text-xs text-deny">
            the modules don't compose — fix the lints, then reopen the graph.
          </div>
        )}
        {graph.data && graph.data.nodes.length > 0 && (
          <PolicyFlow graph={graph.data} />
        )}
        {graph.data && graph.data.nodes.length === 0 && (
          <div className="grid h-full place-items-center font-mono text-xs text-muted-foreground">
            no agents bound yet — add a role binding to see the graph.
          </div>
        )}
      </div>
    </div>
  );
}

export function PolicyGraphDialog({
  open,
  onOpenChange,
  projectId,
  roleNames,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  projectId: string;
  roleNames: string[];
}) {
  const navigate = useNavigate();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="flex h-[82vh] max-w-[min(96vw,1120px)] flex-col gap-0 overflow-hidden p-0"
        style={{ background: "hsl(var(--editor))" }}
      >
        <DialogTitle className="sr-only">Policy graph</DialogTitle>
        <PolicyGraphView
          projectId={projectId}
          roleNames={roleNames}
          enabled={open}
          headerRight={
            <button
              type="button"
              onClick={() => {
                onOpenChange(false);
                navigate("/graph");
              }}
              title="Open the full graph view"
              className={cn(GRAPH_ACTION_BTN, "mr-6")}
            >
              <Maximize2 size={12} />
              Full view
            </button>
          }
        />
      </DialogContent>
    </Dialog>
  );
}
