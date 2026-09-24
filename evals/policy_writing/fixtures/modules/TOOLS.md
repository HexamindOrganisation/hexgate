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

## Roles

`default`, `support`, `billing`.
