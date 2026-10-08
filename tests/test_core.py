import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.journal import TradeJournal
from bot.models import ModelManager
from bot.polymarket import _expected_value


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

    # confidence bounds enforced
    ok, errs = a._validate({"action": "BUY", "symbol": "BTC/USD", "notional": 30,
                            "confidence": 1.5, "rationale": "r"}, "scout")
    assert ok is None and any("confidence" in e for e in errs)


# ---------------- journal non-fill status ----------------

def test_journal_records_status(tmp_path):
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    j.log_trade("2026-09-05T10:00", "BTC/USD", "BUY", 0.01, 100.0, "r",
                fee=0.02, order_id="o9", status="canceled")
    trades = j.get_trades()
    assert trades[0][9] == "canceled" and trades[0][8] == "o9"


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


# ---------------- chat: open bets in system context ----------------

class _FlatBroker:
    class _Acct:
        equity = 100.0
        cash = 100.0
    def get_account(self):
        return self._Acct()
    def get_all_positions(self):
        return []


def _chat_cfg():
    from types import SimpleNamespace
    return SimpleNamespace(
        sma_fast=9, sma_slow=21, symbols=["BTC/USD"],
        scanner={"wallet_start_cash": 10, "wallet_stake": 2},
    )


def test_system_context_lists_open_bets(tmp_path):
    from bot.chat import _system_context
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    j.log_bet("2026-09-08T16:42:43+00:00", "cs2-g2-ast", "G2 vs Astralis?",
              "Astralis", 0.325, 20, estimated_probability=0.45)
    ctx = _system_context(_FlatBroker(), _chat_cfg(), j)
    assert "Tier 3 open bets:" in ctx
    assert "Astralis" in ctx and "G2 vs Astralis?" in ctx
    assert "0.33" in ctx  # price visible so the bot can answer "which bets"


def test_system_context_no_open_bets(tmp_path):
    from bot.chat import _system_context
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    ctx = _system_context(_FlatBroker(), _chat_cfg(), j)
    assert "Tier 3 open bets: none" in ctx


def test_mentions_bot_detection():
    from bot.chat import _mentions_bot
    msg = {"mentions": [{"id": "111", "username": "Bright Bot"}]}
    assert _mentions_bot(msg, "111") is True
    assert _mentions_bot(msg, "999") is False
    assert _mentions_bot({"mentions": []}, "111") is False
    assert _mentions_bot(msg, None) is False


def test_chat_answers_only_on_mention(tmp_path, monkeypatch):
    import base64
    from datetime import datetime, timezone
    import bot.chat as chat
    bot_id = "111"
    token = base64.b64encode(bot_id.encode()).decode().rstrip("=") + ".x.y"
    monkeypatch.setenv("DISCORD_BOT_TOKEN", token)
    monkeypatch.setenv("DISCORD_OWNER_IDS", "222")
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    j.set_meta("discord_chat_channel_id", "999")
    now = datetime.now(timezone.utc).isoformat()
    msgs = [
        {"id": "a", "timestamp": now, "content": "hello everyone",
         "author": {"id": "222", "username": "owner"}, "mentions": []},
        {"id": "b", "timestamp": now, "content": "<@111> tier 3 pnl?",
         "author": {"id": "222", "username": "owner"},
         "mentions": [{"id": "111", "username": "Bright Bot"}]},
    ]
    monkeypatch.setattr(chat, "_get_messages", lambda cid, limit=20: msgs)
    sent = []
    monkeypatch.setattr(chat, "_send_message", lambda cid, content: sent.append(content))

    class _M:
        def generate_text(self, prompt, max_tokens=600, temperature=0.5):
            return "answer"

    chat.run_chat_cycle(_chat_cfg(), _FlatBroker(), journal=j, model=_M())
    assert sent == ["answer"]


def test_chat_role_mention_gets_tagging_hint(tmp_path, monkeypatch):
    """Regression: owner tags a ROLE (<@&...>) instead of the bot user —
    `mentions` stays empty so the mention-gate misses it. Must get a
    tagging hint (not silence, not an LLM answer), and the cursor advances."""
    import base64
    from datetime import datetime, timezone
    import bot.chat as chat
    bot_id = "111"
    token = base64.b64encode(bot_id.encode()).decode().rstrip("=") + ".x.y"
    monkeypatch.setenv("DISCORD_BOT_TOKEN", token)
    monkeypatch.setenv("DISCORD_OWNER_IDS", "222")
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    j.set_meta("discord_chat_channel_id", "999")
    now = datetime.now(timezone.utc).isoformat()
    msgs = [
        {"id": "a", "timestamp": now,
         "content": "<@&9999> what is the current open bet in tier 3?",
         "author": {"id": "222", "username": "owner"}, "mentions": []},
    ]
    monkeypatch.setattr(chat, "_get_messages", lambda cid, limit=20: msgs)
    sent = []
    monkeypatch.setattr(chat, "_send_message", lambda cid, content: sent.append(content))

    class _Exploding:
        def generate_text(self, *a, **k):
            raise AssertionError("LLM must not be consulted for a tagging miss")

    chat.run_chat_cycle(_chat_cfg(), _FlatBroker(), journal=j, model=_Exploding())
    assert len(sent) == 1 and "@Bright Bot" in sent[0]
    assert float(j.get_meta("discord_chat_last_seen") or 0) > 0  # advanced past it


def test_chat_tagging_hint_rate_limited(tmp_path, monkeypatch):
    """Second tagging miss within the cooldown stays silent (no channel spam)."""
    import base64
    import time
    from datetime import datetime, timezone
    import bot.chat as chat
    bot_id = "111"
    token = base64.b64encode(bot_id.encode()).decode().rstrip("=") + ".x.y"
    monkeypatch.setenv("DISCORD_BOT_TOKEN", token)
    monkeypatch.setenv("DISCORD_OWNER_IDS", "222")
    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    j.set_meta("discord_chat_channel_id", "999")
    j.set_meta("discord_chat_mention_hint_at", str(time.time()))  # just hinted
    now = datetime.now(timezone.utc).isoformat()
    msgs = [
        {"id": "a", "timestamp": now, "content": "bright bot, tier 3 pnl?",
         "author": {"id": "222", "username": "owner"}, "mentions": []},
    ]
    monkeypatch.setattr(chat, "_get_messages", lambda cid, limit=20: msgs)
    sent = []
    monkeypatch.setattr(chat, "_send_message", lambda cid, content: sent.append(content))

    class _Exploding:
        def generate_text(self, *a, **k):
            raise AssertionError("LLM must not be consulted for a tagging miss")

    chat.run_chat_cycle(_chat_cfg(), _FlatBroker(), journal=j, model=_Exploding())
    assert sent == []
    assert float(j.get_meta("discord_chat_last_seen") or 0) > 0


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


def test_settlement_finds_closed_markets(tmp_path, monkeypatch):
    """Resolved markets vanish from Gamma's default listing; settle must
    refetch with closed=true or phantom 'open' bets clog the exposure cap."""
    from bot import polymarket as pm

    j = TradeJournal(db_path=str(tmp_path / "t.db"))
    j.log_bet("2026-09-08T10:00", "cs2-old", "old match?", "Yes", 0.5, 20)

    class _Empty:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return []

    class _Closed:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return [{
                "slug": "cs2-old", "closed": True,
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '[1, 0]',
            }]

    def fake_get(url, params=None, headers=None, timeout=None):
        assert url == pm.GAMMA_MARKETS_URL
        if (params or {}).get("closed") == "true":
            return _Closed()
        return _Empty()

    monkeypatch.setattr(pm.requests, "get", fake_get)
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


def _healthy_runs(**overrides):
    """A fresh success for EVERY workflow the meter knows about.

    Derived from WORKFLOW_SCHEDULES so this cannot drift when a workflow is
    added. Hardcoding the list is what let memecoin and futures go unpatched
    here, and a workflow with no runs at all used to render as a benign
    'no runs found' line that matched none of the pain tokens -- so two of
    seven workflows were silently unmonitored in the 'all healthy' case.
    """
    from datetime import datetime, timezone
    from bot import report as rp
    now = datetime.now(timezone.utc).isoformat()
    runs = {wf: [_mk_run(now, "success")] for wf in rp.WORKFLOW_SCHEDULES}
    runs.update(overrides)
    return runs


def test_pain_meter_flags_stalled_workflow(monkeypatch):
    from datetime import datetime, timedelta, timezone
    # last chat run 3h ago -> past 2-run grace on a 15-min cron
    rp = _patch_meter(monkeypatch, _healthy_runs(
        chat=[_mk_run((datetime.now(timezone.utc) - timedelta(hours=3)
                       ).isoformat(), "success")]))
    out = rp.actions_health()
    assert "INVESTIGATE" in out
    assert "chat: STALLED" in out
    assert "agent: ok" in out
    assert "NO RUNS FOUND" not in out


def test_pain_meter_flags_failed_run(monkeypatch):
    from datetime import datetime, timezone
    from bot import report as rp
    rp = _patch_meter(monkeypatch, {
        wf: [_mk_run(datetime.now(timezone.utc).isoformat(), "failure")]
        for wf in rp.WORKFLOW_SCHEDULES
    })
    out = rp.actions_health()
    assert "INVESTIGATE" in out
    assert "chat: LAST RUN FAILED" in out
    assert "0 ok" not in out


def test_pain_meter_all_healthy(monkeypatch):
    rp = _patch_meter(monkeypatch, _healthy_runs())
    out = rp.actions_health()
    assert "INVESTIGATE" not in out
    assert "STALLED" not in out
    assert "FAILED" not in out
    assert "NO RUNS FOUND" not in out


def test_pain_meter_flags_a_workflow_that_never_ran(monkeypatch):
    """A disabled or never-dispatched workflow used to be indistinguishable
    from a working one, because 'no runs found' matched none of the pain
    tokens and the header stayed clean."""
    from bot import report as rp
    runs = _healthy_runs()
    del runs["memecoin"]
    out = _patch_meter(monkeypatch, runs).actions_health()
    assert "memecoin: NO RUNS FOUND" in out
    assert "INVESTIGATE" in out


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
    """A TradingAgent wired for _log_and_alert tests: fake market data, journal,
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
    a.market_data = _Broker()
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


def test_scout_accepts_extra_universe(tmp_path):
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
    # unknown coin still rejected for scout
    ok, errs = a._validate({"action": "BUY", "symbol": "SHIT/USD", "notional": 30,
                            "confidence": 0.9, "rationale": "r"}, "scout")
    assert ok is None and any("whitelist" in e for e in errs)


# ---------------- heartbeat tier labeling ----------------

class _FlatBars:
    def get_crypto_bars(self, symbol, timeframe, limit):
        closes = [100.0] * 60
        return pd.DataFrame({"open": closes, "high": closes,
                             "low": closes, "close": closes})


def _heartbeat_cfg():
    return type("C", (), {
        "symbols": ["BTC/USD"], "timeframe": "15Min", "lookback_bars": 60,
        "sma_fast": 20, "sma_slow": 50,
        "agent": {"shadow_start_cash": 80, "shadow_max_per_position": 40,
                  "scout_extra_universe": []},
        "scanner": {"wallet_start_cash": 60, "wallet_stake": 12},
        "memecoin": {"start_cash": 40, "max_stake": 12, "stop_loss_pct": 25,
                     "take_profit_pct": 50, "time_stop_hours": 72,
                     "max_drawdown_pct": 25},
    })()


def test_heartbeat_one_liner_per_kept_tier(tmp_path, monkeypatch):
    import bot.trader as T

    j = TradeJournal(db_path=str(tmp_path / "h.db"))
    j.set_meta("shadow_positions", '{"BTC/USD": {"qty": 0.4, "entry": 100.0, "opened": "x"}}')
    j.set_meta("shadow_cash", "40.0")
    sent = []
    monkeypatch.setattr(T, "send_notification", lambda msg, cfg: sent.append(msg))
    monkeypatch.setattr(T, "config", _heartbeat_cfg())
    T.send_heartbeat(j, _FlatBars())
    assert len(sent) == 1
    msg = sent[0]
    assert "Tier 2 AI" in msg and "1 open AI trade(s)" in msg
    assert "Tier 3 Bets" in msg
    assert "Tier 4 Coins" in msg
    assert "$80" in msg and "$60" in msg and "$40" in msg
    assert "Tier 1" not in msg and "Tier 5" not in msg
    # idempotent per hour
    T.send_heartbeat(j, _FlatBars())
    assert len(sent) == 1


def test_heartbeat_quiet_when_flat(tmp_path, monkeypatch):
    import bot.trader as T

    j = TradeJournal(db_path=str(tmp_path / "h2.db"))
    sent = []
    monkeypatch.setattr(T, "send_notification", lambda msg, cfg: sent.append(msg))
    monkeypatch.setattr(T, "config", _heartbeat_cfg())
    T.send_heartbeat(j, _FlatBars())
    msg = sent[0]
    # flat everywhere: quiet-day format, no SMA/price noise
    assert "no open trades" in msg
    assert "no open bets" in msg
    assert "nothing held" in msg
    assert "SMA" not in msg


def test_heartbeat_still_sent_when_the_agent_cycle_fails(tmp_path, monkeypatch):
    import bot.agent as A
    import bot.trader as T

    class _DeadAgent:
        def __init__(self, *a, **k):
            pass

        def run_cycle(self):
            raise RuntimeError("model 403")

    j = TradeJournal(db_path=str(tmp_path / "h3.db"))
    sent = []
    monkeypatch.setattr(T, "send_notification", lambda msg, cfg: sent.append(msg))
    monkeypatch.setattr(T, "config", _heartbeat_cfg())
    monkeypatch.setattr(T, "TradeJournal", lambda *a, **k: j)
    monkeypatch.setattr(T, "BinanceDataClient", lambda cfg: _FlatBars())
    monkeypatch.setattr(A, "TradingAgent", _DeadAgent)
    with pytest.raises(RuntimeError, match="model 403"):
        T.run_agent_cycle()
    assert len(sent) == 1 and "Tier 2 AI" in sent[0]
