I updated the `billing` role's `refund_order` rule in `/workspace/policy.yaml`. Gold customers can now be refunded up to 1000. Everyone else stays at the existing 500 cap. `policy validate` passed with no warnings, run against the manifest in `agents.json`. I ran every case below with `--engine wasm`, and each result matched what I expected.

```yaml
  billing:
    inherits: [read_only]
    tools:
      refund_order:
        mode: allow
        constraints:
          - (ctx.customer_tier == "gold" and args.amount <= 1000) or args.amount <= 500
          - args.currency in ["USD", "EUR"]
```

| Case (role `billing`) | Result |
|---|---|
| gold, 1000 USD (limit, inclusive) | ALLOW |
| gold, 1000.01 USD | DENY |
| gold, 500 USD | ALLOW |
| silver, 1000 USD | DENY |
| silver, 500 EUR | ALLOW |
| silver, 501 USD | DENY |
| no tier attribute, 500 USD | ALLOW |
| no tier attribute, 501 USD | DENY |
| gold, 1000 GBP (currency rule still applies) | DENY |
| gold, amount `"1000"` as a string | DENY |
| tier `"Gold"` (case mismatch) | DENY |
| `support` role, any refund | DENY |

**Assumptions to confirm**
- **Where the tier comes from:** `refund_order` has no tier argument (only `order_id`, `amount` and `currency`), so I used a caller attribute, `ctx.customer_tier`. Your server code has to set it to the tier of the customer being refunded.
- **Trust in the attribute:** the 1000 limit is only as trustworthy as whatever sets `ctx.customer_tier`. If a model or user can influence it, they can raise their own cap.
- **Other tiers:** you only specified gold, so every other tier, and a missing tier, stays at 500. That also means a missing attribute fails safe to 500.
- **Limit and currency:** I treated 1000 as inclusive, as the existing 500 was. It is the same number in USD and EUR, with no conversion.
- **Tier name:** the tier value is a lowercase `"gold"` match, so `"Gold"` is denied.
- **Approval alternative:** if you want refunds above 500 for non-gold customers to go to a person for approval instead of being denied, I can add that.
