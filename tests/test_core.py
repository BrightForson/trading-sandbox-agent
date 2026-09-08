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
from bot.polymarket import _expected_value
from backtest import simulate


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


def test_risk_blocks_crypto_allocation_cap(tmp_path):
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    cfg = type("C", (), {"risk": {"max_notional_per_trade": 100,
                                   "max_crypto_allocation_pct": 15}})()
    eng = RiskEngine(cfg, _FakeBrokerAcct(), journal=j)
    ok, reason = eng.check("BTC/USD", "BUY", 0.1, 100, 0,
                           current_crypto_notional=10, account_equity=100)
    assert not ok and "allocation" in reason


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


def test_prediction_expected_value_includes_friction():
    ev = _expected_value(stake=20, price=0.50, probability=0.55, friction_pct=1.0)
    assert ev is not None
    assert ev["fee"] == 0.2
    assert ev["expected_value"] == pytest.approx(1.8)
    assert _expected_value(20, 1.0, 0.6, 1.0) is None


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
    a.scout_symbols = _Cfg.symbols

    ok, errs = a._validate({"action": "BUY", "symbol": "BTC/USD", "notional": 30,
                            "confidence": 0.8, "rationale": "r"}, "scout")
    assert ok is not None and errs == []

    ok, errs = a._validate({"action": "BUY", "symbol": "DOGE/USD", "notional": 30,
                            "confidence": 0.8, "rationale": "r"}, "scout")
    assert ok is None and any("whitelist" in e for e in errs)

    ok, errs = a._validate({"action": "BUY", "symbol": "BTC/USD", "notional": 500,
                            "confidence": 0.8, "rationale": "r"}, "scout")
    assert ok is None and any("cap" in e for e in errs)


def test_agent_validation_role_restrictions(tmp_path):
    from bot.agent import TradingAgent

    class _Cfg:
        symbols = ["BTC/USD", "ETH/USD"]
        agent = {"max_proposed_notional": 50, "min_confidence": 0.7}
        research = {}

    a = TradingAgent.__new__(TradingAgent)
    a.cfg = _Cfg
    a.agent_cfg = _Cfg.agent
    a.symbols = _Cfg.symbols
    a.scout_symbols = _Cfg.symbols

    # scout may not propose SELL
    ok, errs = a._validate({"action": "SELL", "symbol": "BTC/USD", "notional": 0,
                            "confidence": 0.8, "rationale": "r"}, "scout")
    assert ok is None and any("scout" in e for e in errs)

    # babysitter may not propose BUY
    ok, errs = a._validate({"action": "BUY", "symbol": "BTC/USD", "notional": 30,
                            "confidence": 0.8, "rationale": "r"}, "babysitter")
    assert ok is None and any("babysitter" in e for e in errs)

    # confidence bounds enforced
    ok, errs = a._validate({"action": "BUY", "symbol": "BTC/USD", "notional": 30,
                            "confidence": 1.5, "rationale": "r"}, "scout")
    assert ok is None and any("confidence" in e for e in errs)


# ---------------- backtest simulate ----------------

def _backtest_df(closes):
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="15min", tz="UTC")
    return pd.DataFrame({"open": [c * 1.0 for c in closes], "close": closes}, index=idx)


def test_backtest_no_all_in_sizing():
    # $10k capital, notional 100, cap 100: a BUY must deploy ~$100, not the account
    closes = [10.0] * 49 + [10.0, 20.0, 20.0, 20.0, 20.0]
    df = _backtest_df(closes)
    stats = simulate(df, 3, 5, notional=10_000, taker_fee_pct=0.25,
                     slippage_bps=8, max_notional_per_trade=100)
    buys = [t for t in stats["trades"] if t["action"] == "BUY"]
    assert buys, "expected at least one BUY"
    for b in buys:
        assert b["qty"] * b["price"] == pytest.approx(100.0 / (1 + 0.0025), rel=0.01)


def test_backtest_fixed_notional_does_not_compound():
    # repeated round trips at rising prices must not grow the per-trade budget
    base = [10.0] * 5
    # sawtooth: up-cross then down-cross, thrice
    closes = base + [10.0, 12.0, 12.0, 11.0, 11.0, 12.0, 12.0, 11.0, 11.0, 12.0]
    df = _backtest_df(closes)
    stats = simulate(df, 3, 5, notional=100, max_notional_per_trade=100)
    buys = [t for t in stats["trades"] if t["action"] == "BUY"]
    assert len(buys) >= 2
    notionals = [b["qty"] * b["price"] for b in buys]
    for n in notionals[1:]:
        assert n == pytest.approx(notionals[0], rel=0.001)


def test_backtest_next_bar_fill_no_lookahead():
    # signal on bar i, fill at bar i+1's open: with a gap up, entry price is the
    # next open (plus slippage), never the signal bar's close
    closes = [10.0] * 6 + [20.0, 20.0]
    opens = [10.0] * 7 + [30.0]
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="15min", tz="UTC")
    df = pd.DataFrame({"open": opens, "close": closes}, index=idx)
    stats = simulate(df, 3, 5, notional=100)
    buys = [t for t in stats["trades"] if t["action"] == "BUY"]
    assert buys, "expected a BUY"
    assert buys[0]["price"] == pytest.approx(30.0)


def test_backtest_fees_slippage_applied():
    closes = [10.0] * 5 + [10.0, 20.0, 20.0]
    opens = [10.0] * 5 + [10.0, 10.0, 20.0]
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="15min", tz="UTC")
    df = pd.DataFrame({"open": opens, "close": closes}, index=idx)
    stats = simulate(df, 3, 5, notional=100, taker_fee_pct=1.0, slippage_bps=100)
    assert stats["fees"] > 0
    buys = [t for t in stats["trades"] if t["action"] == "BUY"]
    # entry slippage pushes the fill above the 10.0 open
    assert buys[0]["price"] > 10.0


def test_backtest_insufficient_data_returns_zero():
    stats = simulate(_backtest_df([10.0] * 3), 3, 5, notional=100)
    assert stats["round_trips"] == 0 and stats["pnl"] == 0.0


# ---------------- ATR sizing and stops ----------------

def test_size_for_atr_risk_capped(tmp_path):
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    cfg = type("C", (), {"risk": {"max_notional_per_trade": 100,
                                   "target_risk_pct_per_trade": 0.5,
                                   "catastrophic_atr_multiple": 3.0}})()
    eng = RiskEngine(cfg, _FakeBrokerAcct(), journal=j)
    # equity 100 -> risk budget $0.50; stop distance 3*ATR(0.5)=$1.5 at price 100
    # -> target notional 0.5/1.5*100 = $33.33 < fallback 100 and cap 100
    qty = eng.size_for_atr(price=100.0, atr=0.5, cash=100.0, fallback_notional=100.0)
    assert qty * 100.0 == pytest.approx(0.5 / 1.5 * 100.0, rel=0.001)


def test_size_for_atr_cash_bound(tmp_path):
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    cfg = type("C", (), {"risk": {"max_notional_per_trade": 100}})()
    eng = RiskEngine(cfg, _FakeBrokerAcct(), journal=j)
    qty = eng.size_for_atr(price=100.0, atr=None, cash=10.0, fallback_notional=100.0)
    # no ATR -> fallback notional, but never more than 98% of cash
    assert qty * 100.0 == pytest.approx(9.8)


def test_size_for_atr_zero_price(tmp_path):
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    eng = RiskEngine(type("C", (), {"risk": {}})(), _FakeBrokerAcct(), journal=j)
    assert eng.size_for_atr(0.0, 1.0, 100.0, 100.0) == 0.0


def test_atr_stop_triggered_threshold(tmp_path):
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    cfg = type("C", (), {"risk": {"catastrophic_atr_multiple": 3.0}})()
    eng = RiskEngine(cfg, _FakeBrokerAcct(), journal=j)
    # stop = 100 - 3*1 = 97
    hit, msg = eng.atr_stop_triggered(100.0, 96.9, 1.0)
    assert hit and "stop" in msg.lower()
    hit, _ = eng.atr_stop_triggered(100.0, 97.1, 1.0)
    assert not hit
    # missing ATR -> never triggers
    hit, msg = eng.atr_stop_triggered(100.0, 10.0, None)
    assert not hit and "unavailable" in msg.lower()


# ---------------- allocation cap fail-closed ----------------

def test_risk_allocation_cap_fails_closed_without_equity(tmp_path):
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    cfg = type("C", (), {"risk": {"max_notional_per_trade": 100,
                                   "max_crypto_allocation_pct": 15}})()

    class _BrokenBroker:
        trading_client = None

        def __init__(self):
            class TC:
                def get_account(self):
                    raise RuntimeError("api down")
            self.trading_client = TC()

    eng = RiskEngine(cfg, _BrokenBroker(), journal=j)
    ok, reason = eng.check("BTC/USD", "BUY", 0.001, 100, 0, account_equity=None)
    assert not ok and "unreadable" in reason


def test_risk_allocation_cap_explicit_equity(tmp_path):
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    cfg = type("C", (), {"risk": {"max_notional_per_trade": 100,
                                   "max_crypto_allocation_pct": 15}})()
    eng = RiskEngine(cfg, _FakeBrokerAcct(), journal=j)
    # 10 + 5 = 15% of 100: at the cap, not over it
    ok, _ = eng.check("BTC/USD", "BUY", 0.05, 100, 0,
                      current_crypto_notional=10, account_equity=100)
    assert ok
    # 10 + 6 = 16% > cap
    ok, _ = eng.check("BTC/USD", "BUY", 0.06, 100, 0,
                      current_crypto_notional=10, account_equity=100)
    assert not ok


# ---------------- journal non-fill status and P&L ----------------

def test_journal_nonfill_rows_excluded_from_pnl():
    from bot.report import compute_pnl_and_winrate
    # filled BUY then a journaled canceled BUY must not create phantom exposure
    trades = [
        (1, "2026-09-05T10:00", "BTC/USD", "BUY", 0.01, 100.0, "r", 0.02, "o1", "filled"),
        (2, "2026-09-05T10:05", "BTC/USD", "BUY", 0.02, 101.0, "canceled buy", 0.0, "o2", "canceled"),
        (3, "2026-09-05T11:00", "BTC/USD", "SELL", 0.01, 110.0, "r", 0.02, "o3", "filled"),
    ]
    stats = compute_pnl_and_winrate(trades)
    assert stats["round_trips"] == 1
    # (110-100)*0.01 - 0.02 - 0.02 = 0.06
    assert stats["total_pnl"] == pytest.approx(0.06)


def test_journal_records_status(tmp_path):
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    j.log_trade("2026-09-05T10:00", "BTC/USD", "BUY", 0.01, 100.0, "r",
                fee=0.02, order_id="o9", status="canceled")
    trades = j.get_trades()
    assert trades[0][9] == "canceled" and trades[0][8] == "o9"


# ---------------- entry-fixed stop ledger ----------------

def _stop_engine(tmp_path, risk=None):
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    cfg = type("C", (), {"risk": risk or {"catastrophic_atr_multiple": 3.0,
                                          "fallback_stop_pct": 5.0}})()
    return RiskEngine(cfg, _FakeBrokerAcct(), journal=j), j


def test_stop_recorded_fixed_and_persistent(tmp_path):
    eng, _ = _stop_engine(tmp_path)
    stop = eng.entry_fixed_stop("BTC/USD", 100.0, atr=2.0)
    assert stop == pytest.approx(94.0)  # 100 - 3*2
    eng.record_stop("BTC/USD", 100.0, stop)
    # level survives a reload (journal meta persistence)
    entry, level = eng.get_stop("BTC/USD")
    assert entry == 100.0 and level == pytest.approx(94.0)
    # the level never drifts with later volatility: check at 93.9 triggers
    hit, msg = eng.stop_triggered("BTC/USD", 93.9)
    assert hit and "stop" in msg.lower()
    # and a bounce off the SAME current-ATR does not move the stop
    entry, level_after = eng.get_stop("BTC/USD")
    assert level_after == pytest.approx(94.0)


def test_stop_fallback_without_atr(tmp_path):
    eng, _ = _stop_engine(tmp_path)
    stop = eng.entry_fixed_stop("ETH/USD", 200.0, atr=None)
    assert stop == pytest.approx(190.0)  # 5% below entry
    eng.record_stop("ETH/USD", 200.0, stop)
    hit, _ = eng.stop_triggered("ETH/USD", 189.9)
    assert hit
    hit, _ = eng.stop_triggered("ETH/USD", 190.1)
    assert not hit


def test_stop_cleared_on_exit(tmp_path):
    eng, _ = _stop_engine(tmp_path)
    eng.record_stop("SOL/USD", 50.0, 45.0)
    eng.clear_stop("SOL/USD")
    entry, level = eng.get_stop("SOL/USD")
    assert entry is None and level is None
    hit, msg = eng.stop_triggered("SOL/USD", 10.0)  # crash after exit: no stale stop
    assert not hit and "no stop" in msg


def test_stop_never_triggers_on_suspect_mark(tmp_path):
    eng, _ = _stop_engine(tmp_path)
    eng.record_stop("BTC/USD", 100.0, 94.0)
    # a 50x-above-entry broker mark is bad data, not a crash: never act on it
    hit, msg = eng.stop_triggered("BTC/USD", 6000.0)
    assert not hit and "suspect" in msg.lower()


def test_stops_isolated_per_symbol(tmp_path):
    eng, _ = _stop_engine(tmp_path)
    eng.record_stop("BTC/USD", 100.0, 94.0)
    eng.record_stop("ETH/USD", 200.0, 185.0)
    eng.clear_stop("BTC/USD")
    entry, level = eng.get_stop("ETH/USD")
    assert entry == 200.0 and level == 185.0  # ETH stop untouched


# ---------------- backtest stop simulation ----------------

def test_backtest_stop_exit_fills_next_bar_open():
    # downtrend then uptrend -> golden cross at bar 5 -> BUY fills bar 6 open (10.0).
    # the crash bar's close (6.0) triggers the entry-fixed stop and the exit
    # must fill at the NEXT bar's open (7.0), never the crash close
    closes = [9.0] * 5 + [10.0] * 3 + [6.0] * 3
    opens = [9.0] * 5 + [10.0] * 3 + [7.0] * 3
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="15min", tz="UTC")
    df = pd.DataFrame({"open": opens, "close": closes,
                       "high": closes, "low": closes}, index=idx)
    stats = simulate(df, 3, 5, notional=100, atr_period=3,
                     catastrophic_atr_multiple=3.0)
    stop_sells = [t for t in stats["trades"] if t["action"] == "SELL(stop)"]
    assert stop_sells, "expected a stop exit"
    assert stats["stop_exits"] == 1
    buys = [t for t in stats["trades"] if t["action"] == "BUY"]
    assert buys[0]["price"] == pytest.approx(10.0)
    # stop exit fills at the next bar's open (7.0), never the crash close (6.0)
    assert stop_sells[0]["price"] == pytest.approx(7.0)


def test_backtest_stop_level_fixed_at_entry():
    # entry-fixed stop = 10 - 3*0.333 = 9.0 (small pre-entry ranges).
    # AFTER entry the true range explodes (lows crash to 1.0): a stop that
    # floated with current ATR would drop far below 9.0 and never trigger.
    # The entry-fixed stop must stay at 9.0 and exit when close breaches it.
    closes = [9.0] * 5 + [10.0] * 3 + [8.9] * 3
    opens = [9.0] * 5 + [10.0] * 3 + [8.9] * 3
    lows = [9.0] * 5 + [10.0] * 3 + [1.0] * 3  # volatility spike after entry only
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="15min", tz="UTC")
    df = pd.DataFrame({"open": opens, "close": closes,
                       "high": closes, "low": lows}, index=idx)
    stats = simulate(df, 3, 5, notional=100, atr_period=3,
                     catastrophic_atr_multiple=3.0)
    stop_sells = [t for t in stats["trades"] if t["action"] == "SELL(stop)"]
    assert stop_sells, "8.9 must breach the entry-fixed stop despite later spikes"
    # exit fills at the next bar's open after the breach
    assert stop_sells[0]["price"] == pytest.approx(8.9)
    # with no stop modelled, the same series must NOT exit via stop
    stats_nostop = simulate(df, 3, 5, notional=100)
    assert stats_nostop["stop_exits"] == 0


# ---------------- binance paper broker ----------------

class _PaperCfg:
    symbols = ["BTC/USD", "ETH/USD", "SOL/USD"]
    timeframe = "15Min"
    lookback_bars = 120
    execution = {"taker_fee_pct": 0.1, "slippage_bps": 8}
    broker = {"name": "binance_paper", "paper": {"start_cash": 20},
              "taker_fee_pct": 0.1, "slippage_bps": 8}


class _StubData:
    """Priced binance-paper broker without network: fixed closes per symbol."""

    def __init__(self, prices):
        self.prices = prices

    def last_close(self, symbol, interval="15m", limit=2):
        return self.prices[symbol]

    def get_crypto_bars(self, symbol, timeframe, limit):
        price = self.prices[symbol]
        return pd.DataFrame({"open": [price] * 60, "high": [price] * 60,
                             "low": [price] * 60, "close": [price] * 60,
                             "volume": [0.0] * 60})


def _paper_broker(tmp_path, prices=None):
    from bot.binance_paper import BinancePaperBroker
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    b = BinancePaperBroker(_PaperCfg, journal=j)
    b.data = _StubData(prices or {"BTC/USD": 100.0, "ETH/USD": 50.0, "SOL/USD": 10.0})
    return b, j


def test_paper_broker_fill_fee_and_persistence(tmp_path):
    b, _ = _paper_broker(tmp_path, {"BTC/USD": 100.0, "ETH/USD": 50.0, "SOL/USD": 10.0})
    acct = b.get_account()
    assert float(acct.cash) == 20.0 and float(acct.equity) == 20.0

    o = b.place_order("BTC/USD", 0.05, "BUY")
    assert o.status == "filled"
    # fill = 100 * (1 + 8bps) = 100.08
    assert float(o.filled_avg_price) == pytest.approx(100.08)
    # cash = 20 - 0.05*100.08*(1.001) = 20 - 5.009...
    expected_cash = 20.0 - 0.05 * 100.08 * 1.001
    assert float(b.get_account().cash) == pytest.approx(expected_cash)

    pos = b.get_position("BTC/USD")
    assert float(pos.qty) == pytest.approx(0.05)
    assert float(pos.avg_entry_price) == pytest.approx(100.08)

    # persistence across a fresh instance on the same journal
    from bot.binance_paper import BinancePaperBroker
    b2 = BinancePaperBroker(_PaperCfg, journal=b.journal)
    b2.data = _StubData({"BTC/USD": 100.0})
    assert float(b2.get_account().cash) == pytest.approx(expected_cash)
    assert float(b2.get_position("BTC/USD").qty) == pytest.approx(0.05)

    # sell back: proceeds - fee, position cleared
    o2 = b.place_order("BTC/USD", 0.05, "SELL")
    assert float(o2.filled_avg_price) == pytest.approx(100.0 * (1 - 8e-4))
    assert b.get_position("BTC/USD") is None
    # round-trip drag = 2 * (fee 0.1% + slippage 8bps) of $5 each side
    drag = 20.0 - float(b.get_account().cash)
    assert drag == pytest.approx(5.0 * (0.001 + 8e-4) * 2, rel=0.01)


def test_paper_broker_clips_buy_to_cash(tmp_path):
    b, _ = _paper_broker(tmp_path)
    # request 1 BTC = $100 but only $20 cash: clipped to affordable qty
    o = b.place_order("BTC/USD", 1.0, "BUY")
    cash_after = float(b.get_account().cash)
    assert 0.0 <= cash_after < 0.01
    # everything except rounding dust got deployed
    deployed = float(o.filled_qty) * float(o.filled_avg_price) * 1.001
    assert deployed == pytest.approx(20.0, abs=0.01)


def test_paper_broker_rejects_sell_without_position(tmp_path):
    from bot.errors import BrokerError
    b, _ = _paper_broker(tmp_path)
    with pytest.raises(BrokerError):
        b.place_order("ETH/USD", 0.1, "SELL")


def test_paper_broker_seed_from_alpaca(tmp_path):
    b, _ = _paper_broker(tmp_path)
    b.seed_from_alpaca(cash=2.04, positions={
        "ETH/USD": {"qty": 0.0397, "entry": 2458.29}})
    acct = b.get_account()
    assert float(acct.cash) == 2.04
    pos = b.get_position("ETH/USD")
    assert float(pos.qty) == pytest.approx(0.0397)
    assert float(pos.avg_entry_price) == pytest.approx(2458.29)
    # re-seeding is idempotent on shape: same positions dict wins
    b.seed_from_alpaca(cash=5.0, positions={})
    assert b.get_all_positions() == []
    assert float(b.get_account().cash) == 5.0


def test_paper_broker_account_position_shapes(tmp_path):
    b, _ = _paper_broker(tmp_path)
    b.place_order("SOL/USD", 1.0, "BUY")
    acct = b.get_account()
    for attr in ("equity", "cash"):
        assert hasattr(acct, attr)
    p = b.get_position("SOL/USD")
    for attr in ("symbol", "qty", "avg_entry_price", "current_price",
                 "market_value", "unrealized_pl"):
        assert hasattr(p, attr)
    assert float(p.market_value) == pytest.approx(float(p.qty) * float(p.current_price))


def test_make_broker_factory_resolves(tmp_path):
    from bot.broker import make_broker
    from bot.errors import BrokerError

    class _CfgAlpaca:
        broker = {"name": "alpaca"}
        def require(self, *names):
            raise ValueError("Missing environment credentials: " + ", ".join(names))

    with pytest.raises(ValueError):
        make_broker(_CfgAlpaca())

    class _CfgBad:
        broker = {"name": "kraken"}

    with pytest.raises(BrokerError):
        make_broker(_CfgBad())


# ---------------- binance data client ----------------

def test_binance_interval_mapping():
    from bot.binance_data import _interval_for, to_binance_symbol
    from bot.timeframe import make_timeframe
    assert _interval_for("15Min") == "15m"
    assert _interval_for("1Day") == "1d"
    assert _interval_for(make_timeframe("15Min")) == "15m"
    assert _interval_for(make_timeframe("1Day")) == "1d"
    assert to_binance_symbol("BTC/USD") == "BTCUSDT"
    assert to_binance_symbol("ETH/USD") == "ETHUSDT"
    assert to_binance_symbol("XRP/USD") == "XRPUSDT"
    assert to_binance_symbol("DOGE/USD") == "DOGEUSDT"
    assert to_binance_symbol("PEPE") == "PEPE"


# ---------------- polymarket exposure caps ----------------

def test_polymarket_journal_caps(tmp_path):
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    # duplicate (market, side) detection
    assert not j.has_open_bet("will-x", "Yes")
    j.log_bet("2026-09-05T10:00", "will-x", "q", "Yes", 0.5, 20,
              fee=0.2, estimated_probability=0.55, expected_value=1.8)
    assert j.has_open_bet("will-x", "Yes")
    assert not j.has_open_bet("will-x", "No")
    # exposure sums stake + fee for open bets only
    assert j.open_bet_exposure() == pytest.approx(20.2)
    j.log_bet("2026-09-05T10:05", "will-y", "q2", "Yes", 0.4, 10, fee=0.1)
    assert j.open_bet_exposure() == pytest.approx(30.3)
    bets = j.get_open_bets()
    j.update_bet(bets[0][0], "won", 40.0)
    assert j.open_bet_exposure() == pytest.approx(10.1)
    # settled bets feed the scorecard
    card = j.bet_scorecard()
    assert card["settled"] == 1 and card["win_rate_pct"] == 100.0
    assert card["net_pnl"] == pytest.approx(40.0 - 20.0 - 0.2)


# ---------------- tier 3 betting wallet ----------------

class _WalletCfg:
    scanner = {"wallet_start_cash": 10, "wallet_stake": 2}


def _wallet(tmp_path):
    from bot.wallet import BettingWallet
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    return BettingWallet(_WalletCfg, journal=j), j


def test_wallet_empty_baseline(tmp_path):
    w, _ = _wallet(tmp_path)
    v = w.valuation()
    assert v["cash"] == 10.0 and v["locked"] == 0.0 and v["equity"] == 10.0
    assert v["open_bets"] == 0 and v["wins"] == 0 and v["losses"] == 0
    assert not w.is_bust()
    assert "Tier 3 wallet: $10.00 (+0.00% of $10 start)" in w.status_line()
    assert "no settled bets" in w.status_line()


def test_wallet_open_bet_locks_stake(tmp_path):
    w, j = _wallet(tmp_path)
    j.log_bet("2026-09-05T10:00", "will-x", "q", "Yes", 0.5, 20)
    v = w.valuation()
    # open bet: $2 wallet stake moves from cash to locked, equity unchanged
    assert v["cash"] == pytest.approx(8.0)
    assert v["locked"] == pytest.approx(2.0)
    assert v["equity"] == pytest.approx(10.0)
    assert v["open_bets"] == 1


def test_wallet_settled_win_and_loss(tmp_path):
    w, j = _wallet(tmp_path)
    # win at 0.5 price: $2 stake -> $4 payout (+$2)
    j.log_bet("2026-09-05T10:00", "will-x", "q", "Yes", 0.5, 20)
    j.log_bet("2026-09-05T10:05", "will-y", "q2", "Yes", 0.4, 20)
    bets = j.get_all_bets()
    j.update_bet(bets[0][0], "won", 40.0)
    j.update_bet(bets[1][0], "lost", 0.0)
    v = w.valuation()
    # 10 - 2 (won stake) + 4 (payout) - 2 (lost stake) = 10
    assert v["cash"] == pytest.approx(10.0)
    assert v["locked"] == 0.0
    assert v["wins"] == 1 and v["losses"] == 1
    assert "record 1W-1L" in w.status_line()


def test_wallet_expensive_favorite_not_simplified(tmp_path):
    w, j = _wallet(tmp_path)
    # a 0.97 favorite still costs the flat $2 stake and pays stake/price on win
    j.log_bet("2026-09-05T10:00", "will-x", "q", "Yes", 0.97, 20)
    bets = j.get_all_bets()
    j.update_bet(bets[0][0], "won", 20.62)
    v = w.valuation()
    assert v["cash"] == pytest.approx(10.0 - 2.0 + 2.0 / 0.97, rel=1e-3)


def test_wallet_bust_detection(tmp_path):
    w, j = _wallet(tmp_path)
    # five straight losses at $2 exhaust the $10 bankroll; only the first
    # five bets are mirrored (bets after bust are sat out, like a real bettor)
    for i in range(6):
        j.log_bet(f"2026-09-05T1{i}:00", f"will-{i}", "q", "Yes", 0.5, 20)
    for b in j.get_all_bets():
        j.update_bet(b[0], "lost", 0.0)
    v = w.valuation()
    assert v["cash"] == pytest.approx(0.0)
    assert v["wins"] == 0 and v["losses"] == 5
    assert w.is_bust()
    assert "BUST" in w.status_line()


def test_wallet_epoch_reset_ignores_old_bets(tmp_path):
    from datetime import datetime, timedelta, timezone
    w, j = _wallet(tmp_path)
    for i in range(5):
        j.log_bet(f"2026-09-05T1{i}:00", f"will-{i}", "q", "Yes", 0.5, 20)
    for b in j.get_all_bets():
        j.update_bet(b[0], "lost", 0.0)
    assert w.is_bust()
    w.snapshot()
    new_epoch = w.start_new_epoch()
    assert new_epoch == 2 and w.epoch == 2
    v = w.valuation()
    assert v["cash"] == pytest.approx(10.0)
    assert v["wins"] == 0 and v["losses"] == 0
    assert not w.is_bust()
    # a bet logged after the epoch start counts only toward the fresh epoch
    later = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    j.log_bet(later, "will-new", "q", "Yes", 0.5, 20)
    v = w.valuation()
    assert v["locked"] == pytest.approx(2.0) and v["cash"] == pytest.approx(8.0)


def test_wallet_trend_line_from_snapshots(tmp_path):
    w, j = _wallet(tmp_path)
    assert "no history yet" in w.trend_line()
    w.snapshot()
    j.log_bet("2026-09-05T10:00", "will-x", "q", "Yes", 0.5, 20)
    for b in j.get_all_bets():
        j.update_bet(b[0], "won", 40.0)
    w.snapshot()
    trend = w.trend_line()
    assert "$10.00 -> $12.00" in trend


# ---------------- stop-ledger self-heal (re-seed survival) ----------------

def test_ensure_stop_heals_missing_stop(tmp_path):
    # the exact Sep 6 re-seed scenario: a live position whose stop ledger was
    # wiped. The next cycle must record a working stop, not fall through to
    # the daily -5% flatten as the only exit.
    eng, j = _stop_engine(tmp_path)
    healed, stop = eng.ensure_stop("ETH/USD", 2458.29, atr=12.0)
    assert healed and stop == pytest.approx(2458.29 - 3.0 * 12.0)
    entry, level = eng.get_stop("ETH/USD")
    assert entry == 2458.29 and level == pytest.approx(stop)
    # idempotent: a second heal never moves the recorded level
    healed2, stop2 = eng.ensure_stop("ETH/USD", 2458.29, atr=99.0)
    assert not healed2 and stop2 == pytest.approx(stop)


def test_ensure_stop_fallback_without_atr(tmp_path):
    # barless position (feed down): fallback pct guarantees a stop anyway
    eng, _ = _stop_engine(tmp_path)
    healed, stop = eng.ensure_stop("ETH/USD", 200.0, atr=None)
    assert healed and stop == pytest.approx(190.0)  # 5% below entry
    hit, _ = eng.stop_triggered("ETH/USD", 189.9)
    assert hit


def test_ensure_stop_unusable_entry(tmp_path):
    eng, _ = _stop_engine(tmp_path)
    healed, stop = eng.ensure_stop("ETH/USD", 0.0, atr=12.0)
    assert not healed and stop is None


def test_prune_stale_stops(tmp_path):
    eng, _ = _stop_engine(tmp_path)
    eng.record_stop("BTC/USD", 100.0, 94.0)
    eng.record_stop("ETH/USD", 200.0, 185.0)
    # a re-seed leaves only ETH held: BTC's stop is dead weight
    removed = eng.prune_stale_stops({"ETH/USD"})
    assert removed == ["BTC/USD"]
    assert eng.get_stop("BTC/USD") == (None, None)
    entry, level = eng.get_stop("ETH/USD")
    assert entry == 200.0 and level == 185.0
    assert eng.prune_stale_stops({"ETH/USD"}) == []  # idempotent


def test_paper_broker_seed_clears_stale_stops(tmp_path):
    # seeding at the source: the re-seed itself must not leave orphaned stops
    from bot.binance_paper import BinancePaperBroker
    b, j = _paper_broker(tmp_path)
    b.place_order("BTC/USD", 0.05, "BUY")
    j.set_meta("open_stops", '{"BTC/USD": {"entry_price": 100.0, "stop_price": 94.0}}')
    b.seed_from_alpaca(cash=5.0, positions={
        "ETH/USD": {"qty": 0.0397, "entry": 2458.29}})
    from bot.risk import RiskEngine
    eng = RiskEngine(_PaperCfg, b, journal=j)
    assert eng.get_stop("BTC/USD") == (None, None)  # orphan pruned at seed time
    b.place_order("ETH/USD", 0.001, "BUY")
    o = b.place_order("ETH/USD", 0.001, "BUY")
    assert o.status == "filled"
    b.seed_fresh(cash=20.0)
    assert eng._load_stops() == {}


# ---------------- missed-cross catch-up (persistent state) ----------------

class _RelationCfg:
    sma_fast = 20
    sma_slow = 50


def test_sma_relation_states():
    from bot.trader import _sma_relation
    flat = _make_df([10.0] * 60)
    assert _sma_relation(flat, _RelationCfg) == "below"
    up = _make_df([10.0] * 50 + [20.0] * 20)
    assert _sma_relation(up, _RelationCfg) == "above"
    assert _sma_relation(_make_df([10.0] * 10), _RelationCfg) is None


def test_catchup_signal_missed_death_cross():
    from bot.trader import _catchup_signal
    # held position, relation flipped above->below, cross NOT on this bar:
    # the one-cycle-visible death cross was missed — must still exit
    assert _catchup_signal(True, "below", "above", False) == "SELL"
    # ...and it keeps firing on later cycles until the exit executes
    assert _catchup_signal(True, "below", "above", False) == "SELL"
    # fresh cross on the current bar: the strategy fires for it, no catch-up
    assert _catchup_signal(True, "below", "above", True) is None
    # no transition, or transition in the profitable direction: nothing
    assert _catchup_signal(True, "above", "above", False) is None
    assert _catchup_signal(True, "above", "below", False) is None
    assert _catchup_signal(True, None, "above", False) is None
    assert _catchup_signal(True, "below", None, False) is None


def test_catchup_signal_missed_golden_cross():
    from bot.trader import _catchup_signal
    # flat, relation flipped below->above while we were blind: one entry shot
    assert _catchup_signal(False, "above", "below", False) == "BUY"
    assert _catchup_signal(False, "above", "below", True) is None  # fresh: strategy handles it
    # holding through a golden cross is not actionable (long-only, already in)
    assert _catchup_signal(True, "above", "below", False) is None


def test_position_state_roundtrip(tmp_path):
    from bot.trader import _load_position_states, _save_position_states
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    assert _load_position_states(j) == {}
    _save_position_states(j, {"ETH/USD": "above", "BTC/USD": "below"})
    assert _load_position_states(j) == {"ETH/USD": "above", "BTC/USD": "below"}
    # corrupt payload degrades to empty, never crashes the cycle
    j.set_meta("strat_state_positions", "{not json")
    assert _load_position_states(j) == {}


# ---------------- idempotency keys ----------------

def test_client_order_id_stable_and_distinct():
    from bot.trader import _client_order_id
    a = _client_order_id("ETH/USD", "SELL", "death cross reason", 0.01)
    b = _client_order_id("ETH/USD", "SELL", "death cross reason", 0.01)
    c = _client_order_id("ETH/USD", "SELL", "death cross reason", 0.02)
    d = _client_order_id("ETH/USD", "SELL", "other reason", 0.01)
    assert a == b
    assert a != c
    assert a != d


def test_mark_executed_bounds_registry(tmp_path):
    from bot.trader import _mark_executed, _already_executed, IDEMPOTENCY_KEY
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    _mark_executed(j, "abc123", "order-1")
    assert _already_executed(j, "abc123") is True
    assert _already_executed(j, "nope") is False
    for i in range(600):
        _mark_executed(j, f"k{i}", f"order-{i}")
    import json as _json
    raw = _json.loads(j.get_meta(IDEMPOTENCY_KEY))
    assert len(raw) == 500
    assert "k599" in raw and "k0" not in raw


def test_execute_signal_suppresses_duplicate(tmp_path, monkeypatch):
    import bot.trader as T

    j = TradeJournal(db_path=str(tmp_path / "t.db"))

    class _Order:
        id = "7"
        status = "filled"
        filled_qty = "0.01"
        filled_avg_price = "100.0"

    class _FillingBroker:
        def __init__(self):
            self.orders = 0
        def get_position(self, symbol):
            return None
        def get_account(self):
            class _A:
                cash = "20"
                equity = "20"
            return _A()
        def get_all_positions(self):
            return []
        def place_order(self, symbol, qty, side):
            self.orders += 1
            return _Order()
        def await_terminal_order(self, order_id, timeout_seconds=15):
            return _Order()

    broker = _FillingBroker()
    risk = type("R", (), {
        "size_for_atr": staticmethod(lambda p, a, c, n: 0.01),
        "check": staticmethod(lambda *a, **k: (True, "")),
        "entry_fixed_stop": staticmethod(lambda s, e, atr: 95.0),
        "record_stop": staticmethod(lambda *a: None),
        "clear_stop": staticmethod(lambda *a: None),
    })()
    df = pd.DataFrame({"close": [100.0] * 5})
    sig = {"action": "BUY", "reasoning": "golden cross retry test"}

    monkeypatch.setattr(T, "config", type("C", (), {
        "notional": 10, "execution": {"taker_fee_pct": 0.1}})())
    ok1 = T._execute_signal(broker, j, risk, "ETH/USD", df, sig)
    ok2 = T._execute_signal(broker, j, risk, "ETH/USD", df, sig)
    assert ok1 is True and ok2 is True
    assert broker.orders == 1
    assert len(j.get_trades()) == 1


def test_failed_exit_does_not_advance_state(tmp_path, monkeypatch):
    """E2E invariant: a death-cross SELL that fails must leave the recorded
    relation at 'above', so the missed-cross catch-up retries the exit next
    cycle instead of losing it forever."""
    import bot.trader as T
    from bot.errors import BrokerError

    j = TradeJournal(db_path=str(tmp_path / "t.db"))

    class _Pos:
        symbol = "ETH/USD"
        qty = "0.01"
        avg_entry_price = "100.0"
        current_price = "99.0"
        market_value = "0.99"
        unrealized_pl = "-0.01"

    class _Acct:
        cash = "20"
        equity = "20"

    class _FlakyBroker:
        def __init__(self):
            self.sell_attempts = 0
        def get_account(self):
            return _Acct()
        def get_all_positions(self):
            return [_Pos()]
        def get_position(self, symbol):
            return _Pos() if symbol == "ETH/USD" else None
        def get_crypto_bars(self, symbol, timeframe, limit):
            # death cross on the last closed bar, close 99 stays above the
            # healed fallback stop (95) so the strategy exit is what fails
            closes = [200.0] * 50 + [99.0]
            return pd.DataFrame({"open": closes, "high": closes,
                                 "low": closes, "close": closes})
        def place_order(self, symbol, qty, side):
            self.sell_attempts += 1
            raise BrokerError("broker down")
        def await_terminal_order(self, order_id, timeout_seconds=15):
            return None

    broker = _FlakyBroker()
    cfg = type("C", (), {
        "symbols": ["ETH/USD"], "timeframe": "15Min", "lookback_bars": 60,
        "sma_fast": 20, "sma_slow": 50, "notional": 10,
        "active_strategies": ["sma_cross"],
        "risk": {"atr_period": 14, "catastrophic_atr_multiple": 3.0,
                 "fallback_stop_pct": 5.0, "max_notional_per_trade": 100,
                 "max_open_positions": 3, "daily_loss_limit_pct": 0},
        "execution": {"taker_fee_pct": 0.1},
    })()
    monkeypatch.setattr(T, "config", cfg)
    monkeypatch.setattr(T, "make_broker", lambda c: broker)
    monkeypatch.setattr(T, "TradeJournal", lambda *a, **k: j)
    monkeypatch.setattr(T, "send_notification", lambda *a, **k: None)
    monkeypatch.setattr(T, "_load_position_states",
                        lambda journal: {"ETH/USD": "above"})

    T.run_trading_cycle()
    assert broker.sell_attempts == 1  # the exit was attempted...
    # ...and state was NOT advanced: relation stays 'above' so the catch-up
    # logic retries the exit next cycle instead of losing it forever
    assert j.get_meta("strat_state_positions") in (None, '{"ETH/USD": "above"}')


def test_cross_on_current_edge():
    from bot.trader import _cross_on_current_edge
    # the cross is on the last closed bar -> strategy fires this cycle
    df = _make_df([10.0] * 50 + [20.0])
    assert _cross_on_current_edge(df, _RelationCfg) is True
    # the cross happened bars ago (missed cycle) -> needs catch-up
    df_old = _make_df([10.0] * 50 + [20.0, 20.0, 20.0])
    assert _cross_on_current_edge(df_old, _RelationCfg) is False
    # no cross at all
    assert _cross_on_current_edge(_make_df([10.0] * 60), _RelationCfg) is False


# ---------------- report hygiene: shadow trades ----------------

def test_pnl_excludes_shadow_account_trades():
    from bot.report import compute_pnl_and_winrate
    trades = [
        (1, "2026-09-05T10:00", "BTC/USD", "BUY", 0.01, 100.0, "r", 0.02, "o1", "filled"),
        (2, "2026-09-05T10:05", "SOL/USD", "BUY", 0.1, 100.0, "[shadow-account] scout proposal", 0.0, None, "filled"),
        (3, "2026-09-05T11:00", "BTC/USD", "SELL", 0.01, 110.0, "r", 0.02, "o3", "filled"),
        (4, "2026-09-05T12:00", "SOL/USD", "SELL", 0.1, 120.0, "[shadow-account] babysitter exit (pnl +2.00)", 0.0, None, "filled"),
    ]
    stats = compute_pnl_and_winrate(trades)
    # Tier 1 scorecard must contain ONLY the strategy round trip (+$0.06)
    assert stats["round_trips"] == 1
    assert stats["total_pnl"] == pytest.approx(0.06)


# ---------------- chat: our-bot detection ----------------

def test_is_our_bot_decodes_token_id(monkeypatch):
    from bot.chat import _is_our_bot
    import base64
    bot_id = "1545107532173934644"
    token = base64.b64encode(bot_id.encode()).decode().rstrip("=") + ".fake.hmac"
    monkeypatch.setenv("DISCORD_BOT_TOKEN", token)
    assert _is_our_bot({"author": {"id": bot_id}}) is True
    # the raw (undecoded) segment must NOT match — that was the old bug
    raw_first = token.split(".")[0]
    assert _is_our_bot({"author": {"id": raw_first}}) is False
    assert _is_our_bot({"author": {"id": "999"}}) is False
    # bots are filtered by the bot flag regardless of token
    assert _is_our_bot({"author": {"id": "x", "bot": True}}) is True


# ---------------- polymarket settlement ambiguity ----------------

def test_settlement_requires_unambiguous_prices(tmp_path, monkeypatch):
    from bot import polymarket as pm

    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    j.log_bet("2026-09-05T10:00", "will-x", "q?", "Yes", 0.6, 20)

    class _Resp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return [{
                "slug": "will-x", "closed": True,
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '[0.5, 0.5]',  # ambiguous: NOT settled
            }]

    def fake_get(url, params=None, headers=None, timeout=None):
        return _Resp()

    monkeypatch.setattr(pm.requests, "get", fake_get)
    settled = pm.settle_open_bets(_WalletCfg, journal=j)
    assert settled == []
    assert j.get_open_bets()  # still open, not silently scored a loss

    class _Resp2(_Resp):
        def json(self):
            return [{
                "slug": "will-x", "closed": True,
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '[0.9995, 0.0005]',  # clean Yes win
            }]

    monkeypatch.setattr(pm.requests, "get", lambda *a, **k: _Resp2())
    settled = pm.settle_open_bets(_WalletCfg, journal=j)
    assert len(settled) == 1 and settled[0][1] is True
    assert j.get_open_bets() == []


# ---------------- report: actions pain-meter ----------------

class _PainMeterResp:
    status_code = 200
    def __init__(self, runs):
        self._runs = runs
    def raise_for_status(self):
        pass
    def json(self):
        return {"workflow_runs": self._runs}


def _mk_run(created_at, conclusion):
    return {"created_at": created_at, "status": "completed",
            "conclusion": conclusion}


def _patch_meter(monkeypatch, runs_by_wf):
    from datetime import datetime, timedelta, timezone
    from bot import report as rp
    now = datetime.now(timezone.utc)

    def fake_get(url, params=None, headers=None, timeout=None):
        wf = url.rstrip("/runs").rsplit("/", 1)[-1].replace(".yml", "")
        return _PainMeterResp(runs_by_wf.get(wf, []))

    monkeypatch.setattr(rp.requests, "get", fake_get)
    monkeypatch.setenv("GITHUB_REPOSITORY", "test/repo")
    return rp


def test_pain_meter_flags_stalled_workflow(monkeypatch):
    from datetime import datetime, timedelta, timezone
    rp = _patch_meter(monkeypatch, {
        # last trade run 3h ago -> past 2-run grace on a 15-min cron
        "trade": [_mk_run((datetime.now(timezone.utc) - timedelta(hours=3)
                          ).isoformat(), "success")],
        "chat": [_mk_run(datetime.now(timezone.utc).isoformat(), "success")],
        "agent": [_mk_run(datetime.now(timezone.utc).isoformat(), "success")],
        "scanner": [_mk_run(datetime.now(timezone.utc).isoformat(), "success")],
        "report": [_mk_run(datetime.now(timezone.utc).isoformat(), "success")],
    })
    out = rp.actions_health()
    assert "INVESTIGATE" in out
    assert "trade: STALLED" in out
    assert "chat: ok" in out


def test_pain_meter_flags_failed_run(monkeypatch):
    from datetime import datetime, timezone
    rp = _patch_meter(monkeypatch, {
        wf: [_mk_run(datetime.now(timezone.utc).isoformat(), "failure")]
        for wf in ("trade", "chat", "agent", "scanner", "report")
    })
    out = rp.actions_health()
    assert "INVESTIGATE" in out
    assert "trade: LAST RUN FAILED" in out
    assert "0 ok" not in out


def test_pain_meter_all_healthy(monkeypatch):
    from datetime import datetime, timezone
    rp = _patch_meter(monkeypatch, {
        wf: [_mk_run(datetime.now(timezone.utc).isoformat(), "success")]
        for wf in ("trade", "chat", "agent", "scanner", "report")
    })
    out = rp.actions_health()
    assert "INVESTIGATE" not in out
    assert "STALLED" not in out
    assert "FAILED" not in out


def test_pain_meter_api_failure_degrades_gracefully(monkeypatch):
    rp = _patch_meter(monkeypatch, {})

    def boom(url, params=None, headers=None, timeout=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(rp.requests, "get", boom)
    out = rp.actions_health()
    assert "unavailable" in out
    assert "INVESTIGATE" in out  # blindness is pain too


# ---------------- graduation gates ----------------

def _gate_journal(tmp_path):
    return TradeJournal(db_path=str(tmp_path / "g.db"))


def test_agent_alpha_gate_red_on_empty(tmp_path):
    from bot.gates import agent_alpha_gate
    j = _gate_journal(tmp_path)
    r = agent_alpha_gate(j)
    assert r.passed is False
    assert "0/20 proposals evaluated" in r.summary


def test_agent_alpha_gate_green_on_edge(monkeypatch, tmp_path):
    from bot.gates import agent_alpha_gate
    j = _gate_journal(tmp_path)
    for i in range(20):
        j.log_proposal("2026-09-07T10:00", "ai_agent", "scout", "BTC/USD",
                       "BUY", 10, 0.9, "r", exec_status="evaluated",
                       entry_price=100.0)
        j.update_proposal_exec(i + 1, "evaluated", "2026-09-08T10:00",
                               100.0, 1.0, closed_price=105.0,
                               benchmark_return=0.02)

    class _Shadow:
        def __init__(self, *a, **k):
            pass
        def realized_pnl(self):
            return 3.0

    monkeypatch.setattr("bot.shadow.ShadowAccount", _Shadow)
    r = agent_alpha_gate(j)
    assert r.passed is True, r.summary
    assert "positive edge" in r.summary


# ---------------- agent messaging: exhausted-cash suppression ----------------

def _agent_for_messaging(tmp_path, monkeypatch, shadow_cash="80"):
    """A TradingAgent wired for _log_and_alert tests: fake broker, journal,
    shadow account, and a captured send_notification."""
    from bot.agent import TradingAgent
    j = TradeJournal(db_path=str(tmp_path / "a.db"))
    j.set_meta("shadow_cash", shadow_cash)

    class _Cfg:
        symbols = ["BTC/USD"]
        agent = {"shadow": True, "max_proposed_notional": 50,
                 "min_confidence": 0.7, "evaluation_horizon_hours": 24,
                 "shadow_max_per_position": 40}
        research = {}
        timeframe = "15Min"
        lookback_bars = 60
        sma_fast = 20
        sma_slow = 50

    class _Broker:
        def get_crypto_bars(self, symbol, timeframe, limit):
            closes = [100.0] * 60
            return pd.DataFrame({"open": closes, "high": closes,
                                "low": closes, "close": closes})

    a = TradingAgent.__new__(TradingAgent)
    a.cfg = _Cfg
    a.agent_cfg = _Cfg.agent
    a.symbols = _Cfg.symbols
    a.scout_symbols = _Cfg.symbols + ["XRP/USD"]
    a.journal = j
    a.broker = _Broker()
    from bot.shadow import ShadowAccount
    a.shadow = ShadowAccount(_Cfg, _Broker(), journal=j)
    sent = []
    monkeypatch.setattr("bot.agent.send_notification",
                        lambda msg, cfg: sent.append(msg))
    return a, j, sent


def test_exhausted_cash_proposal_journaled_compact_alert_only(tmp_path, monkeypatch):
    a, j, sent = _agent_for_messaging(tmp_path, monkeypatch, shadow_cash="0.30")
    proposal = {"kind": "scout", "symbol": "BTC/USD", "action": "BUY",
                "notional": 40.0, "confidence": 0.9, "rationale": "strong setup"}
    a._log_and_alert(proposal)
    # journaled with full context (evaluation at horizon continues)
    rows = j.get_proposals()
    assert len(rows) == 1
    assert rows[0][4] == "BTC/USD" and rows[0][5] == "BUY"
    # only ONE compact money message, no full recommendation blast
    assert len(sent) == 1
    assert "out of money" in sent[0]
    assert "rationale" not in sent[0].lower()
    assert "confidence" not in sent[0].lower()


def test_funded_proposal_brief_fill_alert(tmp_path, monkeypatch):
    a, j, sent = _agent_for_messaging(tmp_path, monkeypatch, shadow_cash="80")
    proposal = {"kind": "scout", "symbol": "BTC/USD", "action": "BUY",
                "notional": 40.0, "confidence": 0.9, "rationale": "strong setup"}
    a._log_and_alert(proposal)
    assert len(sent) == 1
    assert sent[0].startswith("🟢 Tier 2 AI BUY: BTC/USD")
    assert "$40.00 @ $100.00" in sent[0]


def test_scout_accepts_extra_universe_babysitter_does_not(tmp_path):
    from bot.agent import TradingAgent

    class _Cfg:
        symbols = ["BTC/USD"]
        agent = {"max_proposed_notional": 50, "min_confidence": 0.7,
                 "scout_extra_universe": ["XRP/USD", "DOGE/USD"]}

    a = TradingAgent.__new__(TradingAgent)
    a.cfg = _Cfg
    a.agent_cfg = _Cfg.agent
    a.symbols = _Cfg.symbols
    a.scout_symbols = _Cfg.symbols + _Cfg.agent["scout_extra_universe"]

    # scout may propose XRP (extra universe)
    ok, errs = a._validate({"action": "BUY", "symbol": "XRP/USD", "notional": 30,
                            "confidence": 0.9, "rationale": "r"}, "scout")
    assert ok is not None and errs == []
    # babysitter may NOT (stays scoped to Tier 1 held positions)
    ok, errs = a._validate({"action": "SELL", "symbol": "XRP/USD", "notional": 0,
                            "confidence": 0.9, "rationale": "r"}, "babysitter")
    assert ok is None and any("whitelist" in e for e in errs)
    # unknown coin still rejected for scout
    ok, errs = a._validate({"action": "BUY", "symbol": "SHIT/USD", "notional": 30,
                            "confidence": 0.9, "rationale": "r"}, "scout")
    assert ok is None and any("whitelist" in e for e in errs)


# ---------------- heartbeat tier labeling ----------------

def test_heartbeat_tier1_label_and_tier_one_liners(tmp_path, monkeypatch):
    import bot.trader as T

    class _Pos:
        symbol = "BTC/USD"
        qty = "0.01"
        avg_entry_price = "100.0"
        unrealized_pl = "0.5"

    class _Acct:
        cash = "50"
        equity = "100"

    class _Broker:
        def get_account(self):
            return _Acct()
        def get_all_positions(self):
            return [_Pos()]
        def get_crypto_bars(self, symbol, timeframe, limit):
            closes = [100.0] * 60
            return pd.DataFrame({"open": closes, "high": closes,
                                 "low": closes, "close": closes})

    j = TradeJournal(db_path=str(tmp_path / "h.db"))
    sent = []
    monkeypatch.setattr(T, "send_notification", lambda msg, cfg: sent.append(msg))
    monkeypatch.setattr(T, "config", type("C", (), {
        "symbols": ["BTC/USD"], "timeframe": "15Min", "lookback_bars": 60,
        "sma_fast": 20, "sma_slow": 50, "notional": 100,
        "active_strategies": ["sma_cross"],
        "broker": {"name": "binance_paper", "paper": {"start_cash": 100}},
        "agent": {"shadow_start_cash": 80, "shadow_max_per_position": 40,
                  "scout_extra_universe": []},
        "scanner": {"wallet_start_cash": 60, "wallet_stake": 12},
        "memecoin": {"start_cash": 40, "max_stake": 12, "stop_loss_pct": 25,
                     "take_profit_pct": 50, "time_stop_hours": 72,
                     "max_drawdown_pct": 25},
    })())
    T.send_heartbeat(_Broker(), j)
    assert len(sent) == 1
    msg = sent[0]
    # Tier 1 money line + open position
    assert "Tier 1 BTC/ETH/SOL" in msg
    assert "was $100" in msg
    assert "BTC/USD" in msg
    # one-liners for the other tiers, money-first
    assert "Tier 2 AI" in msg
    assert "Tier 3 Bets" in msg
    assert "Tier 4 Coins" in msg
    assert "$80" in msg and "$60" in msg and "$40" in msg
    # idempotent per hour
    T.send_heartbeat(_Broker(), j)
    assert len(sent) == 1


def test_heartbeat_quiet_when_flat(tmp_path, monkeypatch):
    import bot.trader as T

    class _Acct:
        cash = "100"
        equity = "100"

    class _Broker:
        def get_account(self):
            return _Acct()
        def get_all_positions(self):
            return []
        def get_crypto_bars(self, symbol, timeframe, limit):
            closes = [100.0] * 60
            return pd.DataFrame({"open": closes, "high": closes,
                                 "low": closes, "close": closes})

    j = TradeJournal(db_path=str(tmp_path / "h2.db"))
    sent = []
    monkeypatch.setattr(T, "send_notification", lambda msg, cfg: sent.append(msg))
    monkeypatch.setattr(T, "config", type("C", (), {
        "symbols": ["BTC/USD"], "timeframe": "15Min", "lookback_bars": 60,
        "sma_fast": 20, "sma_slow": 50, "notional": 100,
        "active_strategies": ["sma_cross"],
        "broker": {"name": "binance_paper", "paper": {"start_cash": 100}},
        "agent": {"shadow_start_cash": 80, "shadow_max_per_position": 40,
                  "scout_extra_universe": []},
        "scanner": {"wallet_start_cash": 60, "wallet_stake": 12},
        "memecoin": {"start_cash": 40, "max_stake": 12, "stop_loss_pct": 25,
                     "take_profit_pct": 50, "time_stop_hours": 72,
                     "max_drawdown_pct": 25},
    })())
    T.send_heartbeat(_Broker(), j)
    msg = sent[0]
    # flat everywhere: quiet-day format, no SMA/price noise
    assert "no open trades" in msg
    assert "no open bets" in msg
    assert "nothing held" in msg
    assert "SMA" not in msg
