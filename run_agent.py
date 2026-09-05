#!/usr/bin/env python3
"""Run the AI agent cycle (babysitter + scout), shadow mode.

Modes:
- default: continuous loop (local/laptop use)
- --once: single cycle, then exit (for serverless/scheduled runs e.g. GitHub Actions)
"""
import sys

from bot.trader import run_agent_cycle


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_agent_cycle()
    else:
        import time
        import schedule
        from bot.config import config
        interval = int((getattr(config, "agent", None) or {}).get("cycle_interval_minutes", 60))
        run_agent_cycle()
        schedule.every(interval).minutes.do(run_agent_cycle)
        print(f"Scheduler started (every {interval} min). Press Ctrl+C to exit.")
        try:
            while True:
                schedule.run_pending()
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nExiting...")
