import { cn } from "@/lib/utils";

/** The Hexgate keyhole mark in one colour (`currentColor`), sized like a
 * lucide icon: drop-in for the brand tiles. The keyhole is filled, since
 * these render at 14–20px where an outline blurs. `strokeWidth` is in
 * lucide's 24-unit terms, so it matches the icons beside it. */
export function HexgateMark({
  className,
  strokeWidth = 2,
}: {
  className?: string;
  strokeWidth?: number;
}) {
  return (
    <svg
      viewBox="0 0 32 32"
      fill="none"
      aria-hidden
      className={cn("size-4 shrink-0", className)}
    >
      <path
        d="M16 2.5 27.5 9v14L16 29.5 4.5 23V9L16 2.5Z"
        stroke="currentColor"
        strokeWidth={(strokeWidth * 32) / 24}
        strokeLinejoin="round"
        fill="currentColor"
        fillOpacity={0.18}
      />
      <circle cx="16" cy="13.6" r="2.7" fill="currentColor" />
      <path d="M14.4 15.9 13.4 21.6h5.2l-1-5.7Z" fill="currentColor" />
    </svg>
  );
}
