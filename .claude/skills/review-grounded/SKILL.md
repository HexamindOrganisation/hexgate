---
name: review-grounded
description: Code review where every finding must come with a concrete, realistic failure example, and findings whose only example is far-fetched are dropped. Takes a PR URL or number, a branch, or nothing (the current diff). Reports findings in the chat; posts them to the PR as inline comments only when the user says "go". Use when asked to review code, review a PR, or check a diff before pushing; use /code-review instead when the goal is applying fixes.
---

# Grounded Code Review

Same pipeline as `/code-review`, with one extra gate: a finding survives only if
you can write down a failure that would plausibly happen to this product. The
expensive failure mode of a review is not a missed bug, it is a list of twelve
findings where three matter — the author then either fixes noise or stops
reading reviews.

Zero findings is the ordinary outcome, not a failed review. One or two is a
normal PR. If you are about to report five, you have almost certainly gated
leniently — go back and write the Notice line for each one again.

## 0. What this product actually is

The gate is only as good as the facts it tests against:

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

Skip the review if the PR is closed or is an automated/dependency bump. A draft
PR is reviewable.

A PR that already carries your review is reviewable **again** whenever the diff
has moved since: a review certifies the diff as it stood, never the branch
forever. When nothing has landed since your review, skip. A re-review is a
**general review of the whole PR**, never a check of the new hunks: one pass over
the commits added since, aimed where new work meets what the earlier review
passed, plus one lens over the whole artifact driven by realistic starting
states (what is already running, which mode, which data). New work breaks old
lines without touching them. (On PR #190 a `min_length=1` on a required query
parameter was correct when reviewed; a later commit made the parameter optional,
turning it into a 422 on the most common request. On PR #265 five re-review
rounds each covered only the latest fixes; a human then found the skill reused
whatever answered `/health` — a SQLite API under a full pipeline — on a line
none of them had re-read.)

When you review your own work, launch the review from a fresh subagent that gets
only the PR number, never your list of what changed or what you fixed: a
reviewer that shares the author's context confirms the author's framing. Never
narrow the scope for speed without saying so.

## 2. Context

**Get the code on disk first.** Every lens reads real files and real installed
dependency source, so review a local checkout, not a diff hunk. If the PR is not
checked out, give it its own worktree so the current one is untouched:

```bash
gh pr checkout <n> --branch <branch>   # when the repo is already on that branch, skip
git worktree add ../<n> <branch>       # or: a worktree per PR, named for the PR
```

Confirm where you are with `git branch --show-current`, and run every command
from that directory. Then collect, in parallel:

- The root `CLAUDE.md` plus any `CLAUDE.md` in a directory the diff touches.
- The diff (`gh pr diff <n>`, or `git diff <base>...HEAD`). For a stacked PR,
  diff against its real base, not `main`.
- The PR body — it often states a tradeoff deliberately, and a "finding" that
  restates an acknowledged tradeoff is not a finding.

## 3. Review lenses

Run these as parallel subagents, one lens each, all with the same instruction:
read the real files and the real installed dependency source, never assume
behaviour from the name of a setting. Several lenses leave the diff on purpose —
the lines that break are often unchanged ones the diff now feeds.

Where behaviour depends on a state machine inside a dependency — a stream
aggregator, a retry loop, a callback sequencer — **run it** and enumerate what it
emits on each branch: reading proves the path you read, a thirty-line script
proves the space. When you find a bug on one branch, enumerate the rest before
you stop; the branch with the obvious bug is rarely the only one that behaves
unlike your mental model.

1. **CLAUDE.md compliance** — only what the file actually says, quoted.
2. **Correctness** — bugs in the changed lines. The diff's own tests are not
   evidence: written by the reasoning that produced the code, they assert the
   intent and vary one input at a time. Where a parameter has a legitimate
   "absent" value (empty string, zero UUID, `None`), try the combinations —
   absent here *plus* valid there.
3. **History** — `git blame` / `git log -p` on the touched code: is this undoing
   an earlier deliberate decision whose reason still holds?
4. **Prior review comments** — comments on earlier PRs touching these files that
   apply again here.
5. **Availability** — a hang, stall, silent drop or slow drain is as serious as
   an error. Read upstream defaults the diff activates.
6. **Internal consistency** — every number, cross-reference and claim in changed
   comments and docs, checked against the file it points at. Then grep the whole
   repo for what the change makes stale — the issue it resolves, the function it
   reroutes, the command whose behaviour moved — and check every comment and doc
   that names it. Read a changed docs paragraph for what it implies: two true
   sentences in a row ("`validate` fails on X. Pass the manifest to check Y")
   tell the reader Y fails too. (On PR #318 every sentence checked out, yet a
   comment outside the diff still said the CLI "moves onto it in #303".)
7. **Wire compatibility** — when the diff changes an encoding, compression,
   serialization, protocol version or schema, name every other party that reads
   or writes those bytes and verify *that* side supports the new form in its
   pinned version. For an optional codec, grep the reader's lockfile: many are
   gated behind an extra (`aiokafka[zstd]` → `cramjam`). A valid setting proves
   the writer accepts it, never that anything can decode it. Then ask what the
   reader does during the window where only one side is deployed.
8. **Cross-boundary contracts** — when the diff introduces, renames or reads a
   value another package or container must agree on (an env var, a constant
   copied across packages, a URL, a JWT claim or header name, a Kafka topic, a
   path a proxy routes, a table or column), `git grep` the whole repo for that
   name and its siblings and list every reader and writer. Check they agree on
   the name, the default and the normalisation (trailing slash, scheme, case,
   port), and that the agreement is pinned by a drift test or a single source of
   truth, not by convention. A value that must match byte for byte across
   containers with nothing pinning it is a finding; its Trigger is the deploy
   that sets one side and not the other. (PR #376 made `HEXGATE_MCP_ISSUER` the
   only `iss` platform/mcp accepts; PR #387 made `HEXGATE_PUBLIC_URL` the
   issuer platform/api publishes, and the SDK's `AnyHttpUrl` added a trailing
   slash on one side. Each was reviewed alone, so every token was rejected
   until #396.)
9. **Widened input** — when the diff widens what a value can hold (more
   severities, more codes, a new `None`, a second source), list every consumer
   of that value, **unchanged lines included**, and ask whether each was written
   for the old range. For anything a user runs — a CLI command, an endpoint, a
   page — **run it** on inputs in the new range and judge what the user reads:
   per-line markers, counts, the final verdict line, not only the exit code.
   Count repeats too: a check inside a per-item loop whose result does not
   depend on the loop variable prints once per iteration. (On PR #318 `policy
   validate` showed `info`, `warning` and `error` lints all as `⚠` and counted
   every lint in its failure line; on PR #321 a role-independent lint inside the
   per-role pass printed one error three times.)
10. **Input producers** — when the diff judges something against a reference set
    it treats as complete (a manifest's tools, skills or guards, a config list, a
    registry), read the code that **produces** that set — builders, validators,
    caps, dedupes — and list where it is lossy: `None` meaning both "none" and
    "unknown", a list truncated at a cap, entries merged away. Each lossy case is
    a false positive or negative the diff ships. (On PR #321 `unknown-skill` read
    `manifest.skills or []`; the builders write `skills or None` when a listing
    fails and cut at `MAX_SKILLS`, so a correct policy failed CI.)

## 4. The example gate

For each candidate finding, write the example before deciding whether to keep it:

```
Trigger:  what happens in the world (an operation, a request, a deploy, a volume)
Break:    what the code then does wrong
Notice:   who sees it, how, and whether they would act anyway
```

`Notice` decides most findings, so write it last and honestly. "The engineer
sees the error and fixes it" is a reason to drop. The answer that keeps a
finding is "nobody" — the operation returns success, the log line is skipped,
the screen says the opposite of what is true.

The gate applies to every lens. Being in scope makes an item a candidate, never
a finding; a past review comment is a candidate here, not a standing ruling. So
is a lens's "minor", "residual" or "not counting this" note (on PR #321 two of
the five findings a human later raised sat in lens reports under exactly those
labels). Write the examples as each subagent's list arrives — for a list longer
than three, finish its examples before reading the next. Aggregating first and
gating later is how twelve findings get reported.

A finding whose only Notice is "a reader" — a stale comment, a wrong line
number, a misnamed function in a docstring, a commit-message format — has no
Break and is **housekeeping**: one line after the numbered findings, never a
numbered entry. Text a **user** reads is different: CLI output, an API message,
user-facing docs (`docs/**/*.mdx`) are the product, and a false one is a silent
Break — the exit code is right and the user trusts the line that says "3 lints
at or above warning". Gate those like code.

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
  role-independent). The same goes for a code comment that claims a sibling's
  precedent ("same trick as `list_decisions`"): the author asserted the analogy
  while copying the pattern, so name what makes it work there; if the new site
  differs on that axis — rows three orders of magnitude larger, another sort
  key, another call frequency — the borrowed reasoning is the bug.
- **"Rare input"** — grep the repo's docs, fixtures and examples for the shape
  before calling it rare.
- **"Only a warning" / "loud"** — the severity or loudness is a premise a later
  fix can change; write it down so section 7 can find it.

(On PR #321 four of five human findings had been raised and dropped in review:
one on a severity a later fix raised to `error`, one on a sibling that was not
equivalent, one on a misquoted sibling, one as "rare" with nothing checked.)

Two things that are never drop reasons:

- **What the fix would cost.** The gates are about likelihood and visibility,
  nothing else. "It would need a private attribute, a refactor, a new fixture"
  belongs in the recommendation at the end. A finding dropped because the fix
  looked expensive is a finding you never judged.
- **A fact you did not check.** A factual claim in a drop line is finding-grade
  and gets verified like one you meant to report. A wrong fact in a drop is worse
  than a wrong finding: nothing downstream re-reads it.

**Every dismissal names its rule** — a bullet label above, a section 0 fact, or
the Scope clause. A drop that names none is an open finding, and so is one whose
stated reason this section excludes: the cost or size of the fix; "pre-existing"
or "not caused by the diff" when the diff exposes or surfaces it; a premise
nobody checked against the code; "loud" or "unobservable" without saying who
observes it. Each of these was dismissed, then reported by a human:

```
#405  "a rename needs a migration"; "API design, not caused by this diff" (the tool surfaces it)
#377  "loud"; "unobservable" — neither said who observes it
#411  "no deploy value has them" — on three validator findings, never checked
```

**Dismissal audit.** Before every review round after the first, and before the
PR is opened or the tree certified, re-read the dismissed list the run keeps,
every entry from every round. It is a separate pass with the lens list closed:
it judges the dismissals, not the code again. For each entry, check that the
named rule actually applies and that its premise still holds against the
current code — a later fix can change it. An entry that fails comes back as a
finding and goes through the gate.

One exception overrides every drop rule: **a cost claim is settled by measuring,
not by judgement.** If the Break is about resource use — memory, rows scanned,
latency, payload size, a query plan — no drop rule applies until you have a
number. Bring up the container, seed a table at the size the caps actually
permit, read `system.query_log`. It is usually minutes, and the number either
kills the finding or makes it undeniable. "No ClickHouse was running" is a
reason to go and run one, never a verdict. *No traffic yet* is a fair reason to
defer a measured cost, never to drop an unmeasured one. And do not score such a
finding as loud: a query that burns the server-wide memory budget is quiet on
the request that causes it and degrades every concurrent one.

Keep the finding, and say so plainly, when the example is mundane *and* the
Break is silent: a deploy, a restart, a broker that takes twenty minutes to come
back, a retry that lands twice — and afterwards the system reports success while
something is quietly wrong. Mundane means an operational event that happens to
the running product; "someone reads the docstring" is not a trigger, and "the
deploy stops with a clear error" is not a break.

Before reporting, make one adversarial pass over the survivors whose only job is
to kill them: for each, try to name the fact that makes it not matter — no
customers, loud failure, nothing reads it, path doesn't exist. Never assign a
subagent to find a use case *for* a finding; handed a conclusion, it will
manufacture a scenario. The asymmetry is the point.

**Scope.** Out regardless of how good the example is: pre-existing issues the
diff neither causes nor exposes, linter/typechecker/CI catches, missing test
coverage, and style preferences not written in a `CLAUDE.md`. Always in: a
setting the diff adds that the diff's own comments say buys nothing for its goal
— an unrelated optimization riding along carries risk nobody signed up for, so
read such a comment as a reason to question the line.

## 5. Verify what survives

Re-check each surviving finding against the actual code — the endpoint, the
library source, the config file — and state the evidence you read; for a cost
claim the evidence is a measurement. A finding that cannot be verified is either
labelled uncertain or dropped, never reported as fact.

For every test written alongside a fix, name the mutation it would catch. A test
that still passes with the fix reverted is worse than no test: it is the thing
that stops anyone looking again.

## 6. Report

In the chat, most severe first:

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

Dismissal audit: <N> entries, each with its rule, re-checked on <short sha>; <M> reopened
```

The example is the point of this review, so it gets its own blockquote: the same
three lines you wrote at the gate, not a re-summary, one line each. If Trigger
needs two, the example is not mundane enough to have survived.

Cite `path:line`, clickable in the terminal against the reader's checkout. Use a
GitHub permalink (`.../blob/<full sha>/<path>#L<start>-L<end>`, a line of
context either side) only when the code is not on disk.

If nothing survives the gate, say so in one line and list what you checked.
Otherwise state what you would do — fix here, follow-up issue, or accept — and
wait. Do not fix, commit, open issues or post unless asked.

### Posting inline (only on an explicit "go")

When the findings are to land on the PR, they go as **one review with inline
comments**, each on the line it is about, never as one wall-of-text comment
(rule since PR #360). Build it, show it, and post nothing until the user says go.

1. **Split the list**: "Things to fix" (each with its Trigger/Break/Notice block,
   labelled `Fix 1`, `Fix 2` …) and "Tidy-ups" (one or two sentences, a snippet
   when the fix is a few lines). The reader decides what blocks merge from the
   label alone.
2. **Pin every comment to a line in the diff** — GitHub rejects inline comments
   off the hunks. Check with `git diff origin/main...HEAD -U0 -- <file> | grep
   '^@@'`, and re-grep the symbol rather than trusting a subagent's `path:line`
   (on #360 four of nine were off by 3–100 lines). Anchor on the definition or
   the exact text the finding is about, not the file's first line.
3. **The review `body` is one line**: `Reviewed with Claude Code. **N things to
   fix** (inline, marked Fix 1–N), then tidy-ups.` No intro, no test caveat, no
   list of drops. Off-diff housekeeping (a stale comment elsewhere, PR-body
   wording, commit type) goes on the nearest related diff line or is left out.
4. **Write the payload to a file and post it in one call.** `event` stays
   `COMMENT` unless the user asked for an approval or request-changes. Show the
   user the `path:line → comment` table and the exact command, then stop.

```bash
# <job tmp>/<pr>-review.json
{"event": "COMMENT",
 "body": "Reviewed with Claude Code. **N things to fix** (inline, marked Fix 1–N), then tidy-ups.",
 "comments": [{"path": "deploy/devops_demo.py", "line": 132, "side": "RIGHT",
               "body": "**Fix 1 — …**\n\n> **Trigger** …"}]}

gh api repos/<owner>/<repo>/pulls/<n>/reviews --input <job tmp>/<pr>-review.json
gh api repos/<owner>/<repo>/pulls/<n>/comments --jq length   # == number of inline comments
```

### Certify

**The last thing you do: certify the diff.** A review is about one state of the
tree, so record which one — only for a review you carried to completion, and
only once the dismissal audit (section 4) has run on this final tree:

```bash
bash "${CLAUDE_REVIEW_HOOK:-$HOME/.claude/hooks/require-review.sh}" --certify
```

The same script, as a `PreToolUse` hook on `Bash`, blocks `git commit` and
`git push` while the marker does not match. It fingerprints the *content that
will ship* — every path differing from the merge base with upstream, plus
untracked files, by their bytes — so committing and `git add` keep the
certificate while any real edit voids it. If `--certify` refuses because
`/simplify` has not run on the branch, run it, review again, then certify.
Certifying a tree you did not review is forging your own sign-off; starting the
command with `NO_REVIEW=1` is the honest way past a commit that needs no review.
The matcher catches the shapes a person types, not `bash -c "git commit"` or
aliases: it guards against forgetting, not evasion.

## 7. Fixes are diffs

When you apply the findings — or reviewed your own work and fixed as you went —
the fixes are new, unreviewed code, and the code least likely to get a second
pass, because you already believe them. When the reviews ran in subagents this
section was never in your context: re-read it before the first edit.

Before the edit, write in the chat the **invariant** the fix establishes, not
the symptom it removes: "no state survives an aborted run", not "the prompt map
stops growing". A lens hands you one framing; the fix is owed to the problem, so
verify the invariant over every path, not the one the finding named. Then run
these four checks (on PR #306 four round-one fixes skipped them, and round two
found four new bugs, one per skipped check):

- **Grade against what the code falls back to, not the rule alone.** A severity
  judged from a rule's own mode said a misspelled deny "protects nothing"; under
  an allow default the real tool ran unguarded. Enumerate the surrounding state
  (defaults, modes, which pipeline built the input) **from the definitions,
  never from memory**: open the model and the evaluator and list every field
  that changes the outcome — the round-three fix forgot
  `FileToolPolicy.file_scope`, which `evaluate_tool_call` checks three lines
  later. When the invariant is about an evaluator, test it against the
  evaluator: a parametrized test over every mode × default × field, asserting
  the fix agrees with the real verdicts.
- **When a per-item value starts to vary, find what assumed it was uniform.**
  Severity became per-occurrence while an older `flagged` set still kept the
  first occurrence, so the later, fail-open one was dropped. Grep the function
  for dedupe sets, first-wins `continue`s and `setdefault`s the fix makes wrong.
- **A dedupe chooses a survivor; choose it on purpose.** Skipping repeated
  policy objects kept whichever role sorted first — the synthetic `default` —
  and the message named a role the user never wrote.
- **Apply it at every sibling path.** A filter added to the resolved pipeline
  and not the module one left the same input judged two ways. List the callers
  of the shared helper and the parallel implementations before calling it done.

**Re-gate the drops a fix touched.** A fix can make a dropped finding real:
re-read every drop line from this review (and any dismissed list the caller
keeps) whose premise names what the fix changed — a severity, where a check
runs, a code path — and run its example again. The dismissal audit still
re-checks the whole list before the next round.

Each fix gets a test that **fails with the fix reverted** — revert it, run the
test, see red.

Then review again as section 1 describes: the lenses over what the fix changed,
plus the whole-PR pass, and section 4 on what they find. Re-reading your own fix
is not that pass. Applying findings voids the certificate, so the next commit is
blocked until it has run — the point, because this is the step that gets
skipped. A fix can create a finding: a guard applied to one emit and not its
sibling, a cap on the half of the state that drains itself, an early return that
silently drops what the old code recorded. The code around a real finding is
where the next one hides: it was just rewritten under time pressure by someone
who had stopped looking.
