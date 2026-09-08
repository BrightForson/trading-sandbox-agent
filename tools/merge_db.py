#!/usr/bin/env python3
"""Lossless SQLite journal merge for CI push conflicts.

Parallel workflows both commit data/trades.db; git cannot merge binary files.
Autoincrement ids diverge across runs (two runs can both write id=3 with
different content), so rows are unioned by LOGICAL key, not by id:

  - trades:           (timestamp, symbol, action, qty)
  - proposals:        (timestamp, source, kind, symbol, action)
  - bets:            (timestamp, market, side)
  - tier4_cards:      (timestamp, kind, symbol)
  - wallet_snapshots: (timestamp, epoch)

meta: with --base (the merge-base database, extractable mid-rebase via
`git show :1:data/trades.db`), a true three-way merge is performed per key:
only-remote-changed -> take remote; only-local-changed -> keep local;
both changed -> keep local (the resolving run's journal is the newest
commit). tavily_count_* keys always take the MAX so parallel searches never
undercount the budget. Without --base, only missing keys are inserted from
remote (no way to tell which side changed) and tavily counters take the MAX.

The merge destination is always the LOCAL database: callers must pass the
resolving run's fresh journal as <local_db> and the upstream copy as
<remote_db>. The merged result fails closed: any integrity error raises
instead of leaving a corrupt journal behind.

Usage: python tools/merge_db.py <local_db> <remote_db> [--base <base_db>]
(mutates local_db to contain the union)
"""
import argparse
import sqlite3
import sys

LOGICAL_KEYS = {
    "trades": "t.timestamp = s.timestamp AND t.symbol = s.symbol AND t.action = s.action AND abs(t.qty - s.qty) < 1e-9",
    "proposals": "t.timestamp = s.timestamp AND t.source = s.source AND t.kind = s.kind AND ifnull(t.symbol,'') = ifnull(s.symbol,'') AND t.action = s.action",
    "bets": "t.timestamp = s.timestamp AND t.market = s.market AND t.side = s.side",
    "tier4_cards": "t.timestamp = s.timestamp AND t.kind = s.kind AND t.symbol = s.symbol",
    "wallet_snapshots": "t.timestamp = s.timestamp AND t.epoch = s.epoch",
}


class MergeError(Exception):
    pass


def _meta_map(conn, schema):
    return {k: v for k, v in conn.execute(f"SELECT key, value FROM {schema}.meta")}


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _merge_meta(dst, base):
    """Three-way meta merge (local wins when both sides changed a key)."""
    local = _meta_map(dst, "main")
    remote = _meta_map(dst, "r")
    base_rows = _meta_map(dst, "base") if base else None

    for key, remote_value in remote.items():
        if key.startswith("tavily_count_"):
            rnum = _as_int(remote_value)
            cur = local.get(key)
            if cur is None:
                if remote_value is not None:
                    dst.execute("INSERT INTO main.meta VALUES (?, ?)", (key, remote_value))
            elif rnum is not None and rnum > (_as_int(cur) or -1):
                dst.execute("UPDATE main.meta SET value=? WHERE key=?", (str(rnum), key))
            continue
        if key not in local:
            dst.execute("INSERT INTO main.meta VALUES (?, ?)", (key, remote_value))
            continue
        if local[key] == remote_value:
            continue
        if base_rows is not None:
            base_value = base_rows.get(key)
            if base_value == remote_value:
                continue
            if base_value == local[key]:
                dst.execute("UPDATE main.meta SET value=? WHERE key=?", (remote_value, key))


def merge(local_path, remote_path, base_path=None):
    dst = sqlite3.connect(local_path)
    dst.execute("ATTACH DATABASE ? AS r", (remote_path,))
    base = None
    if base_path:
        base = True
        dst.execute("ATTACH DATABASE ? AS base", (base_path,))
    try:
        for table, match in LOGICAL_KEYS.items():
            cols = [row[1] for row in dst.execute(f"PRAGMA main.table_info({table})")]
            if not cols:
                continue
            no_id = [c for c in cols if c != "id"]
            collist = ",".join(f'"{c}"' for c in no_id)
            # insert remote rows whose logical key doesn't exist locally (new ids assigned)
            dst.execute(
                f'INSERT INTO main.{table}({collist}) '
                f'SELECT {collist} FROM r.{table} s '
                f'WHERE NOT EXISTS (SELECT 1 FROM main.{table} t WHERE {match})'
            )
        _merge_meta(dst, base)
        dst.commit()
        row = dst.execute("PRAGMA quick_check").fetchone()
        if not row or row[0] != "ok":
            raise MergeError(f"integrity check failed after merge: {row}")
        dst.commit()
    finally:
        if base:
            try:
                dst.execute("DETACH DATABASE base")
            except sqlite3.OperationalError:
                pass
        try:
            dst.execute("DETACH DATABASE r")
        except sqlite3.OperationalError:
            pass
        dst.close()


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("local_db", help="resolving run's fresh journal (merge destination)")
    parser.add_argument("remote_db", help="upstream journal to union in")
    parser.add_argument("--base", dest="base_db", default=None,
                        help="merge-base journal (git show :1:data/trades.db)")
    args = parser.parse_args(argv)
    merge(args.local_db, args.remote_db, args.base_db)
    suffix = " (3-way)" if args.base_db else ""
    print(f"merged {args.remote_db} into {args.local_db}{suffix}")


if __name__ == "__main__":
    main(sys.argv[1:])
