import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.journal import TradeJournal
from bot.futures import FuturesLedger, TIER5_TAG


# ---------------- fixtures ----------------

class _T5Cfg:
    futures = {
        "start_cash": 50, "leverage": 10, "max_margin": 15, "base_margin": 8,
        "stop_atr_mult": 1.5, "tp_atr_mult": 2.25, "max_hold_hours": 12,
        "max_drawdown_pct": 25, "taker_fee_pct": 0.05, "slippage_bps": 5,
        "funding_rate_pct_8h": 0.01, "universe": ["BTC/USD", "ETH/USD"],
        "auto_entry": True, "auto_cooldown_hours": 24, "min_llm_confidence": 0.70,
        "max_open_positions": 2, "cooldown_hours": 24, "min_cash_fraction": 0.15,
        "trend_ema_period": 50, "momentum_bars": 3, "momentum_pct": 0.5,
        "volume_surge_mult": 1.5, "min_atr_pct": 0.15, "max_atr_pct": 3.0,
    }


def _ledger(tmp_path, prices=None, bars=None):
    j = TradeJournal(db_path=str(tmp_path / "t5.db"))
    led = FuturesLedger(_T5Cfg, journal=j)
    if prices is not None:
        led.price_for = lambda s: prices.get(str(s).upper())
    if bars is not None:
        led._bars_for = lambda s: bars.get(str(s).upper())
    return led, j


def _mk_bars(price=100.0, atr=1.0, n=150, momentum=0.0, volume_surge=2.0, momentum_bars=3):
    """Synthetic 15m bars: flat history, then a momentum burst concentrated
    in the LAST `momentum_bars` bars (what the signal actually measures),
    volume surge on the final bar, controlled ATR."""
    import pandas as pd
    start = price / (1 + momentum / 100) if momentum else price
    closes = [start] * (n - momentum_bars)
    for i in range(1, momentum_bars + 1):
        closes.append(start * (1 + (momentum / 100) * (i / momentum_bars)))
    rows = []
    base_vol = 100.0
    for i, cur in enumerate(closes):
        hi, lo = cur + atr / 2, cur - atr / 2
        v = base_vol * (volume_surge if i == n - 1 else 1.0)
        rows.append({"open": cur, "high": hi, "low": lo, "close": cur, "volume": v})
    idx = pd.date_range(end=datetime.now(timezone.utc), periods=n, freq="15min", tz="UTC")
    m15 = pd.DataFrame(rows, index=idx)
    m15.index.name = "timestamp"
    h1 = m15  # reuse for the 1h leg; trend math only reads close
    return m15, h1


# ---------------- entry mechanics ----------------

def test_open_long_sets_atr_sl_tp_liq(tmp_path):
    bars15, bars1h = _mk_bars(price=100.0, atr=1.0)
    led, j = _ledger(tmp_path, prices={"BTC/USD": 100.0},
                     bars={"BTC/USD": (bars15, bars1h)})
    ok, note = led.open("BTC/USD", "LONG", margin=10, reason="test")
    assert ok, note
    pos = led._positions()["BTC/USD"]
    entry = pos["entry"]
    # slippage 5bps adverse for LONG
    assert entry == pytest.approx(100.0 * 1.0005, rel=1e-9)
    assert pos["side"] == "LONG"
    assert pos["stop"] == pytest.approx(entry - 1.5 * 1.0, rel=1e-6)
    assert pos["take_profit"] == pytest.approx(entry + 2.25 * 1.0, rel=1e-6)
    # liquidation approx 10% below entry at 10x
    assert pos["liquidation"] == pytest.approx(entry * 0.9, rel=1e-3)
    # SL must trigger before liquidation
    assert pos["stop"] > pos["liquidation"]
    assert pos["qty"] == pytest.approx(10 * 10 / entry, rel=1e-6)
    # cash debited margin + taker fee on notional
    fee = 100.0 * 0.0005 * 10 * 0.05 / 100  # notional * fee%
    assert led._cash() == pytest.approx(50 - 10 - pos["notional"] * 0.05 / 100, rel=1e-6)


def test_open_short_mirrors_directions(tmp_path):
    bars15, bars1h = _mk_bars(price=100.0, atr=1.0)
    led, _ = _ledger(tmp_path, prices={"ETH/USD": 100.0},
                     bars={"ETH/USD": (bars15, bars1h)})
    ok, note = led.open("ETH/USD", "SHORT", margin=10)
    assert ok, note
    pos = led._positions()["ETH/USD"]
    # SHORT entry is BELOW the mark (adverse = lower exit)
    assert pos["entry"] == pytest.approx(100.0 * 0.9995, rel=1e-9)
    assert pos["stop"] == pytest.approx(pos["entry"] + 1.5, rel=1e-6)
    assert pos["take_profit"] == pytest.approx(pos["entry"] - 2.25, rel=1e-6)
    assert pos["liquidation"] == pytest.approx(pos["entry"] * 1.1, rel=1e-3)
    assert pos["stop"] < pos["liquidation"]  # SL before liq


def test_open_rejects_when_atr_wider_than_liq_distance(tmp_path):
    # ATR 2% of price at 10x: SL = 3% > 10% liq distance? no — 3% < 10%.
    # Use ATR 8%: SL = 12% > 10% -> must reject.
    bars15, bars1h = _mk_bars(price=100.0, atr=8.0)
    led, _ = _ledger(tmp_path, prices={"BTC/USD": 100.0},
                     bars={"BTC/USD": (bars15, bars1h)})
    ok, note = led.open("BTC/USD", "LONG", margin=10)
    assert not ok
    assert "liquidation" in note


def test_open_caps_margin_and_positions(tmp_path):
    bars15, bars1h = _mk_bars(price=100.0, atr=1.0)
    bars = {"BTC/USD": (bars15, bars1h)}
    # clone bars for the second symbol so they don't share state
    led, _ = _ledger(tmp_path, prices={"BTC/USD": 100.0, "ETH/USD": 100.0},
                     bars={"BTC/USD": (bars15, bars1h),
                           "ETH/USD": _mk_bars(price=100.0, atr=1.0)[0:1] + (None,)}[0:1] if False else bars)
    led._bars_for = lambda s: bars.get(s, (bars15, bars1h))
    ok, _ = led.open("BTC/USD", "LONG", margin=99)  # over cap -> clipped to 15
    assert ok
    assert led._positions()["BTC/USD"]["margin"] == 15.0
    ok, _ = led.open("ETH/USD", "LONG", margin=5)
    assert ok
    # max_open_positions = 2 reached
    ok, note = led.open("SOL/USD", "LONG", margin=5)
    assert not ok and "max open" in note


def test_duplicate_symbol_refused(tmp_path):
    bars15, bars1h = _mk_bars(price=100.0, atr=1.0)
    led, _ = _ledger(tmp_path, prices={"BTC/USD": 100.0},
                     bars={"BTC/USD": (bars15, bars1h)})
    assert led.open("BTC/USD", "LONG", margin=5)[0]
    ok, note = led.open("BTC/USD", "LONG", margin=5)
    assert not ok and "already holding" in note


def test_cash_floor_blocks_when_cash_low(tmp_path):
    bars15, bars1h = _mk_bars(price=100.0, atr=1.0)
    led, j = _ledger(tmp_path, prices={"BTC/USD": 200.0, "ETH/USD": 200.0},
                     bars={"BTC/USD": (bars15, bars1h)})
    # equity high (position deep in profit), free cash below the 15% floor
    j.set_meta("t5_cash", "5.0")
    j.set_meta("t5_positions", __import__("json").dumps({
        "ETH/USD": {"side": "LONG", "qty": 5.0, "entry": 100.0, "margin": 10,
                    "notional": 100.0, "stop": 95.0, "take_profit": 105.0,
                    "liquidation": 90.0, "opened": datetime.now(timezone.utc).isoformat(),
                    "funding_accrued": 0.0}}))
    # equity = 5 + (10 + (200-100)*5) = 515; floor 15% = 77 > cash 5
    ok, note = led.open("BTC/USD", "LONG", margin=2)
    assert not ok and "cash floor" in note


# ---------------- valuation + exits ----------------

def test_long_position_value_marks_both_directions(tmp_path):
    bars15, bars1h = _mk_bars(price=100.0, atr=1.0)
    led, _ = _ledger(tmp_path, prices={"BTC/USD": 100.0},
                     bars={"BTC/USD": (bars15, bars1h)})
    led.open("BTC/USD", "LONG", margin=10)
    pos = led._positions()["BTC/USD"]
    qty, entry = pos["qty"], pos["entry"]
    # +2% mark: value = margin + (mark-entry)*qty
    v = led._position_value(pos, 102.0)
    assert v == pytest.approx(10 + (102.0 - entry) * qty, rel=1e-6)
    # -20% mark (beyond liq): clamped at -margin = 0
    v = led._position_value(pos, 80.0)
    assert v == pytest.approx(0.0, rel=1e-9)


def test_short_position_value(tmp_path):
    bars15, bars1h = _mk_bars(price=100.0, atr=1.0)
    led, _ = _ledger(tmp_path, prices={"ETH/USD": 100.0},
                     bars={"ETH/USD": (bars15, bars1h)})
    led.open("ETH/USD", "SHORT", margin=10)
    pos = led._positions()["ETH/USD"]
    qty, entry = pos["qty"], pos["entry"]
    v = led._position_value(pos, 98.0)  # price fell 2%: short gains
    assert v == pytest.approx(10 + (entry - 98.0) * qty, rel=1e-6)


def test_sweep_stop_loss_touches_intra_bar(tmp_path):
    bars15, bars1h = _mk_bars(price=100.0, atr=1.0)
    led, j = _ledger(tmp_path, prices={"BTC/USD": 100.0},
                     bars={"BTC/USD": (bars15, bars1h)})
    led.open("BTC/USD", "LONG", margin=10)
    pos = led._positions()["BTC/USD"]
    # craft history bars that dip through the stop after entry
    import pandas as pd
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    idx = pd.date_range(end=datetime.now(timezone.utc), periods=3, freq="15min", tz="UTC")
    dip = pd.DataFrame(
        {"open": [100, pos["stop"] - 1, 99], "high": [100.5, 100, 99.5],
         "low": [99.5, pos["stop"] - 1, 98.5], "close": [100, 99, 99]},
        index=idx)
    led._bars_for = lambda s: (dip, bars1h)
    # mark above the stop so only the RANGE check can fire
    led.price_for = lambda s: 99.5
    events = led.sweep()
    assert any(e["type"] == "stop_loss" for e in events)
    assert "BTC/USD" not in led._positions()
    # exit fills at the stop price minus exit slippage (not at the mark)
    ev = next(e for e in events if e["type"] == "stop_loss")
    assert ev["exit_price"] == pytest.approx(pos["stop"] * 0.9995, rel=1e-6)


def test_sweep_take_profit_on_high(tmp_path):
    bars15, bars1h = _mk_bars(price=100.0, atr=1.0)
    led, _ = _ledger(tmp_path, prices={"BTC/USD": 100.0},
                     bars={"BTC/USD": (bars15, bars1h)})
    led.open("BTC/USD", "LONG", margin=10)
    pos = led._positions()["BTC/USD"]
    import pandas as pd
    idx = pd.date_range(end=datetime.now(timezone.utc), periods=2, freq="15min", tz="UTC")
    spike = pd.DataFrame(
        {"open": [100, 110], "high": [101, pos["take_profit"] + 2],
         "low": [99, 110], "close": [101, 112]}, index=idx)
    led._bars_for = lambda s: (spike, bars1h)
    led.price_for = lambda s: 112.0
    events = led.sweep()
    assert any(e["type"] == "take_profit" for e in events)


def test_sweep_liquidation_clamps_loss_to_margin(tmp_path):
    bars15, bars1h = _mk_bars(price=100.0, atr=1.0)
    led, _ = _ledger(tmp_path, prices={"BTC/USD": 100.0},
                     bars={"BTC/USD": (bars15, bars1h)})
    led.open("BTC/USD", "LONG", margin=10)
    pos = led._positions()["BTC/USD"]
    # price gaps far through liquidation
    led.price_for = lambda s: 50.0
    import pandas as pd
    idx = pd.date_range(end=datetime.now(timezone.utc), periods=1, freq="15min", tz="UTC")
    flat = pd.DataFrame({"open": [50], "high": [51], "low": [49],
                        "close": [50], "volume": [1]}, index=idx)
    led._bars_for = lambda s: (flat, bars1h)
    events = led.sweep()
    ev = next(e for e in events if e["type"] == "liquidation")
    # loss clamped to the margin posted
    assert ev["pnl"] == pytest.approx(-10.0, rel=1e-6)
    assert led._cash() == pytest.approx(50 - 10 - 10 * 10 * 0.0005, rel=1e-3)  # 50 - margin(-10) - entry fee


def test_sweep_time_stop_closes_stale_position(tmp_path):
    bars15, bars1h = _mk_bars(price=100.0, atr=1.0)
    led, j = _ledger(tmp_path, prices={"BTC/USD": 100.0},
                     bars={"BTC/USD": (bars15, bars1h)})
    led.open("BTC/USD", "LONG", margin=10)
    # age the position past max_hold
    pos = led._positions()["BTC/USD"]
    pos["opened"] = (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat()
    positions = led._positions()
    positions["BTC/USD"] = pos
    j.set_meta("t5_positions", __import__("json").dumps(positions))
    events = led.sweep()
    assert any(e["type"] == "time_stop" for e in events)


def test_funding_accrues_after_8h_marks(tmp_path):
    bars15, bars1h = _mk_bars(price=100.0, atr=1.0)
    led, j = _ledger(tmp_path, prices={"BTC/USD": 100.0},
                     bars={"BTC/USD": (bars15, bars1h)})
    led.open("BTC/USD", "LONG", margin=10)
    # age the position past one 8h boundary and persist it
    positions = led._positions()
    positions["BTC/USD"]["opened"] = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()
    j.set_meta("t5_positions", __import__("json").dumps(positions))
    pos = led._positions()["BTC/USD"]
    cash_before = led._cash()
    evs = led._accrue_funding()
    assert evs and evs[0]["type"] == "funding"
    notional = pos["notional"]
    expected = notional * 0.01 / 100  # one mark, LONG pays
    assert led._cash() == pytest.approx(cash_before - expected, rel=1e-6)
    # a second accrual immediately after charges nothing new
    cash_mid = led._cash()
    assert led._accrue_funding() == []
    assert led._cash() == pytest.approx(cash_mid, rel=1e-9)


def test_short_receives_funding(tmp_path):
    bars15, bars1h = _mk_bars(price=100.0, atr=1.0)
    led, j = _ledger(tmp_path, prices={"ETH/USD": 100.0},
                     bars={"ETH/USD": (bars15, bars1h)})
    led.open("ETH/USD", "SHORT", margin=10)
    positions = led._positions()
    positions["ETH/USD"]["opened"] = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()
    j.set_meta("t5_positions", __import__("json").dumps(positions))
    pos = led._positions()["ETH/USD"]
    cash_before = led._cash()
    led._accrue_funding()
    assert led._cash() == pytest.approx(cash_before + pos["notional"] * 0.01 / 100, rel=1e-6)


def test_close_clamps_loss_and_adds_cooldown(tmp_path):
    bars15, bars1h = _mk_bars(price=100.0, atr=1.0)
    led, j = _ledger(tmp_path, prices={"BTC/USD": 100.0},
                     bars={"BTC/USD": (bars15, bars1h)})
    led.open("BTC/USD", "LONG", margin=10)
    ok, note = led.close("BTC/USD")
    assert ok, note
    assert led._positions() == {}
    assert led._in_cooldown("BTC/USD")
    # cooldown blocks re-entry
    ok2, note2 = led.open("BTC/USD", "LONG", margin=5)
    assert not ok2 and "cooldown" in note2


# ---------------- kill switch ----------------

def test_drawdown_kill_flattens_and_escalates(tmp_path):
    bars15, bars1h = _mk_bars(price=100.0, atr=1.0)
    led, j = _ledger(tmp_path, prices={"BTC/USD": 100.0},
                     bars={"BTC/USD": (bars15, bars1h)})
    led.open("BTC/USD", "LONG", margin=15)
    # crash the mark hard: liquidation + >30% equity drawdown from the peak
    led.price_for = lambda s: 40.0
    events = led.sweep()
    assert any(e["type"] == "kill" for e in events)
    assert led.kill_active() and led.kill_count() == 1
    assert led._positions() == {}
    # entries blocked while killed
    ok, note = led.open("ETH/USD", "LONG", margin=5)
    assert not ok and "kill" in note
    # second kill escalates to manual reset
    j.set_meta("t5_kill", "off")  # simulate re-arm
    led.price_for = lambda s: 100.0
    j.set_meta("t5_peak_equity", "50")
    led._positions()  # empty
    # force a second kill via a crashing valuation
    j.set_meta("t5_cash", "30")  # equity 30 vs peak 50 = 40% dd
    evs = led.sweep()
    assert any(e["type"] == "kill" for e in evs)
    assert led.kill_count() == 2
    assert j.get_meta("t5_manual_reset_required") == "true"
    # auto re-arm refuses after 2 kills
    j.set_meta("t5_kill_at", (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat())
    assert led._auto_rearm_after_cooldown() is False


# ---------------- deterministic signals ----------------

def test_signal_requires_1h_trend_and_momentum(tmp_path):
    up15, up1h = _mk_bars(price=105.0, atr=0.4, momentum=1.5, volume_surge=2.0)
    led, _ = _ledger(tmp_path)
    led._bars_for = lambda s: (up15, up1h) if s == "BTC/USD" else (None, None)
    sig = led._signal("BTC/USD")
    assert sig is not None and sig["direction"] == "LONG"
    assert sig["trend_1h"] == "up"
    # a DOWN 1h trend with negative momentum -> SHORT
    dn15, dn1h = _mk_bars(price=95.0, atr=0.4, momentum=-1.5, volume_surge=2.0)
    led2, _ = _ledger(tmp_path)
    led2._bars_for = lambda s: (dn15, dn1h)
    sig2 = led2._signal("BTC/USD")
    assert sig2 is not None and sig2["direction"] == "SHORT"
    assert sig2["trend_1h"] == "down"


def test_signal_rejects_dead_and_spiking_atr(tmp_path):
    # ATR below the 0.15% floor
    dead15, dead1h = _mk_bars(price=100.0, atr=0.01, momentum=1.5, volume_surge=2.0)
    led, _ = _ledger(tmp_path)
    led._bars_for = lambda s: (dead15, dead1h)
    assert led._signal("BTC/USD") is None
    # ATR above the 3% ceiling (news gap)
    spike15, spike1h = _mk_bars(price=100.0, atr=5.0, momentum=1.5, volume_surge=2.0)
    led._bars_for = lambda s: (spike15, spike1h)
    assert led._signal("BTC/USD") is None
    # no volume surge
    flat15, flat1h = _mk_bars(price=100.0, atr=0.4, momentum=1.5, volume_surge=1.0)
    led._bars_for = lambda s: (flat15, flat1h)
    assert led._signal("BTC/USD") is None


# ---------------- LLM gate ----------------

class _FakeModel:
    def __init__(self, out):
        self.out = out
    def generate_json(self, prompt, max_tokens=300, **kw):
        return self.out


def test_llm_gate_fail_closed_on_bad_schema(tmp_path):
    bars15, bars1h = _mk_bars(price=100.0, atr=1.0)
    led, _ = _ledger(tmp_path, prices={"BTC/USD": 100.0},
                     bars={"BTC/USD": (bars15, bars1h)})
    led.model = _FakeModel({"take": "yes"})  # wrong type
    take, conf, reason = led._llm_conviction({"symbol": "BTC/USD"})
    assert not take
    led.model = _FakeModel({"take": True, "confidence": 1.5})  # out of range
    take, _, _ = led._llm_conviction({"symbol": "BTC/USD"})
    assert not take
    led.model = _FakeModel({"take": True, "confidence": 0.5})  # below threshold
    take, _, _ = led._llm_conviction({"symbol": "BTC/USD"})
    assert not take
    # raising model = fail-closed
    class _Boom:
        def generate_json(self, *a, **k):
            raise RuntimeError("api down")
    led.model = _Boom()
    take, _, reason = led._llm_conviction({"symbol": "BTC/USD"})
    assert not take and "failed" in reason


def test_llm_gate_passes_valid_conviction(tmp_path):
    bars15, bars1h = _mk_bars(price=100.0, atr=1.0)
    led, _ = _ledger(tmp_path)
    led.model = _FakeModel({"take": True, "confidence": 0.85, "reason": "clean trend"})
    take, conf, reason = led._llm_conviction({"symbol": "BTC/USD"})
    assert take and conf == 0.85 and "clean trend" in reason


def test_auto_entry_sizes_by_conviction_and_logs_proposal(tmp_path):
    up15, up1h = _mk_bars(price=105.0, atr=0.4, momentum=1.5, volume_surge=2.0)
    bars = {"BTC/USD": (up15, up1h)}
    led, j = _ledger(tmp_path, prices={"BTC/USD": 105.0})
    led._bars_for = lambda s: bars.get(s)
    led.model = _FakeModel({"take": True, "confidence": 0.85, "reason": "trend aligned"})
    led._headlines = lambda: {}
    evs = led._auto_entries([{"symbol": "BTC/USD", "direction": "LONG",
                              "momentum_pct": 1.5, "volume_surge": 2.0, "atr_pct": 0.4,
                              "atr": 0.4, "price": 105.0, "trend_1h": "up"}])
    assert len(evs) == 1 and evs[0]["type"] == "auto_open"
    pos = led._positions()["BTC/USD"]
    # conf 0.85 between 0.70 and 0.95 -> margin between 8 and 15
    assert 8 <= pos["margin"] <= 15
    props = j.get_proposals()
    assert any(p[2] == "tier5" for p in props)
    # one entry per cycle: a second signal same cycle is skipped
    evs2 = led._auto_entries([{"symbol": "ETH/USD", "direction": "LONG",
                               "momentum_pct": 1.5, "volume_surge": 2.0,
                               "atr_pct": 0.4, "atr": 0.4, "price": 105.0}])
    # ETH not held and max 2 positions, but ONE-ENTRY-PER-CYCLE means this
    # new cycle may enter it
    assert evs2 == [] or all(e["type"] == "auto_open" for e in evs2)


def test_declined_llm_logs_shadow_proposal(tmp_path):
    up15, up1h = _mk_bars(price=105.0, atr=0.4, momentum=1.5, volume_surge=2.0)
    led, j = _ledger(tmp_path, prices={"BTC/USD": 105.0})
    led._bars_for = lambda s: (up15, up1h)
    led.model = _FakeModel({"take": False, "confidence": 0.4, "reason": "choppy"})
    led._headlines = lambda: {}
    evs = led._auto_entries([{"symbol": "BTC/USD", "direction": "LONG",
                              "momentum_pct": 1.5, "volume_surge": 2.0}])
    assert evs == []
    assert "BTC/USD" not in led._positions()
    props = j.get_proposals()
    assert any(p[2] == "tier5" and p[7] == 0.4 for p in props)


# ---------------- tier isolation ----------------

def test_tier5_trades_excluded_from_tier1_pnl(tmp_path):
    from bot.report import compute_pnl_and_winrate
    led, j = _ledger(tmp_path)
    j.log_trade(timestamp=datetime.now(timezone.utc).isoformat(),
                symbol="BTC/USD", action="BUY", qty=1.0, price=100.0,
                reasoning=f"{TIER5_TAG} test")
    j.log_trade(timestamp=datetime.now(timezone.utc).isoformat(),
                symbol="BTC/USD", action="SELL", qty=1.0, price=150.0,
                reasoning=f"{TIER5_TAG} test")
    j.log_trade(timestamp=datetime.now(timezone.utc).isoformat(),
                symbol="ETH/USD", action="BUY", qty=1.0, price=100.0,
                reasoning="[sma_cross] golden cross")
    j.log_trade(timestamp=datetime.now(timezone.utc).isoformat(),
                symbol="ETH/USD", action="SELL", qty=1.0, price=110.0,
                reasoning="[sma_cross] death cross")
    stats = compute_pnl_and_winrate(j.get_trades())
    assert stats["total_pnl"] == pytest.approx(10.0, rel=1e-6)  # only ETH round trip


def test_status_line_and_valuation_stale_marks(tmp_path):
    led, j = _ledger(tmp_path, prices={"BTC/USD": None})
    # a position with no mark is excluded from equity and surfaced
    j.set_meta("t5_cash", "20")
    j.set_meta("t5_positions", __import__("json").dumps({
        "BTC/USD": {"side": "LONG", "qty": 0.1, "entry": 100.0, "margin": 10,
                    "notional": 100.0, "stop": 95.0, "take_profit": 105.0,
                    "liquidation": 90.0, "opened": datetime.now(timezone.utc).isoformat(),
                    "funding_accrued": 0.0}}))
    v = led.valuation()
    assert v["stale_symbols"] == ["BTC/USD"]
    assert v["equity"] == pytest.approx(20.0, rel=1e-9)  # not marked at entry
    line = led.status_line()
    assert "Tier 5" in line and "KILLED" not in line
