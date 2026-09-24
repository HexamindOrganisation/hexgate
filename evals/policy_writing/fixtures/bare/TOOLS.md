# order-desk agent: structure

Layout: a single policy file, `policy.yaml` (none yet).

## Tools

| Tool | Arguments |
|---|---|
| `view_orders` | `customer_id: string` |
| `lookup_order` | `order_id: string` |
| `refund_order` | `order_id: string`, `amount: number`, `currency: string` |
| `wire_transfer` | `amount: number`, `iban: string` |
| `send_email` | `to: list[string]`, `subject: string`, `body: string` |
| `export_customers` | `format: string` |
| `delete_customer` | `customer_id: string` |
