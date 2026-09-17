"""AI Act compliance evidence report.

Spec: https://app.notion.com/p/3e2fb45dbae281f794fefa40ff4a1a8d — it is the
canonical copy, and the repo no longer carries one.

One signed document per project, evidencing the controls Hexgate enforces on
the project's agents and the events it recorded about them over a period.

The slice introduces no enforcement capability: every figure it emits is read
back from data the platform already holds (the resolved policy bundle,
``policy_decision``, ``ban_enforcement``, ``llm_invocation``) plus the
operator's own classification entries.

The signed annex is the canonical artifact; everything under ``render/`` is
downstream of it and reads nothing else.

Nothing this slice emits may state that a system or an operator is compliant.
That is a correctness requirement, not a style preference — the wording lives
in :mod:`hexgate_api.features.ai_act.copy` so it can be reviewed in one place.
"""
