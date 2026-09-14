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

// Verdict → color (matches the editor/badges: green allow, amber approval,
// red deny). Kept local hexes so the graph reads on the near-black canvas.
const VERDICT: Record<string, string> = {
  allow: "#3f9166",
  approval_required: "#b9802a",
  deny: "#c84d62",
};

// Hexagon palette per node kind, on the near-black graph ground.
const KIND: Record<
  string,
  { fill: string; stroke: string; text: string; tag: string; label: string }
> = {
  // Fills are OPAQUE (not translucent) so edges — which render below the nodes —
  // are hidden where they pass under a hexagon, and an edge's endpoint (at the
  // node center) is covered by the node.
  agent: {
    fill: "#271826",
    stroke: "#b57aa8",
    text: "#f0e0ec",
    tag: "#c79bbb",
    label: "agent",
  },
  tool: {
    fill: "#15181f",
    stroke: "rgba(255,255,255,0.28)",
    text: "#d3dae6",
    tag: "#8a93a3",
    label: "tool",
  },
  mcp: {
    fill: "#0d2226",
    stroke: "#3fb6c8",
    text: "#c7f1f6",
    tag: "#79cfda",
    label: "mcp",
  },
  role: {
    fill: "#121a2e",
    stroke: "rgba(109,159,240,0.55)",
    text: "#cfe0fb",
    tag: "#9fb6e6",
    label: "role",
  },
};

// A dimmed variant: recede a node by pulling its fill/border/text toward the
// background, NOT by lowering opacity — the node stays OPAQUE so edges still
// pass hidden under it and never show through a faded hexagon.
const DIM = {
  fill: "#0c0f16",
  stroke: "rgba(255,255,255,0.07)",
  text: "#3f4550",
  tag: "#2f343d",
};

type NodeData = { label: string; kind: string };
type EdgeData = Pick<
  PolicyGraphEdge,
  "kind" | "via" | "verdict" | "constraints" | "roles"
>;

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
  // distance from a root (role), used to stagger the flow outward.
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
    <div className="relative" style={{ width: 136, height: 118 }}>
      <Handle type="target" position={Position.Left} style={centerHandle} />
      <svg
        width="136"
        height="118"
        viewBox="0 0 136 118"
        className="absolute inset-0"
        style={{
          filter: glow
            ? `drop-shadow(0 0 ${glow}px ${k.stroke}${hovered ? "cc" : "44"})`
            : undefined,
          transition: "filter 140ms",
        }}
      >
        <polygon
          points="34,6 102,6 136,59 102,112 34,112 0,59"
          fill={fill}
          stroke={stroke}
          strokeWidth={hovered ? 2.6 : 1.5}
          style={{ transition: "fill 140ms, stroke 140ms" }}
        />
        {hovered && (
          <polygon
            points="41.5,17.7 94.5,17.7 121,59 94.5,100.3 41.5,100.3 15,59"
            fill="none"
            stroke={k.stroke}
            strokeWidth="1"
            opacity="0.7"
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

/** Verdict-colored bezier edge with a small kind/via label. Deny + handoff
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
  const dx = targetX - sourceX;
  const dy = targetY - sourceY;
  const dist = Math.hypot(dx, dy) || 1;
  const ux = dx / dist;
  const uy = dy / dist;
  // Stop the visible path at the target node's border so the arrowhead sits on
  // the edge, not hidden under the node center.
  const gap = 60;
  const tX = targetX - ux * gap;
  const tY = targetY - uy * gap;
  // A cubic bezier bowed perpendicular to the line. The bow side alternates by
  // the edge's index parity, so parallel-ish edges don't all curve the same way
  // (and a reversed pair bows the other side, since its direction flips).
  const side = idx % 2 === 0 ? 1 : -1;
  const nx = -uy * side;
  const ny = ux * side;
  const bow = 0.16 * dist;
  const c1x = sourceX + (tX - sourceX) / 3 + nx * bow;
  const c1y = sourceY + (tY - sourceY) / 3 + ny * bow;
  const c2x = sourceX + (2 * (tX - sourceX)) / 3 + nx * bow;
  const c2y = sourceY + (2 * (tY - sourceY)) / 3 + ny * bow;
  const path = `M${sourceX},${sourceY} C${c1x},${c1y} ${c2x},${c2y} ${tX},${tY}`;
  // Label at the bezier midpoint, B(0.5).
  const labelX = 0.125 * sourceX + 0.375 * c1x + 0.375 * c2x + 0.125 * tX;
  const labelY = 0.125 * sourceY + 0.375 * c1y + 0.375 * c2y + 0.125 * tY;
  const color = VERDICT[data?.verdict ?? "allow"] ?? "#8a93a3";
  const dashed = data?.verdict === "deny" || data?.via === "handoff";
  const label =
    data?.kind === "reach"
      ? data.via
      : data?.kind === "admission"
        ? "admit"
        : "";
  const active = selected || hovered;
  const baseWidth = data?.verdict === "deny" ? 1.1 : 1.6;
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
              ? 0.32
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
              background: "#0a0d14",
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

const nodeTypes = { hex: HexNode };
const edgeTypes = { policy: PolicyEdge };

/** Force-directed layout (Fruchterman–Reingold): every node repels every other
 * and each edge pulls its endpoints together, so the graph relaxes into a
 * spread, uncluttered arrangement instead of dense columns. Deterministic — a
 * fixed golden-angle seed + fixed iteration count — so it doesn't jitter between
 * renders; nodes stay draggable afterwards. */
function layout(graph: PolicyGraph): Record<string, { x: number; y: number }> {
  const N = graph.nodes.length;
  const pos = graph.nodes.map((_, i) => {
    const a = i * 2.399963; // golden angle → an even, deterministic spiral seed
    const r = 40 + 26 * Math.sqrt(i);
    return { x: Math.cos(a) * r, y: Math.sin(a) * r };
  });
  const index: Record<string, number> = {};
  graph.nodes.forEach((n, i) => (index[n.id] = i));
  const links = graph.edges
    .map((e) => [index[e.source], index[e.target]] as const)
    .filter(([s, t]) => s != null && t != null);

  const k = 300; // ideal node separation (hexes are ~136px wide)
  const ITER = 420;
  let temp = k * 1.4;
  const cool = temp / (ITER + 1);

  for (let it = 0; it < ITER; it++) {
    const disp = pos.map(() => ({ x: 0, y: 0 }));
    for (let i = 0; i < N; i++) {
      for (let j = i + 1; j < N; j++) {
        let dx = pos[i].x - pos[j].x;
        let dy = pos[i].y - pos[j].y;
        const d = Math.hypot(dx, dy) || 0.01;
        const f = (k * k) / d; // repulsion
        dx = (dx / d) * f;
        dy = (dy / d) * f;
        disp[i].x += dx;
        disp[i].y += dy;
        disp[j].x -= dx;
        disp[j].y -= dy;
      }
    }
    for (const [s, t] of links) {
      let dx = pos[s].x - pos[t].x;
      let dy = pos[s].y - pos[t].y;
      const d = Math.hypot(dx, dy) || 0.01;
      const f = (d * d) / k; // attraction
      dx = (dx / d) * f;
      dy = (dy / d) * f;
      disp[s].x -= dx;
      disp[s].y -= dy;
      disp[t].x += dx;
      disp[t].y += dy;
    }
    for (let i = 0; i < N; i++) {
      const d = Math.hypot(disp[i].x, disp[i].y) || 0.01;
      pos[i].x += (disp[i].x / d) * Math.min(d, temp); // capped by temperature
      pos[i].y += (disp[i].y / d) * Math.min(d, temp);
    }
    temp -= cool; // cool down
  }

  const out: Record<string, { x: number; y: number }> = {};
  graph.nodes.forEach((n, i) => (out[n.id] = pos[i]));
  return out;
}

const nodeLabel = (id: string) => id.slice(id.indexOf(":") + 1);

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

function PolicyFlow({ graph }: { graph: PolicyGraph }) {
  const [selected, setSelected] = useState<PolicyGraphEdge | null>(null);
  // A hovered node/edge is a temporary preview; a *pinned* node (clicked) shows
  // the persistent transitive focus.
  const [hover, setHover] = useState<{ node?: string; edge?: number } | null>(
    null,
  );
  const [pinned, setPinned] = useState<string | null>(null);
  const [animate, setAnimate] = useState(false);
  const pos = useMemo(() => layout(graph), [graph]);
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

  // Each node's distance from a root (a node with no incoming edge — a role, or
  // an ungoverned entry). Staggers the flow animation so dots ripple outward
  // role → agent → tool.
  const depth = useMemo(() => {
    const d = new Map<string, number>();
    const q = graph.nodes
      .filter((n) => (adj.inc.get(n.id) ?? []).length === 0)
      .map((n) => {
        d.set(n.id, 0);
        return n.id;
      });
    for (let h = 0; h < q.length; h++) {
      const u = q[h];
      const du = d.get(u) ?? 0;
      for (const v of adj.out.get(u) ?? [])
        if (!d.has(v)) {
          d.set(v, du + 1);
          q.push(v);
        }
    }
    graph.nodes.forEach((n) => {
      if (!d.has(n.id)) d.set(n.id, 0);
    });
    return d;
  }, [graph, adj]);

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

  // Stable across hover (depend only on graph/pos), so React Flow doesn't
  // regenerate markers or thrash edge mouse events.
  const nodes: Node<NodeData>[] = useMemo(
    () =>
      graph.nodes.map((n) => ({
        id: n.id,
        type: "hex",
        position: pos[n.id] ?? { x: 0, y: 0 },
        data: { label: n.label, kind: n.kind },
        draggable: false,
      })),
    [graph, pos],
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
        },
      })),
    [graph],
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
        onNodeClick={(_, node) =>
          setPinned((p) => (p === node.id ? null : node.id))
        }
        onPaneClick={() => {
          setSelected(null);
          setPinned(null);
        }}
        onNodeMouseEnter={(_, node) => setHover({ node: node.id })}
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
        style={{ background: "#0a0d14", width: "100%", height: "100%" }}
      >
        <Background color="#1b2233" gap={22} />
        <Controls
          showInteractive={false}
          style={{
            background: "#11151f",
            border: "1px solid rgba(255,255,255,0.08)",
            borderRadius: 8,
          }}
        />
        <Panel position="top-right">
          <button
            onClick={() => setAnimate((a) => !a)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-lg border px-3 py-1.5 font-mono text-[11px] backdrop-blur transition-colors",
              animate
                ? "border-primary/60 bg-primary/20 text-foreground"
                : "border-white/10 bg-[#11151f]/90 text-[#8a93a3] hover:text-foreground",
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
        </Panel>
        <Panel position="top-left">
          <div className="flex flex-wrap gap-3 rounded-lg border border-white/10 bg-[#11151f]/90 px-3 py-2 font-mono text-[10px] text-[#8a93a3] backdrop-blur">
            {(["allow", "approval_required", "deny"] as const).map((v) => (
              <span key={v} className="inline-flex items-center gap-1.5">
                <i
                  className="inline-block h-2 w-4 rounded"
                  style={{ background: VERDICT[v] }}
                />
                {v === "approval_required" ? "approval" : v}
              </span>
            ))}
            <span className="text-white/25">·</span>
            <span>
              {pinned
                ? "focused — click empty space to reset"
                : "click a node to trace its paths"}
            </span>
          </div>
        </Panel>
        {warnings.length > 0 && (
          <Panel position="bottom-center">
            <div className="max-w-[560px] rounded-lg border border-[#c84d62]/50 bg-[#180f12]/95 px-3 py-2 font-mono text-[10.5px] text-[#f0a5ad] shadow-xl backdrop-blur">
              <div className="mb-1.5 flex items-center gap-1.5 font-semibold text-[#f5959f]">
                <AlertTriangle size={12} />
                review carefully · {warnings.length} constraint-bypass path
                {warnings.length > 1 ? "s" : ""}
              </div>
              <div className="space-y-1">
                {warnings.slice(0, 3).map((w, i) => (
                  <button
                    key={i}
                    onClick={() => setPinned(w.toolId)}
                    className="block w-full text-left leading-relaxed hover:text-[#ffd0d5]"
                  >
                    <b className="text-[#ffc4ca]">{w.strict}</b> can delegate to{" "}
                    <b className="text-[#ffc4ca]">{w.loose}</b> to call{" "}
                    <b className="text-[#ffc4ca]">{w.tool}</b> beyond its own
                    limit
                    <span className="text-[#9c6a72]">
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
        {selected && (
          <Panel position="bottom-right">
            <div className="w-64 rounded-lg border border-white/10 bg-[#11151f]/95 p-3 font-mono text-[11px] text-[#c7cdd8] shadow-xl backdrop-blur">
              <div className="mb-2 flex items-center justify-between">
                <span
                  className="rounded px-1.5 py-0.5 text-[9px] uppercase tracking-wider"
                  style={{
                    color: VERDICT[selected.verdict],
                    border: `1px solid ${VERDICT[selected.verdict]}66`,
                  }}
                >
                  {selected.verdict === "approval_required"
                    ? "approval"
                    : selected.verdict}
                </span>
                <button
                  onClick={() => setSelected(null)}
                  className="text-white/40 hover:text-white/80"
                >
                  <X size={13} />
                </button>
              </div>
              <div className="mb-1 text-[#e6e9ef]">
                {nodeLabel(selected.source)}
                <span className="text-white/30">
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
                <div className="mb-2 text-[10px] text-[#6b7382]">
                  role{selected.roles.length > 1 ? "s" : ""}:{" "}
                  {selected.roles.join(", ")}
                </div>
              )}
              {selected.constraints.length > 0 ? (
                <div className="space-y-1">
                  <div className="text-[10px] uppercase tracking-wider text-[#6b7382]">
                    constraints
                  </div>
                  {selected.constraints.map((c, i) => (
                    <div
                      key={i}
                      className="rounded bg-black/40 px-2 py-1 text-[10.5px] text-[#b6bdca]"
                    >
                      {c}
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-[10px] text-[#6b7382]">no constraints</div>
              )}
            </div>
          </Panel>
        )}
      </ReactFlow>
    </FocusCtx.Provider>
  );
}

const GRAPH_ACTION_BTN =
  "inline-flex items-center gap-1.5 rounded-md border border-white/15 px-2 py-1 text-[11px] font-medium text-[#cfd6e4] transition-colors hover:bg-white/5";

/** The graph itself — a header (title, role filter, and an optional
 * `headerRight` action) over the ReactFlow canvas. Reused by the inline dialog
 * and the full-page `/graph` route. */
export function PolicyGraphView({
  projectId,
  roleNames,
  enabled = true,
  headerRight,
}: {
  projectId: string;
  roleNames: string[];
  enabled?: boolean;
  headerRight?: React.ReactNode;
}) {
  const [role, setRole] = useState<string>("");
  const graph = usePolicyGraph(projectId, role || undefined, enabled);

  return (
    <div className="flex h-full flex-col" style={{ background: "#0a0d14" }}>
      <div className="flex flex-row items-center justify-between gap-3 border-b border-white/10 px-4 py-3">
        <div className="flex items-center gap-2 text-sm text-[#e6e9ef]">
          <Network size={16} className="text-[#b57aa8]" />
          Policy graph
        </div>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-2 font-mono text-[11px] text-[#8a93a3]">
            role
            <select
              value={role}
              onChange={(e) => setRole(e.target.value)}
              className="rounded border border-white/15 bg-[#11151f] px-1.5 py-0.5 text-[11px] text-[#cfd6e4]"
            >
              <option value="">all roles</option>
              {roleNames.map((r) => (
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
          <div className="grid h-full place-items-center font-mono text-xs text-[#6b7382]">
            resolving graph…
          </div>
        )}
        {graph.isError && (
          <div className="grid h-full place-items-center px-8 text-center font-mono text-xs text-[#f5959f]">
            the modules don't compose — fix the lints, then reopen the graph.
          </div>
        )}
        {graph.data && graph.data.nodes.length > 0 && (
          <PolicyFlow graph={graph.data} />
        )}
        {graph.data && graph.data.nodes.length === 0 && (
          <div className="grid h-full place-items-center font-mono text-xs text-[#6b7382]">
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
        style={{ background: "#0a0d14" }}
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
