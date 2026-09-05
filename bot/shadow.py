"""Shadow account: a virtual $20 ledger for AI proposals (money that doesn't exist).

The AI agent's proposals never touch the real Alpaca account (shadow mode).
This module simulates what WOULD have happened if they had been executed with
real dollars, on a dedicated virtual $20 account:

  - starts at $20.00 cash (config agent.shadow_start_cash, default 20)
  - scout BUY (confidence >= min_confidence) opens a virtual position at the
    current close, sized to the proposed notional, capped at max_per_position
    (default $10) and by remaining shadow cash
  - babysitter SELL closes the virtual position, realizes P&L
  - every agent cycle marks positions to market; equity = cash + position value
  - state persists in journal tables (shadow_trades) + meta (shadow_cash,
    shadow_positions JSON) so it survives across CI runs

Discord gets a one-line status per cycle; the daily report and chat can query
the same ledger.
"""
import json
from datetime import datetime, timezone

from bot.journal import TradeJournal


class ShadowAccount:
    def __init__(self, cfg, broker, journal=None):
        self.cfg = cfg
        self.broker = broker
        self.journal = journal or TradeJournal()
        self.agent_cfg = getattr(cfg, "agent", None) or {}
        self.start_cash = float(self.agent_cfg.get("shadow_start_cash", 20))
        self.max_per_position = float(self.agent_cfg.get("shadow_max_per_position", 10))
        self.symbols = list(cfg.symbols)

    # ---------------- state ----------------

    def _cash(self):
        v = self.journal.get_meta("shadow_cash")
        return float(v) if v is not None else self.start_cash

    def _positions(self):
        raw = self.journal.get_meta("shadow_positions")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _save(self, cash, positions):
        self.journal.set_meta("shadow_cash", str(round(cash, 6)))
        self.journal.set_meta("shadow_positions", json.dumps(positions))

    def _log_trade(self, symbol, action, qty, price, note=""):
        try:
            self.journal.log_trade(
                timestamp=datetime.now(timezone.utc).isoformat(),
                symbol=symbol,
                action=action,
                qty=qty,
                price=price,
                reasoning=f"[shadow-account] {note}",
            )
        except Exception as e:
            print(f"[shadow] failed to log virtual trade: {e}")

    # ---------------- prices ----------------

    def _last_close(self, symbol):
        try:
            from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
            tf = TimeFrame(int(self.cfg.timeframe.replace("Min", "")), TimeFrameUnit.Minute)
            df = self.broker.get_crypto_bars(symbol, tf, self.cfg.lookback_bars)
            if df is None or df.empty:
                return None
            return float(df["close"].iloc[-1])
        except Exception as e:
            print(f"[shadow] price fetch failed for {symbol}: {e}")
            return None

    # ---------------- actions ----------------

    def take_buy(self, symbol, requested_notional, rationale=""):
        """Simulate executing a scout BUY. Returns (ok, note)."""
        if symbol not in self.symbols:
            return False, f"{symbol} not tradable on shadow account"
        if symbol in self._positions():
            return False, f"already holding {symbol}"
        price = self._last_close(symbol)
        if price is None:
            return False, f"no price for {symbol}"
        cash = self._cash()
        notional = min(float(requested_notional or 0), self.max_per_position, cash)
        if notional <= 0.5:
            return False, f"insufficient shadow cash (${cash:.2f})"
        qty = notional / price
        positions = self._positions()
        positions[symbol] = {"qty": qty, "entry": price, "opened": datetime.now(timezone.utc).isoformat()}
        self._save(cash - notional, positions)
        self._log_trade(symbol, "BUY", qty, price, note=f"scout proposal: {rationale[:120]}")
        return True, f"shadow BUY {symbol}: ${notional:.2f} @ ${price:,.2f} (qty {qty:.6f})"

    def take_sell(self, symbol, rationale=""):
        """Simulate executing a babysitter SELL. Returns (ok, note)."""
        positions = self._positions()
        if symbol not in positions:
            return False, f"no shadow position in {symbol}"
        price = self._last_close(symbol)
        if price is None:
            return False, f"no price for {symbol}"
        pos = positions.pop(symbol)
        proceeds = pos["qty"] * price
        cost = pos["qty"] * pos["entry"]
        pnl = proceeds - cost
        cash = self._cash() + proceeds
        self._save(cash, positions)
        self._log_trade(symbol, "SELL", pos["qty"], price,
                        note=f"babysitter exit: {rationale[:120]} (pnl {pnl:+.2f})")
        return True, (f"shadow SELL {symbol} @ ${price:,.2f}: "
                      f"P&L {pnl:+.2f} ({pnl / cost * 100 if cost else 0:+.1f}%)")

    # ---------------- valuation ----------------

    def mark_to_market(self):
        """Return (equity, cash, positions_block). Marks positions to last close."""
        cash = self._cash()
        positions = self._positions()
        lines = []
        total_value = 0.0
        for symbol, pos in positions.items():
            price = self._last_close(symbol)
            if price is None:
                price = pos["entry"]
            value = pos["qty"] * price
            pnl = value - pos["qty"] * pos["entry"]
            total_value += value
            lines.append(f"{symbol}: {pos['qty']:.6f} @ ${price:,.2f} ({pnl:+.2f})")
        equity = cash + total_value
        pos_block = "; ".join(lines) if lines else "flat"
        return equity, cash, pos_block

    def status_line(self):
        equity, cash, pos_block = self.mark_to_market()
        total_return = (equity - self.start_cash) / self.start_cash * 100
        return (f"Shadow account: ${equity:.2f} ({total_return:+.2f}% of ${self.start_cash:.0f} start) | "
                f"cash ${cash:.2f} | {pos_block}")

    def realized_pnl(self):
        """Sum of realized P&L across closed shadow round trips (from the trade log)."""
        trades = [t for t in self.journal.get_trades() if "[shadow-account]" in (t[6] or "")]
        by_symbol = {}
        for t in sorted(trades, key=lambda x: x[1]):
            _, ts, symbol, action, qty, price, _ = t
            by_symbol.setdefault(symbol, []).append((action, qty, price))
        total = 0.0
        for symbol, legs in by_symbol.items():
            buy_queue = []
            for action, qty, price in legs:
                if action == "BUY":
                    buy_queue.append((qty, price))
                else:
                    remaining = qty
                    while remaining > 0 and buy_queue:
                        bq, bp = buy_queue[0]
                        if bq <= remaining:
                            total += (price - bp) * bq
                            remaining -= bq
                            buy_queue.pop(0)
                        else:
                            total += (price - bp) * remaining
                            buy_queue[0] = (bq - remaining, bp)
                            remaining = 0
        return total
