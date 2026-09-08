import os
import re
import time

import requests
from datetime import datetime

from bot.errors import NotificationError

_SECRET_RE = re.compile(r"discord\.com/api/webhooks/\S+|api/webhooks/\d+/\S+")


def _mask_secrets(text):
    """Strip webhook tokens (and similar URL-embedded secrets) from any
    exception/message text before it reaches CI logs (public repo)."""
    return _SECRET_RE.sub("api/webhooks/[REDACTED]", str(text))


def _post_chunk(webhook_url, chunk, max_attempts=3):
    """Post one chunk; on 429 honor Retry-After, on 5xx back off and retry."""
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(webhook_url, json={"content": chunk}, timeout=15)
        except requests.RequestException as e:
            if attempt == max_attempts:
                raise
            time.sleep(attempt * 2)
            continue
        if resp.status_code in (200, 204):
            return True
        if resp.status_code == 429:
            retry_after = 1.0
            try:
                retry_after = float(resp.headers.get("Retry-After", 1))
            except (TypeError, ValueError):
                pass
            if attempt == max_attempts:
                raise NotificationError(f"Discord webhook rate-limited (429) after {max_attempts} attempts")
            time.sleep(min(retry_after, 10))
            continue
        if resp.status_code >= 500 and attempt < max_attempts:
            time.sleep(attempt * 2)
            continue
        raise NotificationError(f"Discord webhook returned {resp.status_code}: {resp.text[:200]}")
    return False


def send_notification(message, config):
    """
    Send a notification to Discord via webhook if DISCORD_WEBHOOK_URL is set.
    Otherwise (or on unrecoverable failure), write to a file in data/reports/.
    On CI, also append to $GITHUB_STEP_SUMMARY so the content survives the
    ephemeral runner (data/reports is gitignored there).
    :param message: the message to send
    :param config: config object (unused for webhook path, kept for interface)
    """
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")

    if webhook_url:
        # Discord webhooks accept max 2000 chars; split long reports
        chunks = [message[i:i+1900] for i in range(0, len(message), 1900)]
        try:
            for chunk in chunks:
                _post_chunk(webhook_url, chunk)
            return
        except Exception as e:
            # Webhook failed -> degrade gracefully to file. Never print the
            # raw exception: requests errors embed the webhook URL (a secret).
            print(f"Discord webhook failed, falling back to file: {_mask_secrets(e)}")

    # Fallback to file
    try:
        os.makedirs("data/reports", exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"data/reports/report_{timestamp}.txt"
        with open(filename, "w") as f:
            f.write(message)
    except Exception as e:
        raise NotificationError(f"Failed to write report to file: {e}")

    # CI: surface the undelivered message in the workflow summary so it is
    # not lost when the ephemeral runner dies (data/reports is gitignored).
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a") as f:
                f.write("\n## Discord notification (file fallback)\n\n```\n"
                        + message[:60000] + "\n```\n")
        except Exception:
            pass
