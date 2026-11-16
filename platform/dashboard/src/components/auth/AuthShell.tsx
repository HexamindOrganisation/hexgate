import { useEffect, useState } from "react";
import { Check, Loader2, type LucideIcon, X } from "lucide-react";
import { Link, useLocation } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { HexgateBrandMark } from "@/components/ui/hexgate-mark";
import { googleOAuthAvailable, startGoogleSignIn } from "@/lib/auth";
import { cn } from "@/lib/utils";

/** The mark's tone: the brand (the app icon) or a status badge. */
type Tone = "brand" | "success" | "error" | "muted";

type MarkIcon = LucideIcon | typeof HexgateBrandMark;

type Mark = { box: string; icon: MarkIcon; size?: string; spin?: boolean };

const MARK: Record<Tone, Mark> = {
  // The app icon itself: the blue keyhole on its dark tile, in either theme.
  brand: {
    box: "rounded-xl bg-[#0b0d12] ring-1 ring-white/10",
    icon: HexgateBrandMark,
    size: "size-7",
  },
  success: { box: "rounded-full bg-success/15 text-success", icon: Check },
  error: { box: "rounded-full bg-deny/10 text-deny", icon: X },
  // The "working…" states: a spinner, so it can't show frozen.
  muted: {
    box: "rounded-full bg-muted text-muted-foreground",
    icon: Loader2,
    spin: true,
  },
};

/**
 * The shared pre-login layout: a centred column with the keyhole brand mark
 * (or a status badge), a title and optional description, then the page's own
 * content in a card. Every /sign-in, onboarding and invite screen uses it, so
 * they read as one surface.
 */
export function AuthShell({
  tone = "brand",
  icon,
  title,
  description,
  children,
  footer,
  card = true,
  wide = false,
}: {
  tone?: Tone;
  /** Override the tone's default glyph (e.g. a mail icon on success). */
  icon?: LucideIcon;
  title: React.ReactNode;
  description?: React.ReactNode;
  children?: React.ReactNode;
  /** Muted line under the card, for the cross-links. */
  footer?: React.ReactNode;
  /** Wrap `children` in the card (default). Off for a bare CTA. */
  card?: boolean;
  /** A wider column, for a denser card (the OAuth consent). */
  wide?: boolean;
}) {
  // A page's own glyph (e.g. the reset lock) sits on the neutral tile, not
  // the app icon's dark one.
  const mark: Mark =
    icon && tone === "brand"
      ? { box: "rounded-xl bg-action text-action-foreground", icon }
      : MARK[tone];
  const Icon = icon ?? mark.icon;
  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4 py-12">
      <div className={cn("w-full", wide ? "max-w-md" : "max-w-sm")}>
        <div className="mb-6 flex flex-col items-center gap-4 text-center">
          <span className={cn("grid size-11 place-items-center", mark.box)}>
            <Icon
              className={cn(mark.size ?? "size-5", mark.spin && "animate-spin")}
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
          (card ? <Card className="p-6">{children}</Card> : children)}
        {footer && (
          <p className="mt-5 text-center text-sm text-muted-foreground">
            {footer}
          </p>
        )}
      </div>
    </div>
  );
}

/** A cross-link in an auth footer (sign-in ↔ sign-up ↔ reset). Carries the
 * page's router state along, so where the user was headed (an invite, an
 * MCP consent request) survives a detour through sign-up. */
export function AuthLink({
  to,
  children,
}: {
  to: string;
  children: React.ReactNode;
}) {
  const { state } = useLocation();
  return (
    <Link
      to={to}
      state={state}
      className="font-medium text-primary underline-offset-2 hover:underline"
    >
      {children}
    </Link>
  );
}

/** The "or / Continue with Google" block, inside the form card; nothing
 * while the backend has Google sign-in off (HEXGATE_GOOGLE_CLIENT_ID unset). */
export function GoogleContinue({ returnTo }: { returnTo?: string }) {
  const [available, setAvailable] = useState(false);
  useEffect(() => {
    let live = true;
    googleOAuthAvailable().then((ok) => live && setAvailable(ok));
    return () => {
      live = false;
    };
  }, []);
  if (!available) return null;
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
        onClick={() => startGoogleSignIn(returnTo).catch(() => undefined)}
      >
        Continue with Google
      </Button>
    </>
  );
}
