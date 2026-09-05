"""Risk module: hard caps checked before every BUY order.

Rules (config.yaml -> risk):
  - kill switch (meta 'kill_switch'): blocks all BUYs until manually cleared
  - max_notional_per_trade: single-order cap
  - max_open_positions: concurrent position cap
  - daily_loss_limit_pct: if today's realized+unrealized loss exceeds this % of
    equity at last check, block BUYs for the rest of the UTC day

SELLs are always allowed (exits reduce risk).
"""
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
            acct = self.broker.trading_client.get_account()
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

    def check(self, symbol, action, qty, price, current_open_positions):
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

        if self._daily_loss_blocked():
            return False, "daily loss limit hit"

        return True, "ok"
