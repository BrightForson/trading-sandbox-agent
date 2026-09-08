"""Tier 3 betting wallet: a virtual $60 ledger mirroring Polymarket paper bets.

Mirrors the Tier 2 shadow-account idea at the user's real deployment size
($60, $12 flat stake): every paper bet the scanner logs is re-staked on this
wallet at a flat stake, settled wins pay stake/price, losses return nothing,
and the resulting equity trend shows whether the tier earns its place at
real-money scale.

The wallet holds no state of its own. It replays the journal's bets table
in placement order every time it is evaluated:

  - each bet is mirrored at the flat stake; if the wallet cannot fund the
    full stake at that point it sits the bet out (not mirrored at all)
  - open mirrored bets hold their stake as locked; equity = cash + locked
  - a settled win pays stake/price; a settled loss returns nothing

Settlements are applied at the bet's placement row, so a payout can
slightly overstate the cash available to bets placed before the real
settlement moment; at 6h scan cycles this is a second-order approximation
for a monitoring scoreboard.

Epochs: when the wallet busts (cash below one full stake with nothing
open), start_new_epoch() begins a fresh bankroll; only bets logged after
the epoch-start timestamp count toward the new epoch.
"""
from datetime import datetime, timezone

WALLET_EPOCH_META = "wallet_epoch"
_EPOCH_START_KEY = "wallet_epoch_started_{}"


class BettingWallet:
    def __init__(self, cfg, journal=None):
        self.cfg = cfg
        self.journal = journal or self._default_journal()
        scanner_cfg = getattr(cfg, "scanner", None) or {}
        self.start_cash = float(scanner_cfg.get("wallet_start_cash", 10))
        self.stake = float(scanner_cfg.get("wallet_stake", 2))
        self.epoch = self._load_epoch()

    def _default_journal(self):
        from bot.journal import TradeJournal
        return TradeJournal()

    def _load_epoch(self):
        v = self.journal.get_meta(WALLET_EPOCH_META)
        try:
            return max(1, int(v)) if v is not None else 1
        except (TypeError, ValueError):
            return 1

    def start_new_epoch(self):
        """Bust recovery: start a fresh bankroll. Returns the new epoch."""
        self.epoch = self._load_epoch() + 1
        self.journal.set_meta(WALLET_EPOCH_META, str(self.epoch))
        self.journal.set_meta(
            _EPOCH_START_KEY.format(self.epoch),
            datetime.now(timezone.utc).isoformat(),
        )
        return self.epoch

    def _epoch_start_timestamp(self):
        """None means all bets count (epoch 1, no reset has ever happened)."""
        if self.epoch <= 1:
            return None
        return self.journal.get_meta(_EPOCH_START_KEY.format(self.epoch))

    def valuation(self):
        """Replay this epoch's bets in placement order; return the wallet state."""
        cutoff = self._epoch_start_timestamp()
        cash = self.start_cash
        locked = 0.0
        wins = losses = open_bets = 0
        for b in self.journal.get_all_bets():
            if cutoff is not None and b[1] < cutoff:
                continue
            price = float(b[5] or 0)
            if not price > 0:
                continue
            if cash < self.stake:
                continue
            cash -= self.stake
            outcome = b[7]
            if outcome == "open":
                locked += self.stake
                open_bets += 1
            elif outcome == "won":
                wins += 1
                cash += self.stake / price
            else:
                losses += 1
        return {
            "cash": cash,
            "locked": locked,
            "equity": cash + locked,
            "open_bets": open_bets,
            "wins": wins,
            "losses": losses,
        }

    def is_bust(self):
        """Bust = cannot fund one stake and nothing open to recover."""
        v = self.valuation()
        return v["cash"] < self.stake and v["locked"] <= 0

    def is_all_in(self):
        """All-in = cannot fund one stake but open bets may still pay out."""
        v = self.valuation()
        return v["cash"] < self.stake and v["locked"] > 0

    def status_line(self):
        v = self.valuation()
        roi = (v["equity"] - self.start_cash) / self.start_cash * 100
        record = (f"{v['wins']}W-{v['losses']}L"
                  if (v["wins"] or v["losses"]) else "no settled bets")
        if v["cash"] < self.stake:
            state = "BUST" if v["locked"] <= 0 else "ALL-IN"
        else:
            state = "ACTIVE"
        return (
            f"Tier 3 wallet: ${v['equity']:.2f} ({roi:+.2f}% of ${self.start_cash:.0f} start) | "
            f"cash ${v['cash']:.2f} | ${v['locked']:.2f} locked in {v['open_bets']} open bet(s) | "
            f"record {record} | {state}"
        )

    def snapshot(self):
        """Log the current valuation to the snapshot history table."""
        v = self.valuation()
        self.journal.log_wallet_snapshot(
            timestamp=datetime.now(timezone.utc).isoformat(),
            epoch=self.epoch,
            cash=round(v["cash"], 6),
            locked=round(v["locked"], 6),
            equity=round(v["equity"], 6),
        )
        return v

    def trend_line(self, max_points=8):
        """Recent equity trajectory, e.g. '$10.00 -> $9.40 -> $11.20'."""
        rows = self.journal.get_wallet_snapshots(epoch=self.epoch, limit=max_points)
        if not rows:
            return f"${self.start_cash:.2f} (no history yet)"
        return " -> ".join(f"${r[5]:.2f}" for r in rows)
