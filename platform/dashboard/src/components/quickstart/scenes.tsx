/**
 * The animated illustrations beside each quickstart step. They are drawings of
 * the flow, not live data: one-shot CSS animations (the `qs-*` classes in
 * index.css) staggered with a `--qs-d` delay. The dialog remounts a scene to
 * replay it. Every scene's resting frame is its final state, so reduced motion
 * just shows the end.
 */

import type { CSSProperties, ReactNode } from "react";
import { MousePointer2 } from "lucide-react";

import { cn } from "@/lib/utils";
import { REGISTER, type Framework } from "@/components/quickstart/content";

/** Delay (seconds) for a qs-* animation. */
function d(seconds: number, extra?: Record<string, string | number>) {
  return { "--qs-d": `${seconds}s`, ...extra } as CSSProperties;
}

/** Typewriter style: delay + character count. */
function typed(seconds: number, text: string) {
  return d(seconds, { "--qs-n": text.length });
}

const NAV = ["Agents", "Policies", "Playground", "Audit", "API keys"];

/** A browser window around a sketch of a dashboard page. */
function Win({
  url,
  active,
  children,
  className,
}: {
  url: string;
  active: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "overflow-hidden rounded-lg border border-border bg-card shadow-xl shadow-black/10",
        className,
      )}
    >
      <div className="flex items-center gap-2 border-b border-border bg-muted/60 px-3 py-1.5">
        <span className="flex gap-1">
          <i className="size-2 rounded-full bg-border" />
          <i className="size-2 rounded-full bg-border" />
          <i className="size-2 rounded-full bg-border" />
        </span>
        <span className="min-w-0 flex-1 truncate rounded bg-card px-2 py-0.5 font-mono text-[10px] text-muted-foreground">
          {url}
        </span>
      </div>
      <div className="grid grid-cols-[86px_minmax(0,1fr)]">
        <div className="flex flex-col gap-0.5 bg-muted/30 p-1.5 text-[10px]">
          {NAV.map((n) => (
            <span
              key={n}
              className={cn(
                "rounded px-1.5 py-1",
                n === active
                  ? "bg-primary/15 font-medium text-primary"
                  : "text-muted-foreground",
              )}
            >
              {n}
            </span>
          ))}
        </div>
        <div className="min-w-0 p-3">{children}</div>
      </div>
    </div>
  );
}

/** A dark terminal panel. */
function Term({
  children,
  className,
  style,
}: {
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <div
      style={style}
      className={cn(
        "overflow-hidden rounded-lg border border-white/10 bg-[#0a0d14] font-mono text-[11px] leading-relaxed text-slate-200 shadow-xl shadow-black/20",
        className,
      )}
    >
      <div className="flex gap-1 border-b border-white/10 px-3 py-1.5">
        <i className="size-2 rounded-full bg-white/15" />
        <i className="size-2 rounded-full bg-white/15" />
        <i className="size-2 rounded-full bg-white/15" />
      </div>
      <div className="p-3">{children}</div>
    </div>
  );
}

function Pill({
  kind,
  children,
  className,
  style,
}: {
  kind: "allow" | "deny" | "warn";
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <span
      style={style}
      className={cn(
        "inline-block rounded-full px-2 py-0.5 font-mono text-[10px] font-semibold",
        kind === "allow" && "bg-allow/15 text-allow",
        kind === "deny" && "bg-deny/15 text-deny",
        kind === "warn" && "bg-approval/15 text-approval",
        className,
      )}
    >
      {children}
    </span>
  );
}

/** Two children stacked in one grid cell — for a frame that replaces another. */
function Swap({ from, to }: { from: ReactNode; to: ReactNode }) {
  return (
    <span className="inline-grid [&>*]:[grid-area:1/1]">
      {from}
      {to}
    </span>
  );
}

/** The hexagon-and-keyhole gate. */
function Gate({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 48 54"
      className={cn("qs-pulse h-16 w-14 text-primary", className)}
      aria-hidden="true"
    >
      <path
        d="M24 2 45 14v26L24 52 3 40V14z"
        fill="hsl(var(--primary) / 0.12)"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinejoin="round"
      />
      <circle cx="24" cy="23" r="5.5" fill="currentColor" />
      <path d="M21.5 26h5l1.5 11h-8z" fill="currentColor" />
    </svg>
  );
}

function Connector({ delay = 0 }: { delay?: number }) {
  return (
    <svg
      viewBox="0 0 40 8"
      className="qs-in h-2 w-6 text-muted-foreground sm:w-10"
      style={d(delay)}
      aria-hidden="true"
    >
      <line
        x1="0"
        y1="4"
        x2="40"
        y2="4"
        stroke="currentColor"
        strokeWidth="2"
        strokeDasharray="5 3"
        className="qs-flow"
      />
    </svg>
  );
}

// ── Step 1 · the idea ───────────────────────────────────────────────────────

export function IdeaScene() {
  const lanes = [
    {
      role: "support",
      verdict: "deny" as const,
      label: "DENY",
      why: "args.amount <= 50 failed",
    },
    {
      role: "billing",
      verdict: "allow" as const,
      label: "ALLOW",
      why: "within args.amount <= 500",
    },
  ];
  return (
    <div className="flex h-full flex-col justify-center gap-4">
      <div className="grid grid-cols-[minmax(0,1fr)_auto_auto_auto_minmax(0,1fr)] items-center gap-y-6">
        {lanes.map((lane, i) => (
          <div key={lane.role} className="contents">
            <div
              className="qs-travel rounded-lg border border-border bg-card p-2.5 shadow-sm"
              style={d(0.2 + i * 0.35)}
            >
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                caller · <span className="font-mono">{lane.role}</span>
              </div>
              <div className="mt-0.5 truncate font-mono text-[11px]">
                refund_order($400)
              </div>
            </div>
            <Connector delay={0.8 + i * 0.35} />
            {i === 0 ? (
              <div className="row-span-2 flex flex-col items-center gap-1 px-1">
                <Gate />
                <span className="text-[10px] font-medium text-muted-foreground">
                  policy
                </span>
              </div>
            ) : null}
            <Connector delay={1.3 + i * 0.35} />
            <div
              className={cn(
                "qs-pop rounded-lg p-2.5 font-mono text-[10.5px]",
                lane.verdict === "allow"
                  ? "bg-allow/12 text-allow"
                  : "bg-deny/12 text-deny",
              )}
              style={d(1.6 + i * 0.35)}
            >
              <div className="font-semibold">
                {lane.verdict === "allow" ? "✓" : "✗"} {lane.label}
              </div>
              <div className="truncate opacity-80">{lane.why}</div>
            </div>
          </div>
        ))}
      </div>
      <p
        className="qs-in text-center text-xs text-muted-foreground"
        style={d(2.5)}
      >
        Same agent, same tool, same request. The caller's role decides.
      </p>
    </div>
  );
}

// ── Step 2 · API key ────────────────────────────────────────────────────────

export function KeyScene() {
  const token = "fty_live_custsupport_En8x…q7Zk";
  return (
    <div className="flex h-full flex-col justify-center">
      <Win url="app.hexgate.ai/tokens" active="API keys">
        <div className="flex items-start justify-between gap-2">
          <div>
            <div className="text-xs font-semibold">API keys</div>
            <div className="text-[10px] text-muted-foreground">
              Keys your agents use to reach this project.
            </div>
          </div>
          <span className="relative">
            <span
              className="qs-press inline-block rounded-md bg-primary px-2 py-1 text-[10px] font-medium text-primary-foreground"
              style={d(1.05)}
            >
              Mint new API key
            </span>
            <MousePointer2
              className="qs-cursor absolute -bottom-3 right-2 size-4 fill-foreground text-background"
              style={d(0.1)}
              aria-hidden="true"
            />
          </span>
        </div>
        <div
          className="qs-in mt-3 rounded-md border border-approval/40 bg-approval/10 p-2 text-[10px]"
          style={d(1.35)}
        >
          <b>Copy this key now.</b> You won't be able to see it again.
          <div className="mt-1.5 rounded bg-card px-2 py-1 font-mono">
            <span className="qs-type" style={typed(1.6, token)}>
              {token}
            </span>
          </div>
        </div>
        <div
          className="qs-in mt-3 grid grid-cols-[1fr_1.3fr_auto] gap-2 border-t border-border pt-2 text-[10px]"
          style={d(2.8)}
        >
          <span className="truncate">refund-agent-dev</span>
          <span className="truncate font-mono text-muted-foreground">
            fty_live_custsupport_…
          </span>
          <span className="text-muted-foreground">just now</span>
        </div>
      </Win>
      <Term className="qs-in mt-3" style={d(3.1)}>
        <span className="text-slate-500"># .env</span>
        <div>
          HEXGATE_API_KEY=<span className="text-amber-300">fty_live_…</span>
        </div>
      </Term>
    </div>
  );
}

// ── Step 3 · register + wrap ────────────────────────────────────────────────

export function RegisterScene({ framework }: { framework: Framework }) {
  // The first line of the command is enough to read; the LangChain variant
  // wraps onto a second line in the code panel beside it.
  const cmd = REGISTER[framework].split(" \\")[0];
  return (
    <div className="flex h-full flex-col justify-center gap-3">
      <Term>
        <span className="text-emerald-400">$ </span>
        <span className="qs-type" style={typed(0.2, cmd)}>
          {cmd}
        </span>
      </Term>
      <Win url="app.hexgate.ai/agents" active="Agents">
        <div className="text-xs font-semibold">Agents</div>
        <div className="mb-2 text-[10px] text-muted-foreground">
          Every agent registered in this project.
        </div>
        <div
          className="qs-in grid grid-cols-[1fr_1fr_auto] items-center gap-2 rounded-md border border-border p-2 text-[10px]"
          style={d(0.4 + cmd.length * 0.035)}
        >
          <span className="truncate font-mono font-semibold">refund_agent</span>
          <span className="truncate font-mono text-muted-foreground">
            refund_order
          </span>
          <Pill
            kind="warn"
            className="qs-pop"
            style={d(0.9 + cmd.length * 0.035)}
          >
            starter policy
          </Pill>
        </div>
        <div
          className="qs-in mt-2 text-[10px] text-muted-foreground"
          style={d(1.3 + cmd.length * 0.035)}
        >
          Wrapped with one adapter call ·{" "}
          <span className="font-mono">HexgateContext</span> carries the caller
        </div>
      </Win>
    </div>
  );
}

// ── Step 4 · policy on the platform ─────────────────────────────────────────

const YAML_LINES: [string, string?][] = [
  ["version: 1"],
  ["roles:"],
  ["  support:", "# small USD refunds"],
  ["    default_policy: { mode: deny }"],
  ["    tools:"],
  ["      refund_order:"],
  ["        mode: allow"],
  ["        constraints:"],
  ["          - args.amount <= 50"],
  ['          - args.currency == "USD"'],
  ["  billing:", "# larger, major currencies"],
  ["    default_policy: { mode: deny }"],
  ["    tools:"],
  ["      refund_order:"],
  ["        mode: allow"],
  ["        constraints:"],
  ["          - args.amount <= 500"],
  ['          - args.currency in ["USD", "EUR"]'],
];

function Editor({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-hidden rounded-md bg-[#0a0d14] py-1.5 font-mono text-[9.5px] leading-[1.45] text-slate-200">
      {children}
    </div>
  );
}

function Line({
  n,
  children,
  className,
  style,
}: {
  n: number;
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <div className={cn("whitespace-pre px-2", className)} style={style}>
      <span className="inline-block w-5 select-none text-slate-600">{n}</span>
      {children}
    </div>
  );
}

export function PolicyScene() {
  const saveAt = 0.6 + YAML_LINES.length * 0.07;
  return (
    <div className="flex h-full flex-col justify-center">
      <Win url="app.hexgate.ai/policies" active="Policies">
        <div className="mb-2 flex items-center justify-between gap-2">
          <div className="truncate text-xs font-semibold">
            Policies · <span className="font-mono">refund_agent</span>
          </div>
          <span
            className="qs-press rounded-md bg-primary px-2 py-1 text-[10px] font-medium text-primary-foreground"
            style={d(saveAt)}
          >
            Save
          </span>
        </div>
        <Editor>
          <Swap
            from={
              <Line n={1} className="qs-out text-slate-500" style={d(0.3)}>
                # starter policy from hexgate register
              </Line>
            }
            to={
              <Line n={1} className="qs-in" style={d(0.5)}>
                {YAML_LINES[0][0]}
              </Line>
            }
          />
          {YAML_LINES.slice(1).map(([code, comment], i) => (
            <Line
              key={i}
              n={i + 2}
              className="qs-in"
              style={d(0.5 + (i + 1) * 0.07)}
            >
              <span className="text-sky-300">{code}</span>
              {comment ? (
                <span className="text-slate-500"> {comment}</span>
              ) : null}
            </Line>
          ))}
        </Editor>
        <div
          className="qs-pop mt-2 flex flex-wrap gap-x-3 text-[10px] text-muted-foreground"
          style={d(saveAt + 0.35)}
        >
          <b className="text-allow">✓ Valid</b>
          <span>Signed</span>
          <span>Applies on the agent's next run</span>
        </div>
      </Win>
    </div>
  );
}

// ── Step 5 · run as two roles ───────────────────────────────────────────────

function AuditRow({
  role,
  verdict,
  reason,
  delay,
}: {
  role: string;
  verdict: "allow" | "deny";
  reason: string;
  delay: number;
}) {
  return (
    <div
      className="qs-in grid grid-cols-[52px_minmax(0,1fr)_auto] items-center gap-2 border-t border-border py-1.5 text-[10px]"
      style={d(delay)}
    >
      <span className="font-mono">{role}</span>
      <span className="truncate font-mono text-muted-foreground">{reason}</span>
      <Pill kind={verdict}>{verdict.toUpperCase()}</Pill>
    </div>
  );
}

export function RunScene() {
  const cmd = "python guarded_agent.py";
  return (
    <div className="flex h-full flex-col justify-center gap-3">
      <Term>
        <span className="text-emerald-400">$ </span>
        <span className="qs-type" style={typed(0.2, cmd)}>
          {cmd}
        </span>
        <div className="qs-in mt-1" style={d(1.3)}>
          <span className="text-red-400">[support]</span> I can't issue this
          refund: $400 is above the $50 limit for your role.
        </div>
        <div className="qs-in" style={d(2)}>
          <span className="text-emerald-400">[billing]</span> Done. I refunded
          400.0 USD on order O-42.
        </div>
      </Term>
      <Win url="app.hexgate.ai/audit" active="Audit">
        <div className="text-xs font-semibold">Audit</div>
        <div className="mb-1 text-[10px] text-muted-foreground">
          Every policy decision in this project.
        </div>
        <AuditRow
          role="support"
          verdict="deny"
          reason="refund_order(400, USD) · amount <= 50 failed"
          delay={2.6}
        />
        <AuditRow
          role="billing"
          verdict="allow"
          reason="refund_order(400, USD) · all constraints passed"
          delay={3}
        />
      </Win>
    </div>
  );
}

// ── Step 6 · live edit, no redeploy ─────────────────────────────────────────

export function LiveScene() {
  return (
    <div className="flex h-full flex-col justify-center gap-3">
      <Win url="app.hexgate.ai/policies" active="Policies">
        <div className="mb-2 flex items-center justify-between gap-2">
          <div className="truncate text-xs font-semibold">
            Policies · <span className="font-mono">refund_agent</span>
          </div>
          <span
            className="qs-press rounded-md bg-primary px-2 py-1 text-[10px] font-medium text-primary-foreground"
            style={d(1.5)}
          >
            Save
          </span>
        </div>
        <Editor>
          <Line n={3}>
            <span className="text-sky-300"> support:</span>
          </Line>
          <Line n={6}>
            <span className="text-sky-300"> refund_order:</span>
          </Line>
          <Line n={8}>
            <span className="text-sky-300"> constraints:</span>
          </Line>
          <Swap
            from={
              <Line n={9} className="qs-out" style={d(0.6)}>
                <span className="text-sky-300"> - args.amount {"<="} </span>
                <span className="text-amber-300">50</span>
              </Line>
            }
            to={
              <Line n={9} className="qs-in qs-mark" style={d(0.8)}>
                <span className="text-sky-300"> - args.amount {"<="} </span>
                <span className="text-amber-300">500</span>
              </Line>
            }
          />
        </Editor>
      </Win>
      <Term className="qs-in" style={d(2)}>
        <span className="text-emerald-400">$ </span>python guarded_agent.py
        <span className="text-slate-500"> # same code, next run</span>
        <div className="qs-in mt-1" style={d(2.5)}>
          <span className="text-emerald-400">[support]</span> Done. I refunded
          400.0 USD on order O-42.
        </div>
      </Term>
      <div
        className="qs-in flex items-center justify-between rounded-lg border border-border bg-card px-3 py-2 text-[10px]"
        style={d(2.8)}
      >
        <span className="font-mono">support · refund_order(400, USD)</span>
        <Swap
          from={
            <Pill kind="deny" className="qs-out" style={d(3.1)}>
              DENY
            </Pill>
          }
          to={
            <Pill kind="allow" className="qs-pop" style={d(3.25)}>
              ALLOW
            </Pill>
          }
        />
      </div>
    </div>
  );
}
