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
import { buildChatFrame, callerName } from "@/lib/playground";
import {
  usePlaygroundAccent,
  usePlaygroundPrefs,
} from "@/lib/playground_prefs";
import { cn } from "@/lib/utils";

interface FormProps {
  projectId: string;
  /** The serving agent, or null when none is serving yet. */
  agentName: string | null;
  roleOptions: string[];
  activeRoles: string[];
  /** null when the roles weren't touched, so the applied choice stands. */
  onApply: (roles: string[] | null) => void;
  onReset: () => void;
  canReset: boolean;
}

/** Who the agent thinks it's talking to: the acting name (per project) and
 * roles. Edits are a draft until Apply, and take effect from the next turn. */
export function SessionSettings({
  open,
  onOpenChange,
  session,
  agentName,
  projectId,
  roleOptions,
  activeRoles,
  onApply,
  onReset,
  canReset,
}: FormProps & {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Bumped by the caller on each opening, so the draft starts fresh. */
  session: number;
}) {
  const accent = usePlaygroundAccent();
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* Portalled out of the Playground, so it carries the accent itself. */}
      <DialogContent className="max-w-lg" style={accent}>
        <SettingsForm
          key={session}
          projectId={projectId}
          agentName={agentName}
          roleOptions={roleOptions}
          activeRoles={activeRoles}
          onApply={(roles) => {
            // The agent may have changed while the dialog was open: keep the
            // roles it still has, else leave the choice alone, never a silent [].
            const kept = roles?.filter((r) => roleOptions.includes(r)) ?? [];
            onApply(roles && (kept.length || !roles.length) ? kept : null);
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
  agentName,
  roleOptions,
  activeRoles,
  onApply,
  onCancel,
  onReset,
  canReset,
}: FormProps & { onCancel: () => void }) {
  const savedName = usePlaygroundPrefs((s) => s.names[projectId] ?? "");
  const setName = usePlaygroundPrefs((s) => s.setName);
  const [name, setDraftName] = useState(savedName);
  const [roles, setRoles] = useState(activeRoles);
  // A name-only Apply must not turn the roles shown at opening (maybe none,
  // while serve was down) into a choice.
  const [rolesTouched, setRolesTouched] = useState(false);
  const toggle = (role: string) => {
    setRolesTouched(true);
    // Keep the policy's order, not the click order.
    setRoles((prev) =>
      prev.includes(role)
        ? prev.filter((r) => r !== role)
        : roleOptions.filter((r) => r === role || prev.includes(r)),
    );
  };
  // Untouched roles aren't applied (Apply sends null), so the turn carries
  // the live roles: preview those, not the draft captured at opening.
  const sentRoles = rolesTouched ? roles : activeRoles;
  const context = buildChatFrame("", {
    roles: sentRoles,
    name,
  }).user_attenuation;

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        setName(projectId, name);
        onApply(rolesTouched ? roles : null);
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
          // What an empty name sends, by the same rule as the turn itself.
          placeholder={callerName("", sentRoles) ?? "default caller"}
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
            {agentName
              ? "The served agent has no roles yet, so the default policy decides."
              : "No agent is serving yet; its roles show here once it connects."}
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
