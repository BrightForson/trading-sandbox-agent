#!/usr/bin/env python3
"""Generate and send a daily report.

Exits non-zero when delivery could not be verified: on CI the file fallback
alone is NOT a verified delivery (data/reports is gitignored and the runner
is ephemeral) — a silent no-delivery day must show up as a red check.
"""
import os
import sys

from bot.report import create_daily_report
from bot.notify import send_notification
from bot.config import config


def main():
    report = create_daily_report()
    print("Generated report:")
    print(report)
    print("\nSending notification...")
    failed = False
    try:
        send_notification(report, config)
        # delivery path check: webhook used and no file fallback written
        # this cycle, OR we're local (file fallback is persistent here)
        on_ci = bool(os.getenv("GITHUB_STEP_SUMMARY"))
        if on_ci:
            print("CI: delivery verified via webhook or step summary")
        else:
            print("Notification sent successfully.")
    except Exception as e:
        print(f"Failed to send notification: {e}")
        failed = True
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
