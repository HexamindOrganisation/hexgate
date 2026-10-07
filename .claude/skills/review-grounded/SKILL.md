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

Skip the review entirely if the PR is closed or is an automated/dependency
bump. A draft PR is reviewable.

A PR that already carries your review is reviewable **again** whenever the diff
has moved since: a review certifies the diff as it stood, never the branch
forever. Re-run it over the commits added since, and spend that pass where the
new work meets what the earlier one passed — a constraint that was right for the
old code is exactly what a later commit invalidates without touching the line.
(On PR #190 a `min_length=1` on a required query parameter was correct when
reviewed; one commit later a second scope made that parameter optional, turning
it into a 422 on the most common request. Nothing re-read it, because the review
had already "happened".) When nothing has landed since your review, skip.

A re-review is still a **general review of the whole PR**, never a check of the
new hunks. Besides the pass over the commits added since, run one lens over the
whole artifact, driven by realistic starting states (what is already running,
which mode, which data), because new work breaks old lines without touching
them. (On PR #265 five re-review rounds each covered only the latest fixes; a
human reviewer then found that the skill reused whatever answered `/health`, a
SQLite API under a full pipeline, on a line none of them had re-read.) When you
review your own work, launch the review from a fresh subagent that gets only the
PR number, never your list of what changed or what you fixed: a reviewer that
shares the author's context confirms the author's framing. Never narrow the
scope for speed without saying so.

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

Where the diff's behaviour depends on a state machine inside a dependency — a
stream aggregator, a retry loop, a callback sequencer — **run it** and enumerate
what it emits on each branch, rather than reading one path and generalising.
Reading proves the path you read; a thirty-line script proves the space. And
when you find a bug on one branch, enumerate the remaining branches before you
stop: the branch that carries the obvious bug is rarely the only one that
behaves unlike your mental model.

1. **CLAUDE.md compliance** — only what the file actually says, quoted.
2. **Correctness** — bugs in the changed lines. The diff's own tests are not
   evidence: they were written by the reasoning that produced the code, so they
   assert the intent rather than probe it, and they almost always vary one
   input at a time. Where a parameter has a legitimate "absent" value (empty
   string, zero UUID, `None`), try the combinations — absent here *plus* valid
   there — which is where that habit leaves a hole.
3. **History** — `git blame` / `git log -p` on the touched code: is this undoing
   an earlier deliberate decision whose reason still holds?
4. **Prior review comments** — comments on earlier PRs touching these files that
   apply again here.
5. **Availability** — a hang, stall, silent drop or slow drain is as serious as
   an error. Read upstream defaults the diff activates.
6. **Internal consistency** — every number, cross-reference and claim in changed
   comments and docs, checked against the file it points at. Then leave the
   diff: grep the whole repo for what the change makes stale — the issue number
   it resolves, the function it reroutes, the command whose behaviour moved — and
   check every comment and doc that names it. And read a changed docs paragraph
   for what it implies, not sentence by sentence: two true sentences in a row
   ("`validate` fails on X. Pass the manifest to check Y") tell the reader Y
   fails too. (On PR #318 every sentence checked out, a comment outside the diff
   still said the CLI "moves onto it in #303", and a human found both.)
7. **Wire compatibility** — when the diff changes an encoding, compression,
   serialization, protocol version or schema, name every other party that reads
   or writes those bytes and verify *that* side supports the new form in its
   pinned version. For an optional codec, grep the reader's lockfile for the
   library implementing it: many are gated behind an extra (`aiokafka[zstd]` →
   `cramjam`) and absent by default. A setting being a valid field proves the
   writer accepts it, never that anything can decode it. Then ask what the
   reader does with the new bytes during the window where only one side is
   deployed.
8. **Widened input** — when the diff widens what a value can hold (more
   severities, more codes, a new `None`, a second source), list every consumer
   of that value, **unchanged lines included**, and ask whether each was written
   for the old range. The lines that break are not in the diff, so a
   hunk-scoped lens never reads them. For anything a user runs — a CLI command,
   an endpoint, a page — **run it** on inputs that hit the new range and judge
   what the user reads: the per-line markers, the counts, the final verdict
   line, not only the exit code. (On PR #318 rerouting `policy validate` fed it
   `info` / `warning` / `error` lints; its printer, written for warnings only,
   showed all three as `⚠`, and its failure line counted every lint, not the
   ones at the gate. A lens ran the CLI on exactly that input and checked only
   the exit code.) Count what it prints too: a check placed inside a per-role or
   per-item loop repeats once per iteration when its result does not depend on
   the loop variable. (On PR #321 a role-independent lint ran inside the
   per-role pass and `policy check` printed and counted one error three times.)
9. **Input producers** — when the diff judges something against a reference
   set it treats as complete (a manifest's tools, skills or guards, a config
   list, a registry), read the code that **produces** that set — builders,
   validators, caps, dedupes — and list where it is lossy: `None` meaning both
   "none" and "unknown", a list truncated at a cap, entries merged away. Each
   lossy case is a false positive or negative the diff ships. (On PR #321
   `unknown-skill` read `manifest.skills or []`; the builders write
   `skills or None` when a listing fails and cut the list at `MAX_SKILLS`, so a
   correct policy failed CI. Every lens varied `None` vs `[]` at the consumer;
   none read the producer.)

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
how twelve findings get reported. A lens's "minor", "residual" or "not counting
this as a finding" notes are candidates like any other and get their own
example: on PR #321 two of the five findings a human later raised sat in lens
reports under exactly those labels.

A finding whose Notice is "a reader" — a stale comment, a wrong line number, a
misnamed function in a docstring, a commit-message format — has no Break in the
code and is housekeeping, not a finding. Text a **user** reads is different:
CLI output, an API message, user-facing docs (`docs/**/*.mdx`) are the product,
and a false one is a silent Break — the exit code is right, the user trusts the
line that says "3 lints at or above warning" or the docs that say a check fails
CI, and nobody notices it is wrong. Gate those like code. Collect these in one line after the
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
  and the wrong thing is true afterwards. Loud only drops a finding when the
  reaction fixes something real: a check that rejects **correct** input (CI
  failing on a policy that is right) is loud and still a finding, because the
  only reaction it leaves is disabling the check or making the input worse.
- **A value nothing reads.** Before reporting a wrong or missing write, find
  the read: a wire schema, a screen, a query, a branch. If no code path
  consumes the value, a regression in it is invisible and harmless. This kills
  most "the actor/timestamp/flag is not stamped here" findings.
- **Config nobody sets.** Grep before claiming an env var, flag or setting
  matters — if nothing in the repo, compose files or DEPLOY.md sets it, the
  finding rests on a hypothetical deployment. This does **not** cover a
  *dependency's* own default: a flag the vendor ships `default_on` and marks
  experimental is a default that moves in a version bump you will take, and it
  moves silently. The question there is whether upstream can flip it, not
  whether you set it.
- **Three coincidences at once.** Two is a bad day; three is fiction.
- **A code path that does not exist yet.** Constants declared ahead of their
  emitter are deliberate here; the future caller is a different PR's problem.
- **Nothing at all.** If no example can be written, the finding is a theory.
  Drop it.

**Every drop line states its premise, with evidence.** The premise is the fact
the drop rests on ("severity is warning", "same as `_drift`", "no author writes
this"); cite the `file:line` that shows it. Three shapes need more than a
sentence:

- **"Same as <sibling>"** — name the sibling's `file:line` and the axis that
  makes them equivalent, and read it: the sibling may do the opposite, or differ
  on exactly the axis that matters (manifest-gated vs not, per-role vs
  role-independent).
- **"Rare input"** — grep the repo's docs, fixtures and examples for the shape
  before calling it rare.
- **"Only a warning" / "loud"** — the severity or loudness is a premise a later
  fix can change; write it down so section 7 can find it.

(On PR #321 four of five human findings had been raised and dropped in review:
one on a severity a later fix in the same round raised to `error`, one on a
sibling that was not equivalent, one on a sibling misquoted — it names roles, the
drop said it didn't — and one as "rare" with nothing checked.)

Two things that are never drop reasons:

- **What the fix would cost.** The gates above are about likelihood and
  visibility, nothing else. "It would need a private attribute, a refactor, a
  new fixture" belongs in the recommendation you write at the end, never in the
  gate. A finding dropped because the fix looked expensive is a finding you
  never actually judged.
- **A fact you did not check.** Any factual claim in a drop line is
  finding-grade and gets verified exactly as one you intended to report. A wrong
  fact in a drop is worse than a wrong finding: nothing downstream re-reads it,
  so it is never corrected.

One exception overrides every drop rule above: **a cost claim is settled by
measuring, not by judgement.** If the Break is about resource use — memory,
rows scanned, latency, payload size, a query plan — none of the drop rules
apply until you have a number. Start the dependency and measure it: bring up
the container, seed a table at the size the caps actually permit, read
`system.query_log`. It is usually minutes, and the number either kills the
finding outright or makes it undeniable. "No ClickHouse was running, so the
lens could not confirm it" is a reason to go and run one, never a verdict.
Judge urgency afterwards — *this product has no traffic yet* is a fair reason
to defer a measured cost, and never a reason to drop an unmeasured one. Beware
too of scoring such a finding as loud: a query that burns the server-wide
memory budget is quiet on the request that causes it and degrades every
concurrent one.

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

The same suspicion belongs on a comment claiming a sibling's precedent —
"same trick as `list_decisions`", "mirrors the usage path". The author copied
a pattern and asserted the analogy in the same breath, so nothing has checked
that the precondition carried over. Go read the sibling and name what makes it
work there; when the new site differs on that one axis — rows three orders of
magnitude larger, a different sort key, a different call frequency — the
borrowed reasoning is the bug, and the comment is what hid it.

## 5. Verify what survives

Re-check each surviving finding against the actual code — read the endpoint, the
library source, the config file. State the evidence you read; for a cost claim
that evidence is a measurement, not a reading. A finding that
cannot be verified either becomes uncertain-and-labelled or gets dropped; it
never gets reported as fact.

For every test written alongside a fix, name the mutation it would catch. A test
that still passes with the fix reverted is passing for the wrong reason, and it
is worse than no test: it is the thing that stops anyone looking again.

## 6. Report

In the chat, never on GitHub. Most severe first:

```
### Code review

<one line on what came out clean, with the evidence>

N findings:

1. **<what breaks>** — <one sentence on the defect>
   <repo-relative path:line — e.g. platform/collector/config.yaml:126>

   > **Trigger** <the mundane operational event>
   > **Break** <what the code then does wrong>
   > **Notice** <who sees it — and why that is nobody>

Housekeeping (optional): <stale comments, wrong pointers, format nits — one line>
```

The example is the whole point of this review, so it gets its own block rather
than a trailing clause: a blockquote under the finding, one labelled line each
for Trigger, Break and Notice — the same three lines you wrote at the gate in
section 4, not a re-summary. Keep each to one line; if Trigger needs two, the
example is not mundane enough to have survived.

Cite `path:line`, which is clickable in the terminal and points at the checkout
the reader already has. Fall back to a GitHub permalink
(`.../blob/<full sha>/<path>#L<start>-L<end>`, a line of context either side)
only when the code is not on disk — a review of someone else's unfetched branch,
or a finding you are asked to post to GitHub.

Then state what you would do about them — fix here, follow-up issue, or accept —
and wait. Do not fix, commit, or open issues unless asked.

If nothing survives the gate, say that in one line and list what you checked.
That is a real outcome, not a failure to find something.

**Then certify the diff, as the last thing you do.** A review is about one state
of the tree, so record which one:

```bash
bash "${CLAUDE_REVIEW_HOOK:-$HOME/.claude/hooks/require-review.sh}" --certify
```

That same script, as a `PreToolUse` hook on `Bash`, blocks `git commit` and
`git push` while the marker does not match. It fingerprints the *content that
will ship* — every path differing from the merge base with upstream, plus
untracked files, each by its bytes — so committing and `git add` do not
invalidate a review, while any real edit does. Skipping this step does not
quietly ship; it stops the next commit. Run it only for a review you carried to
completion: certifying a tree you did not review is forging your own sign-off,
and starting the command with `NO_REVIEW=1` is the honest way past a commit that
needs no review.

It catches the shapes a person types, not every shape a shell allows — a command
line cannot be soundly parsed by a regex, so `bash -c "git commit"` and shell
aliases pass. That is a missed reminder, not a hole: the gate guards against
forgetting, not against someone evading it.

## 7. Fixes are diffs

When you are asked to apply the findings — or when you reviewed your own work and
fixed as you went — the fixes are new, unreviewed code, and they are the code
least likely to get a second pass, because you already believe them.

Before applying a fix, write the **invariant** it is meant to establish, not the
symptom it removes: "no state survives an aborted run", not "the prompt map stops
growing". Then verify the invariant over every path, not over the path the
finding happened to name. A lens hands you one framing of a problem; the fix is
owed to the problem, and a fix verified against the framing leaves the rest of it
in place.

Write the invariant down — in the chat, before the edit — and then run these four
checks against it. On PR #306 four round-one fixes were applied without them, and
round two found four new bugs, each one a check skipped:

- **Grade against what the code falls back to, not the rule alone.** A severity
  judged from a rule's own mode said a misspelled deny "protects nothing"; under
  an allow default the real tool ran unguarded. Enumerate the surrounding state
  (defaults, modes, which pipeline built the input), not just the rule's fields.
  Enumerate it **from the definitions, never from memory**: open the model and
  the evaluator and list every field that changes the outcome. The round-three
  fix to that same bug counted `constraints` as a rule's restriction and forgot
  `FileToolPolicy.file_scope`, which `evaluate_tool_call` checks three lines
  later. When the invariant is about what an evaluator does, test it against
  the evaluator: a parametrized test over every mode × default × field that
  asserts the fix agrees with the real verdicts settles in seconds what
  reasoning missed twice.
- **When a per-item value starts to vary, find what assumed it was uniform.**
  Severity became per-occurrence while an older `flagged` set still kept the first
  occurrence, so the later, fail-open one was dropped. Grep the function for
  dedupe sets, first-wins `continue`s and `setdefault`s the fix now makes wrong.
- **A dedupe chooses a survivor; choose it on purpose.** Skipping repeated policy
  objects kept whichever role sorted first — the synthetic `default` — and the
  message named a role the user never wrote.
- **Apply it at every sibling path.** A filter added to the resolved pipeline and
  not the module one left the same input judged two ways. List the callers of the
  shared helper and the parallel implementations before calling a fix done.

**Re-gate the drops a fix touched.** A fix can make a dropped finding real:
after applying fixes, re-read every drop line from this review (and any
dismissed list the caller keeps) whose premise names what the fix changed — a
severity, where a check runs, a code path — and run its example again.

Each fix gets a test that **fails with the fix reverted** — revert it, run the
test, see red. And when the reviews ran in subagents, this section was never in
your context while you fixed: re-read it before the first edit.

Then re-enter at **section 3** with fresh lenses over what the fix changed, and
run section 4 on what they find. Re-reading your own fix is not the same pass:
the lenses are what caught the first problem, and the fix is the code least
likely to survive them. Applying findings invalidates the certificate above, so
the next commit is blocked until that pass has run — which is the point, because
this is the step that gets skipped. A fix changes behaviour, so it can
create a finding: a guard applied to one emit and not its sibling, a cap on the
half of the state that drains itself, a new early return that silently drops
what the old code recorded. The code around a real finding is where the next one
hides, because that is the code that was just rewritten under time pressure by
someone who had stopped looking for problems.
