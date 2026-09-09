#!/usr/bin/env python3
"""One-shot ledger integrity audit (read-only): reconstruct each tier's
cash/positions from journaled fills and compare against the meta ledgers.

Detects the corruption modes from the 2026-09-08 deep dive (F1 meta reverts):
  - trades rows present but ledger state rolled back (double-sell/duplicate-entry)
  - duplicate fills (same timestamp+symbol+action+qty)
  - cash drift between reconstructed and stored ledgers

Usage: python tools/audit_ledgers.py [--since ISO]   (default: day-zero reset)
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone

T1_TAG = "tier4-memecoin"  # excluded from Tier 1
T2_TAG = "shadow-account"
T5_TAG = "tier5-futures"


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None,
                    help="ISO timestamp cutoff (default: day_zero_reset_at meta)")
    ap.add_argument("--db", default="data/trades.db")
    return ap.parse_args()


def main():
    args = parse_args()
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    since = args.since or meta.get("day_zero_reset_at")
    if not since:
        print("no day_zero_reset_at meta key; pass --since")
        return 1

    trades = conn.execute(
        "SELECT timestamp, symbol, action, qty, price, fee, reasoning FROM trades "
        "WHERE timestamp >= ? ORDER BY timestamp", (since,)).fetchall()

    def is_tag(row, tag):
        return tag in (row["reasoning"] or "")

    tiers = {
        "tier1": {"cash_key": "paper_cash", "pos_key": "paper_positions",
                  "start": 100.0, "rows": [t for t in trades
                                           if not any(is_tag(t, tag) for tag in
                                                      (T1_TAG, T2_TAG, T5_TAG))],
                  "fee_pct": 0.1},
        "tier2": {"cash_key": "shadow_cash", "pos_key": "shadow_positions",
                  "start": 80.0, "rows": [t for t in trades if is_tag(t, T2_TAG)],
                  "fee_pct": 0.0},
        "tier4": {"cash_key": "t4_cash", "pos_key": "t4_positions",
                  "start": 40.0, "rows": [t for t in trades if is_tag(t, T1_TAG)],
                  "fee_pct": 1.0},
    }

    print(f"day-zero: {since}  |  trades since reset: {len(trades)}\n")

    problems = 0
    for name, t in tiers.items():
        cash = t["start"]
        positions = {}
        for r in t["rows"]:
            qty, price = r["qty"], r["price"]
            fee_col = r["fee"] or 0.0
            if r["action"] == "BUY":
                cost = qty * price + fee_col
                cash -= cost
                positions[r["symbol"]] = positions.get(r["symbol"], 0.0) + qty
            else:  # SELL
                held = positions.get(r["symbol"], 0.0)
                sold = min(qty, held)
                positions[r["symbol"]] = held - sold
                if positions[r["symbol"]] < 1e-12:
                    positions.pop(r["symbol"], None)
                # tier2 fee handling mirrors shadow.py; approximate with fee col
                proceeds = sold * price - fee_col
                oversell_qty = qty - sold
                if oversell_qty > 1e-9:
                    print(f"  [{name}] OVERSELL {r['symbol']} at {r['timestamp']}: "
                          f"tried {qty:.8f}, held {held:.8f} (phantom sell)")
                    problems += 1
                cash += proceeds

        stored_cash = float(meta.get(t["cash_key"]) or t["start"])
        stored_pos = json.loads(meta.get(t["pos_key"]) or "{}")
        # normalize stored pos (tier4 positions are dicts with qty field)
        norm_stored = {}
        for k, v in stored_pos.items():
            if isinstance(v, dict):
                norm_stored[k] = float(v.get("qty") or 0)
            else:
                norm_stored[k] = float(v)

        cash_diff = cash - stored_cash
        pos_match = set(norm_stored) == set(positions) and all(
            abs(norm_stored.get(s, 0) - positions.get(s, 0)) < 1e-6 for s in positions)

        status = "OK " if abs(cash_diff) < 0.01 and pos_match else "DRIFT"
        if status == "DRIFT":
            problems += 1
        print(f"[{status}] {name}: {len(t['rows'])} fills")
        print(f"        reconstructed cash ${cash:.2f} vs stored ${stored_cash:.2f} (diff {cash_diff:+.4f})")
        print(f"        reconstructed pos {positions or '{}'}")
        print(f"        stored pos          {norm_stored or '{}'}")
        print()

    # duplicate fills check
    seen = {}
    dups = 0
    for r in trades:
        key = (r["timestamp"], r["symbol"], r["action"], round(r["qty"], 8))
        seen[key] = seen.get(key, 0) + 1
    for key, n in seen.items():
        if n > 1:
            print(f"DUPLICATE FILL x{n}: {key}")
            dups += 1
    if dups:
        problems += dups
    else:
        print("no duplicate fills (timestamp, symbol, action, qty)")

    # tier3 wallet: open bets vs wallet cash
    bets = conn.execute(
        "SELECT market, side, stake, outcome, payout FROM bets WHERE timestamp >= ?",
        (since,)).fetchall()
    open_bets = [b for b in bets if b["outcome"] == "open"]
    settled = [b for b in bets if b["outcome"] != "open"]
    t3_cash = float(meta.get("wallet_cash") or 0)
    print(f"\ntier3: {len(bets)} bets since reset ({len(open_bets)} open, {len(settled)} settled)")
    for b in open_bets:
        print(f"  OPEN: {b['side']} ${b['stake']:.0f} on {b['market'][:60]}")

    # tier5 futures: cash moves by margin+pnl-fee-funding, which is not
    # reconstructable from qty*price alone — report it as an informational
    # balance check (cash+open margins vs start_cash) instead of a full recon
    t5_rows = [t for t in trades if is_tag(t, T5_TAG)]
    t5_cash = float(meta.get("t5_cash") or 50.0)
    t5_pos = json.loads(meta.get("t5_positions") or "{}")
    t5_open_margin = sum(float(p.get("margin") or 0) for p in t5_pos.values())
    print(f"\ntier5: {len(t5_rows)} fills, cash ${t5_cash:.2f} + open margins "
          f"${t5_open_margin:.2f} = ${t5_cash + t5_open_margin:.2f} of $50.00 start")
    kill = meta.get("t5_kill") or "off"
    print(f"  kill switch: {kill}, open positions: {len(t5_pos)}")

    print(f"\n{'='*50}")
    print("AUDIT RESULT:", "PROBLEMS FOUND" if problems else "CLEAN — ledgers reconcile with fills")
    conn.close()
    return 0 if problems == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
