# refund-desk agent: tools and roles

Layout: a single role-keyed file, `policy.yaml` (none yet). Another agent's
policy is in `other_agents/billing-bot.yaml`; don't edit it.

## Tools

| Tool | Arguments |
|---|---|
| `view_orders` | `customer_id: string` |
| `refund_order` | `order_id: string`, `amount: number`, `currency: string` |
| `wire_transfer` | `amount: number`, `iban: string` |
| `send_email` | `to: list[string]`, `subject: string`, `body: string` |

## Roles

`default`, `billing`.
