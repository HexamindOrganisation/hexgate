// Interactive companion to the quickstart's policy.yaml. It mirrors that
// policy's constraints by hand, so keep the RULES below in sync with the YAML
// copied in README.md, docs/introduction.mdx, docs/quickstart.mdx and
// platform/dashboard/src/components/quickstart/content.ts. It explains; the
// SDK decides.
export const PolicyDecider = () => {
  const RULES = {
    support: [
      { expr: "args.amount <= 50", ok: (a, c) => a <= 50 },
      { expr: 'args.currency == "USD"', ok: (a, c) => c === "USD" },
    ],
    billing: [
      { expr: "args.amount <= 500", ok: (a, c) => a <= 500 },
      { expr: 'args.currency in ["USD", "EUR"]', ok: (a, c) => c === "USD" || c === "EUR" },
    ],
  };
  const [role, setRole] = useState("support");
  const [currency, setCurrency] = useState("USD");
  const [amount, setAmount] = useState(400);

  const checks = RULES[role].map((r) => ({ expr: r.expr, pass: r.ok(amount, currency) }));
  const failed = checks.find((c) => !c.pass);
  const allow = !failed;
  const call = `refund_order({"amount": ${amount}, "currency": "${currency}"})`;

  const seg = (options, value, onChange, label) => (
    <div role="group" aria-label={label} className="flex overflow-hidden rounded-lg border border-zinc-950/10 dark:border-white/10">
      {options.map((o) => (
        <button
          key={o}
          type="button"
          aria-pressed={value === o}
          onClick={() => onChange(o)}
          className={
            "flex-1 px-3 py-1.5 font-mono text-sm transition-colors " +
            (value === o
              ? "bg-primary text-white dark:bg-primary-light dark:text-zinc-950"
              : "text-zinc-600 hover:bg-zinc-950/5 dark:text-zinc-300 dark:hover:bg-white/5")
          }
        >
          {o}
        </button>
      ))}
    </div>
  );

  return (
    <div className="not-prose my-6 rounded-2xl border border-zinc-950/10 p-5 dark:border-white/10">
      <p className="m-0 text-base font-semibold text-zinc-900 dark:text-zinc-100">What would this policy decide?</p>
      <p className="mb-4 mt-1 text-sm text-zinc-500 dark:text-zinc-400">
        Change the request. Each role's constraints are applied as you go.
      </p>
      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <p className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-zinc-500">Caller role</p>
          {seg(["support", "billing"], role, setRole, "Caller role")}
        </div>
        <div>
          <p className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-zinc-500">Currency</p>
          {seg(["USD", "EUR", "GBP"], currency, setCurrency, "Currency")}
        </div>
        <div className="sm:col-span-2">
          <label htmlFor="pd-amount" className="mb-1.5 block text-xs font-semibold uppercase tracking-wider text-zinc-500">
            Refund amount
          </label>
          <div className="flex items-center gap-3">
            <input
              id="pd-amount"
              type="range"
              min="0"
              max="1000"
              step="10"
              value={amount}
              onChange={(e) => setAmount(Number(e.target.value))}
              className="min-w-0 flex-1 accent-current text-primary dark:text-primary-light"
            />
            <output htmlFor="pd-amount" className="w-14 text-right font-mono font-semibold tabular-nums text-zinc-900 dark:text-zinc-100">
              {amount}
            </output>
          </div>
        </div>
      </div>
      <div
        aria-live="polite"
        className={
          "mt-4 rounded-xl px-4 py-3 font-mono text-[13px] leading-relaxed break-words " +
          (allow
            ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
            : "bg-red-500/10 text-red-700 dark:text-red-400")
        }
      >
        <div className="font-semibold">
          {allow ? "✓ ALLOW" : "✗ DENY"} · {role} → {call}
        </div>
        {failed && (
          <div className="text-zinc-700 dark:text-zinc-300">
            reason: Policy on "refund_order" denied: constraint failed — {failed.expr}
          </div>
        )}
      </div>
      <ul className="m-0 mt-3 list-none space-y-1 p-0 font-mono text-xs">
        {checks.map((c) => (
          <li key={c.expr} className={"m-0 flex gap-2 " + (c.pass ? "text-zinc-500" : "text-zinc-900 dark:text-zinc-100")}>
            <b className={c.pass ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400"}>{c.pass ? "✓" : "✗"}</b>
            {c.expr}
          </li>
        ))}
      </ul>
    </div>
  );
};
