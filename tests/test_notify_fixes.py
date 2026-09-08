"""Tests for the 2026-09-08 P0c security/CI hygiene fixes.

Covers: webhook secret masking (F3), 429 Retry-After handling with chunk
retry (F7), and GITHUB_STEP_SUMMARY fallback on CI (F7).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import notify
from bot.errors import NotificationError


SECRET_URL = "https://discord.com/api/webhooks/123456789/afAKExY7secretTOKENval"


def test_mask_secrets_hides_webhook_token():
    msg = f"HTTPSConnectionPool(host='discord.com', port=443): Max retries exceeded with url: /api/webhooks/123456789/afAKExY7secretTOKENval"
    masked = notify._mask_secrets(msg)
    assert "afAKExY7secretTOKENval" not in masked
    assert "[REDACTED]" in masked


def test_mask_secrets_full_url():
    masked = notify._mask_secrets(f"failed to post to {SECRET_URL} twice")
    assert "afAKExY7secretTOKENval" not in masked


def _resp(status, headers=None, text=""):
    class R:
        def __init__(self):
            self.status_code = status
            self.headers = headers or {}
            self.text = text
    return R()


def test_post_chunk_retries_on_429(monkeypatch):
    calls = []
    sleeps = []
    monkeypatch.setattr(notify.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(notify.requests, "post",
                        lambda url, json, timeout: calls.append(url) or
                        _resp(429 if len(calls) == 1 else 204))
    assert notify._post_chunk(SECRET_URL, "hello") is True
    assert len(calls) == 2
    assert sleeps  # honored a Retry-After/backoff


def test_post_chunk_gives_up_after_max_429(monkeypatch):
    calls = []
    monkeypatch.setattr(notify.time, "sleep", lambda s: None)
    monkeypatch.setattr(notify.requests, "post",
                        lambda url, json, timeout: calls.append(1) or
                        _resp(429, headers={"Retry-After": "0.01"}))
    with pytest.raises(NotificationError):
        notify._post_chunk(SECRET_URL, "hello", max_attempts=3)
    assert len(calls) == 3


def test_send_notification_falls_back_to_summary(monkeypatch, tmp_path):
    """Webhook down entirely -> message preserved in GITHUB_STEP_SUMMARY + file."""
    # conftest globally no-ops send_notification for safety; this test
    # exercises the real implementation via a clean module instance.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "notify_real", os.path.join(os.path.dirname(notify.__file__), "notify.py"))
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)

    monkeypatch.setenv("DISCORD_WEBHOOK_URL", SECRET_URL)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    def boom(url, json, timeout):
        raise notify.requests.RequestException(f"url leaked: {SECRET_URL}")

    monkeypatch.setattr(real.requests, "post", boom)
    (tmp_path / "data" / "reports").mkdir(parents=True)
    orig_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        real.send_notification("REPORT BODY 123", config=None)
        content = summary.read_text()
        assert "REPORT BODY 123" in content
    finally:
        os.chdir(orig_cwd)


def test_send_notification_success_no_fallback(monkeypatch, tmp_path, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "notify_real2", os.path.join(os.path.dirname(notify.__file__), "notify.py"))
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)

    monkeypatch.setenv("DISCORD_WEBHOOK_URL", SECRET_URL)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(real.requests, "post",
                        lambda url, json, timeout: _resp(204))
    (tmp_path / "data" / "reports").mkdir(parents=True)
    orig_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        real.send_notification("all good", config=None)
        assert "falling back" not in capsys.readouterr().out
        reports = list((tmp_path / "data" / "reports").glob("report_*.txt"))
        assert reports == []  # success path never writes the file fallback
    finally:
        os.chdir(orig_cwd)
