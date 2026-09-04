#!/usr/bin/env python3
"""Run the trading bot.

Modes:
- default: continuous loop (local/laptop use)
- --once: single cycle, then exit (for serverless/scheduled runs e.g. GitHub Actions)
"""
import sys
from bot.trader import main, run_trading_cycle

if __name__ == "__main__":
    if "--once" in sys.argv:
        run_trading_cycle()
    else:
        main()