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

## 4. The example gate

For each candidate finding, write the example before deciding whether to keep it:

```
Trigger:  what happens in the world (an operation, a request, a deploy, a volume)
Break:    what the code then does wrong
Notice:   who sees it, and how
```

Then drop the finding if the example needs any of these to be true:

- **Input no real caller produces.** Hexgate's agents act on content a real
  user's request caused them to read — not attacker-crafted payloads arriving
  from nowhere.
- **Traffic this product does not have.** Hexgate is an early-stage SaaS with a
  handful of stages. A break that needs 50k spans/sec is not a break yet; one
  that needs 200/sec is.
- **Config nobody sets.** Grep before claiming an env var, flag or setting
  matters — if nothing in the repo, compose files or DEPLOY.md sets it, the
  finding rests on a hypothetical deployment.
- **Three coincidences at once.** Two is a bad day; three is fiction.
- **A code path that does not exist yet.** Constants declared ahead of their
  emitter are deliberate here; the future caller is a different PR's problem.
- **Nothing at all.** If no example can be written, the finding is a theory.
  Drop it.

Keep the finding, and say so plainly, when the example is mundane: a deploy, a
restart, a broker that takes twenty minutes to come back, a customer's first RAG
call, a retry that lands twice.

Pre-existing issues, linter/typechecker/CI catches, missing test coverage and
style preferences not written in a `CLAUDE.md` are out of scope regardless of how
good their example is.

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
