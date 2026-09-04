import time
import schedule
from datetime import datetime
from bot.config import config
from bot.broker import AlpacaBroker
from bot.strategy import check_crossover
from bot.journal import TradeJournal
from bot.errors import BrokerError
from bot.notify import send_notification
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
import time as _time

HEARTBEAT_INTERVAL_SECONDS = 3600

def send_heartbeat(broker, journal):
    """Send an hourly status message to Discord if an hour has passed since the last one."""
    now = _time.time()
    last = journal.get_meta("last_heartbeat")
    if last is not None and (now - float(last)) < HEARTBEAT_INTERVAL_SECONDS:
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
        journal.set_meta("last_heartbeat", str(now))
        print(f"[{datetime.now()}] Heartbeat sent")
    except Exception as e:
        print(f"[{datetime.now()}] Heartbeat failed (will retry next cycle): {e}")

def run_trading_cycle():
    """Run one trading cycle for all symbols."""
    print(f"[{datetime.now()}] Starting trading cycle...")
    
    # Initialize components
    try:
        broker = AlpacaBroker(config.alpaca_api_key_id, config.alpaca_api_secret_key)
        journal = TradeJournal()
    except Exception as e:
        print(f"[{datetime.now()}] Failed to initialize components: {e}")
        return
    
    # Set up timeframe for data fetching
    timeframe = TimeFrame(int(config.timeframe.replace('Min', '')), TimeFrameUnit.Minute)
    
    for symbol in config.symbols:
        try:
            print(f"[{datetime.now()}] Processing {symbol}...")
            
            # Fetch recent bars
            df = broker.get_crypto_bars(symbol, timeframe, config.lookback_bars)
            if df is None or df.empty:
                print(f"[{datetime.now()}] No data for {symbol}")
                continue
            
            # Check for crossover
            signal, prev_fast, prev_slow, curr_fast, curr_slow = check_crossover(
                df, config.sma_fast, config.sma_slow
            )
            
            if signal is None:
                print(f"[{datetime.now()}] No crossover signal for {symbol}")
                continue
            
            # Get current position
            position = broker.get_position(symbol)
            current_qty = float(position.qty) if position else 0.0
            
            # Determine action
            action = None
            qty = 0.0
            price = df['close'].iloc[-1]  # latest close price
            
            if signal == "golden" and current_qty == 0:
                # BUY if flat
                action = "BUY"
                # Calculate quantity based on notional
                notional = config.notional
                qty = notional / price
            elif signal == "death" and current_qty > 0:
                # SELL if holding
                action = "SELL"
                qty = current_qty  # sell entire position
            
            if action is None:
                print(f"[{datetime.now()}] Signal {signal} but no action needed for {symbol} (qty={current_qty})")
                continue
            
            # Place order
            print(f"[{datetime.now()}] Placing {action} order for {symbol}: qty={qty:.6f}, price={price:.2f}")
            reasoning = f"{signal} cross: SMA{config.sma_fast} {prev_fast:.2f} {'above' if signal == 'golden' else 'below'} SMA{config.sma_slow} {prev_slow:.2f} -> now {curr_fast:.2f} vs {curr_slow:.2f}"
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
            
        except BrokerError as e:
            print(f"[{datetime.now()}] Broker error for {symbol}: {e}")
            continue
        except Exception as e:
            print(f"[{datetime.now()}] Unexpected error for {symbol}: {e}")
            continue
    
    # Hourly status heartbeat (no-op if less than an hour since last one)
    send_heartbeat(broker, journal)
    
    print(f"[{datetime.now()}] Trading cycle completed.")

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