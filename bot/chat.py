"""Two-way Discord chat via a real bot user (Bright Bot).

Reads recent messages in the webhook's channel via REST (no gateway needed),
lets the agent answer questions with live data, replies through the same
channel using the bot token. Runs on its own 5-min cron — never collides
with the 15-min trading cron.

Safety: chat is READ-ONLY for trading decisions. No matter what is asked,
this module never places orders. It can only answer with data + analysis.
"""
import os
import time
import re
import json
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from bot.journal import TradeJournal
from bot.notify import send_notification

load_dotenv()

API = "https://discord.com/api/v10"
REPLY_COOLDOWN_SECONDS = 90  # don't reply to our own/old messages
STATE_KEY = "discord_chat_last_seen"


def _headers():
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN not set")
    return {"Authorization": f"Bot {token}"}


def extract_channel_id(journal=None):
    """Resolve the alerts channel ID.

    The numeric part of the webhook URL is the WEBHOOK id, not the channel id
    (a past bug). Instead: GET the webhook object -> its channel_id, cached in
    journal meta so we don't re-fetch every cycle.
    """
    if journal is not None:
        cached = journal.get_meta("discord_chat_channel_id")
        if cached:
            return cached
    webhook = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook:
        return None
    try:
        resp = requests.get(webhook, timeout=15)
        resp.raise_for_status()
        channel_id = resp.json().get("channel_id")
        if channel_id and journal is not None:
            journal.set_meta("discord_chat_channel_id", str(channel_id))
        return channel_id
    except Exception as e:
        print(f"[discord-chat] webhook lookup failed: {e}")
        return None


def _get_messages(channel_id, limit=20):
    resp = requests.get(
        f"{API}/channels/{channel_id}/messages",
        headers=_headers(),
        params={"limit": limit},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _send_message(channel_id, content):
    chunks = [content[i:i + 1900] for i in range(0, len(content), 1900)]
    for chunk in chunks:
        resp = requests.post(
            f"{API}/channels/{channel_id}/messages",
            headers={**_headers(), "Content-Type": "application/json"},
            json={"content": chunk},
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            print(f"[discord-chat] send failed {resp.status_code}: {resp.text[:200]}")
            time.sleep(1)


def _is_our_bot(msg):
    """Messages authored by Bright Bot itself (id from token) or other bots."""
    author = msg.get("author", {})
    if author.get("bot"):
        return True
    token = os.getenv("DISCORD_BOT_TOKEN", "")
    first = token.split(".")[0] if token else ""
    return author.get("id") == first


def _answer_prompt(question, context_block):
    return f"""You are the AI assistant for a crypto paper-trading system. You are
chatting with the system's owner in Discord. Answer concisely (under 1500 chars),
plainly, no markdown headers. You cannot and will not execute any trades from
chat — this is analysis only. If asked to buy/sell something, explain what you
would propose and why, and note it's shadow mode (no execution).

Live system context:
{context_block}

User question: {question}

Helpful, direct answer:"""


def _system_context(broker, cfg, journal):
    """Compact live snapshot the chat agent can draw from."""
    try:
        acct = broker.trading_client.get_account()
        acct_block = (f"Equity ${float(acct.equity):,.2f}, Cash ${float(acct.cash):,.2f}, "
                      f"Paper account, Alpaca")
    except Exception as e:
        acct_block = f"account unavailable ({e})"
    try:
        positions = list(broker.trading_client.get_all_positions())
        if positions:
            pos_block = "; ".join(
                f"{p.symbol} {float(p.qty):.6f} @ ${float(p.avg_entry_price):,.0f} "
                f"({float(p.unrealized_plpc) * 100:+.1f}%)"
                for p in positions
            )
        else:
            pos_block = "flat (no open positions)"
    except Exception:
        pos_block = "positions unavailable"
    try:
        trades = journal.get_trades(limit=5)
        trade_block = "; ".join(f"{t[1][:16]} {t[3]} {t[2]} @ {t[5]:.0f}" for t in trades) or "none yet"
    except Exception:
        trade_block = "unavailable"
    try:
        proposals = journal.get_proposals(limit=5)
        prop_block = "; ".join(
            f"{p[1][:16]} {p[3]} {p[4]} {p[2]} conf={p[7]}" for p in proposals
        ) or "none yet"
    except Exception:
        prop_block = "unavailable"
    return (
        f"Account: {acct_block}\nPositions: {pos_block}\n"
        f"Recent trades: {trade_block}\nRecent AI proposals: {prop_block}\n"
        f"Strategy: SMA{cfg.sma_fast}/{cfg.sma_slow} crossover on {', '.join(cfg.symbols)}, "
        f"shadow mode = AI proposes, never executes on the real account."
    )
    try:
        from bot.shadow import ShadowAccount
        shadow = ShadowAccount(cfg, broker, journal=journal)
        acct_line = shadow.status_line()
        pos = shadow._positions()
        if pos:
            lines = []
            for sym, p in pos.items():
                lines.append(f"{sym} qty {p['qty']:.6f} entry ${p['entry']:,.2f}")
            acct_line += " | open: " + "; ".join(lines)
        return context + f"\nAI shadow account (virtual $20): {acct_line}"
    except Exception as e:
        return context + f"\nAI shadow account: unavailable ({e})"


def run_chat_cycle(cfg, broker, journal=None, model=None):
    """One chat cycle: read new messages, answer new human ones, mark seen."""
    journal = journal or TradeJournal()
    channel_id = extract_channel_id(journal=journal)
    if not channel_id:
        print("[discord-chat] no channel id (webhook missing) — skipping")
        return
    try:
        msgs = _get_messages(channel_id, limit=20)
    except Exception as e:
        print(f"[discord-chat] read failed: {e}")
        return

    last_seen = float(journal.get_meta(STATE_KEY) or 0)
    now = time.time()
    fresh = []
    for msg in msgs:
        ts = (datetime.fromisoformat(msg["timestamp"]).timestamp()
              if "timestamp" in msg else 0)
        try:
            ts = datetime.fromisoformat(msg["timestamp"]).timestamp()
        except Exception:
            ts = 0
        if ts <= last_seen or ts > now:
            continue
        if _is_our_bot(msg):
            continue
        fresh.append(msg)

    if not fresh:
        return

    from bot.models import ModelManager
    if model is None:
        model = ModelManager(journal=journal)

    newest_ts = max(
        datetime.fromisoformat(m["timestamp"]).timestamp() for m in fresh
    )
    for msg in reversed(fresh):  # oldest first, natural conversation order
        question = (msg.get("content") or "").strip()
        if not question:
            continue
        author = msg.get("author", {}).get("username", "user")
        try:
            context = _system_context(broker, cfg, journal)
            raw = model.generate_text(
                _answer_prompt(question, context), max_tokens=600, temperature=0.5
            )
            answer = (raw or "").strip()
        except Exception as e:
            answer = f"(agent unavailable: {e})"
        _send_message(channel_id, answer[:1900])
        print(f"[discord-chat] answered {author}: {question[:60]}...")

    journal.set_meta(STATE_KEY, str(newest_ts))
