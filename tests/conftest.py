import pytest


@pytest.fixture(autouse=True)
def _no_external_notifications(monkeypatch):
    """Never let tests post to real Discord (or other external webhooks).

    bot/notify.py reads DISCORD_WEBHOOK_URL from the environment, and
    bot/config.py loads the developer's real .env, so without this a
    passing auto-entry test would fire a live webhook.
    """
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    # same discipline for paid/limited research APIs: tests must never
    # burn real Tavily credits (news_digest/tavily_search read this env)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    no_op = lambda *a, **k: None
    monkeypatch.setattr("bot.notify.send_notification", no_op)
    # modules that did `from bot.notify import send_notification` at import
    # time hold a direct reference; patch those too
    import sys
    for name, mod in list(sys.modules.items()):
        holder = getattr(mod, "send_notification", None)
        if holder is not None and getattr(holder, "__module__", "") == "bot.notify":
            monkeypatch.setattr(mod, "send_notification", no_op)
