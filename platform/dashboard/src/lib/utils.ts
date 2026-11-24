import { clsx, type ClassValue } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

// Teach twMerge the named type scale (index.css `--text-*`); otherwise it
// reads `text-meta` as a colour and drops it next to `text-muted-foreground`.
export const FONT_SIZES = [
  "caption",
  "meta",
  "body-sm",
  "body",
  "title-sm",
  "title",
] as const;

const twMerge = extendTailwindMerge({
  extend: { classGroups: { "font-size": [{ text: [...FONT_SIZES] }] } },
});

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** A short random id for a client-side list key; not unique across tabs. */
export function randomId(): string {
  return Math.random().toString(36).slice(2, 10);
}
