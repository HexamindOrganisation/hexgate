"""Caller attributes a policy reads that no audit row shows the agent sending.

The SDK checks the tools, skills, guards and arguments a policy names against
the manifests (`DRIFT_CODES`, `policy.py`, per R-POL-003). Attributes are set per
request and are in no manifest, so `analyze_policy` has nothing to check them
against: the known ones come from `audit.json` (`sources.py`), and this is the
one check the eval keeps.
"""

from __future__ import annotations

from collections.abc import Iterator

from hexgate.security.constraints import iter_arg_refs, parse_constraint
from hexgate.security.models import AgentPolicy
from hexgate.security.policy_set import PolicySet


def _live_constraint_lines(p: AgentPolicy) -> Iterator[tuple[str, str]]:
    """Each constraint in `p` that can run, with where it sits: `policy-level`,
    `default_policy`, or the tool key it's on.

    A deny rule's constraints are skipped: a deny never evaluates them, and the
    SDK's checks skip them too."""
    yield from (("policy-level", c) for c in p.constraints)
    rules = [("default_policy", p.default_policy), *p.effective_tools.items()]
    for where, rule in rules:
        if rule.mode != "deny":
            yield from ((where, c) for c in rule.constraints)


def unknown_attrs(policy_set: PolicySet, attrs: set[str]) -> list[str]:
    """`ctx.*` paths a constraint reads that aren't in `attrs`, by what they
    apply to."""
    bad = set()
    for role in policy_set.roles:
        for where, text in _live_constraint_lines(policy_set.policy_for(role)):
            bad |= {
                f"{where}: {'.'.join(path)}"
                for path in iter_arg_refs(parse_constraint(text))
                if path[0] == "ctx" and len(path) > 1 and path[1] not in attrs
            }
    return sorted(bad)
