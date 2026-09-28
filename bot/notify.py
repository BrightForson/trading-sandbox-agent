import os
import re
import time

import requests
from datetime import datetime, timezone

from bot.errors import NotificationError

_SECRET_RE = re.compile(
    r"discord\.com/api/webhooks/\S+"
    r"|api/webhooks/\d+/\S+"
    r"|nvapi-[A-Za-z0-9_\-]+"
    r"|gh[pousr]_[A-Za-z0-9_]+"
    r"|github_pat_[A-Za-z0-9_]+"
)
# A requests exception embeds the whole URL, so a query-string credential is
# as much of a leak as a webhook path. Keep the key visible, drop the value.
_SECRET_QUERY_RE = re.compile(
    r"([?&](?:api_?key|apikey|token|secret|access_token)=)[^&\s\"']+",
    re.IGNORECASE,
)


def _mask_secrets(text):
    """Strip webhook tokens and other URL/header-embedded secrets from any
    exception/message text before it reaches CI logs (this repo is public).

    The webhook pattern is not the only shape a secret arrives in, and it was
    the only one covered: a `?api_key=` in any query string and the
    NVIDIA/GitHub token prefixes both passed through untouched.
    """
    masked = _SECRET_RE.sub("[REDACTED]", str(text))
    return _SECRET_QUERY_RE.sub(r"\1[REDACTED]", masked)


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

    if not message or not message.strip():
        # An empty message produced zero chunks, so the loop body never ran and
        # the function returned as if it had delivered — no webhook post, no
        # file, no summary, no log. The docstring promises at-least-one durable
        # write, so refuse rather than silently succeed. Whitespace-only is not
        # the same case: it produced one request that Discord rejected, which
        # did reach the fallback.
        raise NotificationError("refusing to send an empty notification")

    if webhook_url:
        # Discord webhooks accept max 2000 chars; split long reports
        chunks = [message[i:i+1900] for i in range(0, len(message), 1900)]
        try:
            for chunk in chunks:
                _post_chunk(webhook_url, chunk)
            return "webhook"
        except Exception as e:
            # Webhook failed -> degrade gracefully to file. Never print the
            # raw exception: requests errors embed the webhook URL (a secret).
            print(f"Discord webhook failed, falling back to file: {_mask_secrets(e)}")

    # Fallback to file
    try:
        os.makedirs("data/reports", exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"data/reports/report_{timestamp}.txt"
        with open(filename, "w") as f:
            f.write(_mask_secrets(message))
    except Exception as e:
        raise NotificationError(f"Failed to write report to file: {e}")

    # CI: surface the undelivered message in the workflow summary so it is
    # not lost when the ephemeral runner dies (data/reports is gitignored).
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    channel = "file"
    if summary:
        try:
            with open(summary, "a") as f:
                f.write("\n## Discord notification (file fallback)\n\n```\n"
                        + _mask_secrets(message)[:60000] + "\n```\n")
            channel = "file+summary"
        except Exception as e:
            # This was `pass`, the one fully silent loss path. On CI
            # data/reports is gitignored and the runner is ephemeral, so a
            # failure here means the only surviving copy is about to be
            # deleted, and the run still exited 0 claiming delivery.
            print(f"step summary append failed (message is in "
                  f"data/reports/ only): {_mask_secrets(e)}")
    # The channel is returned so the caller can tell a real delivery from a
    # write to a directory that CI is about to throw away. Returning None and
    # letting the caller assume success is how a no-delivery day stayed green.
    return channel
