# Spec: AI Act Compliance Evidence Report (v0)

Source: [AI Act — proposal](https://app.notion.com/p/3d4fb45dbae2815885eaffb9698f0793) ·
design mockup: [AI Act Evidence Report](https://claude.ai/code/artifact/72a0d0f0-2e00-4416-a389-da470f75cc2e)

Drafted 17 Sep 2026. Base branch: `ai_act` (main + 180-day retention, #174).

## Goal

One button, **Generate report**, producing one signed document per project that
evidences the controls Hexgate enforces on the project's agents and the events
recorded about them over a period.

**v0 introduces no new enforcement capability.** Every number in the report comes
from data the platform already holds: the compiled policy bundle, `policy_decision`,
`ban_enforcement`, `llm_invocation`. The only new *stored* data is the operator's
own classification entries (section 1), which the operator asserts and we record.

## Not this

- Not a statutory filing, not an Annex IV dossier, not a FRIA. Our own artifact.
- Never asserts that a system or its operator "is compliant". It states controls
  in place and events recorded.
- Never classifies a system. The operator asserts the risk tier; we record who
  asserted it and when.

This distinction must survive into UI copy and the document header.

---

## PR plan

Five PRs, each ≤ 500 lines of diff including tests. Rule for every cut: **the PR
can be reverted alone and leave main deployable and non-lossy.** All five drafted
17 Sep 2026; they merge in three waves, not five steps — PR 1 and 2 are
independent and go in parallel, PR 3 needs both, PR 4 and 5 go in parallel on top
of 3. PR 5 is buildable against a stubbed endpoint while 3 is in review.

Status: ✅ merged · 🟡 in progress (being implemented or in review) · ⬜ pending.
All five are ⬜ as of 17 Sep 2026 — nothing built yet.

| # | Status | Branch — commit scope | Contents | Depends on | ≈ lines | Merge by |
|---|---|---|---|---|---|---|
| 1 | ⬜ | `vl/feat/agent_ai_act_classification` — `feat(platform-api)` | `AgentClassification` in `models.py` (own table, not columns on `Agent` — see PR 1 below); `GET\|PUT /v1/projects/{project_id}/agents/{name}/classification` on the existing agents router; the completeness rule; `intended_purpose` prefill from the manifest description. No validation of the operator's assertion. Tests: `platform/api/tests/features/agents/`, incl. tenant isolation. | — | 250 | Fri 18 Sep |
| 2 | ⬜ | `vl/feat/authorisation_matrix` — `feat(sdk)` | `hexgate/security/matrix.py`: `authorisation_matrix(PolicySet) -> Matrix` — roles × tools → (`allow`/`approval`/`deny`, constraint text). Deny-by-default fill for absent tools; **per role, not the permissive union** `decision.py` does at call time. Pure function, no I/O. Tests: `tests/security/test_matrix.py` — flat legacy policy, inline-roles, modular after linking, a tool only some roles hold, a role with no tools. | — | 250 | Fri 18 Sep |
| 3 | ⬜ | `vl/feat/ai_act_report` — `feat(platform-api)` | `features/ai_act/` slice: assembler building sections 1–5 from the agent registry, the resolved bundle (PR 2) and the three audit tables; `AiActReport` model; `POST /v1/projects/{id}/ai-act/report`, `GET …/reports`, `GET …/reports/{rpt_id}/annex`. Signed with `FileKeyStore` over the annex digest, same key as bundle manifests. Counts use `count(DISTINCT event_id)` — `ReplacingMergeTree` dedup is eventual. Tests: a golden annex over seeded ClickHouse rows, the generated gap list, tenant isolation. | 1, 2 | 500 | Mon 21 Sep |
| 4 | ⬜ | `vl/feat/ai_act_report_pdf` — `feat(platform-api)` | `features/ai_act/render/`: Jinja2 HTML template + print stylesheet (A4 `@page` margin boxes, repeated `thead`, no orphaned headings) rendered from the **stored annex only** via WeasyPrint at `pdf/a-2b`, `GET …/reports/{rpt_id}.pdf`; Pango + the pinned fonts in `platform/api/Dockerfile`. **Escaping is Jinja autoescaping**, on at the environment. Render failure → 502 with the renderer's diagnostics, never a partial PDF. | 3 | 400 | Tue 22 Sep |
| 5 | ⬜ | `vl/feat/ai_act_tab` — `feat(dashboard)` | `routes/AiAct.tsx` + route in `App.tsx` + nav entry in `AppShell.tsx` beside Bans. Inventory status list with the classification form (Annex III dropdown, links to the Service Desk / Commission checker / FLI checker, checker `last_update_date`); period selector defaulting to the retention window; Generate, download (PDF + annex), history. `lib/api.ts` client. Tests: `AiAct.test.tsx`, `api.test.ts`. **Copy review is part of the PR:** fail on any string claiming conformity. | 3 (stub-buildable) | 450 | Wed 23 Sep |

---

## Report structure

Five sections, trimmed from the mockup. What the mockup has that v0 drops is
listed under each.

### Cover

Org, project + id, period, agent counts (`n registered · n complete · n
incomplete`), requesting user, generation timestamp, report id, signature block
(alg, kid, digest, JWKS URL), and the "what this document is, and is not"
paragraph.

### 1. AI system inventory — Art. 6, Annex III, Art. 26(2)

One row per agent in the project: name, version, bundle hash, intended purpose,
role (provider/deployer), risk tier, Annex III reference, human-oversight owner,
who recorded the entry and when, and the compliance-checker `last_update_date`
the operator relied on.

Agents missing any field are listed as **incomplete** and are never omitted.

### 2. Controls in place — Art. 9, 14, 15, 26(1), 26(2)

Per agent, an authorisation matrix derived from the **compiled** bundle (so
modular and imported policies are covered once resolved): rows = tools, columns
= roles, cells = `Allow | Approval | Deny`, plus the compiled constraint
expressions. Deny by default is stated explicitly. Then a short human-oversight
paragraph (approval gating, the two kill switches) and the Art. 15 data-protection
table: key-based redaction, `secret_guard`, `secret_redactor`, `secret_watch`,
payload cap — each with where it runs, its effect, and what it applies to.

*v0 drop:* the per-agent oversight paragraph is one fixed template, not generated
prose. An agent whose inventory entry is incomplete gets its matrix omitted from
the PDF (it stays in the annex).

### 3. Activity record — Art. 12, 19, 26(6), 72, 73

Headline counters (decisions, denials, approvals required, guard refusals, bans,
model calls, model-call error rate, distinct models); decisions by agent and
outcome; a decision sample; approval-required sample; all ban enforcements; model
calls by agent and model.

*v0 drop:* the stacked share-of-outcomes bar. Counters and tables only.

### 4. Coverage statement — Art. 21, 26(12), 99(5)

Records covered (table, contents, event count, retention, earliest/latest), the
retention/skew/identity caveats, the known gaps, and what the report does not
evidence. The gap list is generated, not hardcoded prose — a gap is emitted when
its condition holds.

v0 gaps, in order: (1) approval outcomes not recorded — the approval handler is
operator code and emits no event; (2) `secret_redactor` / `secret_watch` hits
never leave the SDK, so only `secret_guard` refusals are evidenced; (3) decisions
not yet grouped by run; (4) one gap per agent with an incomplete inventory entry.

### 5. Signature and verification

Ed25519 over the SHA-256 digest of the annex bytes, platform root key — the same
key that signs bundle manifests and biscuits, so the published JWKS verifies both.
Algorithm, kid, digest, signature, JWKS URL, annex filename and size, and the
three-step verification recipe. Stated as an integrity check, not a legal
attestation.

---

## PR 1 — Agent AI Act classification

New table, not new columns on `Agent`. Not for migration reasons — there is no
Alembic (`core/db.py`: the prototype migration story is still `rm hexgate.db &&
restart`), so either shape costs the same today. The reason is ownership and
lifecycle:

- `Agent` is what the agent *is and enforces* — manifest, policy, compiled
  bundle. It is written by the policy author and read on every bundle fetch. A
  classification is an operator's assertion about a system, written by a
  different person on a different cadence.
- The entry needs its own provenance — who recorded it, when, and which
  compliance-checker revision they relied on (Art. 6(4) wants a documented
  assessment with a name and a date). `Agent.updated_at` cannot carry that: a
  policy save bumps it too.
- "Incomplete" then reads as *no row*, rather than nine nullable columns on the
  hot table.

```python
class AgentClassification(SQLModel, table=True):
    __tablename__ = "agent_classification"
    __table_args__ = (UniqueConstraint("agent_id", name="uq_agent_classification"),)

    id: str            # new_id -> "acl_…"
    agent_id: str      # FK agent.id, unique — one current entry per agent
    intended_purpose: Optional[str]     # Art. 3(12); prefilled from the manifest
    operator_role: Optional[str]        # "provider" | "deployer"
    risk_tier: Optional[str]            # "high_risk" | "not_high_risk" | "prohibited" | "minimal"
    annex_iii_point: Optional[str]      # e.g. "5(b)"; null when not_high_risk
    oversight_owner_name: Optional[str] # Art. 26(2) named person
    oversight_owner_contact: Optional[str]
    checker_last_update_date: Optional[date]  # what the operator relied on
    recorded_by_user_id: str            # FK user.id
    recorded_at: datetime
```

Routes on the existing agents router:
`GET|PUT /v1/projects/{project_id}/agents/{name}/classification`.

`intended_purpose` prefills from the manifest description on first GET when the
manifest has one; the operator still has to save it for the entry to count as
recorded. **Completeness** = all of purpose, role, tier, oversight owner present,
plus `annex_iii_point` when `risk_tier == "high_risk"`.

The service writes no opinion: no validation that a tier "matches" a tool set,
no warnings. We record what the operator asserts.

## PR 2 — Authorisation matrix

`hexgate/security/matrix.py`, a pure function over a resolved `PolicySet`:

```python
def authorisation_matrix(policy_set: PolicySet) -> Matrix
# Matrix: roles (ordered, "default" first), tools (ordered),
#         cell(tool, role) -> ("allow" | "approval" | "deny", constraint_text | None)
```

No such helper exists today — `analyzer.py` lints, it does not tabulate. Rules:
a tool absent from a role's policy is `deny` (deny by default); the cell mode is
that role's own mode, not the permissive union across the caller's roles
(`decision.py` does the union at call time; the matrix is per role by design);
constraint text is the compiled expression, rendered as written.

Unit tests cover: flat legacy policy, inline-roles policy, modular policy after
linking, a tool only some roles hold, and a role with no tools.

## PR 3 — Assembler + endpoint

`POST /v1/projects/{project_id}/ai-act/report`, body `{from?, to?}`, defaulting
to the full 180-day retention window ending now.

Assembly, in order:

| Section | Source |
|---|---|
| Cover | `Organization`, `Project`, request user, `FileKeyStore` kid |
| 1 | `Agent` × `AgentVersion` × `AgentClassification` |
| 2 | `Agent.bundle_manifest` + `policy_yaml` resolved → `PolicySet` → PR 2 matrix; the Art. 15 table is fixed content keyed off which plugins the policy registers |
| 3 | `audit.summarize`, `audit.list_decisions`, `audit.list_ban_enforcements`, `llm_invocations` service — all existing, all project-scoped via `query_scope` |
| 4 | Table metadata + min/max `received_at` per table; gap conditions |
| 5 | `FileKeyStore.sign` over the annex digest |

Storage:

```python
class AiActReport(SQLModel, table=True):
    id: str                 # new_id -> "rpt_…"
    project_id: str         # FK project.id
    period_start: datetime
    period_end: datetime
    generated_at: datetime
    generated_by_user_id: str
    annex_json: str         # the exact signed bytes, as text
    annex_sha256: str
    signature: bytes        # Ed25519 over the digest
    signing_kid: str
```

`GET /v1/projects/{id}/ai-act/reports` lists history;
`GET …/reports/{rpt_id}/annex` and `…/reports/{rpt_id}.pdf` download.

The annex is the canonical artifact and what the signature covers. The PDF is a
rendering of it. Both carry the same digest.

Counts use `count(DISTINCT event_id)` — `ReplacingMergeTree` dedup is eventual
and a report that double-counts a retried decision is exactly the "incorrect or
incomplete information" Art. 99(5) penalises.

Generation is synchronous in v0. A 180-day window on a busy project is a handful
of ClickHouse aggregates plus one bounded sample per table; if it stops fitting
in a request, it becomes a job — not in this PR.

## PR 4 — PDF rendering

Renders the stored annex to an A4 document, HTML + CSS through WeasyPrint. One
Jinja2 template and one stylesheet, no per-report logic: the template reads the
annex and nothing else, so a re-render of an old report reproduces it.

Engine: `weasyprint`. A library in the API process rather than a subprocess and
a package bundle, so a render has no binary to shell out to and nothing to
fetch — the image installs Pango and the two font families the stylesheet
names, and that is the whole of what a render depends on.

Layout: `@page` at A4 with margin boxes for a running footer carrying the report
id, "Not a statutory filing" and the page number; `thead` repeated on every page
a table breaks across; `break-after: avoid` on section headings so none is left
alone at a page foot; `table-layout: fixed` with declared column widths so a
long value wraps inside its column rather than over its neighbour.

`pdf_variant="pdf/a-2b"` — the archival profile (fonts embedded, no external
resources, no encryption). Level 3's one addition over 2 is that it permits an
arbitrary embedded file; attaching the signed annex to the PDF is the reason
that would ever be worth taking, and v0 does not.

**Escaping is Jinja's.** Section 3 embeds redacted JSON argument snapshots —
arbitrary operator text. Autoescaping is on for the template environment, so
every value is escaped at the point of interpolation rather than by a helper
each call site has to remember; there is no second escaping vocabulary to get
wrong. Remaining requirements:

- Argument snapshots and every other identifier, digest and path render in a
  fixed-width column with `overflow-wrap: anywhere`, so a long single token
  wraps instead of overflowing.
- A field the annex leaves unset renders as an em-dash, never as `None` or a
  blank cell: a reader has to be able to tell "not recorded" from "recorded
  empty".
- A render failure returns 502 with the renderer's own diagnostics, never a
  partial PDF. The annex is already stored and signed at this point, so a
  failed render is retryable and loses nothing.
- The render runs on its own bounded executor, off the event loop and out of
  the pool the rest of the API shares.

## PR 5 — Dashboard tab

New route `/ai-act` in `App.tsx` + nav entry in `AppShell.tsx`, next to Bans.

- **Inventory status** — one row per agent, complete / incomplete, with the
  missing fields named and a form to record the classification (PR 1). The Annex
  III field is a dropdown of the Annex III points, with links to the AI Act
  Service Desk, the Commission's compliance checker and the FLI checker, and a
  date field for the checker's `last_update_date`.
- **Period selector**, defaulting to the full retention window.
- **Generate report**, then download (PDF and annex).
- **History** — past reports with period, generation time, who generated it, digest.

Copy rule for the whole tab: "controls in place and events recorded", never
"compliant". Reviewers should fail the PR on any string that claims conformity.

---

## Deferred (named in the coverage statement, not built)

- Approval grant events — who approved, and whether the call then ran. Needs a
  new event on the OTLP transport carrying the approver's identity.
- Run grouping, once `run_id` is populated end-to-end (#153, #155).
- `secret_redactor` / `secret_watch` counters — need a new event table.
- Preventive control attestation, Annex IV assembly, deployer checklist,
  scheduled generation, FRIA, org-level rollup.

## Implementation prompts

One per PR, to hand to Claude Code as-is. Each assumes the repo is checked out at
`ai_act` and that this spec is on disk.

Every prompt ends with the same closing block, repeated verbatim in each so they
can be copied independently:

> Then: run `/review-grounded` on the diff and fix every finding it confirms. Run
> `make check-all` and fix every failure — do not leave a test skipped or
> xfailed to make it pass. Commit with a `type(scope): description` message per
> `CLAUDE.md`, push the branch, and open the PR with exactly this description,
> nothing else:
>
> ```
> ## What is changing
> ## Why is this change necessary
> ## Tests
> ```
>
> Keep each section to a few lines. No summary of your process, no bullet lists
> of files touched.

### PR 1 — agent AI Act classification

```
Read Specs/ai_act_evidence_report.md, sections "PR plan" and "PR 1 — Agent AI Act
classification". Branch from ai_act as vl/feat/agent_ai_act_classification.

Implement PR 1 only:
- AgentClassification in platform/api/hexgate_api/models.py, exactly the shape in
  the spec. Its own table, not columns on Agent — the spec says why.
- GET and PUT /v1/projects/{project_id}/agents/{name}/classification on the
  existing agents router, with the project-scoping and tenant checks the sibling
  routes already use. Read those first; do not invent a new auth pattern.
- Completeness rule as specified: purpose, role, tier, oversight owner, plus
  annex_iii_point when risk_tier == "high_risk".
- GET prefills intended_purpose from the manifest description when the stored
  value is null; the entry only counts as recorded once the operator PUTs it.
- The service records the operator's assertion and validates nothing about it. No
  warning when a tier looks inconsistent with the tool set.

Tests in platform/api/tests/features/agents/, including a tenant-isolation case.

Then: run /review-grounded on the diff and fix every finding it confirms. Run
make check-all and fix every failure — do not leave a test skipped or xfailed to
make it pass. Commit with a type(scope): description message per CLAUDE.md, push
the branch, and open the PR with exactly this description, nothing else:

## What is changing
## Why is this change necessary
## Tests

Keep each section to a few lines. No summary of your process, no bullet lists of
files touched.
```

### PR 2 — authorisation matrix

```
Read Specs/ai_act_evidence_report.md, section "PR 2 — Authorisation matrix".
Branch from ai_act as vl/feat/authorisation_matrix.

Implement PR 2 only: hexgate/security/matrix.py, a pure function
authorisation_matrix(policy_set: PolicySet) -> Matrix returning roles × tools →
(mode, constraint_text).

Read hexgate/security/policy_set.py, decision.py and rego.py first and follow
their vocabulary. Three rules the spec fixes and you must not soften:
- a tool absent from a role's policy is deny (deny by default);
- the cell is that role's own mode, NOT the permissive union across a caller's
  roles — decision.py does the union at call time, the matrix is per role;
- constraint text is the compiled expression, rendered as written.

No I/O, no logging, no platform imports.

Tests in tests/security/test_matrix.py covering: a flat legacy policy, an
inline-roles policy, a modular policy after linking, a tool only some roles hold,
and a role with no tools.

Then: run /review-grounded on the diff and fix every finding it confirms. Run
make check-all and fix every failure — do not leave a test skipped or xfailed to
make it pass. Commit with a type(scope): description message per CLAUDE.md, push
the branch, and open the PR with exactly this description, nothing else:

## What is changing
## Why is this change necessary
## Tests

Keep each section to a few lines. No summary of your process, no bullet lists of
files touched.
```

### PR 3 — report assembler and signed endpoint

```
Read Specs/ai_act_evidence_report.md in full — you are implementing the core of
it. Branch from vl/feat/agent_ai_act_classification (PR 1) once PR 2 is also
merged or available; name it vl/feat/ai_act_report.

Implement PR 3 only: the features/ai_act/ slice in platform/api/hexgate_api/.
- Copy the shape of features/llm_invocations/ — service, router, tests.
- AiActReport model exactly as specified.
- POST /v1/projects/{id}/ai-act/report with an optional period, defaulting to the
  full 180-day retention window ending now; GET .../reports; GET
  .../reports/{rpt_id}/annex.
- Assemble sections 1 to 5 from the sources named in the spec's assembly table.
  Reuse audit.summarize, audit.list_decisions, audit.list_ban_enforcements and
  the llm_invocations service — do not write new ClickHouse queries where one of
  those already answers the question. Scope every read through query_scope.
- Section 2 reads the COMPILED bundle via the PR 2 matrix, never the raw
  policy_yaml.
- Every count uses count(DISTINCT event_id). ReplacingMergeTree dedup is eventual
  and a double-counted retry is exactly the misleading figure Art. 99(5)
  penalises.
- The coverage statement's gap list is generated from conditions, not hardcoded
  prose. Emit the four gaps the spec names when their condition holds.
- Sign the SHA-256 digest of the exact annex bytes with FileKeyStore and store
  annex, digest, signature and kid on the row.

Wording constraint, and treat it as a correctness requirement: nothing the
endpoint emits may assert that a system or an operator is compliant.

Tests: a golden annex over seeded ClickHouse rows, the generated gap list, and
tenant isolation.

Then: run /review-grounded on the diff and fix every finding it confirms. Run
make check-all and fix every failure — do not leave a test skipped or xfailed to
make it pass. Commit with a type(scope): description message per CLAUDE.md, push
the branch, and open the PR with exactly this description, nothing else:

## What is changing
## Why is this change necessary
## Tests

Keep each section to a few lines. No summary of your process, no bullet lists of
files touched.
```

### PR 4 — PDF rendering

```
Read Specs/ai_act_evidence_report.md, sections "PR 4 — PDF rendering" and "Note
on the renderer". Branch from vl/feat/ai_act_report (PR 3) as
vl/feat/ai_act_report_pdf.

Implement PR 4 only: platform/api/hexgate_api/features/ai_act/render/.
- A Jinja2 template and a print stylesheet — A4 @page with margin boxes for a
  footer carrying the report id, "Not a statutory filing" and the page number;
  thead repeated where a table breaks across pages; break-after: avoid on
  section headings; table-layout: fixed with declared column widths.
- The template reads the STORED annex and nothing else, so re-rendering an old
  report reproduces it.
- Render with WeasyPrint at pdf_variant="pdf/a-2b"; add Pango and the two font
  families the stylesheet names to platform/api/Dockerfile.
- GET /v1/projects/{id}/ai-act/reports/{rpt_id}.pdf.
- A render failure returns 502 with the renderer's own diagnostics. Never a
  partial PDF.
- The render runs on its own bounded executor, off the event loop and out of
  the pool the rest of the API shares.

Escaping is Jinja autoescaping, enabled on the environment — there is no second
escaping vocabulary and no hand-written escaper. Test that a string carrying
HTML and TeX metacharacters in every annex field still round-trips into the PDF
text. Argument snapshots, digests and paths render fixed-width with
overflow-wrap: anywhere, so a long single token wraps instead of overflowing its
column. An unset field renders as an em-dash, never as "None" or a blank cell.

Design: match the mockup linked at the top of this spec — the type scale, the
restrained rules rather than boxed grids, the small-caps article references
beside each section heading, the counter row, the signature block. It must still
read in black and white on a printer.

Then: run /review-grounded on the diff and fix every finding it confirms. Run
make check-all and fix every failure — do not leave a test skipped or xfailed to
make it pass. Commit with a type(scope): description message per CLAUDE.md, push
the branch, and open the PR with exactly this description, nothing else:

## What is changing
## Why is this change necessary
## Tests

Keep each section to a few lines. No summary of your process, no bullet lists of
files touched.
```

### PR 5 — dashboard AI Act tab

```
Read Specs/ai_act_evidence_report.md, section "PR 5 — Dashboard tab". Branch from
vl/feat/ai_act_report (PR 3) as vl/feat/ai_act_tab.

Implement PR 5 only, in platform/dashboard/.
- routes/AiAct.tsx, a route in App.tsx, a nav entry in AppShell.tsx beside Bans.
  Match how routes/Bans.tsx is built — same data-fetching, loading and error
  patterns. Read it before writing anything.
- Inventory status list: one row per agent, complete or incomplete, naming the
  missing fields, with the classification form from PR 1. The Annex III field is
  a dropdown of the Annex III points with links to the AI Act Service Desk, the
  Commission's compliance checker and the FLI checker, plus a date field for the
  checker's last_update_date.
- Period selector defaulting to the full retention window.
- Generate report, download (PDF and annex), and a history list with period,
  generation time, who generated it and the digest.
- lib/api.ts client for the PR 3 endpoints.

Copy rule, and enforce it on yourself: "controls in place and events recorded",
never "compliant" and never any phrasing that implies conformity. Grep your own
diff for it before committing.

Tests: AiAct.test.tsx and api.test.ts.

Then: run /review-grounded on the diff and fix every finding it confirms. Run
make check-all and fix every failure — do not leave a test skipped or xfailed to
make it pass. Commit with a type(scope): description message per CLAUDE.md, push
the branch, and open the PR with exactly this description, nothing else:

## What is changing
## Why is this change necessary
## Tests

Keep each section to a few lines. No summary of your process, no bullet lists of
files touched.
```

---

## Open questions

- **Project scope only.** Agents and audit data are project-scoped; a customer
  with several projects will want one document. Org rollup later.
- **Are we provider, deployer, or neither, per surface we ship?** Needs writing
  down; it does not block v0, which records the *operator's* role, not ours.
- **Art. 50 already applies to us.** Playground and the chat/serve websockets put
  a user in front of a model. Separate work, not this spec.

---

## Note on the renderer

HTML + CSS via `weasyprint`, per PR 4. This is a document handed to an auditor,
so it wants real typesetting — stable pagination, tables that break across pages
with repeated headers, a footer carrying the report id on every page. CSS Paged
Media gives all three (`@page` margin boxes, `display: table-header-group`,
`break-after: avoid`), and WeasyPrint implements them; browser print-to-PDF is a
different thing and gives none of it reliably.

The engine is a library in the API process, not a binary plus a package bundle.
That removes the class of failure where a render depends on what a remote bundle
served that day, and it makes the escaping story ordinary: Jinja2 autoescaping,
applied at interpolation, instead of a hand-written escaper with its own fuzz
suite. What the renderer does depend on is Pango and two font families, both
pinned by the image.

The decision is confined to PR 4 either way: the annex is the signed artifact and
the renderer is downstream of it, so swapping engines later costs one PR and
changes no signature.
