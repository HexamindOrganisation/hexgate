# order-desk agent: tools and roles

Layout: a single role-keyed file, `policy.yaml`. `org_defaults` holds the
defaults every role gets.

## Tools

| Tool | Arguments |
|---|---|
| `view_orders` | `customer_id: string` |
| `lookup_order` | `order_id: string` |
| `refund_order` | `order_id: string`, `amount: number`, `currency: string` |
| `send_email` | `to: list[string]`, `subject: string`, `body: string` |

Outbound HTTP(S) is gated as `net.http_request` (`host`, `scheme`, `port`).

## Roles

`default`, `support`, plus any the request adds.
