import time
import schedule
from datetime import datetime
from bot.config import config
from bot.broker import AlpacaBroker
from bot.strategies import get_strategies
from bot.risk import RiskEngine
from bot.journal import TradeJournal
from bot.errors import BrokerError
from bot.notify import send_notification
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
import time as _time

HEARTBEAT_INTERVAL_SECONDS = 3600


def send_heartbeat(broker, journal):
    """Send a status message to Discord once per UTC clock hour."""
    now = _time.time()
    current_hour = datetime.utcfromtimestamp(now).strftime("%Y-%m-%dT%H")
    last = journal.get_meta("last_heartbeat_hour")
    if last is not None and last == current_hour:
        return
    try:
        acct = broker.trading_client.get_account()
        positions = list(broker.trading_client.get_all_positions())
        pos_lines = [f"  • {p.symbol}: {float(p.qty):.6f} (${float(p.unrealized_pl):,.2f} unrealized)" for p in positions]
        pos_section = "\n".join(pos_lines) if pos_lines else "flat (no open positions)"

        # SMA gaps per symbol for signal proximity
        tf = TimeFrame(15, TimeFrameUnit.Minute)
        gap_lines = []
        for sym in config.symbols:
            try:
                df = broker.get_crypto_bars(sym, tf, config.lookback_bars)
                close = df['close']
                f = close.rolling(20).mean().iloc[-1]
                s = close.rolling(50).mean().iloc[-1]
                gap_lines.append(f"  • {sym}: SMA20 {f:,.2f} vs SMA50 {s:,.2f} ({(f-s)/s*100:+.2f}%)")
            except Exception:
                gap_lines.append(f"  • {sym}: data unavailable")

        msg = (
            f"🫀 **Heartbeat** {datetime.now().strftime('%H:%M UTC')} (paper)\n"
            f"Equity ${float(acct.equity):,.2f} | Cash ${float(acct.cash):,.2f}\n"
            f"Positions: {pos_section}\n"
            f"Signal gaps (SMA20−SMA50):\n" + "\n".join(gap_lines)
        )
        send_notification(msg, config)
        journal.set_meta("last_heartbeat_hour", current_hour)
        print(f"[{datetime.now()}] Heartbeat sent (hour {current_hour})")
    except Exception as e:
        print(f"[{datetime.now()}] Heartbeat failed (will retry next cycle): {e}")


def run_trading_cycle():
    """Run one trading cycle for all symbols across all registered strategies."""
    print(f"[{datetime.now()}] Starting trading cycle...")

    # Initialize components
    try:
        broker = AlpacaBroker(config.alpaca_api_key_id, config.alpaca_api_secret_key)
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
    timeframe = TimeFrame(int(config.timeframe.replace('Min', '')), TimeFrameUnit.Minute)

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

    for symbol, df in bars_by_symbol.items():
        try:
            print(f"[{datetime.now()}] Processing {symbol}...")

            for strat_name, strat_fn in strategies:
                try:
                    signals = strat_fn(symbol, df, config)
                except Exception as e:
                    print(f"[{datetime.now()}] Strategy {strat_name} error on {symbol}: {e}")
                    continue
                for sig in signals:
                    _execute_signal(broker, journal, risk_engine, symbol, df, sig)
        except BrokerError as e:
            print(f"[{datetime.now()}] Broker error for {symbol}: {e}")
            continue
        except Exception as e:
            print(f"[{datetime.now()}] Unexpected error for {symbol}: {e}")
            continue

    # Hourly status heartbeat (no-op if less than an hour since last one)
    send_heartbeat(broker, journal)

    print(f"[{datetime.now()}] Trading cycle completed.")


def _execute_signal(broker, journal, risk_engine, symbol, df, sig):
    """Validate a signal through risk and execute it with full Discord trail."""
    action = sig["action"]
    price = df['close'].iloc[-1]

    position = broker.get_position(symbol)
    current_qty = float(position.qty) if position else 0.0

    qty = 0.0
    if action == "BUY" and current_qty == 0:
        try:
            acct = broker.trading_client.get_account()
            available = float(acct.cash)
        except Exception:
            available = config.notional
        # size to the smaller of configured notional or ~98% of available cash
        notional = min(config.notional, available * 0.98)
        qty = notional / price
    elif action == "SELL" and current_qty > 0:
        qty = current_qty
    else:
        print(f"[{datetime.now()}] {action} signal for {symbol} but no action needed (qty={current_qty})")
        return

    if qty > 0 and action == "BUY":
        try:
            open_count = len(list(broker.trading_client.get_all_positions()))
        except Exception:
            open_count = 0
        allowed, reason = risk_engine.check(symbol, action, qty, price, open_count)
        if not allowed:
            print(f"[{datetime.now()}] RISK BLOCKED {action} {symbol}: {reason}")
            try:
                send_notification(
                    f"🛑 **Risk blocked {action} — {symbol}**\nReason: {reason}",
                    config
                )
            except Exception:
                pass
            return

    print(f"[{datetime.now()}] Placing {action} order for {symbol}: qty={qty:.6f}, price={price:.2f}")
    reasoning = sig.get("reasoning", action)
    try:
        send_notification(
            f"🔔 **Trade signal — {symbol}**\n"
            f"**{action}** {qty:.6f} @ ~${price:,.2f} (notional ${config.notional})\n"
            f"Reason: {reasoning}\n"
            f"Submitting order to Alpaca paper...",
            config
        )
    except Exception as notify_err:
        print(f"[{datetime.now()}] Pre-trade Discord alert failed (continuing trade): {notify_err}")
    order = broker.place_order(symbol, qty, action.upper())
    journal.log_trade(
        timestamp=datetime.now().isoformat(),
        symbol=symbol,
        action=action,
        qty=qty,
        price=price,
        reasoning=reasoning
    )
    print(f"[{datetime.now()}] Trade logged for {symbol}: {action} {qty:.6f} @ {price:.2f}")
    try:
        send_notification(
            f"✅ **Trade executed — {symbol}**\n"
            f"**{action}** {qty:.6f} @ ~${price:,.2f}\n"
            f"Order ID: {getattr(order, 'id', 'unknown')}\n"
            f"Reason: {reasoning}",
            config
        )
    except Exception as notify_err:
        print(f"[{datetime.now()}] Post-trade Discord alert failed: {notify_err}")


def run_agent_cycle():
    """One AI agent cycle (babysitter + scout) — shadow mode by default."""
    from bot.agent import TradingAgent
    broker = AlpacaBroker(config.alpaca_api_key_id, config.alpaca_api_secret_key)
    journal = TradeJournal()
    agent = TradingAgent(config, broker, journal=journal)
    agent.run_cycle()


def run_scanner_cycle():
    """One Polymarket scanner cycle (read-only API, paper bets)."""
    from bot.polymarket import scan, settle_open_bets
    journal = TradeJournal()
    try:
        settle_open_bets(config, journal=journal)
    except Exception as e:
        print(f"[{datetime.now()}] Bet settlement failed: {e}")
    scan(config, journal=journal)


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
