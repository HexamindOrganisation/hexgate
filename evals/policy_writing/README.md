# Policy-writing eval set

This set measures how well an AI agent turns a plain-English request into a
Hexgate policy. It is developer tooling: nothing here ships or runs in the
product.

- `cases.yaml` has 19 cases. Each gives a request, a starting fixture, and what the finished policy must do.
- `fixtures/` holds the starting projects:
  - `flat`: a single role-keyed file;
  - `agents`: sub-agents, admission and reach;
  - `modules`: boundaries, capabilities and `roles.yaml`.

  Each fixture's `TOOLS.md` lists the real tool and argument names.
- `solutions/` holds one hand-written answer per case. It proves every case can be solved and that its expectations are right.
- `run.py` runs an agent on each case and scores the result.

## Scoring

The fixture is copied into a fresh workspace under `.runs/` and the agent
edits it. The run then passes only if all of these hold:

1. **Valid:** `hexgate policy validate` succeeds for a single file. For modules, `hexgate policy check` and `resolve` both succeed.
2. **Decisions:** every `decisions` entry gives the expected outcome under `hexgate policy test`. Many different YAMLs can pass; the behaviour is what's scored.
3. **Files:** the `changed` and `unchanged` lists hold. For example, a team request must not edit the security boundary.
4. **Answer:** for requests the policy language can't express, or must not grant, the final answer contains one of the `mentions_any` words.

The report also flags any edit outside the workspace.

## Run it

```bash
# The dataset itself: must print 19 / 19.
uv run python evals/policy_writing/run.py --agent reference

# Claude Code with the write-policy skill. Output varies, so repeat.
uv run python evals/policy_writing/run.py --agent claude --repeat 3 --jobs 4

# A subset, by id prefix.
uv run python evals/policy_writing/run.py --agent claude --case m0 --case a04

# Any other agent, e.g. the future LangChain one. The command gets
# $CASE_PROMPT, $CASE_REQUEST and $CASE_WORKSPACE, edits the workspace,
# and prints its final answer on stdout.
uv run python evals/policy_writing/run.py --agent-cmd "python my_agent.py"
```

Each run writes `.runs/<stamp>/report.md` (a pass/fail table) and
`results.json` (every check, plus the agent's answer). Each workspace keeps
the agent's files and `.answer.md` for inspection.

## Adding a case

1. Add an entry to `cases.yaml`. Put in the boundary values (the limit itself, and the limit plus one), a negative case, and a role that must stay unaffected.
2. Add its solution under `solutions/<id>/`: the files to overwrite, plus `ANSWER.md` when the case checks the answer text.
3. `uv run pytest tests/evals` must stay green.
