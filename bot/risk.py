"""Risk module: hard caps checked before every BUY order.

Rules (config.yaml -> risk):
  - kill switch (meta 'kill_switch'): blocks all BUYs until manually cleared
  - max_notional_per_trade: single-order cap
  - max_open_positions: concurrent position cap
  - daily_loss_limit_pct: if today's realized+unrealized loss exceeds this % of
    equity at last check, block BUYs for the rest of the UTC day
  - entry-fixed stops: every BUY records a stop level (entry - ATR multiple,
    or fallback_stop_pct below entry) in the journal; the level never moves
    afterwards and SELLs are triggered against it

SELLs are always allowed (exits reduce risk).
"""
import json
from datetime import datetime, timezone

from bot.journal import TradeJournal


class RiskEngine:
    def __init__(self, cfg, broker, journal=None):
        self.cfg = cfg
        self.broker = broker
        self.journal = journal or TradeJournal()
        self.risk = getattr(cfg, "risk", None) or {}

    def kill_switch_active(self):
        return self.journal.get_meta("kill_switch") == "on"

    def set_kill_switch(self, on, reason="manual"):
        self.journal.set_meta("kill_switch", "on" if on else "off")
        self.journal.set_meta("kill_switch_reason", reason if on else "")

    def _today_key(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _daily_loss_blocked(self):
        """True if today's equity drop exceeded the daily loss limit."""
        limit_pct = float(self.risk.get("daily_loss_limit_pct", 0) or 0)
        if limit_pct <= 0:
            return False
        today = self._today_key()
        # baseline equity recorded at first check of the day
        baseline_key = f"risk_baseline_equity_{today}"
        baseline = self.journal.get_meta(baseline_key)
        try:
            acct = self.broker.get_account()
            equity = float(acct.equity)
        except Exception:
            return False  # can't read account -> don't hard-block on infra failure
        if baseline is None:
            self.journal.set_meta(baseline_key, str(equity))
            self.journal.set_meta("risk_day", today)
            return False
        baseline_val = float(baseline)
        drop_pct = (baseline_val - equity) / baseline_val * 100.0
        return drop_pct >= limit_pct

    def daily_loss_hit(self):
        """Account-level loss check. May record the day's baseline equity on
        the first call of a UTC day (idempotent thereafter)."""
        return self._daily_loss_blocked()

    def size_for_atr(self, price, atr, cash, fallback_notional):
        """Return a long-only quantity bounded by risk, cash, and order caps."""
        if price <= 0:
            return 0.0
        max_notional = float(self.risk.get("max_notional_per_trade", 0) or 0)
        target_notional = min(float(fallback_notional), max_notional) if max_notional else float(fallback_notional)
        try:
            equity = float(self.broker.get_account().equity)
        except Exception:
            equity = float(cash)
        target_risk = equity * float(self.risk.get("target_risk_pct_per_trade", 0) or 0) / 100.0
        stop_distance = float(atr or 0) * float(self.risk.get("catastrophic_atr_multiple", 0) or 0)
        if target_risk > 0 and stop_distance > 0:
            target_notional = min(target_notional, target_risk / stop_distance * price)
        target_notional = min(target_notional, float(cash) * 0.98)
        return max(0.0, target_notional / price)

    def atr_stop_triggered(self, entry_price, current_price, atr):
        """A catastrophic stop is a guardrail, not an optimisation signal."""
        multiple = float(self.risk.get("catastrophic_atr_multiple", 0) or 0)
        if entry_price <= 0 or current_price <= 0 or atr is None or multiple <= 0:
            return False, "ATR stop unavailable"
        stop_price = float(entry_price) - float(atr) * multiple
        if current_price <= stop_price:
            return True, f"catastrophic ATR stop hit (${current_price:.2f} <= ${stop_price:.2f})"
        return False, "ATR stop intact"

    # ---------------- stop ledger (fixed at entry) ----------------

    def _stops_meta_key(self, symbol):
        return f"stop_{symbol.replace('/', '_')}"

    def record_stop(self, symbol, entry_price, stop_price):
        """Persist the stop level decided at entry; it never moves afterwards."""
        if entry_price <= 0 or stop_price <= 0:
            return
        stops = self._load_stops()
        stops[symbol] = {"entry_price": float(entry_price), "stop_price": float(stop_price)}
        self.journal.set_meta("open_stops", json.dumps(stops))

    def get_stop(self, symbol):
        """Return (entry_price, stop_price) recorded at entry, or (None, None)."""
        stop = self._load_stops().get(symbol)
        if not stop:
            return None, None
        return stop.get("entry_price"), stop.get("stop_price")

    def clear_stop(self, symbol):
        stops = self._load_stops()
        if symbol in stops:
            del stops[symbol]
            self.journal.set_meta("open_stops", json.dumps(stops))

    def _load_stops(self):
        raw = self.journal.get_meta("open_stops")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def entry_fixed_stop(self, symbol, entry_price, atr, current_price=None):
        """Compute the stop level to record at BUY time.

        Primary: entry - catastrophic_atr_multiple * entry-ATR (matches the
        assumption `size_for_atr` sized the position against).
        Fallback when ATR is unavailable: stop_pct below entry, so a stop
        always exists.
        :return: stop_price (float) — always > 0
        """
        multiple = float(self.risk.get("catastrophic_atr_multiple", 0) or 0)
        if atr and multiple > 0 and entry_price > 0:
            return max(1e-8, float(entry_price) - float(atr) * multiple)
        pct = float(self.risk.get("fallback_stop_pct", 5.0) or 0)
        return max(1e-8, float(entry_price) * (1 - max(0.0, pct) / 100.0))

    def stop_triggered(self, symbol, current_price):
        """Check the entry-fixed stop for an open position.

        :return: (triggered, reason)
        """
        entry_price, stop_price = self.get_stop(symbol)
        if stop_price is None:
            return False, "no stop recorded"
        if entry_price and float(entry_price) > 0 and current_price >= float(entry_price) * 50:
            # broker marks can glitch to absurd values; never stop out on bad data
            return False, "suspect mark; skipped stop check"
        if current_price <= float(stop_price):
            return True, (f"stop hit (${current_price:.2f} <= recorded stop "
                          f"${float(stop_price):.2f}, entry ${float(entry_price or 0):.2f})")
        return False, "stop intact"

    def check(self, symbol, action, qty, price, current_open_positions,
              current_crypto_notional=0.0, account_equity=None):
        """
        Validate an order against all risk rules.
        :return: (allowed, reason)
        """
        if action == "SELL":
            return True, "sell always allowed"

        if self.kill_switch_active():
            return False, "kill switch active"

        max_notional = float(self.risk.get("max_notional_per_trade", 0) or 0)
        notional = qty * price
        if max_notional > 0 and notional > max_notional + 1e-9:
            return False, f"notional ${notional:.2f} exceeds cap ${max_notional:.2f}"

        max_pos = int(self.risk.get("max_open_positions", 0) or 0)
        if max_pos > 0 and current_open_positions >= max_pos:
            return False, f"open positions {current_open_positions} >= cap {max_pos}"

        allocation_cap = float(self.risk.get("max_crypto_allocation_pct", 0) or 0)
        if allocation_cap > 0:
            if account_equity is None:
                try:
                    account_equity = float(self.broker.get_account().equity)
                except Exception:
                    # hard cap: if we can't verify equity, we can't verify the cap
                    return False, "cannot verify crypto allocation cap: account equity unreadable"
            if float(account_equity) <= 0:
                return False, "cannot verify crypto allocation cap: account equity unreadable"
            if (float(current_crypto_notional) + notional) / float(account_equity) * 100 > allocation_cap:
                return False, f"crypto allocation would exceed {allocation_cap:.1f}% of equity"

        if self._daily_loss_blocked():
            return False, "daily loss limit hit"

        return True, "ok"
