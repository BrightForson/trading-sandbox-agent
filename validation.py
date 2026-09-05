#!/usr/bin/env python3
"""End-to-end sanity check of the full stack (run before pushing).

Checks: config load, broker connectivity, bar fetch (with the sizing fix),
strategy registry, risk engine, journal (trades/proposals/bets), model manager
health + JSON roundtrip, research tools, notifications file fallback.
No orders are placed.
"""
from datetime import datetime

from bot.config import config
from bot.broker import AlpacaBroker
from bot.strategies import get_strategies, sma_cross
from bot.risk import RiskEngine
from bot.journal import TradeJournal
from bot.models import ModelManager
from bot.research import market_stats, trending_coins, fetch_rss_headlines
from bot.notify import send_notification


def main():
    print("=== validation start ===")
    ok = True

    print("[1/8] config...")
    assert config.symbols and config.notional > 0
    print(f"      symbols={config.symbols} notional={config.notional} "
          f"strategies={getattr(config, 'active_strategies', ['sma_cross'])}")

    print("[2/8] broker connectivity...")
    broker = AlpacaBroker(config.alpaca_api_key_id, config.alpaca_api_secret_key)
    acct = broker.trading_client.get_account()
    print(f"      paper equity=${float(acct.equity):,.2f} cash=${float(acct.cash):,.2f}")

    print("[3/8] bar fetch (explicit start/end window)...")
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    tf = TimeFrame(int(config.timeframe.replace('Min', '')), TimeFrameUnit.Minute)
    df = broker.get_crypto_bars("BTC/USD", tf, config.lookback_bars)
    assert df is not None and len(df) >= config.sma_slow + 1, f"only {len(df)} bars"
    print(f"      {len(df)} bars fetched (>= {config.sma_slow + 1} needed)")

    print("[4/8] strategy registry + risk engine...")
    strategies = get_strategies(getattr(config, "active_strategies", ["sma_cross"]))
    assert strategies, "no strategies resolved"
    import pandas as pd
    synthetic = pd.DataFrame({"close": [10.0] * 50 + [20.0]})
    sigs = sma_cross("BTC/USD", synthetic, config)
    assert sigs and sigs[0]["action"] == "BUY"
    journal = TradeJournal()
    risk = RiskEngine(config, broker, journal)
    allowed, reason = risk.check("BTC/USD", "BUY", 0.001, 50, 0)
    print(f"      strategies={[n for n, _ in strategies]}, synthetic signal=BUY, "
          f"risk check small buy: {allowed} ({reason})")

    print("[5/8] journal (trades/proposals/bets tables)...")
    n_trades = len(journal.get_trades(limit=1000))
    n_props = len(journal.get_proposals(limit=1000))
    n_bets = len(journal.get_open_bets())
    print(f"      trades={n_trades} proposals={n_props} open_bets={n_bets}")

    print("[6/8] model manager (health probe + JSON roundtrip)...")
    mm = ModelManager(journal=journal)
    payload = mm.generate_json('Respond with ONLY this JSON: {"ok": true, "n": 42}')
    assert payload.get("ok") is True and payload.get("n") == 42
    print(f"      active model: {mm.active_model}, json roundtrip ok")

    print("[7/8] research tools (free APIs)...")
    stats = market_stats(["BTC/USD"])
    trending = trending_coins()
    heads = fetch_rss_headlines(limit=5)
    print(f"      BTC=${stats.get('BTC/USD', {}).get('price')} trending={trending[:2]} "
          f"headlines={len(heads)}")

    print("[8/8] notification fallback (file)...")
    import os
    os.environ.pop("DISCORD_WEBHOOK_URL", None)
    send_notification(f"validation run {datetime.now().isoformat()}", config)
    print("      file fallback written (data/reports/)")

    print("=== ALL CHECKS PASSED ===")


if __name__ == "__main__":
    main()
