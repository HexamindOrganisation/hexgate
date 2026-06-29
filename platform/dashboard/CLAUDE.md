# Dashboard — Claude Code Instructions

After you make changes, run: `make dashboard-fmt && make dashboard-fmt-check && make dashboard-lint && make dashboard-typecheck`  # format + CI fmt gate + lint + typecheck

## Formatting Rules (CRITICAL)
TSX output MUST conform exactly to Prettier config to pass `make dashboard-fmt-check`:
- Double quotes
- Trailing commas
- 2-space indent
- 80-char line width
