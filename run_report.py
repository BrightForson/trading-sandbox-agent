#!/usr/bin/env python3
"""Generate and send a daily report.

Exits non-zero when delivery could not be verified: on CI the file fallback
alone is NOT a verified delivery (data/reports is gitignored and the runner
is ephemeral) — a silent no-delivery day must show up as a red check.
"""
import os
import sys

from bot.report import create_daily_report
from bot.notify import send_notification, _mask_secrets
from bot.config import config


def main():
    report = create_daily_report()
    print("Generated report:")
    print(_mask_secrets(report))
    print("\nSending notification...")
    failed = False
    try:
        channel = send_notification(report, config)
        # delivery path check: webhook used, OR we're local (file fallback is
        # persistent here), OR the step summary took a copy that outlives the
        # runner. A bare "file" on CI is NOT a verified delivery.
        on_ci = bool(os.getenv("GITHUB_STEP_SUMMARY"))
        if on_ci and channel == "file":
            print("CI: NOT delivered — no webhook and no step-summary copy; "
                  "data/reports is gitignored and this runner is ephemeral")
            failed = True
        elif on_ci:
            print(f"CI: delivery verified via {channel}")
        else:
            print(f"Notification sent successfully ({channel}).")
    except Exception as e:
        print(f"Failed to send notification: {e}")
        failed = True
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
