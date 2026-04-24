# Agent Security Platform — UI Design Spec

Design direction for the Fortify dashboard. Scoped for a single designer/AI pass; opinionated where it needs to be.

## 1. Vibe

> **Enterprise, security-oriented, agent-era, developer-tooling.**
> Calm, dense, confident. Information-rich without being cluttered. The UI should feel like it protects something valuable.

**Reference anchors** (in priority order):
- Linear — layout density, muted palette, typography discipline
- Stripe dashboard — trust signals, informational hierarchy, keys and code UX
- Vercel — minimalist cards, subtle gradients, dark-mode-first
- Cloudflare dashboard — security-product legitimacy, status indicators
- Tailscale admin — clean nav, node/graph displays

**Avoid:** playful illustrations, rounded-cartoonish shapes, overly saturated gradients, AI-stereotype sparkles-everywhere.

---

## 2. Color system

**Direction:** cool blue-indigo primary, slate neutrals, semantic green/red/amber for graph states. Dark mode is the default; light mode must work but isn't the hero.

### shadcn/ui CSS variables

#### Dark mode (default)

```css
:root.dark {
  --background: 222 33% 5%;           /* #0B0F17 — deep cool slate */
  --foreground: 210 40% 96%;          /* #F1F5F9 */

  --card: 222 28% 8%;                 /* #11151F */
  --card-foreground: 210 40% 96%;

  --popover: 222 28% 8%;
  --popover-foreground: 210 40% 96%;

  --primary: 226 78% 65%;             /* #5B7FFF — indigo-blue, slightly cool */
  --primary-foreground: 222 33% 5%;

  --secondary: 222 20% 14%;           /* #1A1F2B */
  --secondary-foreground: 210 40% 96%;

  --muted: 222 20% 12%;               /* #161B25 */
  --muted-foreground: 215 20% 60%;    /* #8896A6 */

  --accent: 222 20% 14%;
  --accent-foreground: 210 40% 96%;

  --destructive: 0 72% 58%;           /* #E54747 */
  --destructive-foreground: 210 40% 96%;

  --border: 222 20% 16%;              /* #1F2533 */
  --input: 222 20% 16%;
  --ring: 226 78% 65%;                /* same as primary for focus */
}
```

#### Light mode

```css
:root {
  --background: 210 40% 98%;          /* #F7F9FC — slight cool tint, not white */
  --foreground: 222 47% 11%;          /* #0F172A */

  --card: 0 0% 100%;
  --card-foreground: 222 47% 11%;

  --primary: 226 70% 52%;             /* #2F4FD6 — darker indigo-blue in light */
  --primary-foreground: 210 40% 98%;

  --secondary: 210 40% 94%;           /* #EBF0F7 */
  --secondary-foreground: 222 47% 11%;

  --muted: 210 40% 94%;
  --muted-foreground: 215 16% 47%;    /* #64748B */

  --accent: 210 40% 94%;
  --accent-foreground: 222 47% 11%;

  --destructive: 0 70% 50%;
  --destructive-foreground: 210 40% 98%;

  --border: 215 20% 88%;              /* #D6DCE5 */
  --input: 215 20% 88%;
  --ring: 226 70% 52%;
}
```

### Semantic colors (for the policy graph)

These are **not** shadcn variables — they're a separate semantic layer used by the graph and status indicators.

```css
--semantic-allow: 142 71% 45%;        /* #1FB866 — emerald, confident green */
--semantic-allow-soft: 142 50% 20%;   /* dim version for fills */

--semantic-deny: 0 72% 58%;           /* #E54747 — reuses destructive */
--semantic-deny-soft: 0 50% 22%;

--semantic-approval: 38 92% 55%;      /* #F2A520 — amber, slightly orange */
--semantic-approval-soft: 38 60% 22%;
```

### When to use what

| Context | Color |
|---|---|
| Primary actions (Save, Publish, Deploy) | `--primary` |
| Dangerous actions (Revoke, Delete) | `--destructive` |
| Graph edge — allow | `--semantic-allow` |
| Graph edge — deny | `--semantic-deny` |
| Graph edge — approval_required | `--semantic-approval` |
| Success status badges | `--semantic-allow` |
| Failure / denied status badges | `--semantic-deny` |
| Muted text, secondary labels | `--muted-foreground` |
| Inactive nav items, placeholders | `--muted-foreground` at 70% opacity |

---

## 3. Typography

**Sans:** **Geist** (preferred) or **Inter** (fallback). Crisp, neutral, industry-standard for developer tooling.

**Mono:** **Geist Mono** or **JetBrains Mono**. Used for:
- API keys / tokens (always monospace — emphasizes literal value)
- Code snippets
- Project IDs
- Policy rule expressions

### Scale

| Role | Size | Weight | Tracking |
|---|---|---|---|
| Display (page titles) | 28px / 1.75rem | 600 | -0.02em |
| Heading 1 | 22px / 1.375rem | 600 | -0.01em |
| Heading 2 | 18px / 1.125rem | 600 | 0 |
| Heading 3 | 15px / 0.9375rem | 600 | 0 |
| Body | 14px / 0.875rem | 400 | 0 |
| Body small | 13px / 0.8125rem | 400 | 0 |
| Label / caption | 12px / 0.75rem | 500 | 0.01em |
| Mono body | 13px / 0.8125rem | 400 | 0 |

Line height: 1.5 for body, 1.3 for headings, 1.4 for mono.

### Tone in copy

- **Direct.** "Create project" not "Let's create your first project!"
- **Technical but clear.** "Token expires in 5 min" not "Your session will end soon"
- **No exclamation marks.** Ever.
- **No emoji.**
- **Error messages give the fact, not sympathy.** "Policy denied: amount > max_amount (50)" not "Sorry, that didn't work."

---

## 4. Iconography — Lucide v1

Use **Lucide React** (v1.x). Stroke-weight 1.5 for regular, 2 for emphasis. 16px default, 20px for primary nav.

### Concept → icon map

| Concept | Icon name |
|---|---|
| Project | `FolderKey` |
| Agent | `Bot` |
| Tool | `Wrench` |
| Role | `Users` |
| Policy / rule | `ShieldCheck` |
| Token / key | `KeyRound` |
| Dev token (long-lived) | `KeySquare` |
| User token (session) | `Ticket` |
| Audit / events | `ScrollText` |
| Playground | `MessageSquareCode` |
| Graph / resource map | `Network` or `Share2` |
| Allow (inline) | `Check` (in allow-green) |
| Deny (inline) | `X` (in deny-red) |
| Approval required | `CircleDashed` (in approval-amber) |
| Serve / live | `Radio` (pulsing when connected) |
| Copy (button) | `Copy` |
| Revoke / delete | `Trash2` |
| Settings | `Settings2` |
| Publish | `Upload` or `CheckCheck` |
| Draft / unpublished | `FileEdit` |
| Signature / crypto | `Fingerprint` |
| Control plane | `Server` |
| Dashboard home | `LayoutDashboard` |
| Docs | `BookOpen` |

Icons always pair with text in primary nav and buttons. Icon-only allowed in dense tables and inline status markers.

---

## 5. Layout

### App shell

```
┌──────────────────────────────────────────────────────────┐
│ [Logo] Fortify    Project: support-bot ▼      [user] [?] │  ← 56px top bar
├──────────┬───────────────────────────────────────────────┤
│          │                                                │
│ Sidebar  │                                                │
│ (220px)  │  Main content                                 │
│          │                                                │
│  Home    │                                                │
│  Graph   │                                                │
│  Play    │                                                │
│  Audit   │                                                │
│  Tokens  │                                                │
│  Settings│                                                │
│          │                                                │
└──────────┴───────────────────────────────────────────────┘
```

- **Top bar:** 56px. Logo left, project switcher center-left, user menu + help right. Subtle border-bottom using `--border`.
- **Sidebar:** 220px fixed (collapsible to 56px icon-only). Uses `--card` background. Active item uses `--primary` at 15% opacity + `--primary` text.
- **Main content:** max-width 1400px, centered, 32px horizontal padding.
- **8px grid.** All spacing, sizing, and positioning snaps to multiples of 4px (with 8px as the common unit).

### Right inspector panel

For the graph view and audit detail view, a **right-side panel** slides in (384px wide) when a node/edge/event is selected. Background `--card`, border-left `--border`.

---

## 6. Component style

### Cards

```
background:   var(--card)
border:       1px solid var(--border)
border-radius: 8px
padding:      24px
shadow:       none (light mode); subtle inner ring in dark mode
```

Avoid heavy drop shadows. Enterprise tools lean on borders and subtle background contrast instead.

### Buttons (shadcn defaults, with tweaks)

- **Primary:** filled `--primary` background, `--primary-foreground` text. Height 36px. Border-radius 6px.
- **Secondary:** `--secondary` bg, `--foreground` text.
- **Ghost:** transparent, hover fills `--accent`.
- **Destructive:** `--destructive` filled. Use sparingly, only for revoke/delete actions.

### Badges (for status)

Pill-shaped. 22px tall. Icon + label, 4px gap.

- **Allow:** bg `--semantic-allow-soft`, text/icon `--semantic-allow`
- **Deny:** bg `--semantic-deny-soft`, text/icon `--semantic-deny`
- **Approval:** bg `--semantic-approval-soft`, text/icon `--semantic-approval`
- **Live / serve active:** bg `--primary` at 15%, text `--primary`, pulsing dot

### Tables

- Row height 44px
- Border-bottom per row using `--border`
- Header row: uppercase, 11px, `--muted-foreground`, letter-spacing 0.05em
- Hover: `--accent` background, 100ms transition

### Code blocks / keys

- Background: `--muted`
- Font: mono
- Padding: 12px 16px
- Border-radius: 6px
- Copy button top-right (icon-only `Copy` lucide)

For secret values (tokens, keys): render as `fty_live_xxxx••••••••••` by default with a "Reveal" toggle.

---

## 7. The graph canvas (§9.2 of main POC doc)

This is the centerpiece of the product's visual identity. Spend effort here.

### Canvas

- Full-bleed main content area (no card wrapper)
- Subtle **dotted grid** background — dots 1px, 24px spacing, color `--border` at 40% opacity
- Zoom / pan standard (React Flow defaults)
- Minimap bottom-right: 180×120px, same styling as canvas
- Controls (zoom in/out/fit) bottom-left as a floating cluster

### Nodes

Two types: `Role` and `Tool`. **Visually distinguish them by shape and icon.**

**Role node:**
- Rounded rectangle, 160×56px
- Icon: `Users` (lucide), 20px, `--primary`
- Title: role label, 14px/600
- Subtle background: `--card`
- Border 1px `--border`, selected state: 2px `--primary`

**Tool node:**
- Rounded rectangle (same dimensions) but with a left-edge **colored strip** (4px wide) indicating category — optional; for POC, all tools use `--muted-foreground`
- Icon: `Wrench` (lucide), 20px, `--foreground`
- Title: tool label, 14px/600
- Subtitle: tool id in mono, 12px, `--muted-foreground`

### Edges

The visual storytelling unit. **Colors matter here.**

- **Allow:** stroke `--semantic-allow`, 2px solid
- **Deny:** stroke `--semantic-deny`, 2px solid
- **Approval required:** stroke `--semantic-approval`, 2px **dashed** (dash pattern: 6 4)
- **Unconstrained edge** (mode only, no constraint payload): slightly thinner, 1.5px
- **Constrained edge** (has constraint data): 2.5px + small icon badge mid-edge showing constraint summary (`≤$50`)
- **Hover:** stroke widens to 3px, subtle glow using stroke color at 30% opacity
- **Selected:** stroke `--primary`, stroke-width 3px, with constraint summary shown

Edge labels: rendered as small pills mid-edge, background `--card`, border matching edge color, font 11px mono.

### Inspector panel (when edge selected)

Right-side panel, 384px. Contents:
- Title: `Support → Refund Order` (14px/600)
- Mode selector: segmented control, three options with color indicators
- Constraints section: key-value rows, add/remove buttons
- Save / Cancel at bottom

---

## 8. Motion

**Principle:** subtle and damped. Motion tells the user "something changed" without demanding attention.

- **Transitions:** 150ms default. 100ms for hover states. 200ms for panel slides.
- **Easing:** `ease-out` for entering, `ease-in` for exiting. No bouncy springs.
- **Loading:** skeleton shimmers (2s cycle, `--muted` to `--secondary`). No spinners unless >1s.
- **Success flash:** a row that just saved pulses `--semantic-allow` at 10% opacity for 400ms.
- **Live indicator:** pulsing dot for `serve` active state — scale 1 → 1.3 → 1, 1.5s cycle.

---

## 9. Page-by-page specs

### 9.1 Dashboard home (`/`)

- Project picker at top (if multiple)
- Four KPI cards (stats across last 24h):
  - Allowed decisions (count, `--semantic-allow` accent number)
  - Denied decisions (count, `--semantic-deny` accent number)
  - Active agents (count, `Bot` icon)
  - Bundle version (current published version + pushed time)
- Recent audit events list (last 10, table style)
- Quick actions row: "Open Graph", "Open Playground", "Copy Key"

### 9.2 Graph / Resource Map (`/graph`)

- Full-height canvas
- Top-right toolbar: `+ Add Role`, `+ Add Tool`, divider, "Advanced (raw)" toggle
- Bottom-right: minimap
- Bottom-left: zoom controls
- Right: inspector panel (appears on selection)
- Top-left: "Draft" / "Published" indicator + "Publish" button when draft differs from published

### 9.3 Playground (`/playground`)

Three-column split:
- **Left (280px):** Session config — role selector (to simulate different user tokens), max tokens, temperature
- **Center (flex):** Chat interface — message stream, composer at bottom
- **Right (400px):** Live telemetry tabs
  - **Decisions:** stream of allow/deny events with timestamp, tool, reason
  - **Audit:** raw audit event JSON stream
  - **Tool calls:** expandable tool call tree (reuse `StreamEvent` rendering)

Serve status top-left: `Radio` icon in green pulsing when agent connected; gray `RadioOff` when not.

### 9.4 Audit (`/audit`)

- Filter bar: date range, role, tool, decision (allow/deny/approval)
- Table: timestamp (mono), role, tool, decision badge, token_id (truncated mono), reason (truncated)
- Click row → right panel with full event detail (JSON viewer)
- Export button (download as JSONL)

### 9.5 Tokens / Keys (`/tokens`)

- List of dev tokens: name, created, last used, scopes summary, copy/revoke
- "Mint new token" button → modal with scope form
- Show token **once** after mint, with prominent "Copy" and a warning that it won't be shown again

### 9.6 Settings (`/settings`)

- Project metadata: name, ID (mono, read-only), created
- Delete project (destructive, requires typing project ID)
- Webhook URL for audit events (future)
- Rotate signing key (future — disabled with tooltip for POC)

---

## 10. Empty states

Every list/view that can be empty needs a thoughtful empty state. Pattern:

- Centered, single lucide icon at 48px `--muted-foreground`
- One-line headline (15px/500)
- Two-line explanation (13px, `--muted-foreground`)
- One primary action button

Examples:
- **No projects:** `FolderKey` + "Create your first project" + "Projects contain policies, keys, and audit streams." + [Create Project] button
- **No policies:** `ShieldCheck` + "No rules yet" + "Add a role and connect it to a tool to grant permission." + [Open Graph Editor] button
- **No audit events:** `ScrollText` + "No events in this range" + "Audit events appear here as your agents act." + [Open Playground] button

---

## 11. The "first impression" moment

Crafted onboarding. When a user signs up and sees the empty dashboard, they should think *"this feels different."*

- Initial empty state of the main dashboard: a single centered card, `Shield` icon at 64px in `--primary`, "Welcome to Fortify", one-line tagline, single CTA "Create your first project."
- The key-mint modal after project creation shows the key in large mono, an `Fingerprint` icon, a one-liner: *"This is the only time we'll show it. Store it safely."*
- After the first policy is published, a small toast (top-right): `CheckCheck` + "Policy published — bundle v1 now serving."

Small moments. High craft. This is where "enterprise-polished" vs "built in a weekend" is distinguished.

---

## 12. What to hand to Claude Design

Paste this document as context, then ask for:

1. A full Figma-equivalent of the **Graph view** (§9.2) — this is the hero screen
2. The **Playground** split layout (§9.3) — this is the differentiator
3. The dashboard **home** (§9.1) — this is the first impression
4. Light and dark mode variants of each
5. One onboarding flow screen (§11)

Leave for later: Audit table, settings, token management — these can follow established shadcn patterns once the hero screens are locked in.

---

## 13. One-line visual brief

> *Linear's layout discipline, Stripe's trust signals, Tailscale's graph elegance — rendered in cool indigo-blue on deep slate, with semantic green/red/amber telling the policy story.*
