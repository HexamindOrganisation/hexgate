---
name: write-adr
description: Write or update an Architecture Decision Record in docs/adr/ using the hexgate house style (R-<AREA>-NNN id, Status, Applies-to globs, MUST/MUST-NOT decision, a detailed Why). Use when recording a durable design/security decision, when asked to "write an ADR / decision record", or when superseding an existing one.
---

# Write a hexgate ADR

ADRs live in `docs/adr/`, one decision per file, named
`R-<AREA>-<NNN>-<kebab-summary>.md` (e.g. `R-GUARD-002-guards-fail-closed.md`).
Read a couple of existing records first — they are the source of truth for the
house style — then match them.

**Terse on *what*, detailed on *why*.** An agent reads these under a token budget
and acts on them literally, so the decision must be unambiguous and the rationale
must survive: six months later nobody remembers why, and without it someone
"cleans up" the decision and reintroduces the bug it was avoiding. The rationale
is the one part you must never compress.

## Before writing

- **Pick the area and the next id.** Existing areas: `AGENT`, `GUARD`, `POL`.
  Reuse one if the decision fits it; open a new area only for a genuinely new
  subsystem. Number is the next free integer in that area (`ls docs/adr/`).
- **One decision per file.** If you can't state it in two sentences, it's
  probably two decisions — split it.
- **Superseding?** Don't delete the old record. Set the old one's Status to
  `Superseded by R-<AREA>-NNN (date)` with a short "Why superseded" note, and set
  the new one's Status to `supersedes …`, so the chain resolves. See
  `R-AGENT-001` → `R-AGENT-002` for the pattern.

## Required shape

Every record has these; keep the optional ones only when they earn their place.

- **Title** — `# R-<AREA>-NNN: <the decision as a short imperative phrase>`.
- **Status** — `Accepted · <YYYY-MM-DD>`, or `Superseded by …` / `supersedes …`.
- **Applies to** — the exact file/dir globs the decision governs, e.g.
  `` `hexgate/guards/**`, `hexgate/adapters/**/tools.py` ``. This is what scopes
  the ADR: an agent loads it when touching those paths, so it is not always-on
  context. Be specific.
- **## Decision** — one or two imperative sentences, then MUST / MUST NOT bullets.
  Never "prefer" or "consider" — those let an agent do the opposite.
- **## Why** — the forces, the tradeoff, and what breaks if the rule is ignored.
  Spend words here. Name the concrete failure the decision avoids (an incident, a
  fail-open, a drift) if there is one.

Optional, when useful:

- **## Consequences** — what this enables or costs downstream (a cleanup job now
  required, a dev-env dependency, a follow-up deferred).
- **## Rejected alternatives** — the specific option you turned down **and why**.
- **## Verify** — a grep/lint/test the agent or CI can actually run to check the
  rule holds. Loading an ADR into a prompt is not enforcement; a check is.

## Template

````markdown
# R-AREA-00N: <decision as a short imperative phrase>

**Status:** Accepted · 2026-09-14
**Applies to:** `hexgate/<path>/**`, `platform/api/<path>/**`

## Decision

<One or two imperative sentences.> Then the specifics:

- <A MUST bullet — the rule, stated so an agent cannot read it two ways.>
- <A MUST NOT bullet — the thing this decision forbids.>

## Why

<The forces and the tradeoff. What breaks if this is ignored — name the concrete
failure (fail-open, drift, incident) the decision avoids. This is the part that
must not be compressed.>

## Consequences

- <What this now requires or costs elsewhere.>

## Rejected alternatives

- **<Option>** — <why it was turned down.>

## Verify

`<a grep / lint / test that checks the rule holds>`
````

## Do / do-not

- Do give the decision a stable id and cite it by id from other docs (a PR body,
  another ADR) instead of re-explaining it.
- Do write MUST / MUST NOT, not "prefer / consider".
- Do keep it under ~200 lines; the Why is where the words go.
- Do add a `Verify` command whenever the rule is mechanically checkable.
- Do not delete or rewrite a superseded record — supersede it and keep the chain.
- Do not restate language/framework basics or anything the code already shows.
