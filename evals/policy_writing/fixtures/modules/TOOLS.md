# support-bot (module layout): tools and roles

Layout: a policy module tree.

- `policies/boundaries/*.yaml`: security-owned ceilings and hard denies. They apply to every role.
- `policies/capabilities/*.yaml`: team-owned grants.
- `roles.yaml`: maps each role to the capabilities it gets.

## Tools

| Tool | Arguments |
|---|---|
| `view_orders` | `customer_id: string` |
| `lookup_order` | `order_id: string` |
| `refund_order` | `order_id: string`, `amount: number`, `currency: string` |
| `issue_credit` | `customer_id: string`, `amount: number` |
| `send_email` | `to: list[string]`, `subject: string`, `body: string` |
| `escalate` | `reason: string` |
| `delete_database` | `name: string` |
| `delete_customer` | `customer_id: string` |

## Other agents this agent can reach

Each can be reached as a tool (the result comes back to this agent) or by
handoff (the conversation is transferred to it).

| Agent | What it does |
|---|---|
| `billing-bot` | Billing questions and invoices |
| `admin-bot` | Account administration |

## Roles

`default`, `support`, `billing`.
