import { Check, Hexagon, Loader2, type LucideIcon, X } from "lucide-react";
import { Link } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { startGoogleSignIn } from "@/lib/auth";
import { cn } from "@/lib/utils";

/** The mark's tone: the brand (dark hexagon tile) or a status badge. */
type Tone = "brand" | "success" | "error" | "muted";

const MARK: Record<Tone, { box: string; icon: LucideIcon }> = {
  brand: { box: "rounded-xl bg-action text-action-foreground", icon: Hexagon },
  success: { box: "rounded-full bg-success/15 text-success", icon: Check },
  error: { box: "rounded-full bg-deny/10 text-deny", icon: X },
  muted: { box: "rounded-full bg-muted text-muted-foreground", icon: Loader2 },
};

/**
 * The shared pre-login layout: a centred column with the hexagon brand mark
 * (or a status badge), a title and optional description, then the page's own
 * content in a card. Every /sign-in, onboarding and invite screen uses it, so
 * they read as one surface.
 */
export function AuthShell({
  tone = "brand",
  icon,
  spin = false,
  title,
  description,
  children,
  footer,
  card = true,
}: {
  tone?: Tone;
  /** Override the tone's default glyph (e.g. a mail icon on success). */
  icon?: LucideIcon;
  /** Spin the mark glyph — the "working…" states. */
  spin?: boolean;
  title: React.ReactNode;
  description?: React.ReactNode;
  children?: React.ReactNode;
  /** Muted line under the card, for the cross-links. */
  footer?: React.ReactNode;
  /** Wrap `children` in the card (default). Off for a bare CTA. */
  card?: boolean;
}) {
  const mark = MARK[tone];
  const Icon = icon ?? mark.icon;
  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4 py-12">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex flex-col items-center gap-4 text-center">
          <span className={cn("grid size-11 place-items-center", mark.box)}>
            <Icon
              className={cn("size-5", spin && "animate-spin")}
              strokeWidth={2}
            />
          </span>
          <div className="space-y-1.5">
            <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
            {description && (
              <p className="text-balance text-sm text-muted-foreground">
                {description}
              </p>
            )}
          </div>
        </div>
        {children &&
          (card ? (
            <div className="rounded-[14px] border border-border bg-card p-6">
              {children}
            </div>
          ) : (
            children
          ))}
        {footer && (
          <p className="mt-5 text-center text-sm text-muted-foreground">
            {footer}
          </p>
        )}
      </div>
    </div>
  );
}

/** A cross-link in an auth footer (sign-in ↔ sign-up ↔ reset). */
export function AuthLink({
  to,
  children,
}: {
  to: string;
  children: React.ReactNode;
}) {
  return (
    <Link
      to={to}
      className="font-medium text-primary underline-offset-2 hover:underline"
    >
      {children}
    </Link>
  );
}

/** The "or / Continue with Google" block, rendered inside the form card when
 * Google sign-in is enabled on the backend. */
export function GoogleContinue() {
  return (
    <>
      <div className="relative">
        <div className="absolute inset-0 flex items-center">
          <span className="w-full border-t border-border" />
        </div>
        <div className="relative flex justify-center text-xs uppercase">
          <span className="bg-card px-2 text-muted-foreground">or</span>
        </div>
      </div>
      <Button
        type="button"
        variant="outline"
        className="w-full"
        onClick={() => startGoogleSignIn().catch(() => undefined)}
      >
        Continue with Google
      </Button>
    </>
  );
}
