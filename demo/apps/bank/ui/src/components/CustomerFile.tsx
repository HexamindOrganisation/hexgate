import { useEffect, useRef, useState } from "react";

import type { ActivityEvent, BankAccount, Customer, Message } from "@/lib/api";
import { cn, eur } from "@/lib/utils";

const FLASH_MS = 1200;

/** One account. Flashes when its balance, state or tags change. */
function AccountCard({ account }: { account: BankAccount }) {
  const sig = `${account.balance_eur}|${account.closed}|${account.flags.join(",")}`;
  const prev = useRef(sig);
  const [flash, setFlash] = useState(false);

  useEffect(() => {
    if (prev.current === sig) return;
    prev.current = sig;
    setFlash(true);
    const t = setTimeout(() => setFlash(false), FLASH_MS);
    return () => clearTimeout(t);
  }, [sig]);

  return (
    <article
      className={cn(
        "account",
        account.flags.length > 0 && "flagged",
        account.closed && "closed",
        flash && "flash",
      )}
      data-account={account.id}
    >
      <div className="flex items-center gap-2">
        <b className="font-mono text-[13px]">{account.id}</b>
        <span className="text-xs text-faint capitalize">{account.kind} account</span>
        {account.closed && <span className="status closed">Closed</span>}
      </div>
      <b className="balance font-mono text-[22px] tracking-tight">{eur(account.balance_eur)}</b>
      {account.flags.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {account.flags.map((f) => (
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

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section aria-label={title}>
      <h2 className="mb-2.5 text-sm font-semibold text-ink-2">{title}</h2>
      {children}
    </section>
  );
}

export function CustomerFile({
  customers,
  openId,
  onOpen,
  inbox,
  events,
  movedOut,
}: {
  customers: Customer[];
  openId: string;
  onOpen: (customerId: string) => void;
  inbox: Message[];
  events: ActivityEvent[];
  movedOut: number;
}) {
  const customer = customers.find((c) => c.id === openId);
  return (
    <main className="flex min-w-0 flex-[999_1_520px] flex-col gap-6 px-8 py-7">
      <header className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1.5">
          <label htmlFor="customer" className="sub">
            Customer file
          </label>
          <select
            id="customer"
            className="input h-10 w-auto min-w-[240px] text-[15px] font-semibold"
            value={openId}
            onChange={(e) => onOpen(e.target.value)}
          >
            {customers.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name} · {c.id}
              </option>
            ))}
          </select>
          {customer && <span className="sub">{customer.email}</span>}
        </div>
        <div className="ml-auto flex flex-col items-end gap-0.5">
          <span className="sub">Moved out today</span>
          <b id="moved-out" className="font-mono text-[22px] tracking-tight">
            {eur(movedOut)}
          </b>
        </div>
      </header>

      {customer && (
        <>
          <Section title="Accounts">
            <div className="grid grid-cols-[repeat(auto-fill,minmax(240px,1fr))] gap-3">
              {customer.accounts.map((a) => (
                <AccountCard key={a.id} account={a} />
              ))}
            </div>
          </Section>

          <Section title="Registered beneficiaries">
            <ul className="box">
              {customer.beneficiaries.map((b) => (
                <li
                  key={b.iban}
                  className="flex flex-wrap items-center gap-x-3 border-b border-line-soft px-4 py-2.5 text-[13px] last:border-b-0"
                >
                  <span className="font-medium">{b.name}</span>
                  <span className="ml-auto font-mono text-xs text-muted">{b.iban}</span>
                </li>
              ))}
            </ul>
          </Section>
        </>
      )}

      <Section title="Branch inbox">
        <ul id="inbox" className="box">
          {inbox.map((m) => (
            <li
              key={m.id}
              className="flex flex-col gap-0.5 border-b border-line-soft px-4 py-2.5 text-[13px] last:border-b-0"
            >
              <div className="flex items-center gap-2">
                <b className="font-mono text-xs">{m.id}</b>
                <span className="font-medium">{m.from}</span>
                <span className="text-muted">{m.subject}</span>
              </div>
              <span className="text-xs text-faint">{m.preview}</span>
            </li>
          ))}
        </ul>
      </Section>

      <Section title="Activity">
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
      </Section>
    </main>
  );
}
