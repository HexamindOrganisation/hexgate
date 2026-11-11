import { cn } from "@/lib/utils";

/** One round colour swatch in an accent picker; ringed when chosen. */
export function AccentSwatch({
  label,
  color,
  selected,
  onSelect,
}: {
  label: string;
  /** HSL channels, e.g. "221 83% 53%". */
  color: string;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={`${label} accent`}
      aria-pressed={selected}
      onClick={onSelect}
      className={cn(
        // Focus is an outline, so it never hides the chosen ring.
        "size-6 rounded-full ring-offset-2 ring-offset-popover transition-shadow focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ring",
        selected && "ring-2 ring-foreground",
      )}
      style={{ backgroundColor: `hsl(${color})` }}
    />
  );
}
