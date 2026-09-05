#!/usr/bin/env bash
# Safe journal commit for parallel workflows.
# Pulls with rebase; on a binary trades.db conflict, performs a lossless
# SQLite union merge (tools/merge_db.py) instead of picking a side.
set -u
MSG="${1:-journal update $(date -u +'%Y-%m-%d %H:%M')}"
cd "$(dirname "$0")/.."

git config user.name "trading-bot"
git config user.email "trading-bot@users.noreply.github.com"
git add data/trades.db
if git diff --cached --quiet; then
  echo "nothing to commit"
  exit 0
fi
git commit -m "$MSG"

for attempt in 1 2 3; do
  if git pull --rebase origin main 2>/tmp/opencode/pull_err.txt; then
    if [ -f data/trades.db.rebase ] || git status --porcelain | grep -q "^UU data/trades.db"; then
      # binary conflict: union-merge remote checkout into ours
      git checkout --theirs data/trades.db 2>/dev/null
      mv data/trades.db /tmp/opencode/remote_trades.db
      git checkout --ours data/trades.db 2>/dev/null
      python tools/merge_db.py data/trades.db /tmp/opencode/remote_trades.db
      git add data/trades.db
      GIT_EDITOR=true git rebase --continue
    fi
    git push && exit 0
  else
    echo "pull attempt $attempt failed:"; cat /tmp/opencode/pull_err.txt
    if git status --porcelain | grep -q "^UU data/trades.db"; then
      git checkout --theirs data/trades.db 2>/dev/null
      mv data/trades.db /tmp/opencode/remote_trades.db
      git checkout --ours data/trades.db 2>/dev/null
      python tools/merge_db.py data/trades.db /tmp/opencode/remote_trades.db
      git add data/trades.db
      GIT_EDITOR=true git rebase --continue || git rebase --abort
    else
      git rebase --abort 2>/dev/null || true
    fi
    sleep $((attempt * 3))
  fi
done
echo "push failed after retries (journal kept locally; next run re-merges)"
exit 0
