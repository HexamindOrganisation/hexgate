import { cn } from "@/lib/utils";

/** The Hexgate keyhole mark, sized like a lucide icon. One colour
 * (`currentColor`) by default, for tiles that set their own; `brand` paints
 * it in the logo's blues, for a dark ground. The keyhole is filled, since it
 * renders at 14–28px where an outline blurs. `strokeWidth` is in lucide's
 * 24-unit terms, so it matches the icons beside it. */
export function HexgateMark({
  className,
  strokeWidth = 2,
  brand = false,
}: {
  className?: string;
  strokeWidth?: number;
  brand?: boolean;
}) {
  const hex = brand ? "#3b82f6" : "currentColor";
  const key = brand ? "#60a5fa" : "currentColor";
  return (
    <svg
      viewBox="0 0 32 32"
      fill="none"
      aria-hidden
      className={cn("size-4 shrink-0", className)}
    >
      <path
        d="M16 2.5 27.5 9v14L16 29.5 4.5 23V9L16 2.5Z"
        stroke={hex}
        strokeWidth={(strokeWidth * 32) / 24}
        strokeLinejoin="round"
        fill={hex}
        fillOpacity={brand ? 0.25 : 0.18}
      />
      <circle cx="16" cy="13.6" r="2.7" fill={key} />
      <path d="M14.4 15.9 13.4 21.6h5.2l-1-5.7Z" fill={key} />
    </svg>
  );
}

/** The full-colour mark, as an icon component (for slots that take one). */
export function HexgateBrandMark(props: {
  className?: string;
  strokeWidth?: number;
}) {
  return <HexgateMark {...props} brand />;
}
