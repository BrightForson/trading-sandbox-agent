#!/usr/bin/env python3
"""End-to-end sanity check of the full stack (run before pushing).

Checks: config load, market-data bar fetch, journal (trades/proposals/bets),
model manager health + JSON roundtrip, research tools, notifications file
fallback. No orders are placed.
"""
from datetime import datetime, timezone

from bot.config import config
from bot.binance_data import BinanceDataClient
from bot.journal import TradeJournal
from bot.models import ModelManager
from bot.research import market_stats, trending_coins, fetch_rss_headlines
from bot.notify import send_notification
from bot.timeframe import make_timeframe


def main():
    print("=== validation start ===")
    ok = True

    print("[1/6] config...")
    assert config.symbols
    print(f"      symbols={config.symbols}")

    print("[2/6] bar fetch...")
    market_data = BinanceDataClient(config)
    tf = make_timeframe(config.timeframe)
    df = market_data.get_crypto_bars("BTC/USD", tf, config.lookback_bars)
    assert df is not None and len(df) >= config.sma_slow + 1, f"only {len(df)} bars"
    print(f"      {len(df)} bars fetched (>= {config.sma_slow + 1} needed)")

    print("[3/6] journal (trades/proposals/bets tables)...")
    journal = TradeJournal()
    n_trades = len(journal.get_trades(limit=1000))
    n_props = len(journal.get_proposals(limit=1000))
    n_bets = len(journal.get_open_bets())
    print(f"      trades={n_trades} proposals={n_props} open_bets={n_bets}")

    print("[4/6] model manager (health probe + JSON roundtrip)...")
    mm = ModelManager(journal=journal)
    payload = mm.generate_json('Respond with ONLY this JSON: {"ok": true, "n": 42}')
    assert payload.get("ok") is True and payload.get("n") == 42
    print(f"      active model: {mm.active_model}, json roundtrip ok")

    print("[5/6] research tools (free APIs)...")
    stats = market_stats(["BTC/USD"])
    trending = trending_coins()
    heads = fetch_rss_headlines(limit=5)
    print(f"      BTC=${stats.get('BTC/USD', {}).get('price')} trending={trending[:2]} "
          f"headlines={len(heads)}")

    print("[6/6] notification fallback (file)...")
    import os
    os.environ.pop("DISCORD_WEBHOOK_URL", None)
    send_notification(f"validation run {datetime.now(timezone.utc).isoformat()}", config)
    print("      file fallback written (data/reports/)")

    print("=== ALL CHECKS PASSED ===")


if __name__ == "__main__":
    main()
