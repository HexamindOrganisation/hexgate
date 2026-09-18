#!/usr/bin/env bash
# Block `git commit` / `git push` when the working diff has moved since the last
# /review-grounded run.
#
# Why this is a hook and not a line in the skill: the skill already says a review
# certifies a diff and that fixes are themselves unreviewed code. Saying it is not
# the problem — the rule gets read, agreed with, and then skipped, because applying
# the review's own findings feels like finishing the review rather than writing new
# code. A hook does not depend on anyone remembering at the moment it matters.
#
# The marker is written by /review-grounded as its last step, via `--certify`
# below. It holds a fingerprint of the tree, so it goes stale on any edit —
# including the fixes the review itself produced, which is the case this exists for.
#
# Lives in .git/, so it is per-worktree and never committed: two worktrees
# reviewing different branches do not certify each other.
set -uo pipefail

# The certified state: HEAD, the tracked diff, AND untracked files — content, not
# just names. Untracked matters more than it looks: a brand-new module is
# untracked for its whole first review, so a hash over `git diff HEAD` alone
# certifies a tree while the file under review is invisible to it.
# --exclude-standard honours .gitignore, so build output and venvs stay out.
fingerprint() {
  {
    git rev-parse HEAD
    git diff HEAD
    git ls-files -o --exclude-standard -z | sort -z | xargs -0r shasum -a 256 2>/dev/null
  } | shasum -a 256 | cut -d' ' -f1
}

# `--certify` is what /review-grounded runs when a review completes. It lives here
# rather than being spelled out in the skill so the writer and the checker cannot
# drift into hashing different things — a drift whose only symptom is a marker
# that never matches, i.e. a permanently blocked commit.
if [ "${1:-}" = "--certify" ]; then
  git rev-parse --git-dir >/dev/null 2>&1 || { echo "not a git repo" >&2; exit 1; }
  fingerprint > "$(git rev-parse --git-dir)/review-grounded-marker"
  echo "certified $(git rev-parse --short HEAD) + working tree"
  exit 0
fi

command=$(jq -r '.tool_input.command // ""')

# Deliberate override, for a commit that genuinely needs no review (a typo in a
# comment, reverting to a reviewed state). Prefix the command with NO_REVIEW=1.
case "$command" in *NO_REVIEW=1*) exit 0 ;; esac

# Match an actual INVOCATION, not a mention: git has to start a command segment
# (line start, ; & | && || or an opening paren), with optional env assignments
# and flags before the subcommand. A bare substring test also fired on
# `grep "git commit"` and on JSON payloads quoting the phrase.
printf '%s' "$command" | grep -Eqi \
  '(^|[;&|(]|&&|\|\|)[[:space:]]*(env[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*git([[:space:]]+-[^[:space:]]+)*[[:space:]]+(commit|push)([[:space:]]|$)' \
  || exit 0

# Not a git repo (or git unavailable): nothing to certify, so do not get in the way.
git_dir=$(git rev-parse --git-dir 2>/dev/null) || exit 0

[ "$(cat "$git_dir/review-grounded-marker" 2>/dev/null)" = "$(fingerprint)" ] && exit 0

# Exit 2 blocks the tool call and feeds stderr back to Claude.
echo "BLOCKED: the diff has moved since the last /review-grounded run (or none has run)." >&2
echo "Re-run /review-grounded over the current diff and fix what it confirms, then commit." >&2
echo "If this commit genuinely needs no review, prefix the command with NO_REVIEW=1." >&2
exit 2
