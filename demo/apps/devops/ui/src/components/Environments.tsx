import { useEffect, useRef, useState } from "react";

import type { ActivityEvent, EnvState, Service } from "@/lib/api";
import { cn } from "@/lib/utils";

const ENVS = ["staging", "prod"] as const;

type Health = "healthy" | "degraded" | "critical" | "restarting" | "deleted";

export function health(d: EnvState): Health {
  if (d.status === "deleted") return "deleted";
  if (d.status === "restarting") return "restarting";
  if (d.error_rate >= 10) return "critical";
  if (d.status === "degraded" || d.error_rate >= 2) return "degraded";
  return "healthy";
}

function healthText(d: EnvState, h: Health): string {
  if (h === "deleted") return "environment deleted";
  if (h === "restarting") return "restarting…";
  if (h === "critical") return `error rate ${d.error_rate.toFixed(0)}%`;
  if (h === "degraded") return `elevated errors ${d.error_rate.toFixed(1)}%`;
  return "healthy";
}

function spark(history: number[]): string {
  const max = Math.max(5, ...history);
  return history
    .map(
      (v, i) =>
        `${((i * 72) / (history.length - 1)).toFixed(1)},${(24 - (v / max) * 21).toFixed(1)}`,
    )
    .join(" ");
}

/** One service in one environment. Flashes when its version, size or health changes. */
function Cell({ d }: { d: EnvState }) {
  const h = health(d);
  const sig = `${d.version}|${d.replicas}|${h}`;
  const prev = useRef(sig);
  const [flash, setFlash] = useState(0);

  useEffect(() => {
    if (prev.current === sig) return;
    prev.current = sig;
    setFlash((n) => n + 1);
    const t = setTimeout(() => setFlash(0), 1200);
    return () => clearTimeout(t);
  }, [sig]);

  return (
    <div className={cn("cell", h, flash > 0 && "flash")}>
      <div className="info">
        <span className="ver">{d.version}</span>
        <span className="health">
          <span className="dot" />
          <span className="ht">
            {healthText(d, h)} · {d.replicas} {d.replicas === 1 ? "replica" : "replicas"}
          </span>
        </span>
      </div>
      <svg className="spark" width="72" height="26" viewBox="0 0 72 26" aria-hidden>
        <polyline
          points={spark(d.history)}
          fill="none"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinejoin="round"
        />
      </svg>
    </div>
  );
}

function hhmm(ts: number): string {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export function Environments({
  services,
  events,
}: {
  services: Service[];
  events: ActivityEvent[];
}) {
  return (
    <main className="flex min-w-0 flex-[999_1_520px] flex-col gap-6 px-8 py-7">
      <header className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <h1 className="text-[22px] font-semibold tracking-tight">Environments</h1>
          <span className="sub">{services.length} services · live</span>
        </div>
        <div className="ml-auto flex flex-wrap gap-2">
          <span className="region">staging · eu-west-1</span>
          <span className="region">prod · eu-west-1</span>
        </div>
      </header>

      <div className="box overflow-x-auto">
        <div className="min-w-[620px]">
          <div className="row head">
            <span>Service</span>
            <span>Staging</span>
            <span>Production</span>
          </div>
          {services.map((s) => (
            <div key={s.name} className="row" data-svc={s.name}>
              <div className="flex flex-col gap-0.5">
                <b className="font-semibold">{s.name}</b>
                <span className="text-xs text-faint">{s.team}</span>
              </div>
              {ENVS.map((env) => (
                <Cell key={env} d={s.envs[env]} />
              ))}
            </div>
          ))}
        </div>
      </div>

      <section aria-labelledby="act-h">
        <h2 id="act-h" className="mb-2.5 text-sm font-semibold text-ink-2">
          Activity
        </h2>
        <ol id="activity" className="box">
          {events.map((e, i) => (
            <li
              key={`${e.at}-${e.text}-${i}`}
              className="flex items-center gap-3 border-b border-line-soft px-4 py-2.5 text-[13px] last:border-b-0"
            >
              <time className="w-11 shrink-0 font-mono text-xs text-faint">{hhmm(e.at)}</time>
              <span className={`dot size-2 k-${e.kind}`} />
              <span className="min-w-0 grow">{e.text}</span>
              <span className="text-xs whitespace-nowrap text-faint">{e.who}</span>
            </li>
          ))}
        </ol>
      </section>
    </main>
  );
}
