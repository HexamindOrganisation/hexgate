import { useEffect, useRef, useState } from "react";
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
} from "@/lib/ai-act";
import {
  ApiError,
  api,
  type AgentClassificationRead,
  type OperatorRole,
  type RiskTier,
} from "@/lib/api";

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

  // Seed on the closed→open transition only, so a background refetch of the
  // classification can't overwrite what the operator is halfway through
  // typing (same rule as CreateBanDialog).
  const wasOpen = useRef(false);
  useEffect(() => {
    if (open && !wasOpen.current) setForm(seed(classification));
    wasOpen.current = open;
  }, [open, classification]);

  const save = useMutation({
    mutationFn: () =>
      api.putAgentClassification(
        agentName,
        {
          intended_purpose: form.intended_purpose.trim() || null,
          operator_role: form.operator_role || null,
          risk_tier: form.risk_tier || null,
          // The Annex III reference belongs to a high-risk assertion; any
          // other tier stores none, whatever was picked before.
          annex_iii_point:
            form.risk_tier === "high_risk"
              ? form.annex_iii_point || null
              : null,
          oversight_owner_name: form.oversight_owner_name.trim() || null,
          oversight_owner_contact: form.oversight_owner_contact.trim() || null,
          checker_last_update_date: form.checker_last_update_date || null,
        },
        projectId,
      ),
    onSuccess: () => {
      qc.invalidateQueries({
        queryKey: ["ai-act", "classification", projectId, agentName],
      });
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
                  {RISK_TIERS.map((t) => (
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
                  {ANNEX_III_POINTS.map((p) => (
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
                placeholder="Name of the person assigned"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="cls-contact">Owner contact (optional)</Label>
              <Input
                id="cls-contact"
                value={form.oversight_owner_contact}
                onChange={(e) => set("oversight_owner_contact", e.target.value)}
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

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button disabled={save.isPending} onClick={() => save.mutate()}>
            {save.isPending ? "Saving…" : "Save entry"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
