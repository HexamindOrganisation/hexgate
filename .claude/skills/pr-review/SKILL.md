---
name: pr-review
description: Auto-review someone else's hexgate pull request in one invocation — run the tuned finder without posting, re-verify only the uncertain findings in fresh context, then post the survivors as one PR comment once the human approves. Use when asked to "review PR 231", "auto-review this PR", or to run the review pass before approving someone's change.
---

# Auto-review a hexgate pull request

Three phases in one invocation: **find**, **verify**, **post**. A human gate sits
between verify and post. Never post without it.

Usage: `/pr-review <pr-number> [post-threshold]` — `post-threshold` defaults to
**50**.

## Why the phases are separate

A review pass has a high false-positive rate, and findings posted on a colleague's
PR cost reviewer credibility when they turn out to be wrong. Verification exists
to spend cheap tokens buying that credibility back.

The rule that makes verification work: **the verifier must never see the reasoning
that produced the finding.** A verifier that reads the finder's argument anchors on
it and defends the claim. One that sees only the claim and the code has to
re-derive it, and drops what does not survive.

## Phase 0 — eligibility and context

Skip the review entirely, and say why, if the PR is closed, is a draft, is
automated, or already carries a review comment from a previous run.

Otherwise gather, with `gh` (never web fetch):

    gh pr view <n> --json title,body,author,state,isDraft,headRefOid,files
    gh pr diff <n>

Capture `headRefOid` — the full SHA is required for permalinks in phase 3. Keep
the diff: phase 1's finder fetches its own, but phase 2's verifiers are handed
theirs by this session.

Review against the root `CLAUDE.md` and any `CLAUDE.md` in directories the PR
touches, never against `CLAUDE.local.md`: local files hold the reviewer's personal
preferences, which are not binding on someone else's PR.

## Phase 1 — find

Do not hand-roll the finder. Invoke the built-in review skill, at high effort,
targeting the PR number, and **without `--comment`**:

    code-review, args: "high <pr-number>"

It already fans out over the diff, applies a tuned false-positive rubric, and runs
its own verification pass, returning each finding with a CONFIRMED or PLAUSIBLE
verdict. Reproducing that by hand yields a weaker prompt.

Omitting `--comment` is what keeps the human gate intact: the skill reports its
findings and posts nothing. Never pass `--comment` here — phase 3 posts, and only
after the user approves.

Do **not** substitute `code-review:code-review`. That command's own steps discard
every finding scored below 80 and then comment on the PR itself. Both defeat this
skill: the 50–79 band never reaches the user, and the comment goes up before
anyone has seen it. Its filter and its self-posting are instructions inside its
prompt, so telling it to report-only is a request the model may not honour — and a
stray `gh pr comment` on a colleague's PR cannot be taken back.

Carry each finding forward with its file, line, one-sentence claim, and verdict.

## Phase 2 — verify the uncertain band

The finder has already verified its own output, so re-checking everything buys
little. Spend the verification pass where it changes a decision.

- **CONFIRMED** findings pass through with a score of 80.
- **PLAUSIBLE** findings go to a verifier — one agent per finding, all launched in
  a single message so they run concurrently.

Each verifier starts fresh and receives exactly: the PR diff, the file and line,
and the one-sentence claim. It must not receive the finder's reasoning, evidence,
or any other finding.

Each returns:

    {"adjusted_score": 0, "verdict": "CONFIRMED|PLAUSIBLE|REJECTED",
     "why": "one sentence naming what in the code decided it",
     "fix": "concrete suggested fix, or null"}

Score 0–100 on confidence the finding is real, using this rubric verbatim:

- **0** — false positive under light scrutiny, or a pre-existing issue.
- **25** — might be real, could not verify. Stylistic points not stated in CLAUDE.md land here.
- **50** — verified real, but possibly a nitpick or rare in practice.
- **75** — verified, very likely hit in practice, and the PR's approach is insufficient. Or stated directly in CLAUDE.md.
- **100** — confirmed, with evidence, and frequent in practice.

`REJECTED` forces a score below 50 regardless. A verifier that cannot locate the
code the claim refers to returns `REJECTED`.

## Phase 3 — report, gate, post

Report every finding to the user as a table — file and line, verdict from phase 1,
adjusted score, one-line claim — including the ones that fell below the threshold,
marked as dropped with the drop reason. The score movement between phases is the
signal that verification is earning its keep.

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
