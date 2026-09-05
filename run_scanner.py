#!/usr/bin/env python3
"""Run the Polymarket scanner cycle (read-only, paper bets)."""
from bot.trader import run_scanner_cycle

if __name__ == "__main__":
    run_scanner_cycle()
