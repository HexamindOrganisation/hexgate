"""AI Act compliance evidence report (Specs/ai_act_evidence_report.md).

One signed document per project, evidencing the controls Hexgate enforces on
the project's agents and the events it recorded about them over a period.

The slice introduces no enforcement capability: every figure it emits is read
back from data the platform already holds (the resolved policy bundle,
``policy_decision``, ``ban_enforcement``, ``llm_invocation``) plus the
operator's own classification entries.

Nothing this slice emits may state that a system or an operator is compliant.
That is a correctness requirement, not a style preference — the wording lives
in :mod:`hexgate_api.features.ai_act.copy` so it can be reviewed in one place.
"""
