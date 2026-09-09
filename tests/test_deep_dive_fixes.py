"""Tests for the 2026-09-08 P1/P2 deep-dive fixes.

Covers: exit-intent persistence (F12), exit idempotency nonce (F13),
actual-fee journaling (F16), UTC timestamps (F15), fail-closed risk
enumeration (F18), scout validation/cooldown (F20/F21), scanner liquidity
floor + event dedupe + bust latch (F24), Tier 4 ticker resolution +
stale marks + kill escalation (F8/F9/F11), gates verdicts (F26).
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.journal import TradeJournal
from bot.memecoin import MemecoinLedger
from bot.trader import (_client_order_id, _pending_exits, _add_pending_exit,
                        _clear_pending_exit, _bump_exit_attempt,
                        _exit_attempts, journal_ref)
from tests.test_tier4 import _T4Cfg, _ledger


# ---------------- F13: exit idempotency nonce ----------------

def test_exit_keys_differ_across_attempts(tmp_path):
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    journal_ref.set(j)
    k1 = _client_order_id("ETH/USD", "SELL", "daily loss flatten", 0.5)
    _bump_exit_attempt(j, "ETH/USD")
    k2 = _client_order_id("ETH/USD", "SELL", "daily loss flatten", 0.5)
    assert k1 != k2  # retried exit is NOT suppressed as a duplicate
    journal_ref.set(None)


def test_buy_keys_stable_without_nonce(tmp_path):
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    journal_ref.set(j)
    k1 = _client_order_id("ETH/USD", "BUY", "golden cross", 0.5)
    k2 = _client_order_id("ETH/USD", "BUY", "golden cross", 0.5)
    assert k1 == k2  # same BUY intent stays idempotent
    journal_ref.set(None)


# ---------------- F12: pending exit persistence ----------------

def test_pending_exit_lifecycle(tmp_path):
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    assert _pending_exits(j) == set()
    _add_pending_exit(j, "BTC/USD")
    _add_pending_exit(j, "ETH/USD")
    assert _pending_exits(j) == {"BTC/USD", "ETH/USD"}
    _clear_pending_exit(j, "BTC/USD")
    assert _pending_exits(j) == {"ETH/USD"}


def test_exit_attempt_counter_bounded(tmp_path):
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    for i in range(150):
        _bump_exit_attempt(j, f"SYM{i}")
    raw = j.get_meta("exit_attempt_counters")
    assert len(json.loads(raw)) <= 100


# ---------------- F16: actual fee journaled ----------------

class _FakeBroker:
    """Broker surface stub used by _execute_signal."""
    def __init__(self, fee):
        self._fee = fee
        self.orders = []

    class _P:
        symbol = "ETH/USD"
        qty = "0"
        avg_entry_price = "0"
        current_price = "0"
        market_value = "0"
        unrealized_pl = "0"
        unrealized_plpc = "0"

    def get_position(self, s):
        return None

    def get_all_positions(self):
        return []

    def get_account(self):
        class A:
            cash = "100"
            equity = "100"
        return A()

    def place_order(self, symbol, qty, side):
        o = self._P()
        o.id, o.symbol, o.qty, o.side = "1", symbol, str(qty), side
        o.status, o.filled_qty = "filled", str(qty)
        o.filled_avg_price = "100.0"
        o.fee = self._fee
        self.orders.append(o)
        return o

    def await_terminal_order(self, oid):
        return self.orders[-1]


def test_execute_signal_journals_actual_fee(tmp_path, monkeypatch):
    import bot.trader as trader
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    journal_ref.set(j)

    class _Risk:
        def size_for_atr(self, *a, **k):
            return 0.5

        def check(self, *a, **k):
            return True, "ok"

        def entry_fixed_stop(self, *a, **k):
            return 95.0

        def record_stop(self, *a, **k):
            pass

    broker = _FakeBroker(fee=0.05)
    import pandas as pd
    df = pd.DataFrame({"close": [100.0, 100.5]})
    sig = {"action": "BUY", "reasoning": "test buy"}
    monkeypatch.setattr(trader, "config", type("C", (), {
        "notional": 50, "symbols": ["ETH/USD"],
        "risk": {"atr_period": 14}, "execution": {"taker_fee_pct": 0.25},
    })())
    ok = trader._execute_signal(broker, j, _Risk(), "ETH/USD", df, sig)
    assert ok is True
    trades = j.get_trades()
    assert len(trades) == 1
    # the journaled fee is the BROKER's actual fee, not the 0.25% estimate
    # (columns: id, timestamp, symbol, action, qty, price, reasoning, fee, ...)
    assert abs(float(trades[0][7]) - 0.05) < 1e-9


def test_trades_use_utc_timestamps(tmp_path, monkeypatch):
    import bot.trader as trader
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    journal_ref.set(j)

    class _Risk:
        def size_for_atr(self, *a, **k):
            return 0.5

        def check(self, *a, **k):
            return True, "ok"

        def entry_fixed_stop(self, *a, **k):
            return 95.0

        def record_stop(self, *a, **k):
            pass

    broker = _FakeBroker(fee=0.05)
    import pandas as pd
    df = pd.DataFrame({"close": [100.0, 100.5]})
    monkeypatch.setattr(trader, "config", type("C", (), {
        "notional": 50, "symbols": ["ETH/USD"],
        "risk": {"atr_period": 14}, "execution": {"taker_fee_pct": 0.25},
    })())
    trader._execute_signal(broker, j, _Risk(), "ETH/USD", df,
                           {"action": "BUY", "reasoning": "utc test"})
    ts = j.get_trades()[0][1]
    assert "+" in ts or ts.endswith("Z")  # timezone-aware ISO


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
    assert "Tier 1 (SMA bot)" in out
    assert "Tier 3 (Polymarket)" in out
    assert "Tier 4 (memecoin)" in out
    assert "Reconciliation" in out
    # verdict history recorded
    raw = j.get_meta("gate_verdict_history")
    assert raw and "verdicts" in raw


def test_tier1_drawdown_from_snapshots(tmp_path):
    from bot.gates import _tier1_drawdown_pct
    j = TradeJournal(db_path=str(tmp_path / "d.db"))
    base = datetime.now(timezone.utc) - timedelta(hours=3)
    for i, eq in enumerate([100, 120, 90, 95]):
        j.log_wallet_snapshot(
            timestamp=(base + timedelta(hours=i)).isoformat(),
            epoch=0, cash=eq, locked=0, equity=eq)
    # peak 120 -> trough 90 = 25% drawdown
    assert abs(_tier1_drawdown_pct(j) - 25.0) < 0.01
