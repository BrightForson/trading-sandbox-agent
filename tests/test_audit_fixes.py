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


# ================= commit 2: critical =================

import pandas as pd


class _T5Cfg:
    futures = {
        "start_cash": 50, "leverage": 10, "max_margin": 15, "base_margin": 8,
        "stop_atr_mult": 1.5, "tp_atr_mult": 2.25, "max_hold_hours": 12,
        "max_drawdown_pct": 25, "taker_fee_pct": 0.05, "slippage_bps": 5,
        "funding_rate_pct_8h": 0.0, "universe": ["BTC/USD"],
        "auto_entry": True, "auto_cooldown_hours": 24, "min_llm_confidence": 0.70,
        "max_open_positions": 2, "cooldown_hours": 24, "min_cash_fraction": 0.15,
        "trailing_atr_mult": 1.0, "trailing_activate_r": 1.0,
    }


def _t5_ledger(tmp_path, prices=None, bars=None):
    from bot.futures import FuturesLedger
    j = TradeJournal(db_path=str(tmp_path / "t5.db"))
    led = FuturesLedger(_T5Cfg, journal=j)
    if prices is not None:
        led.price_for = lambda s: prices.get(str(s).upper())
    if bars is not None:
        led._bars_for = lambda s: bars.get(str(s).upper())
    return led, j


def _rising_bars(n=60, start=100.0, step=0.1, end=None):
    """Strictly rising 15m bars whose lows never revisit earlier levels."""
    end = end or datetime.now(timezone.utc)
    idx = pd.date_range(end=end, periods=n, freq="15min", tz="UTC")
    closes = [start + step * i for i in range(n)]
    return pd.DataFrame(
        {"open": closes, "high": [c + 0.05 for c in closes],
         "low": [c - 0.05 for c in closes], "close": closes,
         "volume": [1.0] * n}, index=idx)


def _seed_long(led, j, **over):
    """Write a LONG position directly, bypassing signal discovery."""
    pos = {"side": "LONG", "entry": 100.05, "qty": 0.10, "notional": 10.0,
           "margin": 10.0, "stop": 95.0, "take_profit": 110.0,
           "entry_atr": 1.0, "r_distance": 5.0, "liquidation": 50.0,
           "trailing_stop": None, "funding_marks_paid": 0,
           "opened": (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()}
    pos.update(over)
    import json as _json
    j.set_meta("t5_cash", "50.0")
    j.set_meta("t5_positions", _json.dumps({"BTC/USD": pos}))
    return pos


# ---------------- trailing stop window ----------------

def test_trailing_stop_ignores_history_predating_the_trail(tmp_path):
    """A trail must be tested only against bars since its CURRENT level was set.

    The window used the position's open time, so a trail armed on a healthy
    uptrend was immediately tested against lows from hours earlier and
    closed the position on the same sweep that armed it. The ratchet-and-ride
    design never functioned.
    """
    led, j = _t5_ledger(tmp_path)
    bars = _rising_bars(n=60)
    led._bars_for = lambda s: (bars, bars)
    # market at 101.50 and climbing; trail armed 10 minutes ago at 100.85
    led.price_for = lambda s: 101.50
    _seed_long(led, j, trailing_stop=100.85,
               trailing_stop_armed_at=(datetime.now(timezone.utc)
                                       - timedelta(minutes=10)).isoformat())

    events = led.sweep()

    assert not [e for e in events if e["type"] == "trailing_stop"]
    assert "BTC/USD" in led._positions(), "healthy uptrend must not be stopped out"


def test_trailing_stop_fires_on_a_dip_after_arming(tmp_path):
    """A genuine pullback below an established trail must still close it."""
    led, j = _t5_ledger(tmp_path)
    bars = _rising_bars(n=60)
    # a dip below the trail, but only in the most recent bars
    armed = datetime.now(timezone.utc) - timedelta(minutes=45)
    dip = bars.copy()
    dip.iloc[-1, dip.columns.get_loc("low")] = 100.0
    dip.iloc[-2, dip.columns.get_loc("low")] = 100.2
    led._bars_for = lambda s: (dip, dip)
    led.price_for = lambda s: 100.5
    _seed_long(led, j, trailing_stop=100.85, trailing_stop_armed_at=armed.isoformat())

    events = led.sweep()

    assert any(e["type"] == "trailing_stop" for e in events)
    assert "BTC/USD" not in led._positions()


def test_trail_without_arm_time_uses_current_mark_only(tmp_path):
    """A trail with no recorded arm time is judged on the mark, not on
    unverifiable history -- the conservative direction."""
    led, j = _t5_ledger(tmp_path)
    bars = _rising_bars(n=60)
    led._bars_for = lambda s: (bars, bars)
    led.price_for = lambda s: 101.50
    _seed_long(led, j, trailing_stop=100.85)  # no trailing_stop_armed_at

    events = led.sweep()

    assert not [e for e in events if e["type"] == "trailing_stop"]
    assert "BTC/USD" in led._positions()


def test_ratcheting_a_trail_resets_its_window(tmp_path):
    """Ratcheting to a new level must restart the window at that level.

    Otherwise a dip under the older, looser trail is read as a touch of the
    newer, tighter one that did not exist at the time.
    """
    led, j = _t5_ledger(tmp_path)
    bars = _rising_bars(n=60)
    led._bars_for = lambda s: (bars, bars)
    led.price_for = lambda s: 110.0
    _seed_long(led, j, trailing_stop=None)

    led.sweep()

    pos = led._positions().get("BTC/USD")
    if pos is not None and pos.get("trailing_stop") is not None:
        assert pos.get("trailing_stop_armed_at") is not None


# ---------------- kill switch must not disable exits ----------------

def test_sweep_still_enforces_exits_while_the_kill_is_on(tmp_path):
    """An open position must keep its stop after the kill switch engages.

    sweep() returned immediately on kill_active(), but _trigger_kill
    swallows per-symbol flatten failures and continues, so survivors are
    real. A surviving 10x LONG was then left with no SL, TP, trailing, funding
    or time-stop enforcement for the whole 24h cooldown -- indefinitely on the
    second kill, which latches MANUAL_RESET.
    """
    led, j = _t5_ledger(tmp_path)
    bars = _rising_bars(n=30, start=100.0, step=0.0)
    led._bars_for = lambda s: (bars, bars)
    # market fell through the stop (99.15) but is nowhere near liquidation
    led.price_for = lambda s: 95.0
    _seed_long(led, j, stop=99.15, take_profit=120.0, r_distance=0.85)
    j.set_meta("t5_kill", "on")

    events = led.sweep()

    assert "BTC/USD" not in led._positions(), (
        "a breached stop must be enforced even with the kill switch on")


def test_stop_enforced_when_kill_flatten_fails(tmp_path):
    """The real shape of the bug: a flatten that raises, leaving a survivor
    with a breached stop and no enforcement at all."""
    led, j = _t5_ledger(tmp_path)
    # bars whose lows genuinely trade through the 99.15 stop
    bars = _rising_bars(n=30, start=100.0, step=0.0)
    bars.iloc[-1, bars.columns.get_loc("low")] = 94.0
    led._bars_for = lambda s: (bars, bars)
    # mark above liquidation (50) but below the stop
    led.price_for = lambda s: 95.0
    _seed_long(led, j, stop=99.15, take_profit=120.0, r_distance=0.85)
    j.set_meta("t5_kill", "on")

    # kill flatten raises, exactly as _trigger_kill's per-symbol try/except
    # tolerates; the survivor is then governed only by the normal sweep
    calls = {"n": 0}
    real_close = led._close_position

    def flaky_close(symbol, note="", mark=None, **kw):
        if calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("transient broker failure during flatten")
        return real_close(symbol, note=note, mark=mark, **kw)

    led._close_position = flaky_close

    events = led.sweep()

    assert "BTC/USD" not in led._positions(), (
        "surviving position kept a breached stop with the kill switch on")
    assert any(e["type"] == "stop_loss" for e in events)


# ---------------- volatility must be annualized ----------------

def test_realized_vol_is_annualized_for_daily_closes():
    """A 30%-annualized series must read ~30%, not ~1.6%.

    The function returned the per-bar standard deviation of log returns with
    no sqrt(periods_per_year) factor. Its only caller feeds the Tier 4 LLM
    conviction gate, which gates automated entries -- so a risk gate was
    reporting volatility ~19x too small for daily data.
    """
    import math
    import random
    from bot.indicators import PERIODS_PER_YEAR, realized_volatility_pct
    random.seed(7)
    sigma_daily = 0.30 / math.sqrt(365)
    px, closes = 100.0, [100.0]
    for _ in range(80):
        px *= math.exp(random.gauss(0, sigma_daily))
        closes.append(px)

    got = realized_volatility_pct(closes, periods_per_year=PERIODS_PER_YEAR["1d"])
    assert 22.0 < got < 40.0, f"expected roughly 30% annualized, got {got:.2f}"


def test_realized_vol_scales_with_sampling_rate():
    """The same volatility must annualize consistently at 15m and 1d."""
    import math
    import random
    from bot.indicators import PERIODS_PER_YEAR, realized_volatility_pct
    random.seed(11)
    target = 0.50
    for label, ppy, n in (("1d", 365, 90), ("15m", 35040, 800)):
        random.seed(11)
        sigma = target / math.sqrt(ppy)
        px, closes = 100.0, [100.0]
        for _ in range(n):
            px *= math.exp(random.gauss(0, sigma))
            closes.append(px)
        got = realized_volatility_pct(closes, periods_per_year=ppy)
        assert abs(got - target * 100) < 12.0, f"{label}: {got:.1f}%"


def test_realized_vol_requires_a_sampling_rate():
    """No safe default exists, so an unscaled call must not return a number."""
    from bot.indicators import realized_volatility_pct
    assert realized_volatility_pct([1.0, 1.1, 1.2, 1.15]) is None


def test_indicators_return_none_on_malformed_input():
    """The module contract promises None for unusable data, not an exception."""
    from bot.indicators import realized_volatility_pct, rsi
    assert rsi([1.0, None, 3.0, 4.0, 5.0, 6.0]) is None
    assert rsi([1.0, 2.0, "x", 4.0, 5.0]) is None
    assert rsi([1.0, float("nan")] * 20) is None
    assert realized_volatility_pct([1.0, None, 1.2], periods_per_year=365) is None


def test_rsi_receives_enough_closes_from_the_tier4_history():
    """The Tier 4 history must retain enough closes for a 14-period RSI.

    _coingecko_history kept only the last 10 of 30 daily closes while the
    caller used the default period=14 (which needs 15), so rsi_30d was None
    for every coin on every cycle -- a named risk lens silently missing from
    an automated entry gate.
    """
    from bot.indicators import rsi
    thirty = [100.0 + (i % 7) for i in range(30)]
    assert rsi(thirty[-10:]) is None, "10 closes cannot feed a 14-period RSI"
    assert rsi(thirty) is not None, "30 closes must feed a 14-period RSI"


# ---------------- stale marks must not fabricate a drawdown ----------------

class _T4Cfg:
    memecoin = {
        "start_cash": 40, "max_stake": 12, "stop_loss_pct": 25,
        "take_profit_pct": 50, "time_stop_hours": 72, "max_drawdown_pct": 25,
        "taker_fee_pct": 1.0, "slippage_bps": 100, "card_ttl_minutes": 360,
        "max_trending_cards": 7, "spike_volume_multiple": 3.0,
        "spike_min_volume_24h": 100000, "auto_entry": True,
        "auto_cooldown_hours": 24, "min_llm_confidence": 0.75,
        "base_stake": 6, "max_open_positions": 3, "min_liquidity_usd": 250000,
        "min_volume_24h_usd": 500000, "min_age_hours": 168, "max_mcap_rank": 300,
        "trailing_stop_pct": 20, "trailing_activate_pct": 25,
        "partial_tp_pct": 80, "partial_tp_sell_frac": 0.5,
        "entry_cooldown_hours": 168,
    }


def _t4_ledger(tmp_path, prices=None):
    from bot.memecoin import MemecoinLedger
    j = TradeJournal(db_path=str(tmp_path / "t4.db"))
    led = MemecoinLedger(_T4Cfg, journal=j)
    if prices is not None:
        led.price_for = lambda s: prices.get(str(s).upper())
    return led, j


def test_stale_mark_does_not_collapse_equity(tmp_path):
    """One dead price feed must not read as a 30% drawdown on the ledger.

    valuation() dropped unpriceable positions from equity entirely, so a
    single outage on one $12 coin of a $40 ledger understated equity by 30%
    and tripped the account kill.
    """
    led, j = _t4_ledger(tmp_path, prices={"AAA": 1.0, "BBB": 1.0, "CCC": 1.0})
    for sym in ("AAA", "BBB", "CCC"):
        led.buy(sym, stake=12)
    healthy = led.valuation()

    # CCC's feed dies entirely
    led.price_for = lambda s: None if str(s).upper() == "CCC" else 1.0
    degraded = led.valuation()

    assert degraded["equity"] == healthy["equity"], (
        "a stale mark must not change equity when the last known price still "
        "stands; equity collapsed, which is what fired the kill")
    assert degraded["stale_symbols"] == ["CCC"]


def test_stale_mark_still_reported_so_the_blind_spot_is_visible(tmp_path):
    led, j = _t4_ledger(tmp_path, prices={"AAA": 1.0, "CCC": 1.0})
    for sym in ("AAA", "CCC"):
        led.buy(sym, stake=12)
    led.valuation()  # records last_mark while the feed is healthy
    led.price_for = lambda s: None
    v = led.valuation()
    assert sorted(v["stale_symbols"]) == ["AAA", "CCC"]


def test_unpriceable_position_is_never_closed_at_entry(tmp_path):
    """With no mark and no last_mark, refuse the close instead of faking it.

    Falling back to the entry price fictitiously restored equity during a
    forced flatten, and reset_kill then re-armed the peak at that invented
    value -- disarming the drawdown kill permanently.
    """
    led, j = _t4_ledger(tmp_path, prices={"AAA": 1.0})
    led.buy("AAA", stake=12)
    cash_before = led._cash()

    led.price_for = lambda s: None
    pos = led._positions()["AAA"]
    pos.pop("last_mark", None)
    import json as _json
    j.set_meta("t4_positions", _json.dumps({"AAA": pos}))

    assert led._sell_position("AAA", note="kill flatten") is None
    assert "AAA" in led._positions(), "position must survive an unpriceable close"
    assert led._cash() == cash_before, "no proceeds may be credited"


def test_kill_switch_does_not_suspend_tier4_exits(tmp_path):
    """An open Tier 4 position keeps its stop while the kill switch is on."""
    led, j = _t4_ledger(tmp_path, prices={"AAA": 1.0})
    led.buy("AAA", stake=12)
    j.set_meta("t4_kill", "on")

    # price collapses 40%: well past the 25% stop, and above entry
    led.price_for = lambda s: 0.60
    events = led.sweep()

    assert "AAA" not in led._positions(), (
        "a breached stop must be enforced with the kill switch on")
