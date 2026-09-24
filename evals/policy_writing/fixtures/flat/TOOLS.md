# support-bot: tools and caller attributes

Layout: a single role-keyed file, `policy.yaml`.

## Tools

| Tool | Arguments |
|---|---|
| `web_search` | `query: string` |
| `view_orders` | `customer_id: string` |
| `refund_order` | `order_id: string`, `amount: number`, `currency: string` (ISO code) |
| `wire_transfer` | `amount: number`, `iban: string` |
| `send_email` | `to: list[string]` (addresses), `subject: string`, `body: string` |
| `read_file` | `file_path: string` (relative to the workspace root) |

Outbound HTTP(S) from the agent's process is gated as `net.http_request`
(`host`, `scheme`, `port`).

## Roles

`default`, `support`, `billing`.

## Caller attributes (`ctx.*`)

| Attribute | Type | Example |
|---|---|---|
| `department` | string | `"finance"`, `"support"` |
| `clearance_level` | int | `3` |
