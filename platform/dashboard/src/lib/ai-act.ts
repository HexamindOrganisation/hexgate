/**
 * Governance tab domain helpers: the vocabulary the classification form offers,
 * display copy for the server's completeness verdict, the default reporting
 * period, and the save-a-download plumbing the history list uses.
 *
 * Copy rule for everything under /ai-act — the tab states the controls in
 * place and the events recorded. It never says a system or its operator is
 * compliant, and it never classifies a system: the operator asserts the risk
 * tier and Hexgate records who asserted it and when.
 */

import type {
  AgentClassificationRead,
  AgentClassificationUpdate,
  OperatorRole,
  RiskTier,
} from "./api";

/** ClickHouse keeps every audit table 180 days
 * (`platform/clickhouse/init/schema.sql`, `TTL … + INTERVAL 180 DAY`), so
 * that is the widest period a report can evidence. */
export const RETENTION_DAYS = 180;

/** Default reporting period: the full retention window ending today. */
export function retentionPeriod(now: Date = new Date()): {
  from: Date;
  to: Date;
} {
  return {
    from: new Date(now.getTime() - RETENTION_DAYS * 86_400_000),
    to: now,
  };
}

/** `Date` → the `YYYY-MM-DD` an `<input type="date">` wants. UTC, so the
 * value shown is the same day the bounds below are built from. */
export function toDateInput(d: Date): string {
  return d.toISOString().slice(0, 10);
}

/** A picked day → the instant that opens it, UTC. */
export function periodStart(day: string): string {
  return new Date(`${day}T00:00:00.000Z`).toISOString();
}

/** A picked day → the instant that closes it, UTC, so a period ending today
 * covers everything recorded today. */
export function periodEnd(day: string): string {
  return new Date(`${day}T23:59:59.999Z`).toISOString();
}

export const OPERATOR_ROLES: { value: OperatorRole; label: string }[] = [
  { value: "provider", label: "Provider" },
  { value: "deployer", label: "Deployer" },
];

export const RISK_TIERS: { value: RiskTier; label: string }[] = [
  { value: "high_risk", label: "High-risk" },
  { value: "not_high_risk", label: "Not high-risk" },
  { value: "prohibited", label: "Prohibited" },
  { value: "minimal", label: "Minimal" },
];

/** Display copy for a recorded tier. A value this dashboard does not know
 * falls through as itself rather than as null: the server counts it as a
 * recorded assertion, so rendering it as the "not asserted" dash would show a
 * complete entry with an empty tier. */
export function riskTierLabel(tier: string | null): string | null {
  if (!tier) return null;
  return RISK_TIERS.find((t) => t.value === tier)?.label ?? tier;
}

/**
 * Annex III of Regulation (EU) 2024/1689, one entry per point. Labels are
 * shortened for a dropdown — the operator determines the point against the
 * Regulation itself and the checkers linked beside the field, not against
 * this list.
 */
export const ANNEX_III_POINTS: { value: string; label: string }[] = [
  { value: "1(a)", label: "1(a) — Remote biometric identification" },
  { value: "1(b)", label: "1(b) — Biometric categorisation" },
  { value: "1(c)", label: "1(c) — Emotion recognition" },
  { value: "2", label: "2 — Critical infrastructure safety components" },
  { value: "3(a)", label: "3(a) — Education: access and admission" },
  { value: "3(b)", label: "3(b) — Education: evaluating learning outcomes" },
  {
    value: "3(c)",
    label: "3(c) — Education: assessing the level of education",
  },
  {
    value: "3(d)",
    label: "3(d) — Education: monitoring students during tests",
  },
  { value: "4(a)", label: "4(a) — Employment: recruitment and selection" },
  { value: "4(b)", label: "4(b) — Employment: terms, promotion, monitoring" },
  { value: "5(a)", label: "5(a) — Eligibility for essential public benefits" },
  { value: "5(b)", label: "5(b) — Creditworthiness and credit scoring" },
  { value: "5(c)", label: "5(c) — Life and health insurance risk and pricing" },
  { value: "5(d)", label: "5(d) — Emergency call triage and dispatch" },
  { value: "6(a)", label: "6(a) — Law enforcement: risk of becoming a victim" },
  { value: "6(b)", label: "6(b) — Law enforcement: polygraphs" },
  { value: "6(c)", label: "6(c) — Law enforcement: reliability of evidence" },
  { value: "6(d)", label: "6(d) — Law enforcement: risk of (re-)offending" },
  { value: "6(e)", label: "6(e) — Law enforcement: profiling" },
  { value: "7(a)", label: "7(a) — Migration: polygraphs" },
  { value: "7(b)", label: "7(b) — Migration: risk assessment of entrants" },
  { value: "7(c)", label: "7(c) — Migration: asylum, visa, residence permits" },
  { value: "7(d)", label: "7(d) — Migration: identifying natural persons" },
  { value: "8(a)", label: "8(a) — Justice: assisting a judicial authority" },
  { value: "8(b)", label: "8(b) — Democratic processes: influencing a vote" },
];

/** References the operator consults when determining the tier and the Annex
 * III point. Linked from the form rather than restated in it — the
 * determination is theirs, and the checkers move faster than this code. */
export const CLASSIFICATION_LINKS: { label: string; url: string }[] = [
  {
    label: "AI Act Service Desk",
    url: "https://ai-act-service-desk.ec.europa.eu/en",
  },
  {
    label: "Commission compliance checker",
    url: "https://ai-act-service-desk.ec.europa.eu/en/eu-ai-act-compliance-checker",
  },
  {
    label: "FLI compliance checker",
    url: "https://artificialintelligenceact.eu/assessment/eu-ai-act-compliance-checker/",
  },
];

/**
 * Display copy for the wire field names `AgentClassificationRead.missing_fields`
 * carries. The endpoint decides *which* fields are missing and in what order —
 * it owns the completeness rule so the report and this tab cannot disagree —
 * and the dashboard only names them.
 *
 * The map covers every field the entry has, not just the ones completeness
 * currently requires: `oversight_owner_contact` and `checker_last_update_date`
 * are optional today, and are here so a later rule that requires one is
 * labelled rather than shown raw.
 */
export const CLASSIFICATION_FIELD_LABELS: Record<string, string> = {
  intended_purpose: "Intended purpose",
  operator_role: "Your role",
  risk_tier: "Risk tier",
  annex_iii_point: "Annex III point",
  oversight_owner_name: "Human-oversight owner",
  oversight_owner_contact: "Owner contact",
  checker_last_update_date: "Checker last update date",
};

/** The server's missing-field list as display copy, in the order it sent. An
 * unrecognised name falls through as itself rather than being dropped: a
 * field this dashboard has not heard of is still a gap the operator must see. */
export function missingFieldLabels(
  c: AgentClassificationRead | null,
): string[] {
  if (!c) return [];
  return c.missing_fields.map((f) => CLASSIFICATION_FIELD_LABELS[f] ?? f);
}

/**
 * Whether a form body asserts anything at all.
 *
 * The PUT rejects a wholly empty body (422): a replace that asserts nothing
 * would name an accountable recorder against no assertions, and wipe a
 * complete entry if the form submitted before it had loaded. Checked here too
 * so the operator gets a disabled button instead of a round trip.
 */
export function assertsSomething(body: AgentClassificationUpdate): boolean {
  return Object.values(body).some((v) => v !== null && v !== "");
}

/** Hand a fetched blob to the browser as a file save. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Not in this tick: Safari starts the download asynchronously after the
  // click, so revoking synchronously can hand the user a truncated or
  // zero-byte file. The URL is per-blob, so holding it a beat costs nothing.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
