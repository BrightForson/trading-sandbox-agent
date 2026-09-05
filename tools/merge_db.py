#!/usr/bin/env python3
"""Lossless SQLite journal merge for CI push conflicts.

Parallel workflows both commit data/trades.db; git cannot merge binary files.
Autoincrement ids diverge across runs (two runs can both write id=3 with
different content), so rows are unioned by LOGICAL key, not by id:

  - trades:     (timestamp, symbol, action, qty)
  - proposals:  (timestamp, source, kind, symbol, action)
  - bets:       (timestamp, market, side)
  - meta:       key (local value wins; tavily_count_* keys take the MAX so
                parallel searches never undercount the budget)

Usage: python tools/merge_db.py <local_db> <remote_db>
(mutates local_db to contain the union)
"""
import sqlite3
import sys

LOGICAL_KEYS = {
    "trades": "t.timestamp = s.timestamp AND t.symbol = s.symbol AND t.action = s.action AND abs(t.qty - s.qty) < 1e-9",
    "proposals": "t.timestamp = s.timestamp AND t.source = s.source AND t.kind = s.kind AND ifnull(t.symbol,'') = ifnull(s.symbol,'') AND t.action = s.action",
    "bets": "t.timestamp = s.timestamp AND t.market = s.market AND t.side = s.side",
}


def merge(local_path, remote_path):
    dst = sqlite3.connect(local_path)
    dst.execute("ATTACH DATABASE ? AS r", (remote_path,))
    try:
        for table, match in LOGICAL_KEYS.items():
            cols = [row[1] for row in dst.execute(f"PRAGMA table_info({table})")]
            if not cols:
                continue
            no_id = [c for c in cols if c != "id"]
            collist = ",".join(f'"{c}"' for c in no_id)
            # insert remote rows whose logical key doesn't exist locally (new ids assigned)
            dst.execute(
                f'INSERT INTO {table}({collist}) '
                f'SELECT {collist} FROM r.{table} s '
                f'WHERE NOT EXISTS (SELECT 1 FROM {table} t WHERE {match})'
            )
        dst.execute("INSERT OR IGNORE INTO meta SELECT key, value FROM r.meta")
        # tavily budget counters: take the max (parallel runs undercount otherwise)
        for key, value in dst.execute("SELECT key, value FROM r.meta WHERE key LIKE 'tavily_count_%'"):
            cur = dst.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if cur is None:
                dst.execute("INSERT INTO meta VALUES (?, ?)", (key, value))
            else:
                dst.execute("UPDATE meta SET value=? WHERE key=? AND CAST(value AS INTEGER) < ?",
                            (value, key, int(value)))
        dst.commit()
    finally:
        dst.execute("DETACH DATABASE r")
        dst.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    merge(sys.argv[1], sys.argv[2])
    print(f"merged {sys.argv[2]} into {sys.argv[1]}")
