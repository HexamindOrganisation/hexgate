# order-desk agent: tools and roles

Layout: a single role-keyed file, `policy.yaml` (none yet).

## Tools

| Tool | Arguments | What it does |
|---|---|---|
| `view_orders` | `customer_id: string` | Lists a customer's orders. Read-only. |
| `lookup_order` | `order_id: string` | Shows one order. Read-only. |
| `refund_order` | `order_id: string`, `amount: number`, `currency: string` | Pays money back to the customer's card. Can only be undone by hand. |
| `wire_transfer` | `amount: number`, `iban: string` | Sends money to any bank account. Irreversible. |
| `send_email` | `to: list[string]`, `subject: string`, `body: string` | Sends an email to any address, internal or external. |
| `export_customers` | `format: string` | Exports every customer record, including personal data, as a file. |
| `delete_customer` | `customer_id: string` | Permanently deletes a customer and their history. Irreversible. |

## Roles

| Role | Who |
|---|---|
| `agent` | Front-line support staff, handling customers day to day. |
| `manager` | Supervises the agents. Can do everything an agent can, and handles escalations. |

Anyone else falls back to `default`.
