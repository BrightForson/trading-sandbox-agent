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
import json
import base64
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from bot.journal import TradeJournal
from bot.notify import send_notification

load_dotenv()

API = "https://discord.com/api/v10"
STATE_KEY = "discord_chat_last_seen"
MAX_REPLIES_PER_CYCLE = 5  # defer the rest; they stay > last_seen and answer next cycle


def _owner_ids():
    """Comma-separated DISCORD_OWNER_IDS env; empty = answer no one (fail-closed)."""
    raw = os.getenv("DISCORD_OWNER_IDS", "").strip()
    return {s.strip() for s in raw.split(",") if s.strip()}


def _advance_seen(journal, msg):
    """Per-message checkpoint: persist the cursor up to this message's ts."""
    try:
        ts = datetime.fromisoformat(msg["timestamp"]).timestamp()
    except Exception:
        return
    cur = float(journal.get_meta(STATE_KEY) or 0)
    if ts > cur:
        journal.set_meta(STATE_KEY, str(ts))


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
        from bot.notify import _mask_secrets
        print(f"[discord-chat] webhook lookup failed: {_mask_secrets(e)}")
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
        for attempt in range(3):
            resp = requests.post(
                f"{API}/channels/{channel_id}/messages",
                headers={**_headers(), "Content-Type": "application/json"},
                json={"content": chunk},
                timeout=15,
            )
            if resp.status_code in (200, 201):
                break
            if resp.status_code == 429:
                try:
                    time.sleep(min(10, float(resp.headers.get("Retry-After", 1))))
                except (TypeError, ValueError):
                    time.sleep(1)
                continue
            print(f"[discord-chat] send failed {resp.status_code}: {resp.text[:200]}")
            time.sleep(1)
            break


def _is_our_bot(msg):
    """Messages authored by Bright Bot itself (id from token) or other bots."""
    author = msg.get("author", {})
    if author.get("bot"):
        return True
    token = os.getenv("DISCORD_BOT_TOKEN", "")
    first = token.split(".")[0] if token else ""
    if not first:
        return False
    try:
        # the token's first segment is the base64-encoded bot user id
        padded = first + "=" * (-len(first) % 4)
        bot_id = base64.b64decode(padded).decode("utf-8", "ignore")
        return author.get("id") == bot_id
    except Exception:
        return False


def _answer_prompt(question, context_block):
    return f"""You are the AI assistant for a crypto paper-trading system. You are
chatting with the system's owner in Discord. Answer concisely (under 1500 chars),
plainly, no markdown headers. You cannot and will not execute any trades from
chat — this is analysis only. If asked to buy/sell something, explain what you
would propose and why, and note it's shadow mode (no execution).

IMPORTANT: answer ONLY from the numbers in the live system context below.
If a number is not in the context, say you don't have it rather than
guessing. Never invent P&L, positions, or risk-gate states.

Live system context:
{context_block}

User question: {question}

Helpful, direct answer:"""


def _system_context(broker, cfg, journal):
    """Compact live snapshot the chat agent can draw from."""
    try:
        acct = broker.get_account()
        acct_block = (f"Equity ${float(acct.equity):,.2f}, Cash ${float(acct.cash):,.2f}, "
                      f"Paper account, Binance public data (simulated)")
    except Exception as e:
        acct_block = f"account unavailable ({e})"
    try:
        positions = list(broker.get_all_positions())
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
    try:
        kill = journal.get_meta("kill_switch") == "on"
        kill_reason = journal.get_meta("kill_switch_reason") or ""
        if kill:
            risk_block = "Tier 1 kill switch: ON (BUYs blocked"
            if kill_reason:
                risk_block += f", reason: {kill_reason}"
            risk_block += ")"
        else:
            risk_block = "Tier 1 kill switch: off (BUYs allowed)"
    except Exception:
        risk_block = "risk-gate state unavailable"
    context = (
        f"Account: {acct_block}\nPositions: {pos_block}\n"
        f"Risk gates: {risk_block}\n"
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
        context += f"\nAI shadow account (virtual $20): {acct_line}"
    except Exception as e:
        context += f"\nAI shadow account: unavailable ({e})"
    try:
        from bot.wallet import BettingWallet
        wallet = BettingWallet(cfg, journal=journal)
        context += f"\nTier 3 Polymarket wallet (virtual ${wallet.start_cash:.0f}): {wallet.status_line()}"
    except Exception as e:
        context += f"\nTier 3 Polymarket wallet: unavailable ({e})"
    return context


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
        from bot.notify import _mask_secrets
        print(f"[discord-chat] read failed: {_mask_secrets(e)}")
        return

    last_seen = float(journal.get_meta(STATE_KEY) or 0)
    now = time.time()
    owners = _owner_ids()
    if not owners:
        # no owner configured: answer no one (fail-closed) but still advance
        # the cursor past bot/ignored messages so they never backlog
        newest = 0.0
        for msg in msgs:
            try:
                ts = datetime.fromisoformat(msg["timestamp"]).timestamp()
            except Exception:
                ts = 0
            newest = max(newest, ts)
        if newest > last_seen:
            journal.set_meta(STATE_KEY, str(newest))
        return

    fresh = []
    for msg in msgs:
        try:
            ts = datetime.fromisoformat(msg["timestamp"]).timestamp()
        except Exception:
            ts = 0
        if ts <= last_seen or ts > now:
            continue
        if _is_our_bot(msg):
            continue
        author_id = msg.get("author", {}).get("id")
        if author_id not in owners:
            continue  # not the owner: never answered, never disclosed to
        fresh.append(msg)

    if not fresh:
        return

    from bot.models import ModelManager
    if model is None:
        model = ModelManager(journal=journal)

    answered = 0
    deferred = 0
    for msg in reversed(fresh):  # oldest first, natural conversation order
        question = (msg.get("content") or "").strip()
        if not question:
            # advance past empty messages so they don't backlog
            _advance_seen(journal, msg)
            continue
        if answered >= MAX_REPLIES_PER_CYCLE:
            deferred += 1
            continue  # leave unseen; answered next cycle (per-message checkpoint)
        author = msg.get("author", {}).get("username", "user")
        try:
            context = _system_context(broker, cfg, journal)
            raw = model.generate_text(
                _answer_prompt(question, context), max_tokens=600, temperature=0.5
            )
            answer = (raw or "").strip()
        except Exception as e:
            from bot.notify import _mask_secrets
            answer = f"(agent unavailable: {_mask_secrets(e)})"
        try:
            _send_message(channel_id, answer[:1900])
            # checkpoint AFTER a successful send: failed sends stay unseen
            # and retry next cycle instead of being silently dropped
            _advance_seen(journal, msg)
        except Exception as e:
            from bot.notify import _mask_secrets
            print(f"[discord-chat] reply send failed (will retry next cycle): {_mask_secrets(e)}")
            break  # stop the burst; un-answered messages remain unseen
        answered += 1
        print(f"[discord-chat] answered {author}: {question[:60]}...")

    if deferred:
        print(f"[discord-chat] deferred {deferred} message(s) to next cycle (reply cap)")
