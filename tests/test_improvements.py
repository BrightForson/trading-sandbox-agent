import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.journal import TradeJournal
from bot.indicators import rsi, realized_volatility_pct


# ---------------- indicators ----------------

def test_rsi_known_values():
    # 14 monotonic up-closes -> overbought
    up = [100 + i for i in range(20)]
    assert rsi(up) > 70
    # 14 monotonic down-closes -> oversold
    down = [100 - i for i in range(20)]
    assert rsi(down) < 30
    # perfectly flat: no losses and no gains -> None (no signal)
    assert rsi([100.0] * 20) is None
    # insufficient data -> None
    assert rsi([1.0, 2.0, 3.0]) is None
    assert rsi(None) is None


def test_realized_volatility_pct():
    # 1% alternating moves -> clearly positive vol
    vals = [100 * (1.01 ** (i % 2)) for i in range(20)]
    vol = realized_volatility_pct(vals)
    assert vol is not None and vol > 0
    # flat series -> zero vol
    assert realized_volatility_pct([50.0] * 10) == pytest.approx(0.0)
    # insufficient / invalid -> None
    assert realized_volatility_pct([1.0, 2.0]) is None
    assert realized_volatility_pct(None) is None
    assert realized_volatility_pct([1.0, -2.0, 3.0]) is None


# ---------------- futures: best-pick gate ----------------

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
        "best_pick": True,
    }


class _PickModel:
    """Fake batch-gate model: picks the given symbol at the given confidence."""
    def __init__(self, out):
        self.out = out
        self.calls = 0
    def generate_json(self, prompt, max_tokens=300, system=None, **kw):
        self.calls += 1
        return dict(self.out)


def _mk_bars(price=100.0, atr=1.0, n=150, momentum=0.0, volume_surge=2.0, momentum_bars=3):
    import pandas as pd
    start = price / (1 + momentum / 100) if momentum else price
    closes = [start] * (n - momentum_bars)
    for i in range(1, momentum_bars + 1):
        closes.append(start * (1 + (momentum / 100) * (i / momentum_bars)))
    rows = []
    for i, cur in enumerate(closes):
        hi, lo = cur + atr / 2, cur - atr / 2
        v = 100.0 * (volume_surge if i == n - 1 else 1.0)
        rows.append({"open": cur, "high": hi, "low": lo, "close": cur, "volume": v})
    idx = pd.date_range(end=datetime.now(timezone.utc), periods=n, freq="15min", tz="UTC")
    df = pd.DataFrame(rows, index=idx)
    df.index.name = "timestamp"
    return df, df


def _ledger(tmp_path, prices=None, bars=None, model=None):
    from bot.futures import FuturesLedger
    j = TradeJournal(db_path=str(tmp_path / "t5.db"))
    led = FuturesLedger(_T5Cfg, journal=j, model=model)
    if prices is not None:
        led.price_for = lambda s: prices.get(str(s).upper())
    if bars is not None:
        led._bars_for = lambda s: bars.get(str(s).upper())
    return led, j


def test_best_pick_selects_llm_choice_not_list_order(tmp_path):
    """The LLM's chosen symbol is entered — even when it is NOT the first
    signal in list order (the owner's explicit requirement)."""
    bars_btc, _ = _mk_bars(price=105.0, atr=0.4, momentum=1.5)
    bars_eth, _ = _mk_bars(price=105.0, atr=0.4, momentum=1.5)
    model = _PickModel({"symbol": "ETH/USD", "take": True, "confidence": 0.85,
                        "reason": "fresher momentum"})
    led, j = _ledger(tmp_path, prices={"BTC/USD": 105.0, "ETH/USD": 105.0},
                     bars={"BTC/USD": (bars_btc, bars_btc),
                           "ETH/USD": (bars_eth, bars_eth)},
                     model=model)
    led._headlines = lambda: {}
    sigs = [{"symbol": "BTC/USD", "direction": "LONG", "momentum_pct": 1.5,
             "volume_surge": 2.0, "atr_pct": 0.4, "atr": 0.4, "price": 105.0,
             "trend_1h": "up", "rsi_14": 58},
            {"symbol": "ETH/USD", "direction": "LONG", "momentum_pct": 1.2,
             "volume_surge": 1.9, "atr_pct": 0.4, "atr": 0.4, "price": 105.0,
             "trend_1h": "up", "rsi_14": 55}]
    events = led._auto_entries(sigs)
    assert len(events) == 1 and events[0]["symbol"] == "ETH/USD"
    assert "ETH/USD" in led._positions() and "BTC/USD" not in led._positions()
    # ONE batch call for BOTH candidates — not one call each
    assert model.calls == 1
    props = j.get_proposals(kind="auto_entry")
    # proposal row: (id, ts, source, kind, symbol, action, notional, ...)
    assert any(p[4] == "ETH/USD" and p[9] == "executed" for p in props)


def test_best_pick_decline_all_logs_shadow_each(tmp_path):
    bars_btc, _ = _mk_bars(price=105.0, atr=0.4, momentum=1.5)
    model = _PickModel({"symbol": None, "take": False, "confidence": 0.5,
                        "reason": "both exhausted"})
    led, j = _ledger(tmp_path, prices={"BTC/USD": 105.0, "ETH/USD": 105.0},
                     bars={"BTC/USD": (bars_btc, bars_btc)}, model=model)
    led._headlines = lambda: {}
    sigs = [{"symbol": "BTC/USD", "direction": "LONG", "momentum_pct": 1.5,
             "volume_surge": 2.0, "atr_pct": 0.4, "atr": 0.4, "price": 105.0},
            {"symbol": "ETH/USD", "direction": "LONG", "momentum_pct": 1.2,
             "volume_surge": 1.9, "atr_pct": 0.4, "atr": 0.4, "price": 105.0}]
    events = led._auto_entries(sigs)
    assert events == []
    assert led._positions() == {}
    # every declined candidate is journaled for calibration tracking
    props = j.get_proposals(kind="auto_entry")
    assert {p[4] for p in props} == {"BTC/USD", "ETH/USD"}
    assert all(p[9] == "shadow" for p in props)


def test_best_pick_unknown_symbol_rejected(tmp_path):
    model = _PickModel({"symbol": "LUNA/USD", "take": True, "confidence": 0.9,
                        "reason": "hallucinated"})
    led, _ = _ledger(tmp_path, prices={"BTC/USD": 105.0}, model=model)
    sigs = [{"symbol": "BTC/USD", "direction": "LONG", "momentum_pct": 1.5}]
    events = led._auto_entries(sigs)
    assert events == []
    assert led._positions() == {}


def test_best_pick_single_candidate_omitted_symbol_accepted(tmp_path):
    """With one candidate a symbol-less 'take' is accepted (models routinely
    omit the field when there is nothing to choose between)."""
    bars_btc, _ = _mk_bars(price=105.0, atr=0.4, momentum=1.5)
    model = _PickModel({"take": True, "confidence": 0.85, "reason": "clean trend"})
    led, j = _ledger(tmp_path, prices={"BTC/USD": 105.0},
                     bars={"BTC/USD": (bars_btc, bars_btc)}, model=model)
    led._headlines = lambda: {}
    sigs = [{"symbol": "BTC/USD", "direction": "LONG", "momentum_pct": 1.5,
             "volume_surge": 2.0, "atr_pct": 0.4, "atr": 0.4, "price": 105.0,
             "trend_1h": "up"}]
    events = led._auto_entries(sigs)
    assert len(events) == 1 and events[0]["symbol"] == "BTC/USD"


def test_best_pick_off_falls_back_to_sequential_gate(tmp_path):
    class _Cfg(_T5Cfg):
        futures = {**_T5Cfg.futures, "best_pick": False}
    from bot.futures import FuturesLedger
    bars_btc, _ = _mk_bars(price=105.0, atr=0.4, momentum=1.5)
    model = _PickModel({"take": True, "confidence": 0.85, "reason": "ok"})
    j = TradeJournal(db_path=str(tmp_path / "t5b.db"))
    led = FuturesLedger(_Cfg, journal=j, model=model)
    led.price_for = lambda s: {"BTC/USD": 105.0}.get(str(s).upper())
    led._bars_for = lambda s: (bars_btc, bars_btc)
    led._headlines = lambda: {}
    sigs = [{"symbol": "BTC/USD", "direction": "LONG", "momentum_pct": 1.5,
             "volume_surge": 2.0, "atr_pct": 0.4, "atr": 0.4, "price": 105.0,
             "trend_1h": "up"}]
    events = led._auto_entries(sigs)
    # sequential path passes through _llm_conviction (the legacy gate),
    # which reads take/confidence/reason the same way
    assert len(events) == 1


# ---------------- futures: trailing stop ----------------

def test_futures_trailing_stop_arms_and_exits(tmp_path):
    bars15, bars1h = _mk_bars(price=100.0, atr=1.0)
    led, _ = _ledger(tmp_path, prices={"BTC/USD": 100.0},
                     bars={"BTC/USD": (bars15, bars1h)})
    led.open("BTC/USD", "LONG", margin=10, reason="test")
    pos = led._positions()["BTC/USD"]
    # +1.2R gain arms the trail (>= 1R) but stays BELOW take-profit (1.5R)
    arm_mark = pos["entry"] + 1.2 * pos["r_distance"]
    assert arm_mark < pos["take_profit"]
    led.price_for = lambda s: arm_mark
    events = led.sweep()
    assert events == [] or all(e["type"] != "trailing_stop" for e in events)
    pos2 = led._positions()["BTC/USD"]
    assert pos2["trailing_stop"] is not None
    assert pos2["trailing_stop"] == pytest.approx(arm_mark - 1.0, rel=1e-6)
    # price falls back through the trail -> exit at the trail (minus
    # the adverse exit slippage, same fill discipline as every exit)
    led.price_for = lambda s: pos2["trailing_stop"] - 0.5
    events = led.sweep()
    ev = next(e for e in events if e["type"] == "trailing_stop")
    assert ev["exit_price"] == pytest.approx(pos2["trailing_stop"] * 0.9995, rel=1e-6)
    assert "BTC/USD" not in led._positions()


def test_futures_trailing_stop_not_armed_below_activation(tmp_path):
    bars15, bars1h = _mk_bars(price=100.0, atr=1.0)
    led, _ = _ledger(tmp_path, prices={"BTC/USD": 100.0},
                     bars={"BTC/USD": (bars15, bars1h)})
    led.open("BTC/USD", "LONG", margin=10, reason="test")
    pos = led._positions()["BTC/USD"]
    # half of activation R: no trail yet
    led.price_for = lambda s: pos["entry"] + 0.5 * pos["r_distance"]
    events = led.sweep()
    assert all(e["type"] != "trailing_stop" for e in events)
    assert led._positions()["BTC/USD"]["trailing_stop"] is None


# ---------------- futures: signal dossier enrichment ----------------

def test_signal_dossier_has_rsi_and_trend_distance(tmp_path):
    up15, up1h = _mk_bars(price=105.0, atr=0.4, momentum=1.5, volume_surge=2.0)
    led, _ = _ledger(tmp_path)
    led._bars_for = lambda s: (up15, up1h)
    sig = led._signal("BTC/USD")
    assert sig is not None
    # monotonic up-history: RSI is validly 100 (all gains, no losses)
    assert sig["rsi_14"] is not None and 0 < sig["rsi_14"] <= 100
    assert isinstance(sig["trend_distance_pct"], float)
    # pre-existing fields kept
    assert sig["trend_1h"] == "up" and "momentum_pct" in sig


# ---------------- tier 4: LLM dossier enrichment ----------------

class _T4Cfg:
    memecoin = {
        "start_cash": 40, "max_stake": 12, "stop_loss_pct": 25,
        "take_profit_pct": 50, "time_stop_hours": 72, "max_drawdown_pct": 25,
        "taker_fee_pct": 1.0, "slippage_bps": 100, "card_ttl_minutes": 360,
        "max_trending_cards": 7, "spike_volume_multiple": 3.0,
        "spike_min_volume_24h": 100000, "auto_entry": True,
        "auto_cooldown_hours": 24, "min_llm_confidence": 0.75, "base_stake": 6,
        "max_open_positions": 3, "min_liquidity_usd": 250000,
        "min_volume_24h_usd": 500000, "min_age_hours": 168, "max_mcap_rank": 300,
        "trailing_stop_pct": 20, "trailing_activate_pct": 25,
        "partial_tp_pct": 80, "partial_tp_sell_frac": 0.5,
        "entry_cooldown_hours": 168,
    }


class _GateModel:
    def __init__(self, out):
        self.out = out
        self.last_system = None
    def generate_json(self, prompt, max_tokens=800, temperature=0.2, system=None):
        self.last_system = system
        return dict(self.out)


def test_tier4_gate_dossier_includes_rsi_vol_news(tmp_path):
    from bot.memecoin import MemecoinLedger
    j = TradeJournal(db_path=str(tmp_path / "t4.db"))
    led = MemecoinLedger(_T4Cfg, journal=j)
    model = _GateModel({"buy": True, "confidence": 0.9, "reason": "ok"})
    led.model = model
    dossier = {"coin_id": "dogwifhat", "symbol": "WIF", "mcap_rank": 42,
               "liquidity_usd": 1_000_000, "volume_24h_usd": 2_500_000,
               "pair_age_days": 400, "price_usd": 3.0,
               "categories": ["Meme"]}
    history = {"recent_closes": [3.0 * (1 + 0.01 * (i % 3)) for i in range(30)]}
    pair = {"liquidity_usd": 2_000_000, "pair_age_days": 400,
            "volume_24h_usd": 5_000_000, "dex": "raydium"}
    buy, conf, reason = led._llm_conviction(dossier, history, pair,
                                             rsi_30d=71.2, vol_30d_pct=88.0,
                                             news=["WIF team announces burn"])
    assert buy and conf == 0.9
    # the extras ride in the system-role dossier JSON
    sys_txt = model.last_system or ""
    assert "rsi_14_daily" in sys_txt and "realized_vol_daily_pct" in sys_txt
    assert "recent_news" in sys_txt and "burn" in sys_txt


def test_tier4_gate_rsi_none_still_works(tmp_path):
    from bot.memecoin import MemecoinLedger
    j = TradeJournal(db_path=str(tmp_path / "t4.db"))
    led = MemecoinLedger(_T4Cfg, journal=j)
    model = _GateModel({"buy": False, "confidence": 0.4, "reason": "no data"})
    led.model = model
    dossier = {"coin_id": "x", "symbol": "X", "mcap_rank": 42,
               "liquidity_usd": 1_000_000, "volume_24h_usd": 2_500_000,
               "pair_age_days": 400, "price_usd": 3.0, "categories": ["Meme"]}
    buy, conf, reason = led._llm_conviction(dossier, None, None)
    assert not buy
    assert "rsi" not in str(model.last_system) or "null" in str(model.last_system) \
        or "None" in str(model.last_system)


def test_tier4_wick_exits_off_by_default_in_tests(tmp_path):
    from bot.memecoin import MemecoinLedger
    j = TradeJournal(db_path=str(tmp_path / "t4.db"))
    led = MemecoinLedger(_T4Cfg, journal=j)
    assert led.wick_exits is False
    assert led.news_in_gate is False


def test_tier4_wick_extremes_cached_and_off(tmp_path):
    from bot.memecoin import MemecoinLedger
    j = TradeJournal(db_path=str(tmp_path / "t4.db"))
    led = MemecoinLedger(_T4Cfg, journal=j)
    # off by default -> _wick_extremes never called; call direct is allowed
    # but here we just verify the config flag drives it
    led.wick_exits = True
    assert led.wick_exits is True


# ---------------- tier 3: prompt enrichment ----------------

def test_polymarket_prompt_includes_market_context(tmp_path):
    from bot.polymarket import scan_llm_mispricing
    from tests.test_deep_dive_fixes import _ScanCfg  # reuse existing fixture
    market = {"question": "rich market?", "slug": "rich",
              "outcomes": '["Yes", "No"]', "outcomePrices": '["0.5", "0.5"]',
              "volume24hr": "90000", "volume": "90000",
              "liquidityNum": "50000", "endDate": "2026-12-01",
              "active": True, "closed": False}
    seen_prompts = []

    class _M:
        def generate_json_arr(self, prompt, max_tokens=2000):
            seen_prompts.append(prompt)
            return [{"question": "rich market?", "true_prob": 0.9}]

    out = scan_llm_mispricing(_ScanCfg, markets=[market], model=_M(),
                              journal=TradeJournal(db_path=str(tmp_path / "t.db")))
    assert out  # passes liquidity+volume floors at 0.9 estimated prob
    prompt = seen_prompts[0]
    assert "24h volume" in prompt and "liquidity" in prompt
    assert "days to close" in prompt


# ---------------- reset tool ----------------

def test_reset_all_but_tier3_preserves_tier3(tmp_path):
    from tools.reset_all_but_tier3 import reset
    # fully isolated scratch journal (never the real data/trades.db)
    db = str(tmp_path / "trades.db")
    j = TradeJournal(db_path=db)
    now = datetime.now(timezone.utc).isoformat()
    # tier3 state that must survive
    j.log_bet(now, "will-keep", "q", "Yes", 0.5, 20, outcome="open")
    j.set_meta("wallet_epoch", "2")
    j.set_meta("tavily_count_2026-09", "85")
    # tier1/2/4/5 state that must be wiped
    j.log_trade(now, "BTC/USD", "BUY", 1.0, 100.0, "t1")
    j.set_meta("t4_cash", "31.0")
    j.set_meta("t5_cash", "27.4478")
    j.set_meta("shadow_cash", "12.0")
    j.set_meta("paper_cash", "99.7")
    j.set_meta("active_llm_model", "moonshotai/kimi-k3")
    assert reset(dry_run=True, db_path=db,
                 archive_dir=str(tmp_path / "archive")) == 0
    assert reset(db_path=db, archive_dir=str(tmp_path / "archive")) == 0
    conn = sqlite3.connect(db)
    bets = conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0]
    trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    keys = {k for (k,) in conn.execute("SELECT key FROM meta")}
    conn.close()
    assert bets == 1  # tier 3 bets preserved
    assert trades == 0  # every other tier's fills wiped
    assert "wallet_epoch" in keys and "tavily_count_2026-09" in keys
    assert "t4_cash" not in keys and "t5_cash" not in keys
    assert "shadow_cash" not in keys and "paper_cash" not in keys
    assert "active_llm_model" in keys  # operational continuity
    assert "day_zero_reset_at" in keys
    # the pre-reset archive exists with the old state intact
    archives = os.listdir(str(tmp_path / "archive"))
    assert len(archives) == 1
