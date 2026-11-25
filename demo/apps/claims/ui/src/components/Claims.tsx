import { useEffect, useRef, useState } from "react";

import type { ActivityEvent, Claim } from "@/lib/api";
import { cn, eur } from "@/lib/utils";

const FLASH_MS = 1200;

/** One claim file. Flashes when its payment, bank details or tags change. */
function ClaimCard({ claim }: { claim: Claim }) {
  const sig = `${claim.paid_eur}|${claim.iban}|${claim.flags.join(",")}`;
  const prev = useRef(sig);
  const [flash, setFlash] = useState(false);

  useEffect(() => {
    if (prev.current === sig) return;
    prev.current = sig;
    setFlash(true);
    const t = setTimeout(() => setFlash(false), FLASH_MS);
    return () => clearTimeout(t);
  }, [sig]);

  const paid = claim.status === "paid";
  return (
    <article
      className={cn("claim", claim.flags.length > 0 && "flagged", flash && "flash")}
      data-claim={claim.id}
    >
      <div className="flex items-center gap-2">
        <b className="font-mono text-[13px]">{claim.id}</b>
        <span className="text-xs text-faint">assigned to {claim.assigned_to}</span>
        <span className={cn("status", paid && "paid")}>
          {paid ? `Paid ${eur(claim.paid_eur)}` : "Open"}
        </span>
      </div>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
        <span className="font-semibold">{claim.policyholder}</span>
        <span className="text-[13px] text-muted">{claim.type}</span>
        <span className="ml-auto text-[13px] text-muted">
          Assessed <b className="text-ink">{eur(claim.assessed_eur)}</b>
        </span>
      </div>
      <div className="bank">
        <span className="text-xs text-faint">Bank details on file</span>
        <span className="font-mono text-xs">{claim.iban}</span>
        <span className="text-xs text-muted">{claim.account_holder}</span>
      </div>
      {(claim.bank_details_changed || claim.flags.length > 0) && (
        <div className="flex flex-wrap gap-1.5">
          {claim.bank_details_changed && <span className="tag changed">Bank details changed</span>}
          {claim.flags.map((f) => (
            <span key={f} className="tag flag">
              {f}
            </span>
          ))}
        </div>
      )}
    </article>
  );
}

function hhmm(ts: number): string {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export function Claims({
  claims,
  events,
  paidTotal,
}: {
  claims: Claim[];
  events: ActivityEvent[];
  paidTotal: number;
}) {
  return (
    <main className="flex min-w-0 flex-[999_1_520px] flex-col gap-6 px-8 py-7">
      <header className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <h1 className="text-[22px] font-semibold tracking-tight">Claims</h1>
          <span className="sub">{claims.length} claims · live</span>
        </div>
        <div className="ml-auto flex flex-col items-end gap-0.5">
          <span className="sub">Paid out today</span>
          <b id="paid-total" className="font-mono text-[22px] tracking-tight">
            {eur(paidTotal)}
          </b>
        </div>
      </header>

      <div className="grid grid-cols-[repeat(auto-fill,minmax(320px,1fr))] gap-3">
        {claims.map((c) => (
          <ClaimCard key={c.id} claim={c} />
        ))}
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
