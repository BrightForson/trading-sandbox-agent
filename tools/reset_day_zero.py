#!/usr/bin/env python3
"""Day-zero reset for data/trades.db: archive, clear, timestamp.

Archives the current DB under data/archive/ (timestamped copy), then
clears all tier state EXCEPT the preserved meta keys, and records the
new day-zero timestamp under meta key `day_zero_reset_at`. Capital
allocations fall back to config.yaml start_cash values on the next run.

Preserved meta keys (operational continuity):
  - discord_chat_last_seen / discord_chat_channel_id (chat poller)
  - active_llm_model (model chain state)
  - t4_kill_count (structural kill history survives resets)

Usage: python tools/reset_day_zero.py [--dry-run]
"""
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = "data/trades.db"
ARCHIVE_DIR = "data/archive"
PRESERVED_META = {
    "discord_chat_last_seen",
    "discord_chat_channel_id",
    "active_llm_model",
    "t4_kill_count",
    "kill_switch",
    "kill_switch_reason",
}
CLEAR_TABLES = ["trades", "proposals", "bets", "wallet_snapshots", "tier4_cards"]


def reset(dry_run=False):
    if not os.path.exists(DB_PATH):
        print(f"no journal at {DB_PATH} — nothing to reset")
        return 1
    conn = sqlite3.connect(DB_PATH)
    meta_rows = conn.execute("SELECT key, value FROM meta").fetchall()
    preserved = {k: v for k, v in meta_rows if k in PRESERVED_META}
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    stamp = now.strftime("%Y-%m-%dT%H-%M-%SZ")

    if dry_run:
        print(f"DRY RUN — would archive to {ARCHIVE_DIR}/trades.db.pre-reset-{stamp}")
        print(f"would clear tables: {', '.join(CLEAR_TABLES)}")
        print(f"would clear meta keys: {[k for k, _ in meta_rows if k not in PRESERVED_META]}")
        print(f"would preserve: {sorted(preserved)}")
        conn.close()
        return 0

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    archive_path = f"{ARCHIVE_DIR}/trades.db.pre-reset-{stamp}"
    shutil.copy2(DB_PATH, archive_path)
    print(f"archived -> {archive_path}")

    cursor = conn.cursor()
    for table in CLEAR_TABLES:
        try:
            cursor.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError:
            pass  # table absent in older DBs
    cursor.execute("DELETE FROM meta")
    for key, value in preserved.items():
        cursor.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (key, value))
    cursor.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('day_zero_reset_at', ?)",
        (now_iso,))
    conn.commit()
    conn.close()
    print(f"day zero reset at {now_iso}")
    print(f"preserved meta: {sorted(preserved)}")
    print("allocations now fall back to config.yaml start_cash values "
          "(T1 100 / T2 80 / T3 60 / T4 40)")
    return 0


if __name__ == "__main__":
    sys.exit(reset(dry_run="--dry-run" in sys.argv))
