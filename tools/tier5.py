#!/usr/bin/env python3
"""Tier 5 futures canary CLI — ops override panel for the automated ledger.

The tier runs itself (auto_entry: true — sweep, signals, LLM gate, sized
entries; see bot/futures.py). This CLI is the human override surface.

Usage:
  python tools/tier5.py status              # ledger + open positions
  python tools/tier5.py cycle               # one full auto cycle now
  python tools/tier5.py open BTC LONG 10    # manual entry override ($ margin)
  python tools/tier5.py close BTC           # manual exit override
  python tools/tier5.py reset-kill          # force re-arm after a kill
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import config
from bot.journal import TradeJournal
from bot.futures import FuturesLedger


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, rest = args[0], args[1:]
    journal = TradeJournal()
    ledger = FuturesLedger(config, journal=journal)

    if cmd == "status":
        print(ledger.status_line())
        if ledger.kill_active():
            print(f"\nKILL ACTIVE: {journal.get_meta('t5_kill_reason') or 'unknown'}")
            print("Entries blocked until: python tools/tier5.py reset-kill")
        return 0

    if cmd == "cycle":
        result = ledger.run_cycle()
        for ev in result["events"]:
            print(f"event: {ev}")
        print(ledger.status_line())
        return 0

    if cmd == "open":
        if len(rest) < 2:
            print("usage: python tools/tier5.py open SYMBOL SIDE [MARGIN]")
            return 1
        symbol, side = rest[0], rest[1].upper()
        margin = float(rest[2]) if len(rest) > 2 else None
        ok, note = ledger.open(symbol, side, margin=margin, reason="manual override")
        print(note)
        return 0 if ok else 1

    if cmd == "close":
        if len(rest) < 1:
            print("usage: python tools/tier5.py close SYMBOL")
            return 1
        ok, note = ledger.close(rest[0])
        print(note)
        return 0 if ok else 1

    if cmd == "reset-kill":
        ok, note = ledger.reset_kill()
        print(note)
        return 0 if ok else 1

    print(f"unknown command: {cmd}")
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
