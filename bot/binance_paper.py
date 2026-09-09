"""Binance paper broker: a local simulated account priced by real Binance data.

The execution half of the broker seam. Mirrors the proven shadow-account
and betting-wallet pattern: ledger state lives in journal meta
(paper_cash, paper_positions, paper_trades, paper_next_order_id) so it
survives across CI runs exactly like data/trades.db always has.

Fills: market orders fill at the latest closed kline close, plus adverse
slippage and minus the Binance spot taker fee (0.1% default; config
execution.taker_fee_pct models it). Exposes Alpaca-shaped account,
position and order objects so _execute_signal and every consumer
(risk, chat, report, agent, validation) work unchanged.
"""
import json
import time as _time
from datetime import datetime, timezone

from bot.binance_data import BinanceDataClient, interval_minutes, _interval_for
from bot.errors import BrokerError


class _Account:
    def __init__(self, cash, positions_value):
        self.cash = str(cash)
        self.equity = str(cash + positions_value)
        self.currency = "USD"


class _Position:
    def __init__(self, symbol, qty, entry, mark):
        self.symbol = symbol
        self.qty = str(qty)
        self.avg_entry_price = str(entry)
        self.current_price = str(mark)
        self.market_value = str(qty * mark)
        self.unrealized_pl = str(qty * (mark - entry))
        self.cost_basis = str(qty * entry)
        self.unrealized_plpc = str((mark - entry) / entry) if entry else "0"


class _Order:
    def __init__(self, order_id, symbol, qty, side, status, filled_qty,
                 filled_avg_price, submitted_at, fee=0.0):
        self.id = str(order_id)
        self.client_order_id = f"paper-{order_id}"
        self.symbol = symbol
        self.qty = str(qty)
        self.side = side
        self.status = status
        self.filled_qty = str(filled_qty)
        self.filled_avg_price = str(filled_avg_price)
        self.submitted_at = submitted_at
        self.created_at = submitted_at
        self.fee = float(fee)  # actual fee charged by this fill


class BinancePaperBroker:
    """Same surface as AlpacaBroker: bars, orders, account, positions.

    A paper SELL of a symbol not held is rejected like Alpaca would, and
    a BUY larger than cash is clipped to affordability, so the risk
    engine's cash checks behave identically.
    """

    def __init__(self, cfg, journal=None):
        from bot.journal import TradeJournal
        self.cfg = cfg
        self.journal = journal or TradeJournal()
        self.data = BinanceDataClient(cfg)
        exec_cfg = getattr(cfg, "execution", None) or {}
        broker_cfg = getattr(cfg, "broker", None) or {}
        self.taker_fee_pct = float(broker_cfg.get("taker_fee_pct",
                                  exec_cfg.get("taker_fee_pct", 0.1)))
        self.slippage_bps = float(broker_cfg.get("slippage_bps",
                                 exec_cfg.get("slippage_bps", 8)))
        paper_cfg = broker_cfg.get("paper", {}) if isinstance(broker_cfg.get("paper"), dict) else {}
        self.start_cash = float(paper_cfg.get("start_cash", 20))
        self.interval = _interval_for(getattr(cfg, "timeframe", "15Min"))

    # ---------------- state (journal meta) ----------------

    def _cash(self):
        v = self.journal.get_meta("paper_cash")
        return float(v) if v is not None else self.start_cash

    def _positions(self):
        raw = self.journal.get_meta("paper_positions")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _save(self, cash, positions, next_id):
        self.journal.set_meta("paper_cash", str(round(cash, 8)))
        self.journal.set_meta("paper_positions", json.dumps(positions))
        self.journal.set_meta("paper_next_order_id", str(next_id))

    def _next_order_id(self):
        v = self.journal.get_meta("paper_next_order_id")
        return int(v) if v is not None else 1

    def _mark(self, symbol):
        """Latest closed-kline close, plus a safety fallback to entry."""
        try:
            price = self.data.last_close(symbol, interval=self.interval, limit=2)
            if price and price > 0:
                return float(price)
        except Exception:
            pass
        pos = self._positions().get(symbol)
        if pos:
            return float(pos["entry"])
        return None

    # ---------------- data ----------------

    def get_crypto_bars(self, symbol, timeframe, limit):
        interval = _interval_for(timeframe)
        df = self.data.get_crypto_bars(symbol, interval, limit)
        if df is None or df.empty:
            raise BrokerError(f"No bars returned for {symbol}")
        return df

    def fetch_history(self, symbol, days, interval=None):
        """Deep history for backtests, straight from the Binance client."""
        return self.data.fetch_history(symbol, days, interval=interval or self.interval)

    # ---------------- account/positions (Alpaca shapes) ----------------

    def get_account(self):
        positions = self._positions()
        total = 0.0
        for sym, pos in positions.items():
            mark = self._mark(sym) or float(pos["entry"])
            total += float(pos["qty"]) * mark
        return _Account(self._cash(), total)

    def get_all_positions(self):
        out = []
        for sym, pos in sorted(self._positions().items()):
            mark = self._mark(sym) or float(pos["entry"])
            out.append(_Position(sym, float(pos["qty"]), float(pos["entry"]), mark))
        return out

    def get_position(self, symbol):
        pos = self._positions().get(symbol)
        if not pos:
            return None
        mark = self._mark(symbol) or float(pos["entry"])
        return _Position(symbol, float(pos["qty"]), float(pos["entry"]), mark)

    # ---------------- orders ----------------

    def _log_order(self, order):
        raw = self.journal.get_meta("paper_trades")
        try:
            log = json.loads(raw) if raw else []
        except Exception:
            log = []
        log.append({"id": int(order.id), "symbol": order.symbol, "qty": float(order.filled_qty),
                    "side": order.side, "status": order.status,
                    "filled_qty": float(order.filled_qty),
                    "filled_avg_price": float(order.filled_avg_price),
                    "fee": float(getattr(order, "fee", 0.0)),
                    "submitted_at": order.submitted_at})
        # bounded: keep the newest 500 entries (per-epoch fill log)
        log = log[-500:]
        self.journal.set_meta("paper_trades", json.dumps(log))

    def place_order(self, symbol, qty, side):
        side = str(side).upper()
        qty = float(qty)
        mark = self._mark(symbol)
        if mark is None:
            raise BrokerError(f"No price available for {symbol}")
        cash = self._cash()
        positions = self._positions()
        next_id = self._next_order_id()
        slip = self.slippage_bps / 10_000.0
        fee_rate = self.taker_fee_pct / 100.0
        now = datetime.now(timezone.utc).isoformat()

        if side == "BUY":
            if qty <= 0:
                raise BrokerError(f"Invalid BUY quantity {qty} for {symbol}")
            price = mark * (1 + slip)
            cost = qty * price
            fee = cost * fee_rate
            if cost + fee > cash:
                qty = max(0.0, cash / (price * (1 + fee_rate)))
                cost = qty * price
                fee = cost * fee_rate
                if qty <= 0 or cost + fee > cash + 1e-9:
                    raise BrokerError(f"Insufficient paper cash (${cash:.2f}) for {symbol}")
            if symbol in positions:
                held = positions[symbol]
                new_qty = float(held["qty"]) + qty
                new_entry = (float(held["entry"]) * float(held["qty"]) + price * qty) / new_qty
                positions[symbol] = {"qty": new_qty, "entry": new_entry}
            else:
                positions[symbol] = {"qty": qty, "entry": price}
            self._save(cash - cost - fee, positions, next_id + 1)
            order = _Order(next_id, symbol, qty, "BUY", "filled", qty, price, now, fee)
            self._log_order(order)
            return order

        if side == "SELL":
            held = positions.get(symbol)
            if not held or float(held["qty"]) <= 0:
                raise BrokerError(f"no paper position in {symbol} to sell")
            sell_qty = min(qty, float(held["qty"]))
            if sell_qty <= 0:
                raise BrokerError(f"Invalid SELL quantity {qty} for {symbol}")
            price = mark * (1 - slip)
            proceeds = sell_qty * price
            fee = proceeds * fee_rate
            remaining = float(held["qty"]) - sell_qty
            if remaining > 1e-12:
                positions[symbol] = {"qty": remaining, "entry": float(held["entry"])}
            else:
                del positions[symbol]
            self._save(cash + proceeds - fee, positions, next_id + 1)
            order = _Order(next_id, symbol, sell_qty, "SELL", "filled", sell_qty, price, now, fee)
            self._log_order(order)
            return order

        raise BrokerError(f"Unknown order side {side}")

    def await_terminal_order(self, order_id, timeout_seconds=15):
        """Paper fills are synchronous: look the order up in the local log."""
        raw = self.journal.get_meta("paper_trades")
        if raw:
            try:
                for o in json.loads(raw):
                    if str(o.get("id")) == str(order_id):
                        return _Order(o["id"], o["symbol"], o["qty"], o["side"],
                                      o["status"], o["filled_qty"],
                                      o["filled_avg_price"], o["submitted_at"],
                                      o.get("fee", 0.0))
            except Exception:
                pass
        raise BrokerError(f"Paper order {order_id} not found in local log")

    # ---------------- seeding ----------------

    @staticmethod
    def _stops_key(symbol):
        return f"stop_{symbol.replace('/', '_')}"

    def _clear_all_stops(self):
        """A (re-)seed replaces the position set: entry-fixed stops for the
        old set must not survive (they'd mis-fire against the new ledger)."""
        raw = self.journal.get_meta("open_stops")
        if not raw:
            return
        try:
            stops = json.loads(raw)
        except Exception:
            stops = {}
        for symbol in list(stops):
            del stops[symbol]
        self.journal.set_meta("open_stops", json.dumps(stops))

    def seed_from_alpaca(self, cash, positions):
        """One-time import of live Alpaca paper state into this ledger."""
        self._clear_all_stops()
        ledger = {sym: {"qty": float(p["qty"]), "entry": float(p["entry"])}
                  for sym, p in positions.items()}
        self._save(float(cash), ledger, self._next_order_id())
        self.journal.set_meta("paper_seeded_at", datetime.now(timezone.utc).isoformat())
        self.journal.set_meta("paper_epoch_start_cash", str(float(cash)))

    def seed_fresh(self, cash):
        self._clear_all_stops()
        self._save(float(cash), {}, self._next_order_id())
        self.journal.set_meta("paper_seeded_at", datetime.now(timezone.utc).isoformat())
        self.journal.set_meta("paper_epoch_start_cash", str(float(cash)))
