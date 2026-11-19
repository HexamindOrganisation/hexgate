/** The hexkit "goo" loader: a shimmering label over three dots that fuse into a
 * liquid blob via the #pg-goo SVG filter. Shown while the turn is still
 * thinking and no tokens have streamed yet. */
export function ThinkingGoo() {
  // One announcement: the visible label carries the status; the goo is decorative.
  return (
    <div className="flex flex-col gap-1.5" role="status">
      <span className="pg-shimmer text-xs font-medium">Thinking…</span>
      <div className="pg-goo" aria-hidden>
        <span className="d1" />
        <span className="d2" />
        <span className="d3" />
      </div>
    </div>
  );
}

/** The once-per-page SVG filter that gels the three dots into a metaball. */
export function GooFilter() {
  return (
    <svg
      width="0"
      height="0"
      aria-hidden
      focusable="false"
      className="absolute"
    >
      <defs>
        <filter id="pg-goo" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur in="SourceGraphic" stdDeviation="4" result="b" />
          <feColorMatrix
            in="b"
            mode="matrix"
            values="1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 20 -9"
          />
        </filter>
      </defs>
    </svg>
  );
}
