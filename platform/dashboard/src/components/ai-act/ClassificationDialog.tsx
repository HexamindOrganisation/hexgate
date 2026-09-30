import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  ANNEX_III_POINTS,
  CLASSIFICATION_LINKS,
  OPERATOR_ROLES,
  RISK_TIERS,
  assertsSomething,
} from "@/lib/ai-act";
import {
  ApiError,
  api,
  type AgentClassificationRead,
  type AgentClassificationUpdate,
  type OperatorRole,
  type RiskTier,
} from "@/lib/api";

/** Field caps the endpoint enforces (`AgentClassificationWrite`); mirrored on
 * the inputs so the operator is stopped at the keyboard rather than by a 422
 * after they have written four thousand characters. */
const MAX_PURPOSE = 4096;
const MAX_NAME = 256;

/** Whether picking this tier should drop the Annex III reference. True only
 * for a tier the form itself offers and that is not high-risk. */
function clearsAnnexPoint(tier: string): boolean {
  return tier !== "high_risk" && RISK_TIERS.some((t) => t.value === tier);
}

/** Empty string is "not asserted" everywhere in this form — Radix Select
 * rejects an empty item value, so an unset select simply has no value. */
interface FormState {
  intended_purpose: string;
  operator_role: OperatorRole | "";
  risk_tier: RiskTier | "";
  annex_iii_point: string;
  oversight_owner_name: string;
  oversight_owner_contact: string;
  checker_last_update_date: string;
}

function seed(c: AgentClassificationRead | null): FormState {
  return {
    intended_purpose: c?.intended_purpose ?? "",
    operator_role: c?.operator_role ?? "",
    risk_tier: c?.risk_tier ?? "",
    annex_iii_point: c?.annex_iii_point ?? "",
    oversight_owner_name: c?.oversight_owner_name ?? "",
    oversight_owner_contact: c?.oversight_owner_contact ?? "",
    checker_last_update_date: c?.checker_last_update_date ?? "",
  };
}

/**
 * Records the operator's Article 6 / Annex III assertion about one agent.
 *
 * The form asks and never argues: no field is required to save, and nothing
 * here checks a tier against the agent's tools. A partially filled entry is a
 * first-class state — the inventory list names what is still missing.
 */
export function ClassificationDialog({
  open,
  onOpenChange,
  projectId,
  agentName,
  classification,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  projectId: string;
  agentName: string;
  /** The agent's loaded entry, or null when it has none. Never `undefined`:
   * the caller only mounts this dialog once the entry has resolved, because
   * seeding from a not-yet-loaded entry and then saving would overwrite a
   * recorded assertion — and its provenance — with blanks. */
  classification: AgentClassificationRead | null;
}) {
  const [form, setForm] = useState<FormState>(() => seed(classification));
  const qc = useQueryClient();

  // No closed→open reseed effect here, unlike CreateBanDialog: the caller
  // mounts this dialog only while an entry is being edited and unmounts it on
  // close, so `open` is always true on mount and the `useState` initializer
  // above is the seed. That already gives the property the effect existed for
  // — a background refetch of the classification cannot overwrite what the
  // operator is halfway through typing, because nothing reseeds after mount.
  // If this ever stays mounted across closes, the effect has to come back.

  // The PUT replaces the entry outright, so the body is the whole assertion.
  // Built once here and reused by the submit-guard below, so what the button
  // checks and what the request sends cannot drift apart.
  const body: AgentClassificationUpdate = {
    intended_purpose: form.intended_purpose.trim() || null,
    operator_role: form.operator_role || null,
    risk_tier: form.risk_tier || null,
    // The Annex III reference belongs to a high-risk assertion, so picking a
    // different tier clears it. Only a tier this build actually offers counts
    // as "picked something else", though: the PUT is a full replace, and a
    // tier we have no option for is one whose field we never showed —-
    // clearing on its behalf would delete a recorded reference the operator
    // never touched.
    annex_iii_point: clearsAnnexPoint(form.risk_tier)
      ? null
      : form.annex_iii_point || null,
    oversight_owner_name: form.oversight_owner_name.trim() || null,
    oversight_owner_contact: form.oversight_owner_contact.trim() || null,
    checker_last_update_date: form.checker_last_update_date || null,
  };

  const save = useMutation({
    mutationFn: () => api.putAgentClassification(agentName, body, projectId),
    onSuccess: (saved) => {
      // Write the response straight into the cache rather than only
      // invalidating: the PUT returns the stored entry, so a refetch that
      // fails would otherwise leave the row showing pre-save values next to a
      // "recorded" toast, with nothing scheduled to correct it
      // (refetchOnWindowFocus is off).
      qc.setQueryData(
        ["ai-act", "classification", projectId, agentName],
        saved,
      );
      toast.success(`Classification recorded for ${agentName}`);
      onOpenChange(false);
    },
    onError: (err) => {
      toast.error(
        err instanceof ApiError && err.status === 403
          ? "You don't have permission to record classifications in this project."
          : `Could not record the classification for ${agentName}.`,
      );
    },
  });

  // A recorded tier this build has no option for still has to appear, or the
  // trigger renders blank and reads as "no tier asserted" — the opposite of
  // what the inventory row next to it says.
  const tierOptions = RISK_TIERS.some((t) => t.value === form.risk_tier)
    ? RISK_TIERS
    : [...RISK_TIERS, { value: form.risk_tier, label: form.risk_tier }].filter(
        (t) => t.value !== "",
      );

  // Same rule for the Annex III point, and for the same reason: the server
  // stores any string up to 32 chars, so a value this build has no option for
  // (an older list, or an entry made through the API) would render as the
  // "Select a point" placeholder while a save re-signs the hidden original.
  const annexOptions = ANNEX_III_POINTS.some(
    (p) => p.value === form.annex_iii_point,
  )
    ? ANNEX_III_POINTS
    : [
        ...ANNEX_III_POINTS,
        { value: form.annex_iii_point, label: form.annex_iii_point },
      ].filter((p) => p.value !== "");

  function set<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Classification — {agentName}</DialogTitle>
          <DialogDescription>
            What you enter here is your own determination under Article 6.
            Hexgate records it, together with who recorded it and when, and
            reproduces it in the evidence report. It does not classify systems
            and does not check your entry.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-1">
          <div className="space-y-1.5">
            <Label htmlFor="cls-purpose">Intended purpose</Label>
            <textarea
              id="cls-purpose"
              value={form.intended_purpose}
              onChange={(e) => set("intended_purpose", e.target.value)}
              rows={3}
              maxLength={MAX_PURPOSE}
              placeholder="What this system is intended to do, and for whom."
              className="flex w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
            />
            <p className="text-xs text-muted-foreground">
              Prefilled from the registered manifest description when you have
              not recorded one — save to make it your own entry.
            </p>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-1.5">
              <Label>Your role</Label>
              <Select
                value={form.operator_role || undefined}
                onValueChange={(v) => set("operator_role", v as OperatorRole)}
              >
                <SelectTrigger aria-label="Your role">
                  <SelectValue placeholder="Select a role" />
                </SelectTrigger>
                <SelectContent>
                  {OPERATOR_ROLES.map((r) => (
                    <SelectItem key={r.value} value={r.value}>
                      {r.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-1.5">
              <Label>Risk tier</Label>
              <Select
                value={form.risk_tier || undefined}
                onValueChange={(v) => set("risk_tier", v as RiskTier)}
              >
                <SelectTrigger aria-label="Risk tier">
                  <SelectValue placeholder="Select a tier" />
                </SelectTrigger>
                <SelectContent>
                  {tierOptions.map((t) => (
                    <SelectItem key={t.value} value={t.value}>
                      {t.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          {form.risk_tier === "high_risk" && (
            <div className="space-y-1.5">
              <Label>Annex III point</Label>
              <Select
                value={form.annex_iii_point || undefined}
                onValueChange={(v) => set("annex_iii_point", v)}
              >
                <SelectTrigger aria-label="Annex III point">
                  <SelectValue placeholder="Select a point" />
                </SelectTrigger>
                <SelectContent>
                  {annexOptions.map((p) => (
                    <SelectItem key={p.value} value={p.value}>
                      {p.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}

          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-1.5">
              <Label htmlFor="cls-owner">Human-oversight owner</Label>
              <Input
                id="cls-owner"
                value={form.oversight_owner_name}
                onChange={(e) => set("oversight_owner_name", e.target.value)}
                maxLength={MAX_NAME}
                placeholder="Name of the person assigned"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="cls-contact">Owner contact (optional)</Label>
              <Input
                id="cls-contact"
                value={form.oversight_owner_contact}
                onChange={(e) => set("oversight_owner_contact", e.target.value)}
                maxLength={MAX_NAME}
                placeholder="email or phone"
              />
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="cls-checker">Checker last update date</Label>
            <Input
              id="cls-checker"
              type="date"
              value={form.checker_last_update_date}
              onChange={(e) => set("checker_last_update_date", e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              The revision date of the checker you relied on, recorded with the
              entry so the report says what you consulted.
            </p>
            <div className="flex flex-wrap gap-x-4 gap-y-1 pt-1">
              {CLASSIFICATION_LINKS.map((l) => (
                <a
                  key={l.url}
                  href={l.url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                >
                  {l.label}
                  <ExternalLink className="size-3" />
                </a>
              ))}
            </div>
          </div>
        </div>

        <DialogFooter className="sm:items-center">
          {!assertsSomething(body) && (
            <span className="mr-auto text-xs text-muted-foreground">
              Fill in at least one field to record an entry.
            </span>
          )}
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            disabled={save.isPending || !assertsSomething(body)}
            onClick={() => save.mutate()}
          >
            {save.isPending ? "Saving…" : "Save entry"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
