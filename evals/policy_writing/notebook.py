"""Policy-writing evals (marimo): run an AI agent on the eval set and explore what it wrote.

Pick cases by category, choose the agent, press Run. Each case copies a starting
project into a fresh workspace, asks the agent to change the policy, then scores
the result with the hexgate CLI (validate, dry-run decisions, file rules, answer
text). The scorecard shows the pass rate per category; the explorer below shows,
for every case, what the agent was given, what was expected, and what it did.

No run needed to look around: the explorer opens on the newest saved run.

Run with `make policy-eval-notebook`, or
`uv run --with marimo marimo edit evals/policy_writing/notebook.py`.
"""

import marimo

__generated_with = "0.25.0"
app = marimo.App(width="full")


@app.cell
def _():
    import argparse
    import json
    import sys
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from dataclasses import asdict
    from pathlib import Path

    import marimo as mo

    HERE = Path(__file__).resolve().parent
    sys.path.insert(0, str(HERE))
    import viewer  # noqa: E402  (evals/policy_writing is not a package)

    run = viewer.load_run_module()
    CASES = viewer.load_cases()
    return (
        CASES,
        HERE,
        ThreadPoolExecutor,
        argparse,
        as_completed,
        asdict,
        json,
        mo,
        run,
        time,
        viewer,
    )


@app.cell
def _(mo):
    mo.md("""
    # Can an AI write Hexgate policies?

    Each case gives an agent a starting project (tools, roles, maybe an existing
    policy) and a plain-English request, typed exactly as a user would:
    `/write-policy <request>`. It passes only if the policy it writes is valid,
    every sample call gets the expected **allow / deny / approval**, it touches only
    the files it should, and it says so when a request can't or mustn't be done.
    """)
    return


@app.cell
def _(CASES, mo):
    categories = list(dict.fromkeys(c["category"] for c in CASES))
    category_pick = mo.ui.multiselect(
        options=categories, value=categories, label="Categories"
    )
    agent_pick = mo.ui.dropdown(
        options={
            "Claude + write-policy skill": "claude",
            "Reference solutions (should all pass)": "reference",
            "Known-bad answers (should all fail)": "negatives",
        },
        value="Claude + write-policy skill",
        label="Agent",
    )
    repeat = mo.ui.number(start=1, stop=5, value=1, label="Attempts per case")
    # More than 8 Claude agents at once can exhaust a laptop's memory.
    jobs = mo.ui.number(start=1, stop=8, value=6, label="In parallel")
    return agent_pick, category_pick, jobs, repeat


@app.cell
def _(CASES, category_pick, mo):
    in_scope = [c for c in CASES if c["category"] in category_pick.value]
    case_pick = mo.ui.multiselect(
        options={f"{c['id']} · {c['request'][:70]}": c["id"] for c in in_scope},
        value=[f"{c['id']} · {c['request'][:70]}" for c in in_scope],
        label="Cases",
    )
    return (case_pick,)


@app.cell
def _(HERE, agent_pick, case_pick, category_pick, jobs, json, mo, repeat):
    run_button = mo.ui.run_button(label="Run", kind="success")

    def _label(path):
        # "20260924-173138 · claude · 31/36" from the run's own files.
        rows = json.loads(path.read_text())
        report = path.parent / "report.md"
        agent = (
            report.read_text().splitlines()[0].split(":", 1)[-1].strip()
            if report.exists()
            else "?"
        )
        ok = sum(r["passed"] for r in rows)
        return f"{path.parent.name} · {agent} · {ok}/{len(rows)}", agent

    _saved = sorted((HERE / ".runs").glob("*/results.json"), reverse=True)
    _labelled = [(_label(p), p) for p in _saved]
    _options = {lab: str(p) for (lab, _a), p in _labelled}
    # Default to the newest agent run, not a reference or scripted one.
    _default = next(
        (lab for (lab, a), _p in _labelled if a.startswith("claude")),
        next(iter(_options), None),
    )
    saved_pick = mo.ui.dropdown(
        options=_options, value=_default, label="Or show a saved run"
    )
    n = len(case_pick.value) * repeat.value
    mo.vstack(
        [
            mo.hstack(
                [category_pick, agent_pick, repeat, jobs], justify="start", gap=1.5
            ),
            case_pick,
            mo.hstack(
                [
                    run_button,
                    mo.md(
                        f"{n} agent runs"
                        + (
                            " (each takes about a minute with Claude)"
                            if agent_pick.value == "claude"
                            else ""
                        )
                    ),
                    saved_pick,
                ],
                justify="start",
                gap=1.5,
            ),
        ],
        gap=1,
    )
    return run_button, saved_pick


@app.cell
def _(
    CASES,
    HERE,
    ThreadPoolExecutor,
    agent_pick,
    argparse,
    as_completed,
    asdict,
    case_pick,
    jobs,
    json,
    mo,
    repeat,
    run,
    run_button,
    time,
    viewer,
):
    def _run_selected():
        _ids = set(case_pick.value)
        _todo = [
            (c, a) for c in CASES if c["id"] in _ids for a in range(1, repeat.value + 1)
        ]
        _stamp = HERE / ".runs" / time.strftime("%Y%m%d-%H%M%S-notebook")
        _stamp.mkdir(parents=True)
        _args = argparse.Namespace(
            agent=agent_pick.value, agent_cmd=None, model=None, timeout=900
        )
        _done = []
        with mo.status.progress_bar(
            total=len(_todo), title="Running cases", remove_on_exit=True
        ) as _bar:
            with ThreadPoolExecutor(
                max_workers=1 if _args.agent != "claude" else jobs.value
            ) as _pool:
                _futs = [
                    _pool.submit(run.run_case, c, a, _stamp, _args) for c, a in _todo
                ]
                for _f in as_completed(_futs):
                    _r = _f.result()
                    _done.append(_r)
                    _bar.update(
                        subtitle=f"{_r.case}: {'passed' if _r.passed else 'failed'}"
                    )
        _order = {c["id"]: i for i, c in enumerate(CASES)}
        _done.sort(key=lambda r: (_order[r.case], r.attempt))
        (_stamp / "results.json").write_text(
            json.dumps(
                [{**asdict(r), "passed": r.passed} for r in _done],
                indent=2,
                ensure_ascii=False,
            )
        )
        run.write_report(_done, _stamp, agent_pick.selected_key, set())
        return viewer.results_from_records(
            [{**asdict(r), "passed": r.passed} for r in _done],
            agent_pick.selected_key,
            _stamp.name,
        )

    # None until Run is pressed, so the cells below still show a saved run.
    fresh = _run_selected() if run_button.value else None
    return (fresh,)


@app.cell
def _(fresh, mo, saved_pick, viewer):
    from pathlib import Path as _Path

    results = fresh or (
        viewer.load_results(_Path(saved_pick.value)) if saved_pick.value else None
    )
    mo.stop(results is None, mo.md("_No run yet. Press **Run**._"))
    return (results,)


@app.cell
def _(CASES, mo, results):
    _cat = {c["id"]: c["category"] for c in CASES}
    _rows, _all, _ok = {}, 0, 0
    for _cid, _runs in results["runs"].items():
        _row = _rows.setdefault(_cat.get(_cid, "other"), [0, 0])
        _row[0] += sum(r["passed"] for r in _runs)
        _row[1] += len(_runs)
        _ok += sum(r["passed"] for r in _runs)
        _all += len(_runs)
    _table = [
        {
            "Category": k,
            "Passed": f"{v[0]}/{v[1]}",
            "Rate": f"{100 * v[0] // max(v[1], 1)}%",
        }
        for k, v in _rows.items()
    ]
    mo.vstack(
        [
            mo.md(f"## Scorecard · {results['agent']} · run `{results['stamp']}`"),
            mo.hstack(
                [
                    mo.stat(
                        value=f"{100 * _ok // max(_all, 1)}%",
                        label="Runs passed",
                        caption=f"{_ok} of {_all}",
                    ),
                    mo.stat(
                        value=str(len(results["runs"])),
                        label="Cases",
                        caption="in this run",
                    ),
                ],
                justify="start",
                gap=2,
            ),
            mo.ui.table(_table, selection=None, pagination=False),
        ],
        gap=1,
    )
    return


@app.cell
def _(mo, results, viewer):
    mo.vstack(
        [
            mo.md(
                "## Explorer\nPick a case on the left: its context, what was expected, the reference answer, and what the agent did."
            ),
            mo.iframe(viewer.build_html(results), height="900px"),
        ]
    )
    return


if __name__ == "__main__":
    app.run()
