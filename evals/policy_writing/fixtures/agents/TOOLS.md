# front-desk agent: tools, sub-agents and roles

Layout: a single role-keyed file, `policy.yaml`.

## Tools

| Tool | Arguments |
|---|---|
| `view_orders` | `customer_id: string` |
| `send_email` | `to: list[string]`, `subject: string`, `body: string` |
| `refund_order` | `order_id: string`, `amount: number`, `currency: string` |

## Other agents this agent can reach

Each can be reached as a tool (the result comes back to this agent) or by
handoff (the conversation is transferred to it).

| Agent | What it does |
|---|---|
| `billing-bot` | Billing questions and invoices |
| `refund-bot` | Processes refunds |
| `admin-bot` | Account administration |

## Roles

`default`, `support`, `billing`.
