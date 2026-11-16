import { cn } from "@/lib/utils";

/** A round initial for an account; decorative, so screen readers read the
 * email beside it, not the letter. */
export function EmailAvatar({
  email,
  className,
}: {
  email: string;
  className?: string;
}) {
  return (
    <span
      aria-hidden
      className={cn(
        "grid size-7 shrink-0 place-items-center rounded-full bg-primary/20 text-xs font-medium uppercase text-primary",
        className,
      )}
    >
      {Array.from(email.trim())[0] ?? "?"}
    </span>
  );
}
