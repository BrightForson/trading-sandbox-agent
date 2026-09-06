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
    if hasattr(broker, "fetch_history"):
        from bot.binance_data import _interval_for
        return broker.fetch_history(symbol, days, interval=_interval_for(timeframe))
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


def simulate(bars, sma_fast, sma_slow, notional, taker_fee_pct=0.0, slippage_bps=0.0,
             max_notional_per_trade=0.0, compound=False,
             atr_period=0, catastrophic_atr_multiple=0.0, fallback_stop_pct=0.0):
    """Simulate a long-only crossover using only information available at close.

    A cross observed on bar ``i`` is filled at the following bar's open. Fees and
    adverse slippage are applied on every fill so the result is not a same-bar,
    zero-friction estimate.

    Sizing mirrors the live loop: each BUY deploys at most
    ``max_notional_per_trade`` (0 = uncapped), never more than the requested
    ``notional`` or 98% of available cash. With ``compound=False`` (default)
    position size stays fixed in dollars, exactly like live fixed-notional
    sizing; wins do NOT grow the next trade.

    Stops mirror the live loop exactly: the stop is fixed at entry
    (entry - catastrophic_atr_multiple * entry-ATR, or fallback_stop_pct
    below entry when ATR is unavailable) and never moves afterwards. A stop
    exit fills at the next bar's open (never the signal bar's close), so the
    simulation can even lose slightly more than the stop distance.
    """
    if isinstance(bars, pd.Series):
        df = pd.DataFrame({"close": bars, "open": bars})
    else:
        df = bars.copy()
        if "close" not in df:
            raise ValueError("bars must contain a close column")
        if "open" not in df:
            df["open"] = df["close"]
    df = df.dropna(subset=["close", "open"]).copy()
    if len(df) < sma_slow + 2:
        return {"round_trips": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "pnl": 0.0, "return_pct": 0.0, "max_drawdown_pct": 0.0,
                "exposure_pct": 0.0, "turnover": 0.0, "fees": 0.0,
                "benchmark_return_pct": 0.0, "trades": []}

    fee_rate = max(0.0, float(taker_fee_pct)) / 100.0
    slip_rate = max(0.0, float(slippage_bps)) / 10_000.0
    per_trade_cap = max(0.0, float(max_notional_per_trade or 0.0))
    base_notional = max(0.0, float(notional))
    if base_notional <= 0:
        return {"round_trips": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "pnl": 0.0, "return_pct": 0.0, "max_drawdown_pct": 0.0,
                "exposure_pct": 0.0, "turnover": 0.0, "fees": 0.0,
                "benchmark_return_pct": 0.0, "trades": []}
    trades = []
    position_qty = 0.0
    entry_cost = 0.0
    cash = base_notional
    initial_capital = base_notional
    total_fees = 0.0
    turnover = 0.0
    wins, losses = 0, 0
    equity_curve = [initial_capital]
    bars_with_position = 0

    fast = df["close"].rolling(sma_fast).mean()
    slow = df["close"].rolling(sma_slow).mean()
    stop_mult = max(0.0, float(catastrophic_atr_multiple or 0.0))
    stop_fallback_pct = max(0.0, float(fallback_stop_pct or 0.0))
    stop_price = 0.0
    if atr_period > 0 and (stop_mult > 0 or stop_fallback_pct > 0) and "high" in df.columns and "low" in df.columns:
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift()).abs(),
            (df["low"] - df["close"].shift()).abs(),
        ], axis=1).max(axis=1)
        atr_series = tr.rolling(atr_period).mean()
    else:
        atr_series = None

    def _entry_stop(entry_px, signal_idx):
        """Stop level as decided at entry; None = no stop modelled."""
        if stop_mult <= 0 and stop_fallback_pct <= 0:
            return None
        if atr_series is not None and stop_mult > 0:
            atr_val = atr_series.iloc[signal_idx]
            if not math.isnan(atr_val) and atr_val > 0:
                return entry_px - stop_mult * float(atr_val)
        if stop_fallback_pct > 0:
            return entry_px * (1 - stop_fallback_pct / 100.0)
        return None

    def _close_position(exit_tag, fill_idx, signal_idx, reason=""):
        nonlocal cash, total_fees, turnover, wins, losses, position_qty, entry_cost
        price = float(df["open"].iloc[fill_idx]) * (1 - slip_rate)
        proceeds = position_qty * price
        fee = proceeds * fee_rate
        net_proceeds = proceeds - fee
        pnl = net_proceeds - entry_cost
        if pnl > 0:
            wins += 1
        else:
            losses += 1
        trades.append({"ts": df.index[fill_idx], "action": exit_tag, "price": price,
                       "qty": position_qty, "fee": fee, "pnl": pnl,
                       "signal_ts": df.index[signal_idx], "reason": reason})
        cash += net_proceeds
        total_fees += fee
        turnover += proceeds
        position_qty = 0.0
        entry_cost = 0.0

    for i in range(sma_slow, len(df) - 1):
        prev_f, prev_s = fast.iloc[i - 1], slow.iloc[i - 1]
        curr_f, curr_s = fast.iloc[i], slow.iloc[i]
        if math.isnan(prev_f) or math.isnan(prev_s) or math.isnan(curr_f) or math.isnan(curr_s):
            continue
        golden = prev_f <= prev_s and curr_f > curr_s
        death = prev_f >= prev_s and curr_f < curr_s
        fill_index = i + 1
        ts = df.index[fill_index]
        # stop check uses bar i's close (information available at signal time);
        # the exit itself fills at bar i+1's open
        if position_qty > 0 and stop_price > 0 and float(df["close"].iloc[i]) <= stop_price:
            _close_position("SELL(stop)", fill_index, i, reason="entry-fixed stop")
            stop_price = 0.0
            continue
        if golden and position_qty == 0:
            price = float(df["open"].iloc[fill_index]) * (1 + slip_rate)
            target_notional = base_notional
            if per_trade_cap > 0:
                target_notional = min(target_notional, per_trade_cap)
            if not compound:
                target_notional = min(target_notional, initial_capital * 0.98)
            target_notional = min(target_notional, cash * 0.98)
            if target_notional <= 0:
                continue
            gross_budget = target_notional / (1 + fee_rate)
            position_qty = gross_budget / price
            fee = gross_budget * fee_rate
            entry_cost = gross_budget + fee
            cash -= entry_cost
            total_fees += fee
            turnover += gross_budget
            trades.append({"ts": ts, "action": "BUY", "price": price, "qty": position_qty,
                           "fee": fee, "signal_ts": df.index[i]})
            stop_price = _entry_stop(price, i) or 0.0
        elif death and position_qty > 0:
            _close_position("SELL", fill_index, i)
            stop_price = 0.0
        if position_qty > 0:
            bars_with_position += 1
        equity_curve.append(cash + position_qty * float(df["close"].iloc[fill_index]))

    # close any open position at the last price for accounting
    if position_qty > 0:
        price = float(df["close"].iloc[-1]) * (1 - slip_rate)
        proceeds = position_qty * price
        fee = proceeds * fee_rate
        pnl = proceeds - fee - entry_cost
        if pnl > 0:
            wins += 1
        else:
            losses += 1
        trades.append({"ts": df.index[-1], "action": "SELL(open)", "price": price,
                       "qty": position_qty, "fee": fee, "pnl": pnl})
        cash += proceeds - fee
        total_fees += fee
        turnover += proceeds
        equity_curve.append(cash)

    total_trades = wins + losses
    pnl_total = cash - initial_capital
    max_dd = 0.0
    peak = initial_capital
    for v in equity_curve:
        peak = max(peak, v)
        if peak > 0:
            dd = (peak - v) / peak
            max_dd = max(max_dd, dd)

    benchmark_entry = float(df["open"].iloc[sma_slow]) * (1 + slip_rate)
    benchmark_qty = initial_capital / (benchmark_entry * (1 + fee_rate))
    benchmark_exit = float(df["close"].iloc[-1]) * (1 - slip_rate)
    benchmark_value = benchmark_qty * benchmark_exit * (1 - fee_rate)

    return {
        "round_trips": total_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / total_trades * 100) if total_trades else 0.0,
        "pnl": pnl_total,
        "return_pct": (pnl_total / initial_capital * 100) if initial_capital else 0.0,
        "max_drawdown_pct": max_dd * 100,
        "exposure_pct": (bars_with_position / max(1, len(df) - sma_slow - 1) * 100),
        "turnover": turnover,
        "fees": total_fees,
        "stop_exits": sum(1 for t in trades if str(t.get("action", "")).startswith("SELL(stop)")),
        "benchmark_return_pct": ((benchmark_value / initial_capital) - 1) * 100,
        "trades": trades,
    }


def run(days=30, timeframe_str=None, per_request_limit=500):
    from bot.timeframe import make_timeframe
    from bot.broker import make_broker

    tf_str = timeframe_str or config.timeframe
    timeframe = make_timeframe(tf_str)

    broker = make_broker(config)

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
        execution = getattr(config, "execution", None) or {}
        risk = getattr(config, "risk", None) or {}
        stats = simulate(df, config.sma_fast, config.sma_slow, config.notional,
                         taker_fee_pct=execution.get("taker_fee_pct", 0),
                         slippage_bps=execution.get("slippage_bps", 0),
                         max_notional_per_trade=risk.get("max_notional_per_trade", 0),
                         atr_period=int(risk.get("atr_period", 0) or 0),
                         catastrophic_atr_multiple=risk.get("catastrophic_atr_multiple", 0),
                         fallback_stop_pct=risk.get("fallback_stop_pct", 0))
        results[symbol] = stats
        combined_pnl += stats["pnl"]
        print(f"  bars: {len(df)}  range: {df.index.min()} -> {df.index.max()}")
        print(f"  round trips: {stats['round_trips']}  (W {stats['wins']} / L {stats['losses']})  stops: {stats['stop_exits']}")
        print(f"  win rate: {stats['win_rate']:.1f}%")
        print(f"  P&L: ${stats['pnl']:.2f}  ({stats['return_pct']:+.2f}% on ${config.notional} notional)")
        print(f"  max drawdown: {stats['max_drawdown_pct']:.2f}%")
        print(f"  exposure: {stats['exposure_pct']:.1f}%  turnover: ${stats['turnover']:.2f}  fees: ${stats['fees']:.2f}")
        print(f"  buy-and-hold benchmark: {stats['benchmark_return_pct']:+.2f}%")
    print(f"\n=== Combined P&L across {len(results)} symbols: ${combined_pnl:.2f} ===")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--timeframe", type=str, default=None, help="e.g. 15Min or 1Day")
    args = parser.parse_args()
    run(days=args.days, timeframe_str=args.timeframe)
