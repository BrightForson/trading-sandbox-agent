#!/usr/bin/env python3
"""Tier 4 memecoin canary CLI — ops override panel for the automated canary.

The tier runs itself (auto_entry: true — sweep, research, dossier,
rug-guard, LLM gate, sized entries; see bot/memecoin.py). This CLI is the
human override surface: manual entries/exits still work, plus kill control
and research inspection.

Usage:
  python tools/tier4.py status              # ledger + fresh research cards
  python tools/tier4.py buy DOGE 12         # manual entry override ($ cap)
  python tools/tier4.py sell DOGE           # manual exit override
  python tools/tier4.py reset-kill           # force re-arm after a kill
  python tools/tier4.py cycle               # one full auto cycle now
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import config
from bot.journal import TradeJournal
from bot.memecoin import MemecoinLedger


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, rest = args[0], args[1:]
    journal = TradeJournal()
    ledger = MemecoinLedger(config, journal=journal)

    if cmd == "status":
        print(ledger.status_line())
        cards = journal.get_tier4_cards(status="fresh")
        if cards:
            print(f"\nFresh research cards ({len(cards)}):")
            for c in cards[:15]:
                print(f"  [{c[2]}] {c[4]} ({c[3]}) — {c[5]}")
        else:
            print("No fresh research cards — run `cycle` to scan.")
        if ledger.kill_active():
            print(f"\nKILL ACTIVE: {journal.get_meta('t4_kill_reason') or 'unknown'}")
            print("Entries blocked until: python tools/tier4.py reset-kill")
        return 0

    if cmd == "buy":
        if len(rest) < 1:
            print("usage: python tools/tier4.py buy SYMBOL [STAKE]")
            return 1
        symbol = rest[0]
        stake = float(rest[1]) if len(rest) > 1 else None
        ok, note = ledger.buy(symbol, stake)
        print(note)
        return 0 if ok else 1

    if cmd == "sell":
        if len(rest) < 1:
            print("usage: python tools/tier4.py sell SYMBOL")
            return 1
        ok, note = ledger.sell(rest[0])
        print(note)
        return 0 if ok else 1

    if cmd == "reset-kill":
        ok, note = ledger.reset_kill()
        print(note)
        return 0 if ok else 1

    if cmd == "cycle":
        result = ledger.run_cycle()
        for ev in result["events"]:
            print(f"event: {ev}")
        print(ledger.status_line())
        return 0

    print(f"unknown command: {cmd}")
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
