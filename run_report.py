#!/usr/bin/env python3
"""Generate and send a daily report."""
from bot.report import create_daily_report
from bot.notify import send_notification
from bot.config import config

def main():
    report = create_daily_report()
    print("Generated report:")
    print(report)
    print("\nSending notification...")
    try:
        send_notification(report, config)
        print("Notification sent successfully.")
    except Exception as e:
        print(f"Failed to send notification: {e}")

if __name__ == "__main__":
    main()