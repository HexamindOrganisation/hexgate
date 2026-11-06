import { useState } from "react";
import { Check } from "lucide-react";

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
import { buildChatFrame } from "@/lib/playground";
import {
  usePlaygroundAccent,
  usePlaygroundPrefs,
} from "@/lib/playground_prefs";
import { cn } from "@/lib/utils";

/** Who the agent thinks it's talking to: the acting name (per project) and
 * roles. Edits are a draft until Apply, and take effect from the next turn. */
export function SessionSettings({
  open,
  onOpenChange,
  projectId,
  roleOptions,
  activeRoles,
  onApply,
  onReset,
  canReset,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  projectId: string;
  roleOptions: string[];
  activeRoles: string[];
  onApply: (roles: string[]) => void;
  onReset: () => void;
  canReset: boolean;
}) {
  const accent = usePlaygroundAccent();
  // A fresh draft per opening, kept mounted while the dialog animates out.
  const [session, setSession] = useState(0);
  const [wasOpen, setWasOpen] = useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) setSession((n) => n + 1);
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* Portalled out of the Playground, so it carries the accent itself. */}
      <DialogContent className="max-w-lg" style={accent}>
        <SettingsForm
          key={session}
          projectId={projectId}
          roleOptions={roleOptions}
          activeRoles={activeRoles}
          onApply={(roles) => {
            // The agent may have changed while the dialog was open: keep the
            // roles it still has, else its current roles, never a silent [].
            const kept = roles.filter((r) => roleOptions.includes(r));
            onApply(kept.length || !roles.length ? kept : activeRoles);
            onOpenChange(false);
          }}
          onCancel={() => onOpenChange(false)}
          onReset={() => {
            onReset();
            onOpenChange(false);
          }}
          canReset={canReset}
        />
      </DialogContent>
    </Dialog>
  );
}

/** Keyed per opening, so the draft starts from the applied values. */
function SettingsForm({
  projectId,
  roleOptions,
  activeRoles,
  onApply,
  onCancel,
  onReset,
  canReset,
}: {
  projectId: string;
  roleOptions: string[];
  activeRoles: string[];
  onApply: (roles: string[]) => void;
  onCancel: () => void;
  onReset: () => void;
  canReset: boolean;
}) {
  const savedName = usePlaygroundPrefs((s) => s.names[projectId] ?? "");
  const setName = usePlaygroundPrefs((s) => s.setName);
  const [name, setDraftName] = useState(savedName);
  const [roles, setRoles] = useState(activeRoles);
  const toggle = (role: string) =>
    // Keep the policy's order, not the click order.
    setRoles((prev) =>
      prev.includes(role)
        ? prev.filter((r) => r !== role)
        : roleOptions.filter((r) => r === role || prev.includes(r)),
    );
  const context = buildChatFrame("", { roles, name }).user_attenuation;

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        setName(projectId, name);
        onApply(roles);
      }}
      className="space-y-5"
    >
      <DialogHeader>
        <DialogTitle>Session settings</DialogTitle>
        <DialogDescription>
          Who the agent thinks it's talking to. Applies from your next message.
        </DialogDescription>
      </DialogHeader>

      <label className="block space-y-1.5">
        <span className="text-sm font-medium">Acting as</span>
        <Input
          value={name}
          onChange={(e) => setDraftName(e.target.value)}
          placeholder="playground"
          className="font-mono"
        />
        <span className="block text-xs text-muted-foreground">
          The caller id the agent sees (
          <span className="font-mono">HexgateContext.user_id</span>).
        </span>
      </label>

      <div className="space-y-1.5">
        <span className="text-sm font-medium">Roles</span>
        {roleOptions.length > 0 ? (
          <>
            <div className="flex flex-wrap gap-2">
              {roleOptions.map((role) => {
                const on = roles.includes(role);
                return (
                  <button
                    key={role}
                    type="button"
                    aria-pressed={on}
                    onClick={() => toggle(role)}
                    className={cn(
                      "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 font-mono text-sm transition-colors",
                      on
                        ? "border-action bg-action text-action-foreground"
                        : "border-border hover:bg-accent",
                    )}
                  >
                    {on && <Check className="size-3.5" />}
                    {role}
                  </button>
                );
              })}
            </div>
            <p className="text-xs text-muted-foreground">
              {roles.length > 1
                ? "The most permissive outcome across these roles wins."
                : roles.length === 1
                  ? "That role's policy decides which tools run."
                  : "No role selected means the default policy decides."}
            </p>
          </>
        ) : (
          <p className="text-xs text-muted-foreground">
            The served agent has no roles yet, so the default policy decides.
          </p>
        )}
      </div>

      <div className="rounded-lg border border-border p-3">
        <div className="flex items-center justify-between text-sm">
          <span className="font-medium">Advanced</span>
          <span className="font-mono text-xs text-muted-foreground">
            sent with each message
          </span>
        </div>
        <pre className="mt-2 whitespace-pre-wrap break-all rounded-md bg-muted px-3 py-2 font-mono text-xs">
          {context
            ? JSON.stringify(context)
            : "nothing: the turn runs as the default caller"}
        </pre>
      </div>

      <DialogFooter className="items-center sm:justify-between">
        <Button
          type="button"
          variant="ghost"
          onClick={onReset}
          disabled={!canReset}
          className="text-deny hover:text-deny"
        >
          Reset conversation
        </Button>
        <div className="flex gap-2">
          <Button type="button" variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <Button type="submit">Apply</Button>
        </div>
      </DialogFooter>
    </form>
  );
}
