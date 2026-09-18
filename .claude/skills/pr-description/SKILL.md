---
name: pr-description
description: Write the body of a hexgate pull request — the house structure (objective, design, important files, test plan, try it, notes) that a reviewer and a review agent can both scan. Use when opening a PR, drafting or rewriting a PR description, or asked "write the PR body / description" for a change on this repo.
---

# Write a hexgate PR description

The goal is a body a reviewer can read top to bottom and know, in order: what
this PR is for, how it works, and which files to open first. A clear description
is the cheapest way to speed up review — most of the time saved is questions the
reviewer never has to ask.

The commit subject is separate and stays `type(scope): description` (see
CLAUDE.md). This skill is about the PR **body**.

## Why the structure matters

- **The reader is no longer only human.** PRs are read by AI agents too — they
  summarize the change, review the diff, and answer questions about it. A
  consistent shape (objective → design → the files that matter) is something both
  a human skims and an agent parses, and an agent can walk the "important files"
  table as a review order the same way a person would.
- **There is more code per PR than there used to be.** The description is the
  main thing standing between a large, partly machine-written diff and a reviewer
  who has to trust it. A structured body keeps review fast no matter who — or
  what — is reading.
- **Guard against rubber-stamping.** When an agent drafts the description and an
  agent reviews the diff, the human can slide into approving a change nobody
  read. At least one section must be written by the person who did the work and be
  something a model cannot fake: the **test plan**. An agent can summarize a diff,
  but it cannot truthfully say what you ran and what you saw.

## Principles

- **Lead with the objective.** The first sentence says what the PR does, before
  any detail. A reviewer should grasp the point without scrolling.
- **Explain why, not just what.** The diff already shows what changed; the body's
  job is the motivation and the approach the diff cannot show.
- **Scale length to the change.** A one-file fix gets a paragraph. A subsystem
  gets the full skeleton. Do not pad a small PR into a big one.
- **Make it scannable.** Short paragraphs, bullet lists, and tables beat prose walls.
- **Point at the files that matter.** In a 13-file diff, name the three that
  carry the design; leave the ten mechanical ones out.
- **Write for someone without the background.** The reviewer may not have been in
  the design discussion.

## Section order

Use these in this order. Drop the ones a given PR does not need.

1. **Objective (required).** One short paragraph. The PR *title* already states
   the change; this section adds what the title cannot, so do not just restate it.
   Make the first sentence stand on its own — a single imperative statement ("Add
   role-aware policy resolution") that reads well alone in the merge history —
   then give the problem it solves and *why*. If it is part of a series,
   reference the prior PRs and say where this one sits. Note the blast radius up
   front ("pure SDK, no platform change", or "adds a migration") so the reviewer
   knows what class of change they are reading.
2. **Design / approach (required for anything non-trivial).** A few lines on the
   technical shape: the key idea, the main type or function, what it reuses vs.
   what is new, and the one or two decisions worth knowing. A small table works
   well when the PR adds several related things (lints, endpoints, flags). Keep
   it to what a reviewer needs, not a full design doc. Link the design doc or the
   relevant ADR (`docs/adr/R-…`) if there is one.
3. **Important files (required when the diff spans more than a few files).** A
   `| File | What to look at |` table naming the files that carry the design and
   what to check in each. Highest-leverage section: it turns a flat file list
   into a review order. Leave out the mechanical files.
4. **Tests (required for anything that changes behaviour).** Not just what you
   ran, but what you *added* so someone fresh to this code can catch a future
   regression. Break it out by layer, dropping the ones that do not apply:
   - **Unit** — the suite you ran and its result.
   - **Integration** — what you ran and *where*: local (against a local stack /
     ClickHouse, see the `integration-tests` skill) or against staging. Say which.
   - **End-to-end / by hand** — the specific cases you checked, UI steps included.
   - **Added** — the tests you added to pin the new behaviour against regression.

   **Write this section yourself** — it is a claim about what you observed, not a
   summary of the diff, and it is the section a reviewer leans on to trust the
   rest. Plainly: "unit: full suite green (142 passed); integration: `pytest -m
   integration` green locally; by hand: deny path returns 403 for the billing
   role; added: a regression test for the empty-scope case."
5. **Try it (when there is something runnable).** A fenced block of commands the
   reviewer can paste to see the change work, ideally against a fixture already
   in the repo (for us, `deploy/demo_policies`). For UI changes, put a screenshot
   or short clip here instead.
6. **Notes (optional).** Loose ends: what stayed unchanged on purpose; risk and
   rollback (migrations, breaking changes, feature flags, how to revert); known
   limitations; follow-ups deferred to a later PR; link to the design doc or ticket.

## Template

````markdown
<Objective: one paragraph. What this PR does and why. Series position and blast
radius if relevant.>

## Design

<A few lines on the approach: the key idea, the main entry point, what is reused
vs new, the decisions worth knowing. A table if it adds several related things.>

## Important files

| File | What to look at |
| --- | --- |
| `path/to/core.py` | the main change and why it is shaped this way |
| `path/to/wiring.py` | how it plugs into the existing path |
| `tests/...` | the cases that pin the behaviour |

## Tests

- **Unit:** <suite run + result>
- **Integration:** <what you ran + local or staging>
- **End-to-end / by hand:** <cases checked>
- **Added:** <tests added to pin the new behaviour>

<Write this section yourself: it is what you observed, not a summary of the diff.>

## Try it

```
<runnable commands against a repo fixture, e.g. deploy/demo_policies>
```

## Notes

- What stayed unchanged on purpose.
- Risk and rollback: migrations, breaking changes, feature flags, how to revert.
- Follow-ups deferred; link to the design doc / ADR / ticket.
````

## Quick do / do-not

- Do state the primary change in sentence one.
- Do name the two or three files that carry the design.
- Do give a paste-able way to see it work.
- Do write the Tests section yourself, not with a tool: it is the one section a
  reviewer trusts, because a model cannot fake what you ran or added.
- Do split an oversized PR rather than compensate with a longer description.
- Do not restate the diff line by line.
- Do not open with implementation detail before the objective.
- Do not use em-dashes as connectors in the body (they get stripped from GitHub
  text here). Use commas, periods, or parentheses.
