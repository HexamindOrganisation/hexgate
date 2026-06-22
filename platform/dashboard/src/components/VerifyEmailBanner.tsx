import { useState } from "react";
import { MailWarning } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useRequestVerify, useUser } from "@/lib/auth";

/**
 * Soft banner shown to logged-in but unverified users. Non-blocking —
 * the rest of the dashboard remains usable; verification gates
 * destructive actions (invite teammates, mint API tokens) at the
 * point of use rather than at the shell level.
 *
 * Phase 5 might promote this into a modal interrupt when the user
 * actually tries to mint a token. For now, a header strip is enough.
 */
export function VerifyEmailBanner() {
  const { user } = useUser();
  const requestVerify = useRequestVerify();
  const [resent, setResent] = useState(false);

  if (!user || user.is_verified) return null;

  return (
    <div className="flex items-center gap-3 border-b border-amber-500/30 bg-amber-500/10 px-4 py-2 text-sm">
      <MailWarning className="h-4 w-4 shrink-0 text-amber-400" />
      <span className="flex-1 text-amber-200">
        Your email <span className="font-mono">{user.email}</span> isn't
        verified yet. Some actions stay locked until you confirm it.
      </span>
      {resent ? (
        <span className="text-amber-300">Check your inbox.</span>
      ) : (
        <Button
          variant="outline"
          size="sm"
          disabled={requestVerify.isPending}
          onClick={async () => {
            try {
              await requestVerify.mutateAsync(user.email);
              setResent(true);
            } catch {
              // 202 is the success path; anything else we just leave
              // the button armed — the banner already conveys the state.
            }
          }}
        >
          {requestVerify.isPending ? "Sending…" : "Resend email"}
        </Button>
      )}
    </div>
  );
}
