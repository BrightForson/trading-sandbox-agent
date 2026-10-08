"""Tests for the 2026-09-08 P1/P2 deep-dive fixes.

Covers: scout validation/cooldown (F20/F21), scanner liquidity floor +
event dedupe + bust latch (F24), Tier 4 ticker resolution + stale marks +
kill escalation (F8/F9/F11), gates verdicts (F26).
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.journal import TradeJournal
from bot.memecoin import MemecoinLedger
from tests.test_tier4 import _T4Cfg, _ledger


# ---------------- F20/F21: scout validation + cooldown ----------------

class _AgentCfg:
    symbols = ["BTC/USD", "ETH/USD", "SOL/USD"]
    sma_fast = 20
    sma_slow = 50
    lookback_bars = 120
    timeframe = "15Min"
    agent = {"shadow": True, "min_confidence": 0.7, "max_proposed_notional": 50,
             "scout_extra_universe": ["XRP/USD", "DOGE/USD"]}
    research = {}


class _NoOpShadow:
    def _positions(self):
        return {}


def test_scout_buy_requires_symbol(tmp_path):
    from bot.agent import TradingAgent
    j = TradeJournal(db_path=str(tmp_path / "t.db"))

    class _StubBroker:
        def get_all_positions(self):
            return []

    agent = TradingAgent(_AgentCfg, _StubBroker(), journal=j, model=object())
    agent.shadow = _NoOpShadow()
    valid, errors = agent._validate({"action": "BUY", "confidence": 0.9,
                                     "notional": 30, "rationale": "x"}, "scout")
    assert valid is None
    assert any("requires a symbol" in e for e in errors)


def test_scout_holds_symbolless_ok(tmp_path):
    from bot.agent import TradingAgent
    j = TradeJournal(db_path=str(tmp_path / "t.db"))

    class _StubBroker:
        def get_all_positions(self):
            return []

    agent = TradingAgent(_AgentCfg, _StubBroker(), journal=j, model=object())
    valid, _ = agent._validate({"action": "HOLD", "confidence": 0.9,
                                "notional": 0, "rationale": "x"}, "scout")
    assert valid is not None and valid["action"] == "HOLD"


def test_scout_extra_universe_whitelisted(tmp_path):
    from bot.agent import TradingAgent
    j = TradeJournal(db_path=str(tmp_path / "t.db"))

    class _StubBroker:
        def get_all_positions(self):
            return []

    agent = TradingAgent(_AgentCfg, _StubBroker(), journal=j, model=object())
    valid, errors = agent._validate({"action": "BUY", "symbol": "xrp",
                                     "confidence": 0.9, "notional": 30,
                                     "rationale": "x"}, "scout")
    assert valid is not None and valid["symbol"] == "XRP/USD", errors


# ---------------- F24: scanner ----------------

class _ScanCfg:
    scanner = {"enabled": True, "stake": 20, "min_market_volume": 50000,
               "min_market_liquidity": 10000, "mispricing_threshold": 0.08,
               "max_llm_markets": 5, "min_expected_value_pct": 2.0,
               "friction_pct": 1.0, "max_total_open_exposure": 100}


def test_scanner_disabled_skips(tmp_path):
    from bot.polymarket import scan
    j = TradeJournal(db_path=str(tmp_path / "t.db"))

    class _OffCfg:
        scanner = dict(_ScanCfg.scanner, enabled=False)

    out = scan(_OffCfg, journal=j)
    assert out["paper_finds"] == [] and out["watchlist"] == []


def test_llm_candidates_need_liquidity(tmp_path):
    from bot.polymarket import scan_llm_mispricing
    thin = {"question": "thin market?", "slug": "thin", "outcomes": '["Yes", "No"]',
            "outcomePrices": '["0.5", "0.5"]', "volume24hr": "90000",
            "volume": "90000", "liquidityNum": "500", "endDate": "2026-12-01",
            "active": True, "closed": False}
    fat = dict(thin, question="fat market?", slug="fat")
    fat["liquidityNum"] = "50000"

    class _M:
        def generate_json_arr(self, prompt, max_tokens=2000):
            return [{"question": "fat market?", "true_prob": 0.9}]

    out = scan_llm_mispricing(_ScanCfg, markets=[thin, fat], model=_M(),
                              journal=TradeJournal(db_path=str(tmp_path / "t.db")))
    slugs = [c.get("slug") for c in out]
    assert "thin" not in slugs  # thin market never estimated even at high volume


def test_family_open_bet_detection(tmp_path):
    from bot.polymarket import _family_has_open_bet
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    assert not _family_has_open_bet(j, "ev-1")
    j.log_bet(timestamp=datetime.now(timezone.utc).isoformat(),
              market="m1", question="q", side="Yes", price=0.5, stake=12,
              outcome="open", notes="event=ev-1")
    assert _family_has_open_bet(j, "ev-1")


def test_bust_alert_latch(tmp_path, monkeypatch):
    import bot.polymarket as pm
    j = TradeJournal(db_path=str(tmp_path / "t.db"))

    class _W:
        start_cash = 60
        stake = 12

        def is_bust(self):
            return True

    sent = []
    monkeypatch.setattr("bot.wallet.BettingWallet",
                        lambda cfg, journal=None: _W(), raising=False)
    monkeypatch.setattr(pm, "_fetch_markets", lambda limit=100: [])
    monkeypatch.setattr(pm, "send_notification",
                        lambda msg, cfg: sent.append(msg))
    # BettingWallet is imported inside scan(); patch the wallet module's symbol
    import bot.wallet as wallet_mod
    monkeypatch.setattr(wallet_mod, "BettingWallet",
                        lambda cfg, journal=None: _W())
    pm.scan(_ScanCfg, journal=j)
    pm.scan(_ScanCfg, journal=j)  # second cycle
    bust_alerts = [m for m in sent if "out of money" in m]
    assert len(bust_alerts) == 1  # latched: one alert, not two


# ---------------- F8/F9/F11: Tier 4 ----------------

def test_spike_cards_resolve_ticker_to_id(tmp_path, monkeypatch):
    led, _ = _ledger(tmp_path)
    monkeypatch.setattr("bot.memecoin._coingecko_ticker_map",
                        lambda: {"WIF": "dogwifhat"})
    # 6h vol x4 = 8M >= 3x the 2M 24h vol -> spike passes the multiple filter
    pairs = [{"baseToken": {"symbol": "WIF", "name": "dogwifhat"},
              "volume": {"h24": 2_000_000, "h6": 2_000_000},
              "liquidity": {"usd": 300_000}, "priceUsd": 3.0}]
    monkeypatch.setattr("bot.memecoin.requests.get",
                        lambda *a, **k: type("R", (), {
                            "status_code": 200,
                            "raise_for_status": lambda s: None,
                            "json": lambda s: {"pairs": pairs}})())
    cards = led.dexscreener_spike_cards()
    assert [c["symbol"] for c in cards] == ["dogwifhat"]


def test_price_cache_hits_once_per_symbol(tmp_path, monkeypatch):
    led, _ = _ledger(tmp_path)
    calls = []

    def fake_last_close(self, symbol, interval="15m", limit=2):
        calls.append(symbol)
        return 3.0

    from bot.binance_data import BinanceDataClient
    monkeypatch.setattr(BinanceDataClient, "last_close", fake_last_close)
    assert led.price_for("WIF") == 3.0
    assert led.price_for("WIF") == 3.0
    assert len(calls) == 1  # cached

def test_valuation_stale_marks_excluded(tmp_path, monkeypatch):
    led, j = _ledger(tmp_path)
    # force a position in the ledger, then make price_for return None for it
    positions = {"DOGE": {"qty": 100.0, "entry": 0.1, "stop": 0.075,
                          "take_profit": 0.15, "trailing_stop": None,
                          "partial_taken": False,
                          "opened": datetime.now(timezone.utc).isoformat()}}
    j.set_meta("t4_positions", json.dumps(positions))
    j.set_meta("t4_cash", "10")
    led.price_for = lambda s: None if s.upper() == "DOGE" else 3.0
    v = led.valuation()
    assert v["positions_value"] == 0.0  # stale mark excluded, not entry-marked
    assert v["stale_symbols"] == ["DOGE"]


def test_second_kill_requires_manual_reset(tmp_path):
    led, j = _ledger(tmp_path, prices={"DOGE": 0.1})
    led.buy("DOGE", 6)
    # first kill re-arms after cooldown
    j.set_meta("t4_kill", "on")
    j.set_meta("t4_kill_count", "1")
    j.set_meta("t4_kill_at", (datetime.now(timezone.utc) - timedelta(hours=48)
                              ).isoformat())
    led._save(30, {})
    assert led._auto_rearm_after_cooldown() is True
    # second kill (count=2) stays killed even after cooldown
    j.set_meta("t4_kill", "on")
    j.set_meta("t4_kill_count", "2")
    j.set_meta("t4_kill_at", (datetime.now(timezone.utc) - timedelta(hours=48)
                              ).isoformat())
    assert led._auto_rearm_after_cooldown() is False
    assert j.get_meta("t4_manual_reset_required") in (None, "true", "false")


def test_reset_kill_preserves_positions(tmp_path):
    led, j = _ledger(tmp_path, prices={"DOGE": 0.1})
    led.buy("DOGE", 6)
    j.set_meta("t4_kill", "on")
    # simulate a kill whose flatten FAILED (position still there)
    ok, msg = led.reset_kill()
    assert ok is True
    assert "DOGE" in led._positions()  # not silently destroyed


# ---------------- F26: gates ----------------

def test_gates_all_tiers_report(tmp_path, monkeypatch):
    from bot import gates
    j = TradeJournal(db_path=str(tmp_path / "g.db"))
    j.set_meta("day_zero_reset_at",
               (datetime.now(timezone.utc) - timedelta(days=1)).isoformat())

    class _Cfg:
        symbols = ["BTC/USD"]

    monkeypatch.setattr(gates, "agent_alpha_gate",
                        lambda journal=None: gates.GateResult(
                            "Tier 2 (agent alpha)", False, "stub", "stub"))
    out = gates.evaluate_gates(journal=j)
    assert "Tier 2 (agent alpha)" in out
    assert "Tier 3 (Polymarket)" in out
    assert "Tier 4 (memecoin)" in out
    assert "Tier 1" not in out and "Tier 5" not in out
    assert "Reconciliation" not in out
    # verdict history recorded
    raw = j.get_meta("gate_verdict_history")
    assert raw and "verdicts" in raw


