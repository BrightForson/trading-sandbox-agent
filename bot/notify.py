import os
import requests
from datetime import datetime
from bot.errors import NotificationError

def send_notification(message, config):
    """
    Send a notification to Discord via webhook if DISCORD_WEBHOOK_URL is set.
    Otherwise (or on failure), write to a file in data/reports/.
    :param message: the message to send
    :param config: config object (unused for webhook path, kept for interface)
    """
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")

    if webhook_url:
        # Discord webhooks accept max 2000 chars; split long reports
        chunks = [message[i:i+1900] for i in range(0, len(message), 1900)]
        try:
            for chunk in chunks:
                resp = requests.post(
                    webhook_url,
                    json={"content": chunk},
                    timeout=15
                )
                if resp.status_code not in (200, 204):
                    raise NotificationError(f"Discord webhook returned {resp.status_code}: {resp.text}")
            return
        except Exception as e:
            # Webhook failed -> degrade gracefully to file
            print(f"Discord webhook failed, falling back to file: {e}")

    # Fallback to file
    try:
        os.makedirs("data/reports", exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"data/reports/report_{timestamp}.txt"
        with open(filename, "w") as f:
            f.write(message)
    except Exception as e:
        raise NotificationError(f"Failed to write report to file: {e}")