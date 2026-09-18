#!/usr/bin/env bash
# Block `git commit` / `git push` while the content about to ship has not been
# through /review-grounded.
#
# Why a hook and not a line in the skill: the skill already says a review
# certifies one diff, and that the fixes a review produces are themselves
# unreviewed code. Saying it is not enough — the rule gets read, agreed with, and
# then skipped, because applying the review's own findings feels like finishing
# the review rather than writing new code.
#
# THREAT MODEL, stated because it decides how hard the matcher tries: this guards
# against forgetting, not against evasion. Nobody here is trying to sneak a commit
# past it, so the matcher catches the shapes a person actually types and anything
# it misses is a missed reminder, not a breach. `bash -c "git commit"`, shell
# aliases and `gh pr merge` are NOT caught: a shell command line cannot be soundly
# parsed by a regex, and pretending otherwise would buy false confidence.
#
# WHAT IS FINGERPRINTED, and why it is not the obvious thing: the content that
# will ship — the union of (files differing from the merge base with upstream) and
# (untracked files), each hashed by its current content. Not `git rev-parse HEAD`,
# and not `git diff HEAD`:
#   * HEAD changes when you commit, so hashing it made every commit invalidate its
#     own certificate and block the push that followed it.
#   * `git diff HEAD` plus a separate untracked stream meant `git add` of a
#     reviewed new file moved it between streams and changed the hash, blocking a
#     commit after a zero-content change — the exact new-module workflow the
#     untracked hashing exists to support.
# Hashing per-path content makes both operations invisible, which is correct:
# neither changes what ships.
set -uo pipefail

die() { printf '%s\n' "$@" >&2; exit 2; }

# A gate that cannot run must not wave the commit through. Each of these
# previously failed open AND silently: without shasum, fingerprint() returned ""
# and `cat` of an absent marker returned "", so the comparison passed.
# shasum needs -a 256; sha256sum takes no such flag and errors on it, so the two
# cannot share one invocation string.
if command -v shasum >/dev/null 2>&1; then HASHER=(shasum -a 256)
elif command -v sha256sum >/dev/null 2>&1; then HASHER=(sha256sum)
else die "require-review: neither shasum nor sha256sum found; refusing to pass a commit it cannot check."
fi
for t in jq grep git sort xargs tr; do
  command -v "$t" >/dev/null 2>&1 || die "require-review: '$t' not found; refusing to pass a commit it cannot check."
done
sum() { "${HASHER[@]}" 2>/dev/null | cut -d' ' -f1; }

# The merge base with upstream starts the reviewable unit — the PR diff. With no
# upstream at all there is no such unit, so fall back to HEAD and accept that a
# commit then re-certifies; that beats blocking every commit in a remote-less repo.
review_base() {
  local ref
  for ref in "$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null)" \
             origin/HEAD origin/main origin/master main master; do
    [ -n "$ref" ] || continue
    git rev-parse --verify --quiet "$ref^{commit}" >/dev/null 2>&1 || continue
    git merge-base HEAD "$ref" 2>/dev/null && return 0
  done
  git rev-parse HEAD 2>/dev/null
}

fingerprint() {
  local base list
  base=$(review_base) || return 1
  [ -n "$base" ] || return 1
  list=$(mktemp) || return 1
  # Union of changed-vs-base and untracked, deduped, NUL-delimited so paths with
  # spaces survive. Tracking state is deliberately absent from the hash — only a
  # path and its bytes, so `git add` of a reviewed file changes nothing.
  { git diff --name-only -z "$base"; git ls-files -o --exclude-standard -z; } \
    | sort -zu > "$list"
  {
    # ONE hasher process over all existing files. Hashing per file in the loop
    # cost ~10ms/file — 20s at 2000 files, past the hook timeout, which fails
    # open: a big untracked tree silently disabled the gate.
    tr '\0' '\n' < "$list" | while IFS= read -r f; do
      [ -f "$f" ] && printf '%s\0' "$f"
    done | xargs -0 "${HASHER[@]}" 2>/dev/null | sort
    # Paths that changed vs base by being deleted have no bytes to hash.
    tr '\0' '\n' < "$list" | while IFS= read -r f; do
      [ -f "$f" ] || printf 'ABSENT %s\n' "$f"
    done | sort
  } | sum
  rm -f "$list"
}

if [ "${1:-}" = "--certify" ]; then
  git rev-parse --git-dir >/dev/null 2>&1 || die "require-review: not a git repository."
  fp=$(fingerprint) || die "require-review: could not fingerprint the tree."
  [ -n "$fp" ] || die "require-review: empty fingerprint; refusing to write a marker that would match anything."
  printf '%s\n' "$fp" > "$(git rev-parse --git-dir)/review-grounded-marker"
  echo "certified: $(git diff --name-only "$(review_base)" | wc -l | tr -d ' ') changed, $(git ls-files -o --exclude-standard | wc -l | tr -d ' ') untracked"
  exit 0
fi

command=$(jq -r '.tool_input.command // ""')

# The override must PREFIX the command, not merely appear in it. As a bare
# substring it fired on `git commit -m "document NO_REVIEW=1"` — and this repo's
# subject matter is this hook, so that is the likely case, not a contrived one.
case "$command" in "NO_REVIEW=1 "*|"NO_REVIEW=1	"*) exit 0 ;; esac

# git must START a command segment, after optional wrappers and flags. `-C <path>`
# and `-c <k=v>` take a SEPARATE argument, so a pattern assuming self-contained
# flags let `git -C /path commit` through entirely.
seg='(^|[;&|(]|&&|\|\|)[[:space:]]*'
wrap='((command|exec|time|nohup|timeout[[:space:]]+[0-9smhd]+|env)[[:space:]]+)*'
envs='([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*'
gitx='([^[:space:]]*/)?git([[:space:]]+(-[cC][[:space:]]+[^[:space:]]+|-[^[:space:]]+))*'
printf '%s' "$command" | grep -Eq "${seg}${wrap}${envs}${gitx}[[:space:]]+(commit|push)([[:space:]]|$)" || exit 0

git_dir=$(git rev-parse --git-dir 2>/dev/null) || exit 0   # not a repo: nothing to certify

current=$(fingerprint) || die "require-review: could not fingerprint the tree."
[ -n "$current" ] || die "require-review: empty fingerprint; refusing to pass unchecked."
[ "$(cat "$git_dir/review-grounded-marker" 2>/dev/null)" = "$current" ] && exit 0

die "BLOCKED: the content about to ship has not been through /review-grounded (or has changed since)." \
    "Run /review-grounded over the current diff, fix what it confirms, then certify." \
    "If this genuinely needs no review, START the command with NO_REVIEW=1."
