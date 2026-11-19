/** A search box with a leading icon, for the pickers and filter dialogs. */

import { Search } from "lucide-react";

import { cn } from "@/lib/utils";

export function SearchField({
  value,
  onChange,
  placeholder,
  label,
  className,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  /** Its accessible name. */
  label: string;
  className?: string;
}) {
  return (
    <label
      className={cn(
        "flex h-10 items-center gap-2 rounded-lg border border-input px-3 focus-within:ring-2 focus-within:ring-ring",
        className,
      )}
    >
      <Search className="size-3.5 text-muted-foreground" aria-hidden />
      <input
        type="search"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        aria-label={label}
        className="min-w-0 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
      />
    </label>
  );
}
