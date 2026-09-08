#!/usr/bin/env bash
# Safe journal commit for parallel workflows.
# Pulls with rebase; on a binary trades.db conflict, performs a lossless
# SQLite union merge (tools/merge_db.py) instead of picking a side.
#
# Conflict resolution uses index STAGES, not --ours/--theirs (which are
# inverted during rebase: --ours is upstream, --theirs is our replayed
# commit):
#   :1: = merge base   :2: = upstream (remote main)   :3: = our fresh journal
# The merge destination is always OUR journal (stage :3:), so the resolving
# run's ledger state is never silently rolled back.
set -u
MSG="${1:-journal update $(date -u +'%Y-%m-%d %H:%M')}"
cd "$(dirname "$0")/.."
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

git config user.name "trading-bot"
git config user.email "trading-bot@users.noreply.github.com"
git add data/trades.db
if git diff --cached --quiet; then
  echo "nothing to commit"
  exit 0
fi
git commit -m "$MSG"

resolve_db_conflict() {
  # Extract the three stages of the conflicted binary DB.
  git show :1:data/trades.db > "$TMP_DIR/base.db" 2>/dev/null || rm -f "$TMP_DIR/base.db"
  git show :2:data/trades.db > "$TMP_DIR/upstream.db" || { echo "FATAL: cannot extract upstream stage :2: of trades.db"; return 1; }
  git show :3:data/trades.db > "$TMP_DIR/ours.db"   || { echo "FATAL: cannot extract our stage :3: of trades.db"; return 1; }
  # Union-merge upstream into OUR journal (ours is the destination);
  # a true 3-way meta merge when the base stage is available.
  if [ -f "$TMP_DIR/base.db" ]; then
    python tools/merge_db.py "$TMP_DIR/ours.db" "$TMP_DIR/upstream.db" --base "$TMP_DIR/base.db" || return 1
  else
    echo "WARNING: no merge-base stage; falling back to 2-way meta merge"
    python tools/merge_db.py "$TMP_DIR/ours.db" "$TMP_DIR/upstream.db" || return 1
  fi
  cp "$TMP_DIR/ours.db" data/trades.db || return 1
  git add data/trades.db
  echo "resolved trades.db conflict: union merge complete"
}

alert_failure() {
  # Best-effort Discord alert so a lost cycle is visible, not silent.
  python - "$MSG" <<'PYEOF' 2>/dev/null || true
import os, sys, requests
url = os.environ.get("DISCORD_WEBHOOK_URL")
if url:
    try:
        requests.post(url, json={"content": f"⚠ CI journal push FAILED — this cycle's writes may be lost. Action needed. ({sys.argv[1]})"}, timeout=10)
    except Exception:
        pass
PYEOF
}

for attempt in 1 2 3; do
  if git pull --rebase origin main 2>"$TMP_DIR/pull_err.txt"; then
    if git status --porcelain | grep -q "^UU data/trades.db"; then
      resolve_db_conflict || { echo "FATAL: merge_db failed; aborting rebase"; git rebase --abort 2>/dev/null; alert_failure; exit 1; }
      GIT_EDITOR=true git rebase --continue || { echo "FATAL: rebase --continue failed"; alert_failure; exit 1; }
    fi
    git push && exit 0
  else
    echo "pull attempt $attempt failed:"; cat "$TMP_DIR/pull_err.txt"
    if git status --porcelain | grep -q "^UU data/trades.db"; then
      resolve_db_conflict || { echo "FATAL: merge_db failed; aborting rebase"; git rebase --abort 2>/dev/null; alert_failure; exit 1; }
      GIT_EDITOR=true git rebase --continue || { git rebase --abort 2>/dev/null || true; }
    else
      git rebase --abort 2>/dev/null || true
    fi
    sleep $((attempt * 3))
  fi
done
echo "FATAL: push failed after retries — journal commit NOT pushed (ephemeral runner: state lost)"
alert_failure
exit 1
