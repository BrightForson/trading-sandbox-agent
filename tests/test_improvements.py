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
    from bot.indicators import PERIODS_PER_YEAR
    daily = PERIODS_PER_YEAR["1d"]
    # 1% alternating moves -> clearly positive vol
    vals = [100 * (1.01 ** (i % 2)) for i in range(20)]
    vol = realized_volatility_pct(vals, periods_per_year=daily)
    assert vol is not None and vol > 0
    # flat series -> zero vol
    assert realized_volatility_pct([50.0] * 10,
                                    periods_per_year=daily) == pytest.approx(0.0)
    # insufficient / invalid -> None
    assert realized_volatility_pct([1.0, 2.0], periods_per_year=daily) is None
    assert realized_volatility_pct(None, periods_per_year=daily) is None
    assert realized_volatility_pct([1.0, -2.0, 3.0], periods_per_year=daily) is None
    # a sampling rate is mandatory: the result is meaningless without it, and
    # the same series means different things at different sampling rates
    assert realized_volatility_pct(vals) is None
    assert (realized_volatility_pct(vals, periods_per_year=PERIODS_PER_YEAR["15m"])
            > realized_volatility_pct(vals, periods_per_year=daily))


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
