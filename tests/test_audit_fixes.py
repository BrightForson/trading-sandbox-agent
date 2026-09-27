"""Regression tests for the 2026-09-27 sandbox audit.

Each test here failed on the code as it stood before the corresponding fix.
Grouped by commit so the provenance stays traceable.

Commit 1 (gate correctness): Tier 1 drawdown must not mix ledgers (epoch
filter), the losing-week streak must use the right column indices, the
Tier 3 gate must read a win rate that bet_scorecard actually returns, and
a split round trip must be scored once rather than per partial sell.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.journal import TradeJournal


# ---------------- commit 1: Tier 1 drawdown must not mix ledgers ----------------

def test_tier1_drawdown_ignores_other_epochs(tmp_path):
    """Tier 3's separate $60 bankroll must not count as a Tier 1 drawdown.

    Both ledgers share wallet_snapshots; only epoch 0 is Tier 1. Before the
    fix the peak walked across a $100 account and a $60 one, reporting a
    40% drawdown on an account that never lost a cent -- a false KILL.
    """
    from bot.gates import _tier1_drawdown_pct
    j = TradeJournal(db_path=str(tmp_path / "d.db"))
    base = datetime.now(timezone.utc) - timedelta(hours=6)
    # Tier 1 (epoch 0): completely flat at $100
    for i in range(4):
        j.log_wallet_snapshot(
            timestamp=(base + timedelta(hours=i)).isoformat(),
            epoch=0, cash=100.0, locked=0, equity=100.0)
    # Tier 3 (epoch 1): a wholly separate $60 virtual wallet
    for i in range(4):
        j.log_wallet_snapshot(
            timestamp=(base + timedelta(hours=i)).isoformat(),
            epoch=1, cash=60.0, locked=0, equity=60.0)

    assert _tier1_drawdown_pct(j) == 0.0


def test_tier1_drawdown_still_sees_its_own_trough(tmp_path):
    """The epoch filter must not blind Tier 1 to its own real drawdown."""
    from bot.gates import _tier1_drawdown_pct
    j = TradeJournal(db_path=str(tmp_path / "d.db"))
    base = datetime.now(timezone.utc) - timedelta(hours=6)
    for i, eq in enumerate([100, 120, 90, 95]):
        j.log_wallet_snapshot(
            timestamp=(base + timedelta(hours=i)).isoformat(),
            epoch=0, cash=eq, locked=0, equity=eq)
    # peak 120 -> trough 90 = 25%
    assert abs(_tier1_drawdown_pct(j) - 25.0) < 0.01


# ---------------- commit 1: losing-week streak column indices ----------------

def test_tier1_losing_week_streak_counts_real_losing_weeks(tmp_path):
    """Three consecutive losing weeks must read as a streak of >= 2.

    The filter tested t[7] (the REAL column) for the string "filled", so
    every trade was dropped and the streak was permanently 0 -- a documented
    Tier 1 KILL criterion that could never fire. The P&L proxy also read
    t[8] (order_id, TEXT) as a float, which raised and was swallowed.
    """
    from bot.gates import _tier1_losing_week_streak
    j = TradeJournal(db_path=str(tmp_path / "s.db"))

    # three separate ISO weeks, each a losing round trip
    monday = datetime(2026, 9, 7, tzinfo=timezone.utc)
    for w, exit_price in enumerate([80.0, 70.0, 60.0]):
        day = monday + timedelta(weeks=w)
        j.log_trade(day.isoformat(), "BTC/USD", "BUY", 1.0, 100.0,
                    "golden cross", fee=0.1, order_id=f"buy-{w}", status="filled")
        j.log_trade((day + timedelta(hours=1)).isoformat(), "BTC/USD", "SELL",
                    1.0, exit_price, "death cross", fee=0.1,
                    order_id=f"sell-{w}", status="filled")

    assert _tier1_losing_week_streak(j) >= 2


def test_tier1_losing_week_streak_ignores_non_filled(tmp_path):
    """A cancelled fill must not count toward a losing week."""
    from bot.gates import _tier1_losing_week_streak
    j = TradeJournal(db_path=str(tmp_path / "s.db"))
    monday = datetime(2026, 9, 7, tzinfo=timezone.utc)
    j.log_trade(monday.isoformat(), "BTC/USD", "BUY", 1.0, 100.0,
                "cancelled entry", fee=0.0, order_id="x", status="canceled")
    assert _tier1_losing_week_streak(j) == 0


def test_tier1_losing_week_streak_breaks_on_a_winning_week(tmp_path):
    """A profitable week must terminate the streak."""
    from bot.gates import _tier1_losing_week_streak
    j = TradeJournal(db_path=str(tmp_path / "s.db"))
    monday = datetime(2026, 9, 7, tzinfo=timezone.utc)
    for w, (entry, exit_) in enumerate([(100.0, 80.0), (100.0, 150.0)]):
        day = monday + timedelta(weeks=w)
        j.log_trade(day.isoformat(), "BTC/USD", "BUY", 1.0, entry, "buy",
                    fee=0.0, order_id="b", status="filled")
        j.log_trade((day + timedelta(hours=1)).isoformat(), "BTC/USD", "SELL",
                    1.0, exit_, "sell", fee=0.0, order_id="s", status="filled")
    # most recent week is the winner -> streak of 0
    assert _tier1_losing_week_streak(j) == 0


def test_deploying_capital_is_not_a_losing_week(tmp_path):
    """A week that only OPENED positions has realized nothing, so it is not a
    losing week.

    The old proxy netted BUY cash against SELL cash per week, which measured
    capital deployed rather than money lost. Once the column-index bug was
    fixed that proxy began firing the 2-losing-weeks KILL on a strategy that
    had merely bought a position and left it open.
    """
    from bot.gates import _tier1_losing_week_streak
    j = TradeJournal(db_path=str(tmp_path / "s.db"))
    monday = datetime(2026, 9, 7, tzinfo=timezone.utc)
    # week 1: buy and hold, never sold -> nothing realized
    j.log_trade(monday.isoformat(), "BTC/USD", "BUY", 1.0, 100.0,
                "golden cross", fee=0.0, order_id="b", status="filled")
    # week 2: a small winning round trip -> realized positive
    day2 = monday + timedelta(weeks=1)
    j.log_trade(day2.isoformat(), "ETH/USD", "BUY", 1.0, 100.0,
                "golden cross", fee=0.0, order_id="b2", status="filled")
    j.log_trade((day2 + timedelta(hours=1)).isoformat(), "ETH/USD", "SELL",
                1.0, 120.0, "death cross", fee=0.0, order_id="s2", status="filled")

    assert _tier1_losing_week_streak(j) == 0


def test_week_with_only_an_open_buy_is_not_counted_at_all(tmp_path):
    """A buy that never closes must not appear in the weekly ledger."""
    from bot.gates import _tier1_weekly_realized_pnl
    j = TradeJournal(db_path=str(tmp_path / "s.db"))
    monday = datetime(2026, 9, 7, tzinfo=timezone.utc)
    j.log_trade(monday.isoformat(), "BTC/USD", "BUY", 1.0, 100.0,
                "golden cross", fee=0.0, order_id="b", status="filled")
    assert _tier1_weekly_realized_pnl(j.get_trades()) == {}


def test_round_trip_credited_to_its_closing_week(tmp_path):
    """A position opened in one week and closed in the next is credited to
    the week it closed, not the week the cash went out."""
    from bot.gates import _tier1_weekly_realized_pnl
    j = TradeJournal(db_path=str(tmp_path / "s.db"))
    monday = datetime(2026, 9, 7, tzinfo=timezone.utc)
    j.log_trade(monday.isoformat(), "BTC/USD", "BUY", 1.0, 100.0,
                "golden cross", fee=0.0, order_id="b", status="filled")
    close_day = monday + timedelta(weeks=1)
    j.log_trade(close_day.isoformat(), "BTC/USD", "SELL", 1.0, 150.0,
                "death cross", fee=0.0, order_id="s", status="filled")

    weekly = _tier1_weekly_realized_pnl(j.get_trades())
    close_key = f"{close_day.isocalendar()[0]}-W{close_day.isocalendar()[1]:02d}"
    assert weekly == {close_key: 50.0}


# ---------------- commit 1: Tier 3 gate win rate ----------------

def test_tier3_gate_reports_real_win_rate(tmp_path):
    """2 wins and 1 loss is a 66.7% win rate, not 0%.

    The gate read score["wins"], a key bet_scorecard never returns, and the
    fallback dict seeded "wins": 0 so the bug looked intentional. That made
    the win rate a hard 0% forever, which drives the >=10-settled-bet KILL
    criterion to fire regardless of the real record.
    """
    from bot.gates import tier3_gate
    j = TradeJournal(db_path=str(tmp_path / "b.db"))
    base = datetime.now(timezone.utc) - timedelta(days=3)
    outcomes = [("won", 60.0), ("won", 50.0), ("lost", 0.0)]
    for i, (outcome, payout) in enumerate(outcomes):
        bid = j.log_bet(timestamp=(base + timedelta(hours=i)).isoformat(),
                        market=f"mkt-{i}", question=f"q{i}", side="Yes",
                        price=0.5, stake=20.0, outcome="open", fee=0.2)
        j.update_bet(bid, outcome, payout)

    score = j.bet_scorecard()
    assert score["settled"] == 3
    assert abs(score["win_rate_pct"] - 66.7) < 0.1

    gate = tier3_gate(journal=j)
    # the gate detail line must carry the true win rate
    assert "67%" in gate.details or "66." in gate.details


# ---------------- commit 1: split round trips scored once ----------------

def test_split_round_trip_counted_once():
    """One buy sold in two sells is ONE economic round trip.

    Both FIFO branches incremented round_trips/wins, so a position exited in
    two tranches was scored twice -- inflating the reported win rate and
    trade count without any extra economics.
    """
    from bot.report import compute_pnl_and_winrate
    trades = [
        (1, "2026-01-01T00:00:00", "BTC/USD", "BUY", 2.0, 100.0, "r", 0.0, "o1", "filled"),
        (2, "2026-01-02T00:00:00", "BTC/USD", "SELL", 1.0, 120.0, "r", 0.0, "o2", "filled"),
        (3, "2026-01-03T00:00:00", "BTC/USD", "SELL", 1.0, 110.0, "r", 0.0, "o3", "filled"),
    ]
    stats = compute_pnl_and_winrate(trades)
    # cash math is already correct: bought 2 @ 100 = 200, sold 120 + 110 = 230
    assert stats["total_pnl"] == 30.0
    assert stats["round_trips"] == 1
    assert stats["num_trades"] == 1
    assert stats["winning_trades"] == 1


def test_fully_closed_round_trip_still_counts():
    """The single-fill case must keep counting exactly one round trip."""
    from bot.report import compute_pnl_and_winrate
    trades = [
        (1, "2026-01-01T00:00:00", "BTC/USD", "BUY", 1.0, 100.0, "r", 0.0, "o1", "filled"),
        (2, "2026-01-02T00:00:00", "BTC/USD", "SELL", 1.0, 120.0, "r", 0.0, "o2", "filled"),
    ]
    stats = compute_pnl_and_winrate(trades)
    assert stats["total_pnl"] == 20.0
    assert stats["round_trips"] == 1
    assert stats["winning_trades"] == 1


def test_break_even_round_trip_is_not_a_loss():
    """A flat round trip is breakeven, not a loss."""
    from bot.report import compute_pnl_and_winrate
    trades = [
        (1, "2026-01-01T00:00:00", "ETH/USD", "BUY", 1.0, 100.0, "r", 0.0, "o1", "filled"),
        (2, "2026-01-02T00:00:00", "ETH/USD", "SELL", 1.0, 100.0, "r", 0.0, "o2", "filled"),
    ]
    stats = compute_pnl_and_winrate(trades)
    assert stats["total_pnl"] == 0.0
    assert stats["losing_trades"] == 0
    assert stats["winning_trades"] == 0
    assert stats["round_trips"] == 1
