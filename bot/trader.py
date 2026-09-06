import time
import schedule
import pandas as pd
from datetime import datetime, timezone
from bot.config import config
from bot.broker import make_broker
from bot.strategies import get_strategies
from bot.risk import RiskEngine
from bot.journal import TradeJournal
from bot.errors import BrokerError
from bot.notify import send_notification
from bot.timeframe import make_timeframe
import time as _time

HEARTBEAT_INTERVAL_SECONDS = 3600


def send_heartbeat(broker, journal):
    """Send a status message to Discord once per UTC clock hour."""
    now = _time.time()
    current_hour = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H")
    last = journal.get_meta("last_heartbeat_hour")
    if last is not None and last == current_hour:
        return
    try:
        acct = broker.get_account()
        positions = list(broker.get_all_positions())
        pos_lines = [f"  • {p.symbol}: {float(p.qty):.6f} (${float(p.unrealized_pl):,.2f} unrealized)" for p in positions]
        pos_section = "\n".join(pos_lines) if pos_lines else "flat (no open positions)"

        # SMA gaps per symbol for signal proximity (closed bars only)
        tf = make_timeframe(config.timeframe)
        gap_lines = []
        for sym in config.symbols:
            try:
                df = broker.get_crypto_bars(sym, tf, config.lookback_bars)
                close = df['close'].iloc[:-1]  # drop the still-forming bar
                f = close.rolling(20).mean().iloc[-1]
                s = close.rolling(50).mean().iloc[-1]
                gap_lines.append(f"  • {sym}: SMA20 {f:,.2f} vs SMA50 {s:,.2f} ({(f-s)/s*100:+.2f}%)")
            except Exception:
                gap_lines.append(f"  • {sym}: data unavailable")

        msg = (
            f"🫀 **Heartbeat** {datetime.now(timezone.utc).strftime('%H:%M UTC')} (paper)\n"
            f"Equity ${float(acct.equity):,.2f} | Cash ${float(acct.cash):,.2f}\n"
            f"Positions: {pos_section}\n"
            f"Signal gaps (SMA20−SMA50):\n" + "\n".join(gap_lines)
        )
        send_notification(msg, config)
        journal.set_meta("last_heartbeat_hour", current_hour)
        print(f"[{datetime.now()}] Heartbeat sent (hour {current_hour})")
    except Exception as e:
        print(f"[{datetime.now()}] Heartbeat failed (will retry next cycle): {e}")


# ---------------- persistent strategy state ----------------
# Edge-triggered signals (a cross on ONE bar) die with the cycle that saw
# them: if that run is late or fails, the exit/entry is skipped forever. This
# state, persisted in journal meta, survives missed cycles: the transition
# is derived from the CURRENT fast-vs-slow relation vs the LAST SUCCESSFULLY
# PROCESSED one, so a missed bar catch-up happens however late it arrives.

import json as _json

POSITION_STATE_KEY = "strat_state_positions"


def _load_position_states(journal):
    raw = journal.get_meta(POSITION_STATE_KEY)
    if not raw:
        return {}
    try:
        data = _json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_position_states(journal, states):
    journal.set_meta(POSITION_STATE_KEY, _json.dumps(states))


def _sma_relation(df, cfg):
    """'above' | 'below' | None (insufficient data) using closed bars."""
    fast, slow = cfg.sma_fast, cfg.sma_slow
    if df is None or len(df) < slow:
        return None
    close = df["close"]
    f = close.rolling(fast).mean().iloc[-1]
    s = close.rolling(slow).mean().iloc[-1]
    if f != f or s != s:  # NaN guard
        return None
    return "above" if f > s else "below"


def _cross_on_current_edge(df, cfg):
    """True if the fast/slow cross happened on the LAST closed bar (the
    edge-triggered strategy will fire for it this cycle) vs earlier (the
    transition was missed by a late/failed cycle and needs catch-up)."""
    from bot.strategy import check_crossover
    try:
        signal, *_ = check_crossover(df, cfg.sma_fast, cfg.sma_slow)
    except Exception:
        return False
    return signal is not None


def _catchup_signal(holding, relation, prev_relation, fresh_cross):
    """Reconciliation for a cross missed by late/failed cycles.

    Derived from the CURRENT fast-vs-slow relation vs the last SUCCESSFULLY
    PROCESSED one — level-triggered, so a cross visible for a single 15-min
    cycle can no longer be silently skipped forever.
    :return: 'SELL' (missed death cross while holding — retries until the
             exit executes), 'BUY' (missed golden cross while flat — exactly
             one catch-up shot), or None
    """
    if fresh_cross or relation is None or prev_relation is None or relation == prev_relation:
        return None
    if relation == "below" and prev_relation == "above" and holding:
        return "SELL"
    if relation == "above" and prev_relation == "below" and not holding:
        return "BUY"
    return None


def run_trading_cycle():
    """Run one trading cycle for all symbols across all registered strategies."""
    print(f"[{datetime.now()}] Starting trading cycle...")

    # Initialize components
    try:
        broker = make_broker(config)
        journal = TradeJournal()
    except Exception as e:
        print(f"[{datetime.now()}] Failed to initialize components: {e}")
        return

    strategies = get_strategies(getattr(config, "active_strategies", ["sma_cross"]))
    if not strategies:
        print(f"[{datetime.now()}] No strategies registered — nothing to do")
        return

    risk_engine = RiskEngine(config, broker, journal)

    # Set up timeframe for data fetching
    timeframe = make_timeframe(config.timeframe)

    # Fetch bars once per symbol; strategies share them
    bars_by_symbol = {}
    for symbol in config.symbols:
        try:
            df = broker.get_crypto_bars(symbol, timeframe, config.lookback_bars)
            if df is None or df.empty:
                print(f"[{datetime.now()}] No data for {symbol}")
                continue
            # Drop the still-forming (unclosed) bar: signals must use closed bars only
            df = df.iloc[:-1]
            bars_by_symbol[symbol] = df
        except BrokerError as e:
            print(f"[{datetime.now()}] Broker error fetching {symbol}: {e}")
        except Exception as e:
            print(f"[{datetime.now()}] Unexpected fetch error for {symbol}: {e}")

    if risk_engine.daily_loss_hit():
        print(f"[{datetime.now()}] Daily loss limit hit")
        if (getattr(config, "risk", None) or {}).get("flatten_on_daily_loss", False):
            try:
                held_symbols = {p.symbol for p in broker.get_all_positions()}
            except Exception as e:
                print(f"[{datetime.now()}] Could not enumerate positions for flatten: {e}")
                held_symbols = set(bars_by_symbol)
            # map broker symbols (ETHUSD) back to our format (ETH/USD)
            slash_map = {s.replace("/", ""): s for s in config.symbols}
            for sym in held_symbols:
                symbol = slash_map.get(sym, sym)
                df = bars_by_symbol.get(symbol)
                if df is None:
                    df = pd.DataFrame({"close": [0.0]})
                _execute_signal(broker, journal, risk_engine, symbol, df, {
                    "action": "SELL",
                    "reasoning": "account-level daily loss limit: flattening paper exposure",
                })
        send_heartbeat(broker, journal)
        return

    position_states = _load_position_states(journal)

    for symbol, df in bars_by_symbol.items():
        symbol_cycle_ok = True
        relation = None
        try:
            print(f"[{datetime.now()}] Processing {symbol}...")

            # ---- persistent SMA relation: catch crosses missed by late/failed
            # cycles. A death cross visible for exactly one 15-min bar is no
            # longer a single-cycle event: if the previous PROCESSED state was
            # 'above' and we are now 'below', that transition happened while
            # we were blind, and the exit must fire now. Exits retry every
            # cycle until executed; entries get exactly one catch-up shot.
            relation = _sma_relation(df, config)
            prev_relation = position_states.get(symbol)
            fresh_cross = _cross_on_current_edge(df, config)

            position = broker.get_position(symbol)
            if position:
                # self-heal: a position without a recorded stop (e.g. seeded
                # after a re-seed wiped the ledger) gets one NOW, from the
                # current bars' ATR or the fallback pct
                healed, stop_price = risk_engine.ensure_stop(
                    symbol, getattr(position, "avg_entry_price", 0),
                    _atr(df, int((getattr(config, "risk", None) or {}).get("atr_period", 14))))
                if healed:
                    print(f"[{datetime.now()}] Healed missing stop for {symbol}: "
                          f"${stop_price:.2f} (entry ${float(position.avg_entry_price):.2f})")
                    try:
                        send_notification(
                            f"🩹 **Stop healed — {symbol}**\n"
                            f"Position had no recorded stop (ledger lost in a re-seed). "
                            f"Recorded stop ${stop_price:.2f} vs entry "
                            f"${float(position.avg_entry_price):,.2f}.",
                            config)
                    except Exception:
                        pass
                # check the entry-fixed stop against the latest closed bar
                # (live broker mark as fallback if bars are stale/unusable)
                mark = float(df["close"].iloc[-1])
                if mark <= 0:
                    mark = float(getattr(position, "current_price", 0) or 0)
                triggered, reason = risk_engine.stop_triggered(symbol, mark)
                if not triggered and mark <= 0:
                    # recorded stop exists but no usable mark: fall back to the
                    # legacy current-ATR check rather than skipping entirely
                    atr = _atr(df, int((getattr(config, "risk", None) or {}).get("atr_period", 14)))
                    triggered, reason = risk_engine.atr_stop_triggered(
                        float(position.avg_entry_price), mark, atr
                    )
                if triggered:
                    _execute_signal(broker, journal, risk_engine, symbol, df, {
                        "action": "SELL", "reasoning": reason,
                    })
                    symbol_cycle_ok = False  # state not advanced; re-check next cycle
                    continue
                holding = True
                catchup = _catchup_signal(holding, relation, prev_relation, fresh_cross)
                if catchup == "SELL":
                    # missed death cross: exit retries until it executes
                    executed = _execute_signal(broker, journal, risk_engine, symbol, df, {
                        "action": "SELL",
                        "reasoning": (f"[sma_cross] missed death cross catch-up: SMA relation was "
                                      f"'{prev_relation}' at last processed cycle, now '{relation}'"),
                    })
                    if not executed:
                        symbol_cycle_ok = False
            else:
                holding = False
                catchup = _catchup_signal(holding, relation, prev_relation, fresh_cross)
                if catchup == "BUY":
                    # missed golden cross while flat: one catch-up entry shot
                    # (the transition is consumed either way — entries never
                    # retry-spam; a fresh cross is left to the strategy itself)
                    _execute_signal(broker, journal, risk_engine, symbol, df, {
                        "action": "BUY",
                        "reasoning": (f"[sma_cross] missed golden cross catch-up: SMA relation was "
                                      f"'{prev_relation}' at last processed cycle, now '{relation}'"),
                    })

            for strat_name, strat_fn in strategies:
                try:
                    signals = strat_fn(symbol, df, config)
                except Exception as e:
                    print(f"[{datetime.now()}] Strategy {strat_name} error on {symbol}: {e}")
                    symbol_cycle_ok = False
                    continue
                for sig in signals:
                    executed = _execute_signal(broker, journal, risk_engine, symbol, df, sig)
                    if not executed and sig.get("action") == "SELL":
                        # exit did not execute while a position is held:
                        # do NOT advance state — the transition must retry
                        symbol_cycle_ok = False
                    # a failed/blocked BUY consumes the transition by design:
                    # entries are one-shot and never retry-spam
        except BrokerError as e:
            print(f"[{datetime.now()}] Broker error for {symbol}: {e}")
            symbol_cycle_ok = False
        except Exception as e:
            print(f"[{datetime.now()}] Unexpected error for {symbol}: {e}")
            symbol_cycle_ok = False
        finally:
            # state advances ONLY on a fully successful cycle for this symbol:
            # a late/failed cycle leaves the old state so its transitions are
            # caught on the next successful one
            if symbol_cycle_ok:
                if relation is not None:
                    position_states[symbol] = relation
                _save_position_states(journal, position_states)

    # stops protect positions even when their bar feed failed this cycle
    try:
        slash_map = {s.replace("/", ""): s for s in config.symbols}
        held_symbols = {slash_map.get(p.symbol, p.symbol)
                        for p in broker.get_all_positions()}
        for symbol in held_symbols - set(bars_by_symbol):
            position = broker.get_position(symbol)
            if not position:
                continue
            mark = float(getattr(position, "current_price", 0) or 0)
            # barless positions can't compute ATR here; the fallback pct stop
            # guarantees they're not stop-less until bars return
            healed, stop_price = risk_engine.ensure_stop(
                symbol, getattr(position, "avg_entry_price", 0), None)
            if healed:
                print(f"[{datetime.now()}] Healed missing stop for {symbol} (no bars): "
                      f"${stop_price:.2f}")
                try:
                    send_notification(
                        f"🩹 **Stop healed — {symbol}**\n"
                        f"Recorded fallback stop ${stop_price:.2f} vs entry "
                        f"${float(position.avg_entry_price):,.2f} (bar feed unavailable).",
                        config)
                except Exception:
                    pass
            if mark <= 0:
                continue
            triggered, reason = risk_engine.stop_triggered(symbol, mark)
            if triggered:
                _execute_signal(broker, journal, risk_engine, symbol, None, {
                    "action": "SELL", "reasoning": reason,
                })
        # ledger stops for symbols no longer held are dead weight (e.g. a
        # re-seed wiped positions): drop them so they can never mis-fire
        stale = risk_engine.prune_stale_stops(held_symbols)
        if stale:
            print(f"[{datetime.now()}] Pruned stale stops: {stale}")
    except Exception as e:
        print(f"[{datetime.now()}] Stop sweep for barless positions failed: {e}")

    # Hourly status heartbeat (no-op if less than an hour since last one)
    send_heartbeat(broker, journal)

    print(f"[{datetime.now()}] Trading cycle completed.")


def _execute_signal(broker, journal, risk_engine, symbol, df, sig):
    """Validate a signal through risk and execute it with full Discord trail.

    :return: True if a fill was confirmed (or nothing needed doing),
             False when the signal did NOT execute (blocked, failed, zero qty)
    """
    action = sig["action"]
    try:
        price = float(df['close'].iloc[-1])
    except Exception:
        price = 0.0

    position = broker.get_position(symbol)
    current_qty = float(position.qty) if position else 0.0
    if position and price <= 0:
        # no usable bar data (e.g. flatten without bars): fall back to broker marks
        try:
            price = float(getattr(position, "current_price", 0) or 0)
        except Exception:
            price = 0.0

    qty = 0.0
    if action == "BUY" and current_qty == 0:
        try:
            acct = broker.get_account()
            available = float(acct.cash)
        except Exception:
            available = config.notional
        atr = _atr(df, int((getattr(config, "risk", None) or {}).get("atr_period", 14)))
        qty = risk_engine.size_for_atr(price, atr, available, config.notional)
        if qty <= 0 or price <= 0:
            # risk sizing clipped the order to nothing (or no usable price):
            # never submit an empty order to the broker
            print(f"[{datetime.now()}] BUY {symbol} sized to zero (price={price}, "
                  f"cash={available}); skipping")
            return False
    elif action == "SELL" and current_qty > 0:
        qty = current_qty
    else:
        print(f"[{datetime.now()}] {action} signal for {symbol} but no action needed (qty={current_qty})")
        return True  # desired end state already holds; nothing failed

    if qty > 0 and action == "BUY":
        try:
            positions = list(broker.get_all_positions())
            open_count = len(positions)
            crypto_notional = sum(abs(float(getattr(p, "market_value", 0) or 0)) for p in positions)
            account_equity = float(broker.get_account().equity)
        except Exception:
            open_count = 0
            crypto_notional = 0.0
            account_equity = None
        allowed, reason = risk_engine.check(symbol, action, qty, price, open_count,
                                            current_crypto_notional=crypto_notional,
                                            account_equity=account_equity)
        if not allowed:
            print(f"[{datetime.now()}] RISK BLOCKED {action} {symbol}: {reason}")
            try:
                send_notification(
                    f"🛑 **Risk blocked {action} — {symbol}**\nReason: {reason}",
                    config
                )
            except Exception:
                pass
            return False

    print(f"[{datetime.now()}] Placing {action} order for {symbol}: qty={qty:.6f}, price={price:.2f}")
    reasoning = sig.get("reasoning", action)
    try:
        send_notification(
            f"🔔 **Trade signal — {symbol}**\n"
            f"**{action}** {qty:.6f} @ ~${price:,.2f} (notional ~${qty * price:,.2f})\n"
            f"Reason: {reasoning}\n"
            f"Submitting order to broker...",
            config
        )
    except Exception as notify_err:
        print(f"[{datetime.now()}] Pre-trade Discord alert failed (continuing trade): {notify_err}")
    try:
        order = broker.place_order(symbol, qty, action.upper())
        confirmed = broker.await_terminal_order(order.id)
    except BrokerError as e:
        print(f"[{datetime.now()}] Order submission/confirmation failed for {symbol}: {e}")
        return False
    raw_status = getattr(confirmed, "status", "")
    status = str(getattr(raw_status, "value", raw_status)).lower()
    if status == "filled":
        fill_qty = float(getattr(confirmed, "filled_qty", None) or qty)
        fill_price = float(getattr(confirmed, "filled_avg_price", None) or price)
        fee_pct = float((getattr(config, "execution", None) or {}).get("taker_fee_pct", 0)) / 100.0
        estimated_fee = fill_qty * fill_price * fee_pct
        journal.log_trade(
            timestamp=datetime.now().isoformat(), symbol=symbol, action=action,
            qty=fill_qty, price=fill_price, reasoning=reasoning, fee=estimated_fee,
            order_id=str(getattr(order, "id", "")), status="filled"
        )
        if action == "BUY":
            # fix the stop at entry: level decided now, never moved afterwards
            entry_atr = _atr(df, int((getattr(config, "risk", None) or {}).get("atr_period", 14))) if df is not None else None
            stop_price = risk_engine.entry_fixed_stop(symbol, fill_price, entry_atr)
            risk_engine.record_stop(symbol, fill_price, stop_price)
            print(f"[{datetime.now()}] Stop recorded for {symbol}: ${stop_price:.2f} (entry ${fill_price:.2f})")
        elif action == "SELL":
            # keep the stop if a partial fill leaves a residual position
            try:
                remaining = broker.get_position(symbol)
            except Exception:
                remaining = None
            if remaining is None or float(getattr(remaining, "qty", 0) or 0) <= 0:
                risk_engine.clear_stop(symbol)
        print(f"[{datetime.now()}] Confirmed paper fill for {symbol}: {action} {fill_qty:.6f} @ {fill_price:.2f}")
        try:
            send_notification(
                f"✅ **Trade executed — {symbol}**\n"
                f"**{action}** {fill_qty:.6f} @ ${fill_price:,.2f}\n"
                f"Order ID: {getattr(order, 'id', 'unknown')}\n"
                f"Reason: {reasoning}",
                config
            )
        except Exception as notify_err:
            print(f"[{datetime.now()}] Post-trade Discord alert failed: {notify_err}")
        return True
    else:
        # journal non-fills so the audit trail matches the broker's order history
        filled_qty = float(getattr(confirmed, "filled_qty", None) or 0)
        journal.log_trade(
            timestamp=datetime.now().isoformat(), symbol=symbol, action=action,
            qty=filled_qty, price=float(getattr(confirmed, "filled_avg_price", None) or 0.0),
            reasoning=f"{reasoning} [order ended as {status or 'pending'}; no fill]",
            fee=0.0, order_id=str(getattr(order, "id", "")),
            status=status or "pending",
        )
        print(f"[{datetime.now()}] Order {getattr(order, 'id', 'unknown')} ended as {status or 'pending'}; journaled as non-fill")
        return False


def _atr(df, period):
    """Compute ATR from closed bars; return None when the data is insufficient."""
    if df is None or period <= 0 or len(df) < period + 1:
        return None
    close = df["close"]
    high = df["high"] if "high" in df else close
    low = df["low"] if "low" in df else close
    ranges = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    value = ranges.rolling(period).mean().iloc[-1]
    return float(value) if value == value and value > 0 else None


def run_agent_cycle():
    """One AI agent cycle (babysitter + scout) — shadow mode by default."""
    from bot.agent import TradingAgent
    broker = make_broker(config)
    journal = TradeJournal()
    agent = TradingAgent(config, broker, journal=journal)
    agent.run_cycle()


def run_scanner_cycle():
    """One Polymarket scanner cycle (read-only API, paper bets)."""
    from bot.polymarket import scan, settle_open_bets
    from bot.wallet import BettingWallet
    journal = TradeJournal()
    try:
        settle_open_bets(config, journal=journal)
    except Exception as e:
        print(f"[{datetime.now()}] Bet settlement failed: {e}")
    scan(config, journal=journal)
    try:
        wallet = BettingWallet(config, journal=journal)
        wallet.snapshot()
        send_notification(f"💵 {wallet.status_line()}", config)
    except Exception as e:
        print(f"[{datetime.now()}] Wallet snapshot failed: {e}")


def main():
    """Main entry point: run the trading cycle every 15 minutes."""
    # Run immediately on start
    run_trading_cycle()

    # Schedule to run every 15 minutes
    schedule.every(15).minutes.do(run_trading_cycle)

    print("Scheduler started. Press Ctrl+C to exit.")
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nExiting...")


if __name__ == "__main__":
    main()
