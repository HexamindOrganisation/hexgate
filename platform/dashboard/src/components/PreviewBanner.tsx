import { FlaskConical } from "lucide-react";

/**
 * Beta "preview" strip — sets the expectation that accounts and data may be
 * reset. Opt-in via VITE_PREVIEW_BANNER (set only in the deploy build) so
 * local dev and any non-beta build stay clean.
 */
const PREVIEW_ON = ["1", "true", "yes", "on"].includes(
  (import.meta.env.VITE_PREVIEW_BANNER ?? "").toLowerCase(),
);

export function PreviewBanner() {
  if (!PREVIEW_ON) return null;

  return (
    <div className="flex items-center gap-3 border-b border-sky-500/30 bg-sky-500/10 px-4 py-2 text-sm">
      <FlaskConical className="h-4 w-4 shrink-0 text-sky-400" />
      <span className="flex-1 text-sky-200">
        Preview — beta environment. Accounts and data may be reset without
        notice.
      </span>
    </div>
  );
}
