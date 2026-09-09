#!/usr/bin/env python3
"""Selective day-zero reset: everything EXCEPT Tier 3 (Polymarket).

Owner decision 2026-09-09: all-loss ledger history was retired, but the
Tier 3 wallet keeps its settled/open paper bets (they are the tier's
ongoing measurement). Archives the current DB first (timestamped copy).

What is KEPT:
  - bets table (all Tier 3 paper bets, open and settled)
  - wallet_* meta keys (epoch, bust latch, epoch start cash)
  - tavily_count_* meta keys (the 1500-credit monthly budget accounting)
  - operational meta (discord chat state, active LLM model, kill switches,
    kill counts — structural history survives resets)

What is RESET to day-zero:
  - trades / proposals / wallet_snapshots / tier4_cards tables
  - tier1 paper_* ledger state (falls back to broker.paper.start_cash)
  - tier2 shadow_* ledger state (falls back to agent.shadow_start_cash)
  - tier4 t4_* ledger state (falls back to memecoin.start_cash)
  - tier5 t5_* ledger state (falls back to futures.start_cash)
  - every other meta key (stops, peaks, cooldowns, eval state, caches)

Usage: python tools/reset_all_but_tier3.py [--dry-run]
"""
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = "data/trades.db"
ARCHIVE_DIR = "data/archive"

KEEP_META_PREFIXES = ("tavily_count_", "wallet_")
KEEP_META_KEYS = {
    "discord_chat_last_seen", "discord_chat_channel_id",
    "active_llm_model", "t4_kill_count", "t5_kill_count",
    "kill_switch", "kill_switch_reason",
}
CLEAR_TABLES = ["trades", "proposals", "wallet_snapshots", "tier4_cards"]


def _keep_meta(key):
    return key in KEEP_META_KEYS or any(key.startswith(p) for p in KEEP_META_PREFIXES)


def reset(dry_run=False, db_path=DB_PATH, archive_dir=ARCHIVE_DIR):
    if not os.path.exists(db_path):
        print(f"no journal at {db_path} — nothing to reset")
        return 1
    conn = sqlite3.connect(db_path)
    meta_rows = conn.execute("SELECT key, value FROM meta").fetchall()
    keep = {k: v for k, v in meta_rows if _keep_meta(k)}
    dropped = [k for k, _ in meta_rows if not _keep_meta(k)]
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    stamp = now.strftime("%Y%m%dT%H%M%SZ")

    if dry_run:
        print(f"DRY RUN — would archive to {archive_dir}/trades.db.pre-reset-{stamp}")
        print(f"would clear tables: {', '.join(CLEAR_TABLES)} (bets table KEPT)")
        print(f"would drop meta keys: {dropped}")
        print(f"would keep meta keys: {sorted(keep)}")
        conn.close()
        return 0

    os.makedirs(archive_dir, exist_ok=True)
    archive_path = f"{archive_dir}/trades.db.pre-reset-{stamp}"
    shutil.copy2(db_path, archive_path)
    print(f"archived -> {archive_path}")

    cursor = conn.cursor()
    for table in CLEAR_TABLES:
        try:
            cursor.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError:
            pass
    cursor.execute("DELETE FROM meta")
    for key, value in keep.items():
        cursor.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (key, value))
    cursor.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('day_zero_reset_at', ?)",
        (now_iso,))
    conn.commit()

    open_bets = cursor.execute(
        "SELECT COUNT(*) FROM bets WHERE outcome='open'").fetchone()[0]
    all_bets = cursor.execute("SELECT COUNT(*) FROM bets").fetchone()[0]
    conn.close()
    print(f"day zero reset at {now_iso} (Tier 3 preserved: "
          f"{all_bets} bets, {open_bets} open)")
    print("allocations fall back to config start_cash "
          "(T1 100 / T2 80 / T4 40 / T5 50); T3 wallet untouched")
    return 0


if __name__ == "__main__":
    sys.exit(reset(dry_run="--dry-run" in sys.argv))
