"""Backtester: replay the SMA crossover strategy over historical bars.

Fetches up to ~1000 daily or 15-min bars per symbol from Alpaca (free tier
paginates ~86 bars/request; we page until we have the window), simulates the
long-only SMA20/50 crossover with fixed notional per BUY, and reports
P&L / win rate / max drawdown / time-in-market per symbol + combined.

Usage (from repo root):
    python backtest.py                    # default: 15Min bars, ~30d window
    python backtest.py --days 90          # longer window for 15Min
    python backtest.py --timeframe 1Day --days 365   # daily bars, 1 year
"""
import argparse
import math
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

from bot.config import config
from bot.strategy import check_crossover


PAGE_LIMIT = 500


def fetch_bars(broker, symbol, timeframe, days):
    """Page forward through history to collect `days` worth of closed bars."""
    end = datetime.now(timezone.utc)
    deadline = end - timedelta(days=days)
    frames = []
    cursor = deadline
    while cursor < end:
        try:
            from alpaca.data.requests import CryptoBarsRequest
            req = CryptoBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=timeframe,
                start=cursor,
                end=min(cursor + timedelta(days=12), end),
                limit=1000,
            )
            df = broker.data_client.get_crypto_bars(req).df
        except Exception as e:
            print(f"  bar fetch failed for {symbol} at {cursor}: {e}")
            break
        if df is None or df.empty:
            break
        frames.append(df)
        last_ts = df.index.get_level_values("timestamp").max()
        nxt = last_ts + timedelta(minutes=1)
        if nxt <= cursor:
            break
        cursor = nxt
    if not frames:
        return None
    out = pd.concat(frames)
    if isinstance(out.index, pd.MultiIndex):
        out = out.reset_index(level=0, drop=True)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    # drop the still-forming bar
    if not out.empty:
        out = out.iloc[:-1]
    return out


def simulate(closes, sma_fast, sma_slow, notional):
    """Simulate long-only SMA crossover over a close-price series.
    Returns stats dict and trade list."""
    df = pd.DataFrame({"close": closes})
    trades = []
    position_qty = 0.0
    entry_price = 0.0
    cash_spent = 0.0
    realized_pnl = 0.0
    wins, losses = 0, 0
    equity_curve = []

    fast = df["close"].rolling(sma_fast).mean()
    slow = df["close"].rolling(sma_slow).mean()

    for i in range(sma_slow, len(df)):
        price = float(df["close"].iloc[i])
        prev_f, prev_s = fast.iloc[i - 1], slow.iloc[i - 1]
        curr_f, curr_s = fast.iloc[i], slow.iloc[i]
        if math.isnan(prev_f) or math.isnan(prev_s) or math.isnan(curr_f) or math.isnan(curr_s):
            continue
        golden = prev_f <= prev_s and curr_f > curr_s
        death = prev_f >= prev_s and curr_f < curr_s
        ts = df.index[i]
        if golden and position_qty == 0:
            position_qty = notional / price
            entry_price = price
            cash_spent += notional
            trades.append({"ts": ts, "action": "BUY", "price": price, "qty": position_qty})
        elif death and position_qty > 0:
            proceeds = position_qty * price
            pnl = proceeds - cash_spent
            realized_pnl += pnl
            if pnl > 0:
                wins += 1
            else:
                losses += 1
            trades.append({"ts": ts, "action": "SELL", "price": price, "qty": position_qty, "pnl": pnl})
            position_qty = 0.0
            entry_price = 0.0
            cash_spent = 0.0
        # mark-to-market equity: realized so far + open position value - cost basis
        equity_curve.append(realized_pnl + (position_qty * price - cash_spent if position_qty > 0 else 0.0))

    # close any open position at the last price for accounting
    if position_qty > 0:
        price = float(df["close"].iloc[-1])
        pnl = position_qty * price - cash_spent
        if pnl > 0:
            wins += 1
        else:
            losses += 1
        trades.append({"ts": df.index[-1], "action": "SELL(open)", "price": price, "qty": position_qty, "pnl": pnl})
        position_qty = 0.0

    total_trades = wins + losses
    pnl_total = sum(t.get("pnl", 0.0) for t in trades)
    # max drawdown on mark-to-market equity curve (relative to initial capital)
    max_dd = 0.0
    peak = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        base = notional  # drawdown measured against initial capital
        if base > 0:
            dd = (peak - v) / base
            max_dd = max(max_dd, dd)

    bars_in_market = sum(1 for t in trades if t["action"] == "BUY")
    return {
        "round_trips": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / total_trades * 100) if total_trades else 0.0,
        "pnl": pnl_total,
        "return_pct": (pnl_total / notional * 100) if notional else 0.0,
        "max_drawdown_pct": max_dd * 100,
        "bars_with_position": len(equity_curve),
        "trades": trades,
    }


def run(days=30, timeframe_str=None, per_request_limit=500):
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from bot.broker import AlpacaBroker

    tf_str = timeframe_str or config.timeframe
    if tf_str == "1Day":
        timeframe = TimeFrame.Day
    else:
        minutes = int(tf_str.replace("Min", ""))
        timeframe = TimeFrame(minutes, TimeFrameUnit.Minute)

    broker = AlpacaBroker(config.alpaca_api_key_id, config.alpaca_api_secret_key)

    print(f"=== Backtest: SMA{config.sma_fast}/{config.sma_slow} crossover, "
          f"{tf_str} bars, last {days} days ===")
    combined_pnl = 0.0
    results = {}
    for symbol in config.symbols:
        print(f"\nFetching {days}d of {tf_str} bars for {symbol}...")
        df = fetch_bars(broker, symbol, timeframe, days)
        if df is None or len(df) < config.sma_slow + 2:
            print(f"  insufficient data for {symbol} ({0 if df is None else len(df)} bars) — skipping")
            continue
        closes = df["close"]
        stats = simulate(closes, config.sma_fast, config.sma_slow, config.notional)
        results[symbol] = stats
        combined_pnl += stats["pnl"]
        print(f"  bars: {len(df)}  range: {df.index.min()} -> {df.index.max()}")
        print(f"  round trips: {stats['round_trips']}  (W {stats['wins']} / L {stats['losses']})")
        print(f"  win rate: {stats['win_rate']:.1f}%")
        print(f"  P&L: ${stats['pnl']:.2f}  ({stats['return_pct']:+.2f}% on ${config.notional} notional)")
        print(f"  max drawdown: {stats['max_drawdown_pct']:.2f}%")
    print(f"\n=== Combined P&L across {len(results)} symbols: ${combined_pnl:.2f} ===")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--timeframe", type=str, default=None, help="e.g. 15Min or 1Day")
    args = parser.parse_args()
    run(days=args.days, timeframe_str=args.timeframe)
