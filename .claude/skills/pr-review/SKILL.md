---
name: pr-review
description: Auto-review someone else's hexgate pull request in one invocation — fan out independent review agents, fact-check every finding in fresh context, then post the survivors as one PR comment once the human approves. Use when asked to "review PR 231", "auto-review this PR", or to run the review pass before approving someone's change.
---

# Auto-review a hexgate pull request

Three phases in one invocation: **find**, **verify**, **post**. A human gate sits
between verify and post. Never post without it.

Usage: `/pr-review <pr-number> [post-threshold]` — `post-threshold` defaults to
**50**.

## Why the phases are separate

A single review pass has a high false-positive rate, and findings posted on a
colleague's PR cost reviewer credibility when they turn out to be wrong. The
verify phase exists to spend cheap tokens buying back that credibility.

The rule that makes verification work: **the verifier must never see the
reasoning that produced the finding.** A verifier that reads the finder's
argument anchors on it and defends the claim. A verifier that sees only the claim
and the code has to re-derive it, and drops the ones that do not survive. Every
verification agent therefore starts in fresh context and receives only the diff,
the claim, and the file and line — never the finder's transcript or rationale.

Do **not** implement phase 1 by invoking `code-review:code-review`. That command
owns its own pipeline: it filters findings below 80 and posts to the PR itself.
Both fight this skill — the 50–80 band never reaches the human gate, and the
comment goes up before anyone approves it. Own the review prompt here instead.

## Phase 0 — eligibility and context

Skip the review entirely, and say why, if the PR is closed, is a draft, is
automated, or already carries a review comment from a previous run.

Otherwise gather, with `gh` (never web fetch):

    gh pr view <n> --json title,body,author,state,isDraft,headRefOid,files
    gh pr diff <n>

Capture `headRefOid` — the full SHA is required for permalinks in phase 3.

Collect the paths of the root `CLAUDE.md` and any `CLAUDE.md` in directories the
PR touches. Review against those, not against `CLAUDE.local.md`: local files hold
the reviewer's personal preferences, which are not binding on someone else's PR.

## Phase 1 — find

Launch these agents **in a single message** so they run concurrently. Each gets
the diff and the CLAUDE.md paths, works independently, and does not see the
others' output.

1. **CLAUDE.md adherence** — only rules the relevant CLAUDE.md states explicitly.
   Quote the rule text for each finding.
2. **Bug scan** — read the diff alone, no wider context. Large bugs only.
3. **History** — `git log` and `git blame` on the modified regions. Flag changes
   that reintroduce a reverted fix or contradict why the code got that shape.
4. **Prior review comments** — comments on earlier PRs touching these files, where
   the same guidance applies again.
5. **In-code guidance** — comments and docstrings in the modified files that the
   change now contradicts.
6. **Hexgate surfaces** — the change's blast radius across the monorepo: SDK
   versus `platform/api` versus `platform/collector` versus `dashboard`, ClickHouse
   or Postgres migrations that are not idempotent, and `HEXGATE_`-prefixed env or
   policy-bundle behaviour.

Each agent returns JSON only:

    {"findings": [{"file": "...", "line": 0, "claim": "one sentence",
                   "evidence": "what in the code shows it",
                   "dimension": "bug", "score": 0}]}

Score 0–100 on confidence that the finding is real, using this rubric verbatim:

- **0** — false positive under light scrutiny, or a pre-existing issue.
- **25** — might be real, could not verify. Stylistic points not stated in CLAUDE.md land here.
- **50** — verified real, but possibly a nitpick or rare in practice.
- **75** — verified, very likely hit in practice, and the PR's approach is insufficient. Or stated directly in CLAUDE.md.
- **100** — confirmed, with evidence, and frequent in practice.

Not findings, in this phase or the next:

- Pre-existing issues, and real issues on lines the PR did not modify
- Anything a linter, typechecker, compiler, or CI run would catch
- Nitpicks a senior engineer would not raise
- Missing tests, docs, or general security posture, unless CLAUDE.md requires it
- Issues explicitly silenced in the code
- Behaviour changes that are plainly intentional parts of the change

Merge the returned findings, collapsing duplicates across dimensions — keep the
highest score and note every dimension that raised it. Discard below 50.

## Phase 2 — verify

For every surviving finding, launch **one agent per finding, all in a single
message**. Each starts fresh and receives exactly: the PR diff, the file and line,
and the one-sentence claim. It must not receive the finder's evidence string,
dimension, original score, or any other finding.

Each verifier reads the actual code and returns:

    {"adjusted_score": 0, "verdict": "CONFIRMED|PLAUSIBLE|REJECTED",
     "why": "one sentence naming what in the code decided it",
     "fix": "concrete suggested fix, or null"}

Same rubric. `REJECTED` forces a score below 50 regardless. A verifier that cannot
locate the code the claim refers to returns `REJECTED`.

## Phase 3 — report, gate, post

Report every verified finding to the user as a table — file and line, original
score, adjusted score, verdict, one-line claim — including the ones that fell
below the threshold, marked as dropped and with the drop reason. The score
movement is the signal that the verify pass is working.

**Then stop and wait.** The user approves, edits, or discards findings. Do not
post, and do not approve or request changes on the PR, without an explicit
instruction in reply to that table.

On approval, post **one** comment with `gh pr comment`, containing only findings
at or above the post threshold, ordered by adjusted score:

    ### Code review

    Found N issues:

    1. **<claim>** (score: <adjusted>)
       <one or two sentences: what breaks, and when>
       Suggested fix: <concrete change>
       <permalink>

Permalink rules — a broken link wastes the author's time:

- `https://github.com/<owner>/<repo>/blob/<full-sha>/<path>#L<start>-L<end>`
- Use the literal `headRefOid` from phase 0. Never a command substitution such as
  `$(git rev-parse HEAD)` — the comment renders as raw Markdown and will not
  expand it.
- Centre the range on the line, with at least one line either side.

No emojis. If nothing clears the threshold, post nothing and tell the user the PR
came back clean, naming what was checked.
