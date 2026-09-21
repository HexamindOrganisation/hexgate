"""Every fixed string the evidence report emits, in one module.

The report must never state that a system, a project or an operator is
compliant, and must never classify a system — it states which controls were in
place and which events were recorded, and it attributes the risk tier to the
operator who asserted it. Keeping the prose here means a reviewer checks that
rule by reading one file rather than grepping an assembler.

Nothing here is per-report; the assembler supplies every varying value.
"""

from __future__ import annotations

from typing import Final

TITLE: Final = "AI Act compliance evidence report"
SPEC_VERSION: Final = "v0"

# Cover — "what this document is, and is not". The second paragraph is the one
# that keeps the artifact honest; do not soften it.
WHAT_THIS_IS: Final = (
    "This document states which controls Hexgate had in place for this "
    "project's agents during the period below, and which events Hexgate "
    "recorded about those agents in the same period. It is generated from the "
    "project's resolved policy bundles and from the platform's own event log."
)
WHAT_THIS_IS_NOT: Final = (
    "This document is not a statutory filing, not Annex IV technical "
    "documentation and not a fundamental-rights impact assessment. It does "
    "not state that this project, its agents or its operator meet any legal "
    "requirement. It does not classify any AI system: the risk tier and Annex "
    "III reference in section 1 are the operator's own assertions, recorded "
    "here with the date on which they were made and, where the account still "
    "exists, the name of the person who made them. Absence of a recorded "
    "event is not evidence that nothing "
    "happened — see section 4 for what this report does and does not cover."
)

# Section 2, human oversight (Art. 14, 26(2)). One fixed template for every
# agent: v0 generates no prose. It describes mechanisms the platform actually
# operates, so the text has to stay in step with the two Ban kinds and the
# needs_approval outcome.
HUMAN_OVERSIGHT: Final = (
    "Human oversight of this agent rests on two mechanisms. First, approval "
    "gating: where a role's cell in the matrix above reads Approval, a call "
    "made under that role alone is not executed on the caller's authority — "
    "the SDK returns a needs-approval decision to the host application, which "
    "must route the call to a person before it runs. This is a per-role "
    "statement and not a guarantee about every caller: a caller carrying "
    "several roles is resolved to the most permissive of their cells, so "
    "another role reading Allow for the same tool removes the gate for that "
    "caller. Second, two operator kill switches: an "
    "agent ban stops every call by the named agent, and a user ban stops "
    "every call made on behalf of the named user. Both are evaluated at the "
    "SDK's invoke-time gate ahead of policy, take effect on the agent's next "
    "ban-feed refresh, and are recorded — creation, the operator who created "
    "them, and every blocked attempt (section 3)."
)
OVERSIGHT_OWNER_NOTE: Final = (
    "The named person below is the oversight owner the operator recorded for "
    "this agent (Art. 26(2)). Hexgate records the name; it does not verify it."
)

# Section 2, Art. 15 data-protection controls. Fixed content: one row per
# control the product ships, each naming where it runs, what it does, what it
# applies to, and whether this report can evidence it firing.
#
# ``registration`` is load-bearing and must not be dropped: the three secret
# plugins are guards the operator registers in their OWN code
# (``create_agent(guards=[...])``), so the platform cannot see whether a given
# agent has them wired. The table therefore states what each control does, not
# that it is switched on.
DATA_PROTECTION_CONTROLS: Final = (
    {
        "control": "key-based redaction",
        "runs_in": "SDK, before an event leaves the host process",
        "effect": (
            "replaces the value under any key matching password, passwd, "
            "secret, token, apikey (with or without a - or _ separator), "
            "credential or authorization with [REDACTED]"
        ),
        "applies_to": (
            "the audit copy of a tool call's arguments (substring key match) "
            "and the policy-fact attribute bag (whole-key match)"
        ),
        "registration": "always on; not operator-configurable",
        "evidenced_here": (
            "yes — every argument snapshot in section 3 passed through it"
        ),
    },
    {
        "control": "secret_guard",
        "runs_in": "SDK, before-guard at the tool boundary",
        "effect": ("refuses the call when its arguments carry a probable credential"),
        "applies_to": "tool call arguments",
        "registration": (
            "registered by the operator in host code; the platform cannot "
            "observe whether this agent has it wired"
        ),
        "evidenced_here": (
            "yes — a refusal is recorded as a decision with error type "
            "guard_denied, counted under guard refusals in section 3"
        ),
    },
    {
        "control": "secret_redactor",
        "runs_in": "SDK, before-guard at the tool boundary",
        "effect": (
            "strips the credential from the arguments and lets the call proceed"
        ),
        "applies_to": "tool call arguments",
        "registration": (
            "registered by the operator in host code; the platform cannot "
            "observe whether this agent has it wired"
        ),
        "evidenced_here": (
            "no — the SDK emits no event when it redacts, so this report "
            "cannot show whether it fired; see the section 4 gap "
            '"Redactor and watch hits do not leave the SDK"'
        ),
    },
    {
        "control": "secret_watch",
        "runs_in": "SDK, after-guard, observe only",
        "effect": (
            "flags a probable credential in a tool's result; does not alter "
            "the result or stop the call"
        ),
        "applies_to": "tool call results",
        "registration": (
            "registered by the operator in host code; the platform cannot "
            "observe whether this agent has it wired"
        ),
        "evidenced_here": (
            "no — the observation stays in the host process; see the section 4 "
            'gap "Redactor and watch hits do not leave the SDK"'
        ),
    },
    {
        "control": "payload cap",
        "runs_in": "SDK before transmission, and the platform at ingest",
        "effect": (
            "the SDK replaces an over-cap payload with a marked, truncated "
            "preview; the platform's direct-ingest endpoint refuses an "
            "over-cap payload outright rather than storing a silently "
            "trimmed one"
        ),
        "applies_to": (
            "the arguments column (8 KiB) and the hint and attributes "
            "columns (4 KiB each) of a decision event"
        ),
        "registration": "always on; not operator-configurable",
        "evidenced_here": (
            "yes — a truncated snapshot in section 3 carries a _truncated "
            "marker and its original byte length"
        ),
    },
)

DENY_BY_DEFAULT: Final = (
    "The matrix is complete for the tools named in the resolved bundle: every "
    "role has a cell for every listed tool. A Deny cell does not say why: the "
    "role's policy may refuse the tool outright, may never have granted it, or "
    "may have granted it under a condition nothing can satisfy. Read a Deny as "
    "the standing answer, not as evidence of an authored rule. For tools the "
    "bundle "
    'does not name, the standing answer is the "any other tool" row below '
    "rather than an assumption — deny-by-default is the usual posture, but a "
    "role whose policy sets a permissive fallback really does reach tools it "
    "never lists, and this report states what that role's policy says instead "
    "of asserting a control over it. Each cell is that one role's own mode. At "
    "call time a caller carrying several roles is resolved to the most "
    "permissive of their cells, which this per-role table does not show."
)

ANY_OTHER_TOOL_NOTE: Final = (
    "Each role's standing authorisation for an ordinary tool the bundle does "
    "not name. It does not speak for reaching another agent — those rules, "
    "where the policy declares any, appear as their own rows in the grid "
    "above."
)
# Whether either agent-level gate runs is a separate, opt-in signal from the
# policy (PolicySet.declares_admission / declares_reach), so the report derives
# both from the grid rather than asserting either one. They are reported
# separately because they engage separately: a policy may declare admission and
# no reach, or the reverse, and one statement covering both would be false in
# each of those cases.
AGENT_REACH_DECLARED: Final = (
    "The agent.tool: and agent.handoff: rows above are this policy's declared "
    "rules for reaching another agent. Whether a given framework can enforce "
    "them depends on its adapter exposing the delegation target."
)
AGENT_REACH_NOT_DECLARED: Final = (
    "This policy declares no rules for reaching another agent, so the "
    "agent-reach gate does not run for this agent and this report evidences "
    "no control over which other agents it may reach."
)
AGENT_ADMISSION_DECLARED: Final = (
    "The agent.run row above is this policy's declared rule for which roles "
    "may start this agent at all. Whether it is enforced depends on the "
    "framework: some adapters have no run-entry hook to apply it at, and log "
    "that the run proceeded without an admission check rather than refusing "
    "it. This report is generated from the policy and cannot see which "
    "framework wraps the agent."
)
AGENT_ADMISSION_NOT_DECLARED: Final = (
    "This policy declares no admission rule, so the admission gate does not "
    "run for this agent and this report evidences no control over which roles "
    "may start it."
)
ALIASED_DEFAULT_NOTE: Final = (
    "This policy declares no default role, so the default column above "
    "duplicates the role {role}. It is not a baseline the operator authored, "
    "and it should not be read as one."
)

MATRIX_UNAVAILABLE_NO_BUNDLE: Final = (
    "No resolved policy bundle is stored for this agent, so no authorisation "
    "matrix could be derived. Until a bundle compiles, the SDK falls back to "
    "evaluating the agent's policy document directly, which this report does "
    "not tabulate."
)

# Section 5 — stated as an integrity check, never as a legal attestation.
SIGNATURE_STATEMENT: Final = (
    "The signature below is an integrity check over the bytes of this "
    "report's annex. It shows that the annex is the document this platform "
    "generated and that it has not been altered since. It is not a legal "
    "attestation, and it makes no claim about the accuracy of what the annex "
    "records or about anyone's compliance."
)
VERIFICATION_RECIPE: Final = (
    "1. Download the annex and compute its SHA-256 digest over the exact "
    "bytes received; it must equal the digest published with the annex.",
    "2. Fetch the platform's public signing key from the JWKS path in this "
    "section, resolved against the host this report was obtained from, and "
    "select the key whose fingerprint equals the kid in this section.",
    "3. Verify the Ed25519 signature published with the annex against that "
    "public key, over the 32 raw digest bytes from step 1 — not over the "
    "annex itself, and not over the hex text of the digest.",
)

# Section 4 — what a reader must not read into the numbers.
CAVEATS: Final = (
    "Retention: events older than the retention window shown for each table "
    "above have been deleted by the store's TTL, so a period reaching past it "
    "is covered only from the earliest surviving event.",
    "Clock skew: occurred_at is stamped by the host process and may drift "
    "from the platform's received_at. The period filter is applied to "
    "occurred_at, so an event whose host clock was wrong can fall in the "
    "wrong period.",
    "Identity: user_id and the role set are asserted by the host application "
    "when it calls the SDK. Hexgate records them; it does not authenticate "
    "the end user behind them.",
    "Delivery: an event reaches the store through the host process and a "
    "buffered transport. A host that crashed before flushing, or a dropped "
    "batch, leaves no record and no marker, so these counts are a lower "
    "bound on what happened.",
    "Deduplication: counts are over distinct event ids, so a retried send is "
    "counted once even before the store's background merge collapses it.",
)
NOT_EVIDENCED: Final = (
    "That any tool call actually ran. The platform records the decision, not "
    "the execution: an allowed call the host never made looks identical to "
    "one it made.",
    "The outcome of a call that required approval — whether a person granted "
    "it, who they were, and whether the call then ran.",
    "Anything about a tool call made outside the SDK's gate. Only calls that "
    "went through Hexgate are recorded.",
    "The correctness of the operator's risk-tier and Annex III assertions in "
    "section 1, or of the oversight owner named there.",
)

MATRIX_SOURCE_DRIFTED: Final = (
    "The agent's policy document has changed since the stored bundle was "
    "compiled, so no matrix is shown: tabulating the current document would "
    "describe rules the SDK is not enforcing, and the compiled bundle itself "
    "is opaque. Saving the policy again recompiles the bundle."
)
MATRIX_PROJECT_UNRESOLVED: Final = (
    "This project composes its policy from modules, and the module set does "
    "not currently compose, so no resolved policy was available to tabulate. "
    "The agents keep enforcing their last successfully compiled bundle; this "
    "report cannot show what that bundle authorises."
)
MATRIX_SOURCE_UNREADABLE: Final = (
    "The policy the stored bundle was compiled from could not be re-read as a "
    "policy set, so no matrix could be derived from it."
)

# Section 4, records covered. One line per event table saying what a row in it
# means, so a reader knows what a count is a count OF.
TABLE_CONTENTS: Final = {
    "policy_decision": (
        "one row per policy decision at the SDK's tool gate — allow, deny or "
        "approval required, including guard refusals"
    ),
    "ban_enforcement": (
        "one row per call refused by an operator kill switch, before any "
        "policy evaluation"
    ),
    "llm_invocation": (
        "one row per model call an instrumented agent made, with its token "
        "counts and outcome"
    ),
}


# Section 1 and 3 lead-ins. Here rather than inline in the assembler so the
# conformity-claim test, which reads this module's source, covers them too.
INVENTORY_ATTRIBUTION: Final = (
    "Every assertion in this section is the operator's, recorded on the date "
    "shown; Hexgate stores it and does not assess it. Where a row carries no "
    "recorder, the account that recorded it has since been erased — the "
    "assertion and its date stand, but the name no longer does. Such a row is "
    "still listed as complete, because completeness is about the fields the "
    "operator filled in."
)
DECISION_SAMPLE_NOTE: Final = (
    "A bounded sample of decisions in the period, newest first — not the full "
    "log. Argument snapshots are the redacted, capped copies described in "
    "section 2."
)
APPROVAL_SAMPLE_NOTE: Final = (
    "A bounded sample of calls that required approval, newest first. Whether "
    "approval was then granted is not recorded — see section 4."
)
BAN_ENFORCEMENT_NOTE: Final = (
    "Every blocked attempt recorded in the period, up to the cap this report "
    "applies; total is the unpaginated count, and truncated says whether the "
    "cap clipped the list."
)
