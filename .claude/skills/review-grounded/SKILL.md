---
name: review-grounded
description: Code review where every finding must come with a concrete, realistic failure example, and findings whose only example is far-fetched are dropped. Takes a PR URL or number, a branch, or nothing (the current diff). Reports findings in the chat — never posts to GitHub. Use when asked to review code, review a PR, or check a diff before pushing; use /code-review instead when the goal is posting inline PR comments or applying fixes.
---

# Grounded Code Review

Same pipeline as `/code-review`, with one extra gate: a finding survives only if
you can write down a failure that would plausibly happen to this product. The
gate exists because the expensive failure mode of a review is not a missed bug,
it is a list of twelve findings where three matter — the author then either
fixes noise or stops reading reviews.

Zero findings is the ordinary outcome, not a failed review. One or two is a
normal PR. If you are about to report five, you have almost certainly gated
leniently — go back and write the Notice line for each one again.

## 0. What this product actually is

The gate is only as good as the facts it tests against, so they are written
down here rather than re-derived each run:

- **No customers yet.** A break that needs a real end user, a paying tenant, or
  a customer-facing event to have already happened has not happened.
- **One engineer.** No second developer to confuse, no separate operator, no
  handover. Drop any finding whose only victim is a hypothetical colleague.
- **Prod and staging are live and deploy from git** — prod from a release tag,
  staging from `main`. Deploys, restarts and migrations are real events.
- **Data at risk is the team's own.** Keys, agents and policies can be re-minted
  or re-saved. Losing them is an inconvenience, not an incident.

When one of these is what kills a finding, say so in the drop line — it is the
most reusable thing a review produces.

## 1. Eligibility

Skip the review entirely if the PR is closed, is an automated/dependency bump,
or already carries your review. A draft PR is reviewable.

## 2. Context

**Get the code on disk first.** Every lens below reads real files and real
installed dependency source, so review a local checkout, not a diff hunk. If the
PR is not already checked out, put it in its own worktree so the current one is
untouched:

```bash
gh pr checkout <n> --branch <branch>   # when the repo is already on that branch, skip
git worktree add ../<n> <branch>       # or: a worktree per PR, named for the PR
```

Confirm where you are with `git branch --show-current` before reading anything,
and run every command from that directory.

Then collect, in parallel:

- The root `CLAUDE.md` plus any `CLAUDE.md` in a directory the diff touches.
- The diff itself (`gh pr diff <n>`, or `git diff <base>...HEAD`). For a stacked
  PR, diff against its real base, not `main`.
- The PR body — it often states a tradeoff deliberately, and a "finding" that
  restates an acknowledged tradeoff is not a finding.

## 3. Review lenses

Run these as parallel subagents, one lens each. Give every lens the same
instruction: read the real files and the real installed dependency source, never
assume behaviour from the name of a setting.

1. **CLAUDE.md compliance** — only what the file actually says, quoted.
2. **Correctness** — bugs in the changed lines.
3. **History** — `git blame` / `git log -p` on the touched code: is this undoing
   an earlier deliberate decision whose reason still holds?
4. **Prior review comments** — comments on earlier PRs touching these files that
   apply again here.
5. **Availability** — a hang, stall, silent drop or slow drain is as serious as
   an error. Read upstream defaults the diff activates.
6. **Internal consistency** — every number, cross-reference and claim in changed
   comments and docs, checked against the file it points at.
7. **Wire compatibility** — when the diff changes an encoding, compression,
   serialization, protocol version or schema, name every other party that reads
   or writes those bytes and verify *that* side supports the new form in its
   pinned version. For an optional codec, grep the reader's lockfile for the
   library implementing it: many are gated behind an extra (`aiokafka[zstd]` →
   `cramjam`) and absent by default. A setting being a valid field proves the
   writer accepts it, never that anything can decode it. Then ask what the
   reader does with the new bytes during the window where only one side is
   deployed.

## 4. The example gate

For each candidate finding, write the example before deciding whether to keep it:

```
Trigger:  what happens in the world (an operation, a request, a deploy, a volume)
Break:    what the code then does wrong
Notice:   who sees it, how, and whether they would act anyway
```

`Notice` is the line that decides most findings, so write it last and write it
honestly. "The engineer sees the error and fixes it" is a reason to drop, not a
reason to report. The answer that keeps a finding is "nobody" — the operation
returns success, the log line is skipped, the screen says the opposite of what
is true.

The gate applies to every lens, including CLAUDE.md compliance, internal
consistency and prior review comments. A category being "in scope" makes an item
a candidate, never a finding; a past review comment that applied to an earlier
PR is a candidate here, not a standing ruling. Write the examples as each
subagent's list arrives — if a list is longer than three, write the example for
each item before reading the next list. Aggregating first and gating later is
how twelve findings get reported.

A finding whose Notice is "a reader" — a stale comment, a wrong line number, a
misnamed function in a docstring, a commit-message format — has no Break in the
code and is housekeeping, not a finding. Collect these in one line after the
numbered findings, never as numbered entries.

Then drop the finding if the example needs any of these to be true:

- **Input no real caller produces.** Hexgate's agents act on content a real
  user's request caused them to read — not attacker-crafted payloads arriving
  from nowhere.
- **Traffic this product does not have.** Hexgate is an early-stage SaaS with a
  handful of stages. A break that needs 50k spans/sec is not a break yet; one
  that needs 200/sec is. The same applies to events, not just volume: with no
  customers, a break that needs a customer — or a departing teammate, or data
  that accumulated before this deploy — needs something that has not occurred.
- **A failure that announces itself.** If the Break stops the thing at the
  moment of action — a make target that exits non-zero, a container that
  crash-loops, a request that 500s on the first try — the engineer sees it and
  reacts, and nothing is silently wrong. Loud-and-recoverable is not a finding;
  say what it costs (a retry, a re-run, five minutes) and drop it. What survives
  this filter is the silent break: the one where the operation reports success
  and the wrong thing is true afterwards.
- **A value nothing reads.** Before reporting a wrong or missing write, find
  the read: a wire schema, a screen, a query, a branch. If no code path
  consumes the value, a regression in it is invisible and harmless. This kills
  most "the actor/timestamp/flag is not stamped here" findings.
- **Config nobody sets.** Grep before claiming an env var, flag or setting
  matters — if nothing in the repo, compose files or DEPLOY.md sets it, the
  finding rests on a hypothetical deployment.
- **Three coincidences at once.** Two is a bad day; three is fiction.
- **A code path that does not exist yet.** Constants declared ahead of their
  emitter are deliberate here; the future caller is a different PR's problem.
- **Nothing at all.** If no example can be written, the finding is a theory.
  Drop it.

Keep the finding, and say so plainly, when the example is mundane *and* the
Break is silent: a deploy, a restart, a broker that takes twenty minutes to come
back, a retry that lands twice — and afterwards the system reports success while
something is quietly wrong. Mundane means an operational event that happens to
the running product; "someone reads the docstring" is not a trigger, and "the
deploy stops with a clear error" is not a break.

Before reporting, make one adversarial pass over the surviving list whose only
job is to kill findings — not to justify them. Take each one and try to name the
fact that makes it not matter: no customers, loud failure, nothing reads it,
path doesn't exist. Never assign a subagent to find a use case *for* a finding;
handed a conclusion, it will manufacture a scenario, which is how a list of five
gets built. The asymmetry is the point.

Pre-existing issues, linter/typechecker/CI catches, missing test coverage and
style preferences not written in a `CLAUDE.md` are out of scope regardless of how
good their example is.

One thing that is always in scope: a setting the diff adds that the diff's own
comments say buys nothing for its goal. An unrelated optimization riding along
carries risk nobody signed up for. Read such a comment as a reason to question
the line, not as documentation of it.

## 5. Verify what survives

Re-check each surviving finding against the actual code — read the endpoint, the
library source, the config file. State the evidence you read. A finding that
cannot be verified either becomes uncertain-and-labelled or gets dropped; it
never gets reported as fact.

## 6. Report

In the chat, never on GitHub. Most severe first:

```
### Code review

<one line on what came out clean, with the evidence>

N findings:

1. **<what breaks>** — <the example, one or two sentences>
   <repo-relative path:line — e.g. platform/collector/config.yaml:126>

Housekeeping (optional): <stale comments, wrong pointers, format nits — one line>
```

Cite `path:line`, which is clickable in the terminal and points at the checkout
the reader already has. Fall back to a GitHub permalink
(`.../blob/<full sha>/<path>#L<start>-L<end>`, a line of context either side)
only when the code is not on disk — a review of someone else's unfetched branch,
or a finding you are asked to post to GitHub.

Then state what you would do about them — fix here, follow-up issue, or accept —
and wait. Do not fix, commit, or open issues unless asked.

If nothing survives the gate, say that in one line and list what you checked.
That is a real outcome, not a failure to find something.
