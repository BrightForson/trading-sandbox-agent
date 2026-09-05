import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.strategy import check_crossover
from bot.strategies import get_strategies, sma_cross
from bot.risk import RiskEngine
from bot.journal import TradeJournal
from bot.models import ModelManager


# ---------------- strategy ----------------

def _make_df(closes):
    return pd.DataFrame({"close": closes})


def test_crossover_golden():
    # flat 50 bars then one uptick: SMA20 crosses above SMA50 on the final bar
    closes = [10.0] * 50 + [20.0]
    df = _make_df(closes)
    signal, *_ = check_crossover(df, 20, 50)
    assert signal == "golden"


def test_crossover_death():
    closes = [20.0] * 50 + [10.0]
    df = _make_df(closes)
    signal, *_ = check_crossover(df, 20, 50)
    assert signal == "death"


def test_crossover_none_when_flat():
    closes = [10.0] * 60
    df = _make_df(closes)
    signal, *_ = check_crossover(df, 20, 50)
    assert signal is None


def test_crossover_insufficient_data():
    df = _make_df([10.0] * 10)
    signal, *_ = check_crossover(df, 20, 50)
    assert signal is None


class _FakeCfg:
    sma_fast = 20
    sma_slow = 50
    notional = 100


def test_registry_sma_cross_buy_signal():
    df = _make_df([10.0] * 50 + [20.0])
    sigs = sma_cross("BTC/USD", df, _FakeCfg())
    assert len(sigs) == 1 and sigs[0]["action"] == "BUY"


def test_registry_resolves_and_skips_unknown():
    out = get_strategies(["sma_cross", "nope"])
    assert len(out) == 1 and out[0][0] == "sma_cross"


# ---------------- risk ----------------

class _FakeBrokerAcct:
    class _Acct:
        equity = "100"

    trading_client = None

    def __init__(self):
        class TC:
            def get_account(self):
                return _FakeBrokerAcct._Acct()

        self.trading_client = TC()


def test_risk_blocks_oversized_notional(tmp_path):
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    cfg = type("C", (), {"risk": {"max_notional_per_trade": 100, "max_open_positions": 3,
                                  "daily_loss_limit_pct": 5.0}})()
    eng = RiskEngine(cfg, _FakeBrokerAcct(), journal=j)
    ok, reason = eng.check("BTC/USD", "BUY", 0.01, 20000, 0)  # $200 notional
    assert not ok and "exceeds cap" in reason


def test_risk_blocks_too_many_positions(tmp_path):
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    cfg = type("C", (), {"risk": {"max_notional_per_trade": 100, "max_open_positions": 2,
                                  "daily_loss_limit_pct": 5.0}})()
    eng = RiskEngine(cfg, _FakeBrokerAcct(), journal=j)
    ok, reason = eng.check("BTC/USD", "BUY", 0.001, 500, 2)  # already at cap
    assert not ok and "positions" in reason


def test_risk_sell_always_allowed(tmp_path):
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    j.set_meta("kill_switch", "on")
    cfg = type("C", (), {"risk": {"max_notional_per_trade": 100}})()
    eng = RiskEngine(cfg, _FakeBrokerAcct(), journal=j)
    ok, _ = eng.check("BTC/USD", "SELL", 1.0, 100, 99)
    assert ok


def test_risk_kill_switch_blocks_buys(tmp_path):
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    j.set_meta("kill_switch", "on")
    cfg = type("C", (), {"risk": {"max_notional_per_trade": 100}})()
    eng = RiskEngine(cfg, _FakeBrokerAcct(), journal=j)
    ok, reason = eng.check("BTC/USD", "BUY", 0.001, 100, 0)
    assert not ok and "kill switch" in reason


def test_risk_allows_normal_buy(tmp_path):
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    cfg = type("C", (), {"risk": {"max_notional_per_trade": 100, "max_open_positions": 3,
                                  "daily_loss_limit_pct": 5.0}})()
    eng = RiskEngine(cfg, _FakeBrokerAcct(), journal=j)
    ok, reason = eng.check("BTC/USD", "BUY", 0.001, 100, 1)
    assert ok, reason


# ---------------- journal ----------------

def test_journal_proposals_and_bets(tmp_path):
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    j.log_proposal("2026-09-05T00:00:00", "ai_agent", "scout", "BTC/USD", "BUY",
                   50, 0.8, "test rationale")
    props = j.get_proposals()
    # columns: id, timestamp, source, kind, symbol, action, notional, confidence, rationale, exec_status
    assert len(props) == 1 and props[0][5] == "BUY"
    j.update_proposal_exec(props[0][0], "paper_filled", "2026-09-05T01:00:00", 60000.0, 12.5)
    props = j.get_proposals()
    assert props[0][9] == "paper_filled"

    j.log_bet("2026-09-05T00:00:00", "test-market", "Will X happen?", "Yes", 0.97, 20)
    bets = j.get_open_bets()
    assert len(bets) == 1
    j.update_bet(bets[0][0], "won", 20.62)
    assert j.get_open_bets() == []


# ---------------- shadow account ----------------

class _FakeBrokerPrices:
    """Yields a fixed price per symbol via a stub bar frame."""
    def __init__(self, prices):
        self.prices = prices

    def get_crypto_bars(self, symbol, timeframe, limit):
        import pandas as pd
        price = self.prices[symbol]
        return pd.DataFrame({"close": [price] * 60})


def test_shadow_account_lifecycle(tmp_path):
    from bot.shadow import ShadowAccount

    class _Cfg:
        symbols = ["BTC/USD", "ETH/USD", "SOL/USD"]
        timeframe = "15Min"
        lookback_bars = 120
        agent = {"shadow_start_cash": 20, "shadow_max_per_position": 10}

    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    broker = _FakeBrokerPrices({"SOL/USD": 100.0, "BTC/USD": 50000.0})
    s = ShadowAccount(_Cfg(), broker, journal=j)

    assert "Shadow account: $20.00" in s.status_line()

    # diversified sizing: cap at $10 even if $15 requested
    ok, note = s.take_buy("SOL/USD", 15)
    assert ok and "$10.00" in note
    equity, cash, _ = s.mark_to_market()
    assert cash == 10.0

    # duplicate symbol blocked
    ok, note = s.take_buy("SOL/USD", 5)
    assert not ok and "already holding" in note

    # price move up -> unrealized profit (price 100 -> 120)
    broker.prices["SOL/USD"] = 120.0
    equity, _, block = s.mark_to_market()
    assert equity > 20.0

    # sell realizes the P&L
    ok, note = s.take_sell("SOL/USD")
    assert ok and "+$19" not in note  # just ensure it executed
    # qty 0.1 @ entry 100 -> sold 120 = +$2.00 profit
    assert s.realized_pnl() == 2.0
    assert s._cash() == 10.0 + 12.0
    assert "flat" in s.status_line()


def test_shadow_account_sizing_caps(tmp_path):
    from bot.shadow import ShadowAccount

    class _Cfg:
        symbols = ["BTC/USD", "ETH/USD", "SOL/USD"]
        timeframe = "15Min"
        lookback_bars = 120
        agent = {"shadow_start_cash": 20, "shadow_max_per_position": 10}

    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    broker = _FakeBrokerPrices({"BTC/USD": 100.0, "ETH/USD": 100.0, "SOL/USD": 100.0})
    s = ShadowAccount(_Cfg(), broker, journal=j)

    # fill the account: 2 positions at $10 each exhausts cash
    ok1, _ = s.take_buy("BTC/USD", 10)
    ok2, _ = s.take_buy("ETH/USD", 10)
    ok3, note = s.take_buy("SOL/USD", 10)
    assert ok1 and ok2
    assert not ok3 and "insufficient" in note


# ---------------- model manager ----------------

def test_json_parsing_variants():
    assert ModelManager._parse_json('{"a": 1}') == {"a": 1}
    assert ModelManager._parse_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert ModelManager._parse_json('Sure! {"a": 3} hope that helps') == {"a": 3}
    arr = ModelManager._parse_json_arr('[{"q": "x", "p": 0.5}]')
    assert arr[0]["p"] == 0.5
    with pytest.raises(Exception):
        ModelManager._parse_json("no json here")


# ---------------- agent validation ----------------

def test_agent_proposal_validation(tmp_path):
    from bot.agent import TradingAgent

    class _Cfg:
        symbols = ["BTC/USD", "ETH/USD"]
        agent = {"max_proposed_notional": 50, "min_confidence": 0.7}
        research = {}

    class _B:
        pass

    a = TradingAgent.__new__(TradingAgent)  # skip __init__ (no LLM needed)
    a.cfg = _Cfg
    a.agent_cfg = _Cfg.agent
    a.symbols = _Cfg.symbols

    ok, errs = a._validate({"action": "BUY", "symbol": "BTC/USD", "notional": 30,
                            "confidence": 0.8, "rationale": "r"}, "scout")
    assert ok is not None and errs == []

    ok, errs = a._validate({"action": "BUY", "symbol": "DOGE/USD", "notional": 30,
                            "confidence": 0.8, "rationale": "r"}, "scout")
    assert ok is None and any("whitelist" in e for e in errs)

    ok, errs = a._validate({"action": "BUY", "symbol": "BTC/USD", "notional": 500,
                            "confidence": 0.8, "rationale": "r"}, "scout")
    assert ok is None and any("cap" in e for e in errs)
