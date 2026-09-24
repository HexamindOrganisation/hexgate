"""Build a self-contained HTML explorer of the eval set.

    uv run python evals/policy_writing/viewer.py                 # cases only
    uv run python evals/policy_writing/viewer.py --latest        # + the newest run under .runs/
    uv run python evals/policy_writing/viewer.py --results .runs/<stamp>/results.json

Left: the cases by category. Right: the request, the full prompt, what the
agent was given (TOOLS.md and the starting policy), the expectations, the
reference solution as a diff, and, when a run is loaded, every attempt's
checks, answer and changed files. Writes .runs/viewer.html (open it in a
browser; it needs no server).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
RUNS = HERE / ".runs"


def _run_module():
    spec = importlib.util.spec_from_file_location("policy_eval_run", HERE / "run.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod


def read_tree(root: Path) -> dict[str, str]:
    """Every non-hidden file under root, keyed by its relative path."""
    if not root.is_dir():
        return {}
    return {
        str(p.relative_to(root)): p.read_text()
        for p in sorted(root.rglob("*"))
        if p.is_file()
        and not any(part.startswith(".") for part in p.relative_to(root).parts)
    }


def load_results(path: Path) -> dict:
    repo = HERE.parents[1]
    runs: dict[str, list] = {}
    for r in json.loads(path.read_text()):
        ws = repo / r["workspace"]
        runs.setdefault(r["case"], []).append(
            {
                "attempt": r["attempt"],
                "passed": r["passed"],
                "seconds": r["seconds"],
                "answer": r["answer"],
                "checks": r["checks"],
                "files": read_tree(ws) if ws.is_dir() else None,
            }
        )
    report = path.parent / "report.md"
    agent = "unknown"
    if report.exists():
        first = report.read_text().splitlines()[0]
        agent = first.split(":", 1)[-1].strip() if ":" in first else first
    return {"agent": agent, "stamp": path.parent.name, "runs": runs}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--results", type=Path, help="A run's results.json to show alongside the cases."
    )
    ap.add_argument(
        "--latest",
        action="store_true",
        help="Use the newest .runs/<stamp>/results.json.",
    )
    ap.add_argument("--out", type=Path, default=RUNS / "viewer.html")
    ap.add_argument(
        "--bare",
        action="store_true",
        help="Omit the <html>/<head> wrapper (for hosts that add their own).",
    )
    args = ap.parse_args()

    run = _run_module()
    cases = yaml.safe_load((HERE / "cases.yaml").read_text())
    for c in cases:
        c.setdefault("category", "other")
        c.setdefault("expect", {})
        c["prompt"] = run.PROMPT.format(
            request=c["request"],
            workspace="<workspace>",
            layout=run.layout_of(HERE / "fixtures" / c["fixture"]),
        )

    results_path = args.results
    if args.latest:
        stamps = sorted(RUNS.glob("*/results.json"))
        results_path = stamps[-1] if stamps else None
    data = {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "cases": cases,
        "fixtures": {
            p.name: read_tree(p)
            for p in sorted((HERE / "fixtures").iterdir())
            if p.is_dir()
        },
        "solutions": {
            p.name: read_tree(p)
            for p in sorted((HERE / "solutions").iterdir())
            if p.is_dir()
        },
        "results": load_results(results_path) if results_path else None,
    }
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = (HERE / "viewer.html").read_text().replace("__DATA__", blob)
    if not args.bare:
        html = (
            '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            "</head>\n<body>\n" + html + "\n</body>\n</html>\n"
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html)
    print(
        f"wrote {args.out}"
        + (f" (with run {results_path.parent.name})" if results_path else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
