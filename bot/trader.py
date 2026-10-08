from datetime import datetime, timezone
from bot.config import config
from bot.binance_data import BinanceDataClient
from bot.journal import TradeJournal
from bot.notify import send_notification


def _tier_one_liners(journal, market_data):
    """Money lines + activity one-liners for tiers 2/3/4 (best-effort each)."""
    from bot.brief import money_line, emoji_for
    lines = []
    # Tier 2 — AI shadow account
    try:
        from bot.shadow import ShadowAccount
        sh = ShadowAccount(config, market_data, journal=journal)
        v = sh.mark_to_market()
        delta = v[0] - sh.start_cash
        lines.append(f"{emoji_for(delta)} {money_line('Tier 2 AI', v[0], sh.start_cash)}"
                     + (f" | {len(sh._positions())} open AI trade(s)" if sh._positions() else " | no open trades"))
    except Exception:
        pass
    # Tier 3 — Polymarket wallet
    try:
        from bot.wallet import BettingWallet
        w = BettingWallet(config, journal=journal)
        v = w.valuation()
        delta = v["equity"] - w.start_cash
        lines.append(f"{emoji_for(delta)} {money_line('Tier 3 Bets', v['equity'], w.start_cash)}"
                     + (f" | {v['open_bets']} open bet(s)" if v["open_bets"] else " | no open bets"))
    except Exception:
        pass
    # Tier 4 — memecoin canary
    try:
        from bot.memecoin import MemecoinLedger
        led = MemecoinLedger(config, journal=journal)
        v = led.valuation()
        delta = v["equity"] - led.start_cash
        pos = led._positions()
        lines.append(f"{emoji_for(delta)} {money_line('Tier 4 Coins', v['equity'], led.start_cash)}"
                     + (f" | holding {', '.join(sorted(pos))}" if pos else " | nothing held"))
    except Exception:
        pass
    return "\n".join(lines)


def send_heartbeat(journal, market_data):
    """One brief hourly glance: money per tier, activity only if it exists."""
    current_hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    last = journal.get_meta("last_heartbeat_hour")
    if last is not None and last == current_hour:
        return
    try:
        body = _tier_one_liners(journal, market_data)
        if not body:
            print(f"[{datetime.now(timezone.utc)}] Heartbeat skipped: no tier status available")
            return
        send_notification(f"🫀 {body}", config)
        journal.set_meta("last_heartbeat_hour", current_hour)
        print(f"[{datetime.now(timezone.utc)}] Heartbeat sent (hour {current_hour})")
    except Exception as e:
        print(f"[{datetime.now(timezone.utc)}] Heartbeat failed (will retry next cycle): {e}")


def run_agent_cycle():
    """One AI agent (scout) cycle in shadow mode, then the hourly heartbeat.

    The heartbeat runs even when the agent cycle raises, so a model outage
    still leaves the owner one status message per hour; the error is then
    re-raised so the workflow run is marked failed.
    """
    from bot.agent import TradingAgent
    market_data = BinanceDataClient(config)
    journal = TradeJournal()
    try:
        agent = TradingAgent(config, market_data, journal=journal)
        agent.run_cycle()
    finally:
        try:
            send_heartbeat(journal, market_data)
        except Exception as e:
            print(f"[{datetime.now(timezone.utc)}] Heartbeat failed: {e}")


def run_scanner_cycle():
    """One Polymarket scanner cycle (read-only API, paper bets)."""
    from bot.polymarket import scan, settle_open_bets
    from bot.wallet import BettingWallet
    journal = TradeJournal()
    try:
        settle_open_bets(config, journal=journal)
    except Exception as e:
        print(f"[{datetime.now(timezone.utc)}] Bet settlement failed: {e}")
    scan(config, journal=journal)
    try:
        wallet = BettingWallet(config, journal=journal)
        wallet.snapshot()
        from bot.brief import money_line, emoji_for
        v = wallet.valuation()
        delta = v["equity"] - wallet.start_cash
        bets_txt = f" | {v['open_bets']} open bet(s)" if v["open_bets"] else " | no open bets"
        send_notification(f"{emoji_for(delta)} {money_line('Tier 3 Bets', v['equity'], wallet.start_cash)}{bets_txt}", config)
    except Exception as e:
        print(f"[{datetime.now(timezone.utc)}] Wallet snapshot failed: {e}")
